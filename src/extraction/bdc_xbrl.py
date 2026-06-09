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

import datetime
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
    BalanceSheet, CashFlowStatement, DistributionsLeverage, Fact, FairValueHierarchy,
    FeesExpenseSupport, FilingExtraction, FinancialHighlights, IncomeStatement, PortfolioSummary,
    ShareClassNAV, StatementOfChanges, Source,
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
# Fair-value hierarchy (Data Dictionary §6). Investment fair value is tagged by level on
# this axis (cross-tabbed with asset-type / valuation-technique). First concept wins.
FV_HIERARCHY_AXIS = "dim_us-gaap_FairValueByFairValueHierarchyLevelAxis"
FV_CONCEPTS = ["us-gaap:InvestmentOwnedAtFairValue", "us-gaap:InvestmentsFairValueDisclosure"]

# Schedule of investments — holding-level facts on the investment-identifier axis (§9).
# Member label is "Issuer Name | Affiliation". Reassessment 2026-06-08 confirmed these are
# the robustly-tagged us-gaap concepts; per-filer-inconsistent ones (rate, commitment) are
# handled gracefully (the derived metric stays null when coverage is thin).
INVESTMENT_AXIS = "dim_us-gaap_InvestmentIdentifierAxis"
HOLDING_CONCEPTS = {
    "us-gaap:InvestmentOwnedAtFairValue": "fair_value",
    "us-gaap:InvestmentOwnedAtCost": "cost",
    "us-gaap:InvestmentOwnedBalancePrincipalAmount": "principal",
    "us-gaap:InvestmentInterestRate": "rate",                    # all-in rate (often untagged)
    "us-gaap:InvestmentBasisSpreadVariableRate": "spread",       # spread over base (robust)
    "us-gaap:InvestmentInterestRatePaidInKind": "pik_rate",
    "us-gaap:InvestmentInterestRateFloor": "floor",
    "us-gaap:InvestmentOwnedBalanceShares": "shares",
    "us-gaap:InvestmentCompanyFinancialCommitmentToInvesteeFutureAmount": "commitment",
    "us-gaap:InvestmentOwnedPercentOfNetAssets": "pct_na",
}
# Additive holding fields (summed across a member's same-date facts); the rest are rate-like
# attributes where we keep the first non-null value.
_HOLDING_ADDITIVE = {"fair_value", "cost", "principal", "commitment", "shares"}
# Trust the FV-weighted all-in yield only if the rate concept covers >= this share of FV
# (Apollo/First Eagle barely tag InvestmentInterestRate -> their yield stays null, by design).
HOLDING_YIELD_COVERAGE_MIN = 0.60
# Schedule-of-investments reconciliation (Layer 2 gate). The summed holding fair values
# should ~equal the balance-sheet investments total. A few filers tag SUBTOTAL/aggregation
# rows (by-industry, by-type, grand totals — e.g. Kennedy Lewis pre-2025) on the holding
# axis, so the leaves over-sum by multiples. When the (post-Layer-1) sum exceeds the balance
# sheet by this factor, the SOI is structurally contaminated -> we SUPPRESS the derived §9
# metrics (leave them null) rather than publish corrupted numbers. 1.25 sits well above the
# observed legit-noise band (most filings reconcile within 1.05-1.2x; the broken ones are >=2x).
HOLDINGS_RECON_GATE = 1.25
# Layer-1 scale-reconciliation tolerance: dropping a minority decimals-scale group only counts
# as "the fix" if the remaining leaves land within this fraction of the balance-sheet total.
HOLDINGS_SCALE_TOL = 0.05
# hierarchy member suffix -> FairValueHierarchy field
FV_LEVEL_FIELDS = {
    "FairValueInputsLevel1Member": "fv_level_1",
    "FairValueInputsLevel2Member": "fv_level_2",
    "FairValueInputsLevel3Member": "fv_level_3",
    "FairValueMeasuredAtNetAssetValuePerShareMember": "fv_nav_practical_expedient",
}
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
    # PIK dividends + the combined interest-and-dividend PIK line. Captured so C7 can
    # bound a filer's total PIK income (some break PIK out here, some fold it into the
    # interest line). Additive: these feed the C7 PIK band only, not C5/derived.
    "pik_dividend_income": ["us-gaap:DividendIncomeOperatingPaidInKind"],
    "pik_income_combined": ["us-gaap:InterestAndDividendIncomeOperatingPaidInKind"],
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

# Liquidity (§12) — undrawn revolver/credit-facility capacity comes from
# us-gaap:LineOfCreditFacilityRemainingBorrowingCapacity via FactSet.undrawn_capacity(), which
# handles the undimensioned total (Apollo/Blackstone/HPS) and the cross-tabbed per-facility case
# (AB/BlackRock/John Hancock/PGIM/Prospect). See that method for the methodology and C8 for the gate.

# Statement of cash flows (§5b) — DURATION facts, undimensioned, for the primary period. For an
# investment company, buying/selling investments is an OPERATING activity (no separate investing
# section usually), so net_cash_investing is often absent. net_change_in_cash is the bottom-line
# change INCLUDING the FX effect; effect_of_fx is captured so C9 foots exactly (op+inv+fin+fx).
CASH_FLOW_CONCEPTS: dict[str, list[str]] = {
    "net_cash_operating": ["us-gaap:NetCashProvidedByUsedInOperatingActivities"],
    "net_cash_investing": ["us-gaap:NetCashProvidedByUsedInInvestingActivities"],
    "net_cash_financing": ["us-gaap:NetCashProvidedByUsedInFinancingActivities"],
    "effect_of_fx": [
        "us-gaap:EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "us-gaap:EffectOfExchangeRateOnCashAndCashEquivalents"],
    "net_change_in_cash": [
        "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",
        "us-gaap:CashAndCashEquivalentsPeriodIncreaseDecrease"],
    "interest_paid": ["us-gaap:InterestPaidNet", "us-gaap:InterestPaid"],
    "investment_purchases": ["us-gaap:PaymentsForPurchaseOfInvestmentOperatingActivity",
                             "us-gaap:PaymentsToAcquireInvestments"],
    "investment_sales": ["us-gaap:ProceedsFromDispositionOfInvestmentOperatingActivity",
                         "us-gaap:ProceedsFromSaleMaturityAndCollectionsOfInvestments"],
}

# Statement of changes (§5) — only the distributions total for now (enables coverage).
CHANGES_CONCEPTS: dict[str, list[str]] = {
    "distributions_declared": ["us-gaap:InvestmentCompanyDividendDistribution",
                               "us-gaap:DistributionsMade"],
    # Capital share transactions (DURATION), best-effort. The net-asset roll-forward has many
    # filer-specific components (DRIP reinvestment, offering costs, early-repurchase deductions),
    # so C6 is SOFT (flag-and-keep): it passes for filers whose components net out within
    # tolerance, and the captured line items stay useful data regardless of whether C6 closes.
    "capital_raised": ["us-gaap:ProceedsFromIssuanceOfCommonStock",
                       "us-gaap:StockIssuedDuringPeriodValueNewIssues"],
    "repurchases": ["us-gaap:StockRepurchasedDuringPeriodValue",
                    "us-gaap:PaymentsForRepurchaseOfCommonStock",
                    "us-gaap:StockRepurchasedAndRetiredDuringPeriodValue"],
}

# Capital share activity (§5, detail) — DURATION facts, tagged PER SHARE CLASS (no undimensioned
# total for most filers) -> extracted via duration_class_scalar (undimensioned, else sum over the
# share-class axis). The DRIP value also feeds the C6 roll-forward retry.
SHARE_ACTIVITY_CONCEPTS: dict[str, list[str]] = {
    "shares_issued_new": ["us-gaap:StockIssuedDuringPeriodSharesNewIssues"],
    "proceeds_new_issues": ["us-gaap:StockIssuedDuringPeriodValueNewIssues"],
    "shares_issued_drip": ["us-gaap:StockIssuedDuringPeriodSharesDividendReinvestmentPlan"],
    "value_drip": ["us-gaap:StockIssuedDuringPeriodValueDividendReinvestmentPlan"],
    "shares_repurchased": ["us-gaap:StockRepurchasedDuringPeriodShares",
                           "us-gaap:StockRepurchasedAndRetiredDuringPeriodShares"],
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

    def instant_scalar_at(self, concepts: list[str], date_iso: str) -> Fact:
        """First non-dimensioned instant value among candidates whose instant == date_iso.
        Used for the statement-of-changes BEGINNING balance (equity at the prior period end,
        i.e. the day before period_start)."""
        for concept in concepts:
            rows = self.df[
                (self.df["concept"] == concept)
                & (self.df["is_dimensioned"] == False)  # noqa: E712
                & (self.df["period_type"] == "instant")
            ]
            if rows.empty:
                continue
            rows = rows.copy()
            rows["_inst"] = rows["period_instant"].map(self._iso)
            cur = rows[(rows["_inst"] == date_iso) & rows["numeric_value"].notna()]
            if not cur.empty:
                r = cur.iloc[0]
                return Fact(value=float(r["numeric_value"]), source=Source.XBRL, confidence=0.95,
                            raw_text=str(r.get("label", "")) or None)
        return Fact()

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

    def undrawn_capacity(self) -> Fact:
        """Undrawn revolver / credit-facility capacity (§12) =
        us-gaap:LineOfCreditFacilityRemainingBorrowingCapacity at the reporting date.

        Prefer the UNDIMENSIONED total — a clean fund-level figure for filers that tag it
        (Apollo/Blackstone/HPS). Otherwise the figure is only tagged per-facility, and those rows
        are CROSS-TABBED: the same total appears under a coarse axis-signature AND a finer
        breakdown (e.g. AB, John Hancock: the (CreditFacility,) sum equals the
        (CreditFacility, LineOfCreditFacility) sum). Summing ALL rows double-counts. So we group
        facts by their exact axis-signature and take the LARGEST single group's sum of distinct
        members: cross-tab groups are equal (→ that is the total), and a filer that splits debt
        across genuinely-distinct instrument types (e.g. BlackRock: revolving facilities vs note
        tranches vs promissory) yields the dominant revolving line — the most relevant, and
        conservative (never over-counts), liquidity figure. We deliberately do NOT derive undrawn
        from MaximumBorrowingCapacity − drawn: the maximum-capacity facts double-count across the
        CreditFacility + LegalEntity axes and not all filers tag facility-level drawn. C8 bounds
        the result for plausibility."""
        concept = "us-gaap:LineOfCreditFacilityRemainingBorrowingCapacity"
        rows = self.df[(self.df["concept"] == concept)
                       & (self.df["period_type"] == "instant")
                       & self.df["numeric_value"].notna()].copy()
        if rows.empty:
            return Fact()
        rows["_inst"] = rows["period_instant"].map(self._iso)
        rows = rows[rows["_inst"] == self.reporting_date]
        if rows.empty:
            return Fact()
        undim = rows[rows["is_dimensioned"] == False]  # noqa: E712
        if not undim.empty:
            return Fact(value=float(undim.iloc[0]["numeric_value"]), source=Source.XBRL,
                        confidence=0.97, raw_text="remaining capacity (undimensioned total)")
        # Only per-facility rows -> group by axis-signature; take the largest group's distinct sum.
        dim_cols = self._dim_cols
        rows["_sig"] = rows.apply(
            lambda r: tuple(sorted(c for c in dim_cols if pd.notna(r[c]))), axis=1)
        best = None
        for sig, grp in rows.groupby("_sig"):
            grp = grp.drop_duplicates(subset=list(sig)) if sig else grp
            total = float(grp["numeric_value"].sum())
            if best is None or total > best:
                best = total
        if best is None:
            return Fact()
        return Fact(value=best, source=Source.XBRL, confidence=0.90,
                    raw_text=f"remaining capacity (largest of {rows['_sig'].nunique()} axis groups)")

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

    def _duration_class_sum(self, concept: str, target_months: int) -> float | None:
        """Sum a duration concept across StatementClassOfStock members for the primary period.
        Capital share activity (shares/value issued, repurchased) is tagged PER SHARE CLASS with
        no undimensioned total for most filers (e.g. Apollo). Mirrors _duration_affiliation_sum:
        only single-axis (class-only) rows are summed, deduped per class, so cross-tabbed rows
        can't double-count."""
        if SHARE_CLASS_AXIS not in self.df.columns:
            return None
        rows = self.df[
            (self.df["concept"] == concept)
            & (self.df["period_type"] == "duration")
            & self.df[SHARE_CLASS_AXIS].notna()
        ].copy()
        if rows.empty:
            return None
        dim_cols = self._dim_cols
        rows["_n"] = rows[dim_cols].notna().sum(axis=1)            # share class is the only axis
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
        rows = rows.drop_duplicates(subset=[SHARE_CLASS_AXIS])    # one row per share class
        return float(rows["numeric_value"].sum())

    def duration_class_scalar(self, concepts: list[str], target_months: int) -> Fact:
        """Duration value preferring the undimensioned total, else summed across share classes
        (capital share activity — see _duration_class_sum)."""
        for concept in concepts:
            best = self._duration_best(concept, target_months)
            if best is not None:
                return Fact(value=float(best["numeric_value"]), source=Source.XBRL,
                            confidence=0.96, raw_text=str(best.get("label", "")) or None)
            summed = self._duration_class_sum(concept, target_months)
            if summed is not None:
                return Fact(value=summed, source=Source.XBRL, confidence=0.93,
                            raw_text=f"{concept.split(':')[-1]} (sum over share classes)")
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

    def _nav_practical_expedient(self) -> Fact:
        """The 'measured at NAV' (practical-expedient) bucket, which sits OUTSIDE the L1/L2/L3
        hierarchy (money-market / alternative investments). Filers tag it under varied concepts:
        on the hierarchy NAV member (e.g. us-gaap:AlternativeInvestment) or as a standalone
        '...MeasuredAtNetAssetValue' line. Reporting-date instant value (first match wins)."""
        inst = self.df[(self.df["period_type"] == "instant")
                       & self.df["numeric_value"].notna()].copy()
        if inst.empty:
            return Fact()
        inst["_inst"] = inst["period_instant"].map(self._iso)
        inst = inst[inst["_inst"] == self.reporting_date]
        if inst.empty:
            return Fact()
        inst["_ndim"] = inst[self._dim_cols].notna().sum(axis=1)
        # (a) AlternativeInvestment / InvestmentOwnedAtFairValue on the hierarchy NAV member
        if FV_HIERARCHY_AXIS in inst.columns:
            nav = inst[inst[FV_HIERARCHY_AXIS].astype(str).str.contains("NetAssetValue", na=False)
                       & (inst["_ndim"] == 1)
                       & inst["concept"].isin(["us-gaap:AlternativeInvestment",
                                               "us-gaap:InvestmentOwnedAtFairValue"])]
            if not nav.empty:
                r = nav.iloc[0]
                return Fact(value=float(r["numeric_value"]), source=Source.XBRL, confidence=0.90,
                            raw_text=f"{str(r['concept']).split(':')[-1]} (NAV member)")
        # (b) standalone undimensioned '...MeasuredAtNetAssetValue' concept (incl. custom ck:)
        und = inst[(inst["_ndim"] == 0)
                   & inst["concept"].astype(str).str.contains("MeasuredAtNetAssetValue", na=False)]
        if not und.empty:
            r = und.iloc[0]
            return Fact(value=float(r["numeric_value"]), source=Source.XBRL, confidence=0.85,
                        raw_text=str(r["concept"]).split(":")[-1])
        return Fact()

    def fv_hierarchy(self, total: float | None) -> dict[str, Fact]:
        """{fv_level_1/2/3, fv_nav_practical_expedient: Fact} from the fair-value hierarchy
        axis at the reporting date. Per level, PREFER the per-level TOTAL row (hierarchy axis
        is the only dimension — reliable, reconciles to the undimensioned total); else FALL
        BACK to summing the asset-type breakdown (hierarchy + exactly one other dim). The
        breakdown is often cross-tabbed (asset-type x valuation-technique), so a naive sum can
        double-count — we therefore TRUST the fallback only if the levels reconcile to `total`
        (the undimensioned investments-at-fair-value); otherwise we discard and leave §6 for
        the LLM/HTML fallback rather than store double-counted values."""
        if FV_HIERARCHY_AXIS not in self.df.columns:
            return {}
        dim_cols = self._dim_cols
        for concept in FV_CONCEPTS:
            rows = self.df[
                (self.df["concept"] == concept)
                & (self.df["period_type"] == "instant")
                & self.df[FV_HIERARCHY_AXIS].notna()
                & self.df["numeric_value"].notna()
            ].copy()
            if rows.empty:
                continue
            rows["_inst"] = rows["period_instant"].map(self._iso)
            rows = rows[rows["_inst"] == self.reporting_date]
            if rows.empty:
                continue
            rows["_ndim"] = rows[dim_cols].notna().sum(axis=1)
            out: dict[str, Fact] = {}
            used_sum = False
            for member, field in FV_LEVEL_FIELDS.items():
                lr = rows[rows[FV_HIERARCHY_AXIS].astype(str).str.endswith(member)]
                if lr.empty:
                    continue
                level_total = lr[lr["_ndim"] == 1]                 # only the hierarchy axis
                if not level_total.empty:
                    val = float(level_total["numeric_value"].iloc[0])
                else:
                    breakdown = lr[lr["_ndim"] == 2]                # hierarchy + one other axis
                    if breakdown.empty:
                        continue
                    val = float(breakdown["numeric_value"].sum())
                    used_sum = True
                out[field] = Fact(value=val, source=Source.XBRL, confidence=0.95,
                                  raw_text=f"{concept.split(':')[-1]} {member}")
            if not out:
                continue
            # NAV-practical-expedient (money-market / alternative investments measured at NAV)
            # sits OUTSIDE the L1/L2/L3 hierarchy and is often tagged under a DIFFERENT concept
            # (us-gaap:AlternativeInvestment) or a custom '...MeasuredAtNetAssetValue' line. If our
            # buckets undershoot the total, plug that gap -- but ONLY keep it if the result then
            # reconciles, so a filing that already balances can never be broken.
            if total is not None and "fv_nav_practical_expedient" not in out:
                s = sum(f.value for f in out.values())
                tol = max(abs(total) * 0.001, 1000.0)   # match the C4 validation tolerance
                if s < total - tol:
                    navpe = self._nav_practical_expedient()
                    if navpe.value is not None and abs(s + navpe.value - total) <= tol:
                        out["fv_nav_practical_expedient"] = navpe
            # Self-check the asset-type-sum fallback: if levels don't reconcile to the known
            # total, the breakdown was cross-tabbed (double-counted) -> discard it.
            if used_sum and total is not None:
                s = sum(f.value for f in out.values())
                if abs(s - total) > max(abs(total) * 0.005, 1000.0):
                    return {}
            return out
        return {}

    def holdings(self, reconcile_to: float | None = None) -> list[dict]:
        """Schedule-of-investments rows for the CURRENT period (§9). One dict per holding
        (investment-identifier member) with the robustly-tagged us-gaap fields. The SOI in a
        10-K/10-Q carries BOTH current and prior year, so we filter to facts dated at the
        reporting date (else we'd double-count / mix years). Additive fields are summed across
        a member's same-date facts; rate-like fields take the first non-null. Returns [] when
        the axis isn't tagged (older / LLC filers) — those holdings are HTML/LLM territory.

        `reconcile_to` (the balance-sheet investments-at-fair-value total) enables Layer-1
        scale recovery: a few filers double-tag a handful of holdings at a 1000x-inflated scale
        (decimals=-3 phantom twins, e.g. Prospect) alongside the correct decimals=0 leaves. When
        the holdings span multiple decimals scales and the full sum is wildly off, we drop the
        minority scale-group IF that makes the remaining leaves reconcile — see _reconcile_scale."""
        if INVESTMENT_AXIS not in self.df.columns:
            return []
        held = self.df[self.df[INVESTMENT_AXIS].notna()
                       & self.df["numeric_value"].notna()].copy()
        if held.empty:
            return []
        held["_inst"] = held["period_instant"].map(self._iso)
        held = held[held["_inst"] == self.reporting_date]
        if held.empty:
            return []
        rows: dict[str, dict] = {}
        member_scale: dict[str, str] = {}  # member -> decimals of its fair_value fact (Layer 1)
        for _, r in held.iterrows():
            field = HOLDING_CONCEPTS.get(r["concept"])
            if field is None:
                continue
            member = str(r[INVESTMENT_AXIS])
            h = rows.get(member)
            if h is None:
                issuer, _, affil = member.partition(" | ")
                h = {"issuer": issuer.strip(), "affiliation": (affil.strip() or None)}
                rows[member] = h
            val = float(r["numeric_value"])
            if field in _HOLDING_ADDITIVE:
                h[field] = h.get(field, 0.0) + val
            elif field not in h:
                h[field] = val
            if field == "fair_value" and member not in member_scale:
                member_scale[member] = str(r.get("decimals"))
        return _reconcile_scale(rows, member_scale, reconcile_to)


def _reconcile_scale(rows: dict[str, dict], member_scale: dict[str, str],
                     target: float | None) -> list[dict]:
    """Layer 1: recover Prospect-style scale errors. A clean SOI reports every holding at one
    `decimals` scale; a few filers double-tag a handful of holdings at a 1000x-inflated scale
    (decimals=-3 phantom twins) so the leaf sum balloons. When (a) we have a balance-sheet
    investments total to reconcile against, (b) the full holdings sum is materially off it,
    (c) the holdings span >1 decimals scale, and (d) dropping the MINORITY scale-group (fewer
    rows) makes the remainder reconcile within HOLDINGS_SCALE_TOL — drop that group. Only fires
    when it demonstrably fixes the sum, so clean single-scale filers are untouched. Filings that
    don't fit this shape (e.g. Kennedy Lewis subtotal contamination) fall through unchanged and
    are caught by the Layer-2 gate in apply_holdings_summary."""
    members = list(rows.keys())
    if not target or target <= 0 or not members:
        return list(rows.values())

    def fv_sum(ms) -> float:
        return sum((rows[m].get("fair_value") or 0.0) for m in ms)

    total = fv_sum(members)
    if total <= 0 or abs(total / target - 1.0) <= HOLDINGS_SCALE_TOL:
        return list(rows.values())  # already reconciles (or no FV) — keep all
    scales = {member_scale.get(m) for m in members if rows[m].get("fair_value") is not None}
    if len(scales) < 2:
        return list(rows.values())  # single scale -> not a scale-dup problem
    for d in scales:
        keep = [m for m in members if member_scale.get(m) != d]
        drop = [m for m in members if member_scale.get(m) == d]
        if not keep or len(drop) >= len(keep):       # only ever drop a minority group
            continue
        if abs(fv_sum(keep) / target - 1.0) <= HOLDINGS_SCALE_TOL:
            keepset = set(keep)
            return [rows[m] for m in members if m in keepset]
    return list(rows.values())  # no clean scale fix -> leave for the Layer-2 gate


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
    # Statement of cash flows (§5b) — duration facts for the primary period.
    cash_flow = CashFlowStatement()
    for field, concepts in CASH_FLOW_CONCEPTS.items():
        setattr(cash_flow, field, facts.duration_scalar(concepts, target_months))
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
    # Capital share activity (§5 detail) — per-share-class duration facts, summed across classes.
    for field, concepts in SHARE_ACTIVITY_CONCEPTS.items():
        setattr(changes, field, facts.duration_class_scalar(concepts, target_months))
    # Net-asset balances for the C6 roll-forward (soft / flag-and-keep). Ending = current net
    # assets; beginning = equity at the instant before period_start (the prior period end).
    # Captured for the spreadsheet regardless of whether the roll-forward identity reconciles.
    changes.ending_net_assets = bs.total_net_assets
    if period_start:
        beg_date = (datetime.date.fromisoformat(period_start)
                    - datetime.timedelta(days=1)).isoformat()
        changes.beginning_net_assets = facts.instant_scalar_at(
            BALANCE_SHEET_CONCEPTS["total_net_assets"], beg_date)

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

    # Fair-value hierarchy (§6) — dimensional; lights up C4 when the levels reconcile to the
    # undimensioned investments-at-fair-value (which doubles as fv_total).
    fv = FairValueHierarchy()
    ifv = bs.investments_at_fair_value.value
    for field, fact in facts.fv_hierarchy(ifv).items():
        setattr(fv, field, fact)
    if any(getattr(fv, f).value is not None
           for f in ("fv_level_1", "fv_level_2", "fv_level_3", "fv_nav_practical_expedient")):
        fv.fv_total = bs.investments_at_fair_value

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
        fair_value=fv,
        income_statement=inc,
        statement_of_changes=changes,
        cash_flow=cash_flow,
        financial_highlights=highlights,
        distributions_leverage=dist_lev,
        fees=fees,
        portfolio_summary=portfolio,
        share_classes_nav=share_classes_nav,
        accession_no=str(filing.accession_no),
        extraction_source_file=None,
    )
    # Schedule of investments (§9): parse holding-level rows, derive the summary metrics into
    # portfolio_summary/liquidity, and carry the raw rows on the model (PrivateAttr, not
    # serialized) so the runner can write them to the separate per-filing holdings CSV.
    # Liquidity (§12): undrawn credit-facility capacity (see FactSet.undrawn_capacity). Set before
    # apply_holdings_summary (which fills liquidity.unfunded_commitments) and compute_derived
    # (which uses undrawn_debt_capacity for liquidity_coverage).
    extraction.liquidity.undrawn_debt_capacity = facts.undrawn_capacity()

    holdings = facts.holdings(reconcile_to=bs.investments_at_fair_value.value)
    extraction._holdings = holdings
    apply_holdings_summary(extraction, holdings)
    compute_derived(extraction)
    return extraction


def _ratio(num, den):
    if num is None or den in (None, 0):
        return None
    return num / den


def _plausible_rate(v) -> bool:
    """A rate/spread sanity gate for FV-weighted averages. Genuine rates are fractions
    (e.g. 0.05 = 5%). A minority of holdings carry mis-scaled / junk values (e.g. Apollo
    tags ~4% of spreads as >=1, up to 7.5) that would otherwise corrupt the weighted mean.
    We weight only plausible fractions; the raw value is still stored in the holdings CSV."""
    return v is not None and 0.0 < v < 1.0


def _is_affiliated(affil: str | None) -> bool:
    """Affiliation member labels: 'Non-Affiliated Issuer' (not affiliated) vs 'Affiliated' /
    'Controlled' / 'Non-Controlled Affiliate' (affiliated). Match 'affiliat' but exclude the
    'non-affiliat' prefix."""
    if not affil:
        return False
    a = affil.lower()
    return "affiliat" in a and "non-affiliat" not in a


def apply_holdings_summary(e: FilingExtraction, holdings: list[dict]) -> None:
    """Derive the §9 portfolio summary metrics from the holding-level rows and store them
    (source=COMPUTED). Each metric computes ONLY when its input covers enough of the portfolio,
    else it stays null — never emit a partial-coverage number (anti-fragility). Holding rows
    themselves are stored separately (per-filing CSV); this only fills the summary fields."""
    ps, liq = e.portfolio_summary, e.liquidity

    def mk(v):
        return Fact(value=v, source=Source.COMPUTED, confidence=0.95) if v is not None else Fact()

    if not holdings:
        return
    fv = [h["fair_value"] for h in holdings if h.get("fair_value") is not None]
    n_fv, total_fv = len(fv), sum(fv)
    # Layer-2 reconciliation gate: if the leaf holdings (after Layer-1 scale recovery) still
    # over-sum the balance-sheet investments total beyond HOLDINGS_RECON_GATE, the SOI is
    # structurally contaminated with subtotal/aggregation rows (e.g. Kennedy Lewis pre-2025,
    # whose by-industry/by-type/grand-total rows are tagged on the holding axis). Those
    # corrupt every derived metric (num_holdings, top-10, FV-weighted means), so we SUPPRESS
    # them — leave the §9 fields null rather than publish wrong numbers. The raw rows still go
    # to the per-filing CSV, and the spreadsheet's reconciliation column flags the mismatch.
    ifv = e.balance_sheet.investments_at_fair_value.value
    if ifv and total_fv and total_fv / ifv >= HOLDINGS_RECON_GATE:
        return
    if n_fv:
        ps.num_holdings = mk(float(n_fv))
        ps.pct_holdings_with_pik = mk(sum(1 for h in holdings if h.get("pik_rate")) / n_fv)
    if total_fv:
        ps.top_10_concentration = mk(sum(sorted(fv, reverse=True)[:10]) / total_fv)
        ps.pct_floating_rate = mk(
            sum(h["fair_value"] for h in holdings
                if h.get("fair_value") is not None and h.get("spread") is not None) / total_fv)
        # pct_affiliated needs the "Issuer | Affiliation" label convention. Some filers
        # (Apollo, First Eagle) cram the whole SOI row into the member with no separator ->
        # affiliation unknown. Only compute when affiliation parses for most of the portfolio;
        # else leave null rather than report a misleading 0%.
        aff_known_fv = sum(h["fair_value"] for h in holdings
                           if h.get("fair_value") is not None and h.get("affiliation"))
        if aff_known_fv / total_fv >= 0.5:
            ps.pct_affiliated = mk(
                sum(h["fair_value"] for h in holdings
                    if h.get("fair_value") is not None and _is_affiliated(h.get("affiliation"))) / total_fv)
        # FV-weighted spread (spread is tagged across filers; gate out mis-scaled outliers).
        sp = [(h["fair_value"], h["spread"]) for h in holdings
              if h.get("fair_value") is not None and _plausible_rate(h.get("spread"))]
        sp_fv = sum(f for f, _ in sp)
        if sp_fv:
            ps.weighted_avg_spread = mk(sum(f * s for f, s in sp) / sp_fv)
        # FV-weighted all-in yield — only when the (often-untagged) rate concept covers enough FV.
        rt = [(h["fair_value"], h["rate"]) for h in holdings
              if h.get("fair_value") is not None and _plausible_rate(h.get("rate"))]
        rt_fv = sum(f for f, _ in rt)
        if rt_fv and rt_fv / total_fv >= HOLDING_YIELD_COVERAGE_MIN:
            ps.weighted_avg_portfolio_yield = mk(sum(f * r for f, r in rt) / rt_fv)
    commits = [h["commitment"] for h in holdings if h.get("commitment") is not None]
    if commits:
        liq.unfunded_commitments = mk(sum(commits))


def compute_derived(e: FilingExtraction) -> FilingExtraction:
    """Compute the derived metrics (Data Dictionary §10) that depend only on fields we
    already extract. Each result is tagged source=COMPUTED. Missing inputs -> empty Fact.
    Metrics needing not-yet-extracted inputs (distribution coverage, lending spread,
    liquidity coverage) are left for later increments."""
    bs, inc, ps, d = e.balance_sheet, e.income_statement, e.portfolio_summary, e.derived
    liq = e.liquidity

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
    # Liquidity coverage (§10): can the fund fund its unfunded commitments from cash + undrawn
    # debt capacity? Computes only when all three inputs are present (else null, per coverage policy).
    undrawn, unfunded = liq.undrawn_debt_capacity.value, liq.unfunded_commitments.value
    if None not in (cash, undrawn, unfunded) and unfunded:
        d.liquidity_coverage = mk((cash + undrawn) / unfunded)
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
