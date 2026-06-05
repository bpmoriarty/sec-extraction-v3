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
from models import BalanceSheet, Fact, FilingExtraction, ShareClassNAV, Source  # noqa: E402

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

# Per-share-class fields (Data Dictionary §3). These come as facts dimensioned by the
# share-class axis below. First candidate concept that yields classes wins.
SHARE_CLASS_AXIS = "dim_us-gaap_StatementClassOfStockAxis"
PER_CLASS_CONCEPTS: dict[str, list[str]] = {
    "class_net_assets": ["us-gaap:AssetsNet", "us-gaap:StockholdersEquity"],
    "class_shares_outstanding": [
        "us-gaap:SharesOutstanding",
        "us-gaap:CommonStockSharesOutstanding",
        "dei:EntityCommonStockSharesOutstanding",
    ],
    "class_nav_per_share": ["us-gaap:NetAssetValuePerShare"],
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

    def sum_scalar(self, concepts: list[str]) -> Fact:
        """Sum current-period non-dimensioned values across concepts (e.g. some filers
        report cash as separate Cash + CashEquivalents lines instead of one combined)."""
        total, found, parts = 0.0, False, []
        for concept in concepts:
            rows = self.df[
                (self.df["concept"] == concept)
                & (self.df["is_dimensioned"] == False)  # noqa: E712
            ].copy()
            if rows.empty:
                continue
            rows["_inst"] = rows["period_instant"].map(self._iso)
            cur = rows[rows["_inst"] == self.reporting_date]
            pick = cur if not cur.empty else rows.sort_values("_inst").tail(1)
            val = pick.iloc[0]["numeric_value"]
            if pd.notna(val):
                total += float(val)
                found = True
                parts.append(concept.split(":")[-1])
        if found:
            return Fact(value=total, source=Source.XBRL, confidence=0.95,
                        raw_text=" + ".join(parts))
        return Fact()

    @staticmethod
    def _normalize_class(member) -> str | None:
        """'bcred:CommonClassIMember' / 'us-gaap:ClassSMember' -> 'I' / 'S'."""
        s = str(member).split(":")[-1]
        for tok in ("CommonClass", "Class", "CommonStock", "Common",
                    "Shares", "Share", "Member", "Stock"):
            s = s.replace(tok, "")
        return s.strip() or None

    @property
    def _dim_cols(self) -> list[str]:
        return [c for c in self.df.columns if c.startswith("dim_")]

    def per_class(self, concepts: list[str]) -> dict[str, Fact]:
        """{class_label: Fact} from facts dimensioned ONLY by the share-class axis,
        for the current period. Single-axis filter avoids cross-tabs (e.g. class x
        consolidation). First candidate concept that yields classes wins."""
        result: dict[str, Fact] = {}
        if SHARE_CLASS_AXIS not in self.df.columns:
            return result
        dim_cols = self._dim_cols
        for concept in concepts:
            rows = self.df[
                (self.df["concept"] == concept) & self.df[SHARE_CLASS_AXIS].notna()
            ].copy()
            if rows.empty:
                continue
            rows["_ndim"] = rows[dim_cols].notna().sum(axis=1)   # only class-dimensioned
            rows["_inst"] = rows["period_instant"].map(self._iso)
            rows = rows[(rows["_ndim"] == 1) & (rows["_inst"] == self.reporting_date)]
            for _, r in rows.iterrows():
                cls = self._normalize_class(r[SHARE_CLASS_AXIS])
                if cls and cls not in result and pd.notna(r["numeric_value"]):
                    result[cls] = Fact(value=float(r["numeric_value"]),
                                       source=Source.XBRL, confidence=0.97,
                                       raw_text=str(r.get("label", "")) or None)
            if result:
                return result
        return result

    def share_classes(self) -> list[str]:
        """Clean share-class list from the share-class axis members only."""
        if SHARE_CLASS_AXIS not in self.df.columns:
            return []
        classes: list[str] = []
        for m in self.df[SHARE_CLASS_AXIS].dropna().unique():
            c = self._normalize_class(m)
            if c and c not in classes:
                classes.append(c)
        return sorted(classes)


# ── Extractor ─────────────────────────────────────────────────────────────────

def extract_bdc(cik: int | str, form: str = "10-Q") -> FilingExtraction:
    """Extract the latest `form` filing for a BDC `cik` into a FilingExtraction."""
    company = Company(int(cik))
    filing = company.get_filings(form=form).latest()
    xbrl = filing.xbrl()

    reporting_date = FactSet._iso(filing.period_of_report)
    facts_df = xbrl.facts.query().with_dimensions().to_dataframe()
    facts = FactSet(facts_df, reporting_date)

    # Balance sheet (Data Dictionary §2)
    bs = BalanceSheet()
    for field, concepts in BALANCE_SHEET_CONCEPTS.items():
        setattr(bs, field, facts.scalar(concepts))
    # Cash fallback: some filers (e.g. HPS) report Cash + CashEquivalents separately
    # rather than one combined concept — sum them if the combined line was missing.
    if bs.cash_and_equivalents.value is None:
        bs.cash_and_equivalents = facts.sum_scalar(
            ["us-gaap:Cash", "us-gaap:CashEquivalentsAtCarryingValue"]
        )

    # Per-share-class NAV (Data Dictionary §3)
    classes = facts.share_classes()
    per_class = {field: facts.per_class(concepts)
                 for field, concepts in PER_CLASS_CONCEPTS.items()}
    share_classes_nav = [
        ShareClassNAV(
            class_label=cls,
            class_net_assets=per_class["class_net_assets"].get(cls, Fact()),
            class_shares_outstanding=per_class["class_shares_outstanding"].get(cls, Fact()),
            class_nav_per_share=per_class["class_nav_per_share"].get(cls, Fact()),
        )
        for cls in classes
    ]

    extraction = FilingExtraction(
        cik=str(int(cik)).zfill(10),
        fund_name=str(getattr(company, "name", "") or company.name),
        form_type=form,
        reporting_date=reporting_date,
        period_months=12 if form == "10-K" else 3,
        filing_date=FactSet._iso(filing.filing_date) or None,
        share_classes=classes,
        balance_sheet=bs,
        share_classes_nav=share_classes_nav,
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
    print("Per-share-class (net assets / shares / NAV):")
    for sc in extraction.share_classes_nav:
        na = sc.class_net_assets.value
        sh = sc.class_shares_outstanding.value
        nav = sc.class_nav_per_share.value
        na_s = f"{na:>18,.0f}" if na is not None else f"{'(missing)':>18s}"
        sh_s = f"{sh:>16,.0f}" if sh is not None else f"{'(missing)':>16s}"
        nav_s = f"{nav:>8,.2f}" if nav is not None else f"{'(--)':>8s}"
        print(f"  class {sc.class_label:6s} NA {na_s}  sh {sh_s}  NAV {nav_s}")


if __name__ == "__main__":
    set_identity(EDGAR_IDENTITY)
    # Smoke test on Apollo Debt Solutions BDC.
    result = extract_bdc(1837532, form="10-Q")
    _coverage(result)
