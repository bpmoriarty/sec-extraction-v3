"""
bdc_xbrl.py — Extract BDC 10-K / 10-Q financial data from XBRL into the
FilingExtraction schema. This is the pilot extraction front-end (XBRL-first).

APPROACH (see docs/DATA_DICTIONARY.md and the locked plan):
  - We map each schema field to one or more candidate us-gaap CONCEPT names and pull
    the value from the filing's XBRL facts. Concept-based mapping is robust across
    filers (unlike matching the rendered row labels, which vary).
  - XBRL `numeric_value` is already in ACTUAL dollars (no thousands scaling needed).
  - For each field we try candidate concepts in order and take the first match; if none
    match, the field is left null (an empty Fact) for the later LLM-fallback pass.
  - A coverage report prints which fields were found vs. missing, so we can refine the
    concept map filing by filing.

THIS VERSION (increment 1): metadata + balance sheet + share-class detection.
Income statement, per-class NAV, statement of changes, fair value, and the allocator
fields come in later increments.

Run a quick test:  uv run python src/extraction/bdc_xbrl.py
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pandas as pd
from edgar import set_identity, Company

# Import the schema. The project runs scripts directly (no installed package), so we add
# the schema folder to the path and import from it.
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"
sys.path.insert(0, str(SCHEMA_DIR))
from models import BalanceSheet, Fact, FilingExtraction, Source  # noqa: E402

warnings.filterwarnings("ignore")
EDGAR_IDENTITY = "brianpmoriarty@gmail.com"


# ── Concept map ───────────────────────────────────────────────────────────────
# schema field -> ordered list of candidate us-gaap concepts (first match wins).
# Where a line is commonly company-specific, we list the standard options and accept
# that some filers will miss here (refined as we test more funds).

BALANCE_SHEET_CONCEPTS: dict[str, list[str]] = {
    "total_assets": ["us-gaap:Assets"],
    "total_liabilities": ["us-gaap:Liabilities"],
    "total_net_assets": ["us-gaap:AssetsNet", "us-gaap:StockholdersEquity"],
    "investments_at_fair_value": [
        "us-gaap:InvestmentOwnedAtFairValue",
        "us-gaap:InvestmentsFairValueDisclosure",
        "us-gaap:AvailableForSaleSecuritiesDebtSecurities",
    ],
    "cash_and_equivalents": ["us-gaap:CashAndCashEquivalentsAtCarryingValue"],
    "total_debt": [
        "us-gaap:DebtLongtermAndShorttermCombinedAmount",
        "us-gaap:LongTermDebt",
        "us-gaap:DebtInstrumentCarryingAmount",
    ],
}


# ── Fact access ─────────────────────────────────────────────────────────────────

class FactSet:
    """Thin wrapper over a filing's XBRL facts as a single DataFrame, with helpers
    to pull a scalar value by concept for the current reporting period."""

    def __init__(self, facts_df: pd.DataFrame, reporting_date: str):
        self.df = facts_df
        self.reporting_date = reporting_date  # 'YYYY-MM-DD'

    @staticmethod
    def _iso(v) -> str:
        return str(v)[:10] if v is not None else ""

    def scalar(self, concepts: list[str]) -> Fact:
        """First non-dimensioned fact (current instant period) among candidate concepts."""
        for concept in concepts:
            rows = self.df[
                (self.df["concept"] == concept)
                & (self.df["is_dimensioned"] == False)  # noqa: E712
            ]
            if rows.empty:
                continue
            # Prefer the row whose instant matches the reporting date (current period);
            # fall back to the latest available instant.
            rows = rows.copy()
            rows["_inst"] = rows["period_instant"].map(self._iso)
            cur = rows[rows["_inst"] == self.reporting_date]
            pick = cur if not cur.empty else rows.sort_values("_inst").tail(1)
            val = pick.iloc[0]["numeric_value"]
            if pd.notna(val):
                return Fact(value=float(val), source=Source.XBRL, confidence=0.98,
                            raw_text=str(pick.iloc[0].get("label", "")) or None)
        return Fact()  # not found -> empty, value stays None

    def share_classes(self) -> list[str]:
        """Detect share-class labels from the dimensioned per-class net-asset facts."""
        rows = self.df[
            (self.df["concept"].isin(["us-gaap:AssetsNet", "us-gaap:StockholdersEquity"]))
            & (self.df["is_dimensioned"] == True)  # noqa: E712
        ]
        classes = []
        for lbl in rows.get("label", pd.Series(dtype=str)).dropna().unique():
            # labels look like "Class S Shares" -> "S"
            s = str(lbl).replace("Class", "").replace("Shares", "").strip()
            if s and s not in classes:
                classes.append(s)
        return classes


# ── Extractor ─────────────────────────────────────────────────────────────────

def extract_bdc(cik: int | str, form: str = "10-Q") -> FilingExtraction:
    """Extract the latest `form` filing for a BDC `cik` into a FilingExtraction."""
    company = Company(int(cik))
    filing = company.get_filings(form=form).latest()
    xbrl = filing.xbrl()

    reporting_date = FactSet._iso(filing.period_of_report)
    facts_df = xbrl.facts.query().to_dataframe()
    facts = FactSet(facts_df, reporting_date)

    # Balance sheet (Data Dictionary §2)
    bs = BalanceSheet()
    for field, concepts in BALANCE_SHEET_CONCEPTS.items():
        setattr(bs, field, facts.scalar(concepts))

    extraction = FilingExtraction(
        cik=str(int(cik)).zfill(10),
        fund_name=str(getattr(company, "name", "") or company.name),
        form_type=form,
        reporting_date=reporting_date,
        period_months=12 if form == "10-K" else 3,
        filing_date=FactSet._iso(filing.filing_date) or None,
        share_classes=facts.share_classes(),
        balance_sheet=bs,
        accession_no=str(filing.accession_no),
        extraction_source_file=None,
    )
    return extraction


def _coverage(extraction: FilingExtraction) -> None:
    """Print which balance-sheet fields were found vs missing."""
    print(f"\n{extraction.fund_name}  |  {extraction.form_type}  |  "
          f"period {extraction.reporting_date}  |  classes={extraction.share_classes}")
    print("Balance sheet coverage:")
    for field in BALANCE_SHEET_CONCEPTS:
        fact = getattr(extraction.balance_sheet, field)
        if fact.value is not None:
            print(f"  [x] {field:28s} {fact.value:>20,.0f}")
        else:
            print(f"  [ ] {field:28s} {'(missing)':>20s}")


if __name__ == "__main__":
    set_identity(EDGAR_IDENTITY)
    # Smoke test on Apollo Debt Solutions BDC.
    result = extract_bdc(1837532, form="10-Q")
    _coverage(result)
