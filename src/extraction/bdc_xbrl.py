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
from edgar import set_identity, configure_http, Company

# Import the schema. The project runs scripts directly (no installed package), so we add
# the schema folder to the path and import from it.
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"
sys.path.insert(0, str(SCHEMA_DIR))
from models import (  # noqa: E402
    BalanceSheet, DistributionsLeverage, Fact, FeesExpenseSupport, FilingExtraction,
    FinancialHighlights, IncomeStatement, PortfolioSummary, ShareClassNAV,
    StatementOfChanges, Source,
)

warnings.filterwarnings("ignore")
EDGAR_IDENTITY = "brianpmoriarty@gmail.com"


# ── Concept map ───────────────────────────────────────────────────────────────
# schema field -> ordered list of candidate us-gaap concepts (first match wins).
# Where a line is commonly company-specific, we list the standard options and accept
# that some filers will miss here (refined as we test more funds).

BALANCE_SHEET_CONCEPTS: dict[str, list[str]] = {
    "total_assets": ["us-gaap:Assets"],
    "total_liabilities": ["us-gaap:Liabilities"],
    # Net assets = stockholders'/members' equity for an investment company. Order matters:
    #   - StockholdersEquity FIRST — corp-structured BDCs (equals AssetsNet when both tagged).
    #     First Eagle mis-signs AssetsNet (-301.88M vs correct +301.88M), so AssetsNet must
    #     not win there.
    #   - MembersCapital / MembersEquity — LLC/partnership funds (e.g. Terra Income Fund 6 LLC)
    #     that don't tag StockholdersEquity at all.
    #   - AssetsNet LAST — fallback only, for filers that tag nothing else.
    "total_net_assets": ["us-gaap:StockholdersEquity", "us-gaap:MembersCapital",
                         "us-gaap:MembersEquity", "us-gaap:AssetsNet"],
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
        "us-gaap:Borrowings",
        "us-gaap:DebtAndCapitalLeaseObligations",
    ],
}

# When no single combined debt total exists, sum these distinct balance-sheet debt
# lines (revolver draws + notes). Mirrors the cash sum fallback.
DEBT_SUM_CONCEPTS = ["us-gaap:LineOfCredit", "us-gaap:OtherLongTermDebt", "us-gaap:NotesPayable"]

# Per-share-class fields (Data Dictionary §3). These come as facts dimensioned by the
# share-class axis below. First candidate concept that yields classes wins.
SHARE_CLASS_AXIS = "dim_us-gaap_StatementClassOfStockAxis"
# Some filers (e.g. Blackstone) report income components split by investment affiliation
# (unaffiliated / affiliated-noncontrolled / affiliated-controlled) instead of one total.
AFFILIATION_AXIS = "dim_us-gaap_InvestmentIssuerAffiliationAxis"
PER_CLASS_CONCEPTS: dict[str, list[str]] = {
    # Same ordering rationale as total_net_assets (StockholdersEquity first so First Eagle's
    # mis-signed per-class AssetsNet doesn't drive a negative computed NAV).
    "class_net_assets": ["us-gaap:StockholdersEquity", "us-gaap:MembersCapital",
                         "us-gaap:MembersEquity", "us-gaap:AssetsNet"],
    "class_shares_outstanding": [
        "us-gaap:SharesOutstanding",
        "us-gaap:CommonStockSharesOutstanding",
        "dei:EntityCommonStockSharesOutstanding",
    ],
    "class_nav_per_share": ["us-gaap:NetAssetValuePerShare"],
}

# Income statement (Data Dictionary §4) — DURATION facts. Components (interest / PIK /
# dividend / other) sum to total_investment_income (C7).
INCOME_CONCEPTS: dict[str, list[str]] = {
    "interest_income": ["us-gaap:InterestIncomeOperatingPaidInCash",
                        "us-gaap:InvestmentIncomeInterest", "us-gaap:InterestIncomeOperating"],
    "pik_interest_income": ["us-gaap:InterestIncomeOperatingPaidInKind"],
    "dividend_income": ["us-gaap:DividendIncomeOperating", "us-gaap:InvestmentIncomeDividend"],
    "other_investment_income": ["us-gaap:OtherIncome", "us-gaap:OtherInvestmentIncomeOperating"],
    "total_investment_income": ["us-gaap:GrossInvestmentIncomeOperating",
                                "us-gaap:InvestmentIncomeOperating", "us-gaap:InvestmentIncomeNet"],
    # Dictionary defines total_expenses NET of fee waivers (NII is computed off the net
    # figure). Candidate order = net concepts first; the GROSS line
    # (InvestmentIncomeInvestmentExpense) is the LAST resort so a filer that tags both
    # gross + net (e.g. AB Private Lending) picks the net one and C5 reconciles.
    "total_expenses": ["us-gaap:OperatingExpenses", "us-gaap:InvestmentCompanyExpensesNet",
                       "us-gaap:InvestmentCompanyExpenseAfterReductionOfFeeWaiverAndReimbursement",
                       "us-gaap:InvestmentIncomeInvestmentExpense"],
    # Income/excise tax — subtracted to reconcile NII (C5). Prefer the GAAP total-tax
    # line; fall back to the investment-company-specific excise-tax concept, then the
    # combined excise/sales-tax line some filers use (e.g. Crescent). NOTE: this is
    # refined after the extract loop for filers that split tax into Current + Deferred
    # (e.g. Oaktree) — see the income_tax refinement below.
    "income_tax_expense": ["us-gaap:IncomeTaxExpenseBenefit",
                           "us-gaap:InvestmentCompanyExciseTaxExpense",
                           "us-gaap:ExciseAndSalesTaxes"],
    "net_investment_income": ["us-gaap:NetInvestmentIncome"],
    # Authoritative subtotals used as C5 cross-check anchors (Data Dictionary §4).
    "income_before_tax": [
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"],
    "nii_after_expense_and_tax": ["us-gaap:InvestmentIncomeOperatingAfterExpenseAndTax"],
    "net_realized_gain_loss": [
        "us-gaap:RealizedGainLossInvestmentDerivativeAndForeignCurrencyTransactionPriceChangeOperatingAfterTax",
        "us-gaap:GainLossOnInvestments", "us-gaap:RealizedInvestmentGainsLosses"],
    "net_change_unrealized": [
        "us-gaap:UnrealizedGainLossInvestmentDerivativeAndForeignCurrencyTransactionPriceChangeOperatingAfterTax",
        "us-gaap:UnrealizedGainLossOnInvestments"],
    "net_increase_in_net_assets_ops": ["us-gaap:NetIncomeLoss", "us-gaap:ProfitLoss"],
}

# Fees (Data Dictionary §11) — DURATION facts.
FEE_CONCEPTS: dict[str, list[str]] = {
    "management_fee": ["us-gaap:ManagementFeeExpense", "us-gaap:InvestmentCompanyManagementFeeExpense"],
    "incentive_fee": ["us-gaap:IncentiveFeeExpense"],
}

# Portfolio scalars (Data Dictionary §9) — INSTANT facts.
PORTFOLIO_SCALAR_CONCEPTS: dict[str, list[str]] = {
    "investments_at_cost": ["us-gaap:InvestmentOwnedAtCost", "us-gaap:InvestmentOwnedAtCostNet"],
}

# Financial highlights (§7). Often per-class and fuller in 10-K; expense/NII ratios may be
# absent in 10-Q (LLM-fallback / 10-K territory).
HIGHLIGHTS_CONCEPTS: dict[str, list[str]] = {
    "expense_ratio": ["us-gaap:InvestmentCompanyExpenseRatio"],
    "net_investment_income_ratio": ["us-gaap:InvestmentCompanyRatioOfNetInvestmentIncomeLossToAverageNetAssets1"],
    "total_return": ["us-gaap:InvestmentCompanyTotalReturn"],
    "portfolio_turnover": ["us-gaap:InvestmentCompanyPortfolioTurnover"],
}

# Distributions & leverage (§8).
DIST_LEVERAGE_CONCEPTS: dict[str, list[str]] = {
    "asset_coverage_ratio": ["us-gaap:InvestmentCompanySeniorSecurityIndebtednessAssetCoverageRatio"],
    "weighted_avg_interest_rate": ["us-gaap:LongTermDebtWeightedAverageInterestRateOverTime",
                                   "us-gaap:DebtWeightedAverageInterestRate"],
}

# Statement of changes (§5) — only the distributions total for now (enables coverage).
CHANGES_CONCEPTS: dict[str, list[str]] = {
    "distributions_declared": ["us-gaap:InvestmentCompanyDividendDistribution",
                               "us-gaap:DistributionsMade"],
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
                & (self.df["period_type"] == "instant")  # instant facts only (see scalar_any)
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
                & (self.df["period_type"] == "instant")
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
    def _months(ps: str, pe: str) -> int | None:
        import datetime
        try:
            a = datetime.date.fromisoformat(ps)
            b = datetime.date.fromisoformat(pe)
            return round((b - a).days / 30.44)
        except Exception:
            return None

    def _duration_best(self, concept: str, target_months: int):
        """Best non-dimensioned duration row for `concept`: ends on the reporting date
        and whose length is closest to target_months (3 for 10-Q, 12 for 10-K).
        This is how we pick the primary quarter and ignore 10-Q year-to-date rows."""
        rows = self.df[
            (self.df["concept"] == concept)
            & (self.df["is_dimensioned"] == False)  # noqa: E712
            & (self.df["period_type"] == "duration")
        ].copy()
        if rows.empty:
            return None
        rows["_ps"] = rows["period_start"].map(self._iso)
        rows["_pe"] = rows["period_end"].map(self._iso)
        rows = rows[(rows["_pe"] == self.reporting_date) & rows["numeric_value"].notna()]
        if rows.empty:
            return None
        rows["_m"] = rows.apply(lambda r: self._months(r["_ps"], r["_pe"]), axis=1)
        rows = rows[rows["_m"].notna()].copy()
        if rows.empty:
            return None
        rows["_md"] = (rows["_m"] - target_months).abs()
        return rows.sort_values("_md").iloc[0]

    def _duration_affiliation_sum(self, concept: str, target_months: int) -> float | None:
        """Sum a duration concept across investment-affiliation members for the primary
        period. Used when a filer splits income components by affiliation rather than
        reporting one undimensioned total (e.g. Blackstone)."""
        if AFFILIATION_AXIS not in self.df.columns:
            return None
        rows = self.df[
            (self.df["concept"] == concept)
            & (self.df["period_type"] == "duration")
            & self.df[AFFILIATION_AXIS].notna()
        ].copy()
        if rows.empty:
            return None
        dim_cols = self._dim_cols
        rows["_n"] = rows[dim_cols].notna().sum(axis=1)            # affiliation is the only axis
        rows["_ps"] = rows["period_start"].map(self._iso)
        rows["_pe"] = rows["period_end"].map(self._iso)
        rows = rows[(rows["_n"] == 1) & (rows["_pe"] == self.reporting_date)
                    & rows["numeric_value"].notna()].copy()
        if rows.empty:
            return None
        rows["_m"] = rows.apply(lambda r: self._months(r["_ps"], r["_pe"]), axis=1)
        rows = rows[rows["_m"].notna()].copy()
        if rows.empty:
            return None
        rows["_md"] = (rows["_m"] - target_months).abs()
        rows = rows[rows["_md"] == rows["_md"].min()]              # the primary-period window
        rows = rows.drop_duplicates(subset=[AFFILIATION_AXIS])    # one row per affiliation member
        return float(rows["numeric_value"].sum())

    def duration_scalar(self, concepts: list[str], target_months: int) -> Fact:
        """First candidate concept with a value for the primary period. For each concept
        we try the undimensioned total first, then fall back to summing across the
        investment-affiliation axis."""
        for concept in concepts:
            best = self._duration_best(concept, target_months)
            if best is not None:
                return Fact(value=float(best["numeric_value"]), source=Source.XBRL,
                            confidence=0.97, raw_text=str(best.get("label", "")) or None)
            summed = self._duration_affiliation_sum(concept, target_months)
            if summed is not None:
                return Fact(value=summed, source=Source.XBRL, confidence=0.95,
                            raw_text=f"{concept.split(':')[-1]} (sum over affiliation)")
        return Fact()

    def scalar_any(self, concepts: list[str], target_months: int) -> Fact:
        """Ratio/value fields that may be tagged as either instant or duration
        (e.g. asset coverage = instant; weighted-avg interest rate = duration)."""
        f = self.scalar(concepts)
        return f if f.value is not None else self.duration_scalar(concepts, target_months)

    def duration_period_start(self, concepts: list[str], target_months: int) -> str | None:
        for concept in concepts:
            best = self._duration_best(concept, target_months)
            if best is not None:
                return best["_ps"]
        return None

    @staticmethod
    def _normalize_class(member) -> str | None:
        """'bcred:CommonClassIMember' / 'us-gaap:ClassSMember' -> 'I' / 'S'."""
        s = str(member).split(":")[-1]
        for tok in ("CommonClass", "Class", "CommonStock", "Common",
                    "Shares", "Share", "Member", "Stock"):
            s = s.replace(tok, "")
        s = s.strip()
        # Drop combined members like 'SDAndI' ("Class S, D and I") — not a real class.
        if not s or "and" in s.lower():
            return None
        return s

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
    return extract_filing(company, filing, str(int(cik)).zfill(10), form)


def extract_filing(company, filing, cik: str, form: str) -> FilingExtraction:
    """Extract a specific filing object into a FilingExtraction (used by the runner)."""
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
    # Debt fallback: filers reporting revolver + notes separately (e.g. AB Private
    # Lending) instead of one combined debt line -> sum the components.
    if bs.total_debt.value is None:
        bs.total_debt = facts.sum_scalar(DEBT_SUM_CONCEPTS)

    # Income statement (§4) + fees (§11) — duration facts for the primary period
    target_months = 12 if form == "10-K" else 3
    inc = IncomeStatement()
    for field, concepts in INCOME_CONCEPTS.items():
        setattr(inc, field, facts.duration_scalar(concepts, target_months))
    # Income-tax refinement: some filers (e.g. Oaktree) tag a tiny/partial
    # IncomeTaxExpenseBenefit while the real tax that bridges pre-tax income to NII is
    # split into Current + Deferred components. When that split exists and is larger,
    # it is the truer total — use it. (AB, which tags only the combined excise line,
    # has no Current/Deferred split, so it is left untouched.)
    ct = facts.duration_scalar(["us-gaap:CurrentIncomeTaxExpenseBenefit"], target_months).value
    dt = facts.duration_scalar(["us-gaap:DeferredIncomeTaxExpenseBenefit"], target_months).value
    if ct is not None or dt is not None:
        split_tax = (ct or 0.0) + (dt or 0.0)
        cur = inc.income_tax_expense.value
        if cur is None or abs(split_tax) > abs(cur):
            inc.income_tax_expense = Fact(value=split_tax, source=Source.XBRL,
                                          confidence=0.95,
                                          raw_text="Current + Deferred income tax")
    fees = FeesExpenseSupport()
    for field, concepts in FEE_CONCEPTS.items():
        setattr(fees, field, facts.duration_scalar(concepts, target_months))
    period_start = facts.duration_period_start(
        INCOME_CONCEPTS["total_investment_income"] + ["us-gaap:NetInvestmentIncome"],
        target_months,
    )

    # Portfolio scalars (§9)
    portfolio = PortfolioSummary()
    for field, concepts in PORTFOLIO_SCALAR_CONCEPTS.items():
        setattr(portfolio, field, facts.scalar(concepts))

    # Financial highlights (§7) + distributions & leverage (§8) + distributions (§5).
    # These ratios may be tagged instant or duration -> scalar_any.
    highlights = FinancialHighlights()
    for field, concepts in HIGHLIGHTS_CONCEPTS.items():
        setattr(highlights, field, facts.scalar_any(concepts, target_months))
    dist_lev = DistributionsLeverage()
    for field, concepts in DIST_LEVERAGE_CONCEPTS.items():
        setattr(dist_lev, field, facts.scalar_any(concepts, target_months))
    changes = StatementOfChanges()
    for field, concepts in CHANGES_CONCEPTS.items():
        setattr(changes, field, facts.scalar_any(concepts, target_months))

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
        period_start=period_start,
        period_months=target_months,
        filing_date=FactSet._iso(filing.filing_date) or None,
        share_classes=classes,
        balance_sheet=bs,
        income_statement=inc,
        statement_of_changes=changes,
        financial_highlights=highlights,
        distributions_leverage=dist_lev,
        fees=fees,
        portfolio_summary=portfolio,
        share_classes_nav=share_classes_nav,
        accession_no=str(filing.accession_no),
        extraction_source_file=None,
    )
    compute_derived(extraction)
    return extraction


def _ratio(num, den):
    if num is None or den in (None, 0):
        return None
    return num / den


def compute_derived(e: FilingExtraction) -> FilingExtraction:
    """Compute the derived metrics (Data Dictionary §10) that depend only on fields we
    already extract. Each result is tagged source=COMPUTED. Missing inputs -> empty Fact.
    Metrics needing not-yet-extracted inputs (distribution coverage, lending spread,
    liquidity coverage) are left for later increments."""
    bs, inc, ps, d = e.balance_sheet, e.income_statement, e.portfolio_summary, e.derived

    def mk(v):
        return Fact(value=v, source=Source.COMPUTED, confidence=0.95) if v is not None else Fact()

    ta, tl = bs.total_assets.value, bs.total_liabilities.value
    tna, debt, cash = bs.total_net_assets.value, bs.total_debt.value, bs.cash_and_equivalents.value
    ifv, icost = bs.investments_at_fair_value.value, ps.investments_at_cost.value

    d.leverage_ratio = mk(_ratio(debt, tna))
    d.net_debt = mk(debt - cash) if (debt is not None and cash is not None) else Fact()
    # Regulatory-style asset coverage: (assets - non-debt liabilities) / debt
    if None not in (ta, tl, debt) and debt:
        d.asset_coverage_pct = mk((ta - (tl - debt)) / debt)
    d.portfolio_mark = mk(_ratio(ifv, icost))
    d.pik_income_ratio = mk(_ratio(inc.pik_interest_income.value, inc.total_investment_income.value))
    d.distribution_coverage_ratio = mk(
        _ratio(inc.net_investment_income.value, e.statement_of_changes.distributions_declared.value))
    return e


def _fmt(v) -> str:
    return f"{v:,.0f}" if v is not None else "(missing)"


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
    inc = extraction.income_statement
    print(f"Income statement coverage (period {extraction.period_start} -> "
          f"{extraction.reporting_date}, {extraction.period_months}mo):")
    for field in INCOME_CONCEPTS:
        fact = getattr(inc, field)
        if fact.value is not None:
            print(f"  [x] {field:32s} {fact.value:>20,.0f}")
        else:
            print(f"  [ ] {field:32s} {'(missing)':>20s}")
    # Inline identity sanity checks (the real validation layer comes later)
    comps = [inc.interest_income.value, inc.pik_interest_income.value,
             inc.dividend_income.value, inc.other_investment_income.value]
    if all(c is not None for c in comps) and inc.total_investment_income.value is not None:
        diff = sum(comps) - inc.total_investment_income.value
        print(f"      C7 components sum vs total: diff={diff:,.0f}")
    if (inc.total_investment_income.value is not None and inc.total_expenses.value is not None
            and inc.net_investment_income.value is not None):
        tax = inc.income_tax_expense.value or 0.0
        diff = (inc.total_investment_income.value - inc.total_expenses.value - tax) - inc.net_investment_income.value
        print(f"      C5 income identity (TII-exp-tax vs NII): diff={diff:,.0f} (tax={tax:,.0f})")
    fees = extraction.fees
    print(f"  fees: management={_fmt(fees.management_fee.value)}  incentive={_fmt(fees.incentive_fee.value)}")
    d = extraction.derived
    def _r(f): return f"{f.value:,.3f}" if f.value is not None else "(--)"
    print("Derived metrics:")
    print(f"  leverage={_r(d.leverage_ratio)}  asset_cov_pct={_r(d.asset_coverage_pct)}  "
          f"portfolio_mark={_r(d.portfolio_mark)}  pik_ratio={_r(d.pik_income_ratio)}  "
          f"net_debt={_fmt(d.net_debt.value)}")
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
    configure_http(use_system_certs=True)  # OS cert store → works behind corporate SSL inspection
    # Smoke test on Apollo Debt Solutions BDC.
    result = extract_bdc(1837532, form="10-Q")
    _coverage(result)
