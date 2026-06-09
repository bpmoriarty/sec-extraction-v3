"""
models.py — The typed schema for one extracted filing.

This is the code version of docs/DATA_DICTIONARY.md. Every field here corresponds to a
field in that document, and the two must stay in sync. The schema does three jobs:
  1. Defines exactly what we extract (the shape of the data).
  2. Validates that extracted data is well-formed (pydantic enforces types/ranges).
  3. Generates the spreadsheet columns downstream.

KEY IDEAS (read these first if you're new to the project):
  - Every *number* we pull is wrapped in a `Fact`, which records not just the value but
    WHERE it came from (XBRL / LLM / computed) and HOW confident we are. That provenance
    is what lets us trust and audit the dataset later.
  - All monetary values are stored in ACTUAL DOLLARS and shares in ACTUAL SHARES. XBRL
    often reports "in thousands"; we normalize on the way in, so nothing here is scaled.
  - There are two "grains" (levels of detail):
      * fund-period   -> one set of values per filing (balance sheet, income, etc.)
      * fund-period-class -> one set per share class (NAV, shares) -> see ShareClassNAV
  - A filing models ONE reporting period (the current period). Prior-period values come
    from that fund's prior filing, so temporal checks happen across FilingExtraction rows.

pydantic v2. No new dependency — pydantic ships with edgartools.
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field, PrivateAttr


# ── Provenance ──────────────────────────────────────────────────────────────────

class Source(str, Enum):
    """Where a value came from. Drives both trust and debugging."""
    XBRL = "xbrl"          # pulled straight from structured XBRL tags (most reliable)
    LLM = "llm"            # read from the document by Claude (fallback for gaps)
    COMPUTED = "computed"  # calculated by us from other fields (derived metrics)


class Fact(BaseModel):
    """
    A single extracted numeric value plus its provenance and confidence.

    `value` is None when the field wasn't found — that's normal; not every field
    appears in every filing. Counts/shares are stored as float for simplicity.
    """
    value: float | None = None
    source: Source | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    raw_text: str | None = None   # the original snippet, for auditing


class ValidationCheck(BaseModel):
    """Result of one validation rule (see src/validation/rules.py)."""
    rule: str                       # e.g. "C1", "I1"
    name: str
    tier: str                       # "identity" | "reasonableness" | "temporal"
    status: str                     # "pass" | "fail" | "skipped"
    message: str | None = None


class Composition(BaseModel):
    """
    A breakdown map, e.g. {industry_name: fair_value}. Provenance applies to the
    whole map rather than each entry.
    """
    items: dict[str, float] = Field(default_factory=dict)
    source: Source | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


# ── Statement sections (grain: fund-period) ──────────────────────────────────────

class BalanceSheet(BaseModel):                       # Data Dictionary §2
    total_assets: Fact = Field(default_factory=Fact)
    total_liabilities: Fact = Field(default_factory=Fact)
    total_net_assets: Fact = Field(default_factory=Fact)
    investments_at_fair_value: Fact = Field(default_factory=Fact)
    cash_and_equivalents: Fact = Field(default_factory=Fact)
    total_debt: Fact = Field(default_factory=Fact)
    # Balance-sheet detail (instant). Receivables/payables and equity components, plus the
    # tagged "total liabilities and net assets" line used as a free C1b cross-check vs total_assets.
    interest_receivable: Fact = Field(default_factory=Fact)
    receivable_for_investments: Fact = Field(default_factory=Fact)     # unsettled trades (sold)
    other_assets: Fact = Field(default_factory=Fact)
    payable_for_investments: Fact = Field(default_factory=Fact)        # unsettled trades (purchased)
    interest_payable: Fact = Field(default_factory=Fact)
    management_fee_payable: Fact = Field(default_factory=Fact)
    distribution_payable: Fact = Field(default_factory=Fact)
    additional_paid_in_capital: Fact = Field(default_factory=Fact)
    accumulated_deficit: Fact = Field(default_factory=Fact)            # accumulated distributed earnings (losses)
    liabilities_and_equity: Fact = Field(default_factory=Fact)         # = total_assets (C1b cross-check)


class IncomeStatement(BaseModel):                    # Data Dictionary §4
    # Investment-income components. PIK (payment-in-kind) interest is accrued but not
    # paid in cash — an important credit-stress signal — so we break it out explicitly.
    # C7 check: these components should sum to total_investment_income (flag-and-keep:
    # a shortfall usually means a filer broke out an income line we didn't capture).
    interest_income: Fact = Field(default_factory=Fact)
    pik_interest_income: Fact = Field(default_factory=Fact)
    # PIK can also arrive as dividends (or a single combined interest+dividend PIK line).
    # BDC PIK tagging is inconsistent — some filers fold PIK into the interest line,
    # others break it out across these overlapping concepts — so C7 reads all of them to
    # bound the filer's total PIK rather than demand the components sum exactly.
    pik_dividend_income: Fact = Field(default_factory=Fact)
    pik_income_combined: Fact = Field(default_factory=Fact)
    dividend_income: Fact = Field(default_factory=Fact)
    other_investment_income: Fact = Field(default_factory=Fact)
    total_investment_income: Fact = Field(default_factory=Fact)
    total_expenses: Fact = Field(default_factory=Fact)
    # Income tax / excise tax sits between income-before-tax and NII:
    # NII = total_investment_income - total_expenses - income_tax_expense. RIC-compliant
    # BDCs are ~tax-free (this is None/0), but funds paying excise tax (e.g. AB Private
    # Lending) need it for the C5 identity to reconcile. See validation rule C5.
    income_tax_expense: Fact = Field(default_factory=Fact)
    net_investment_income: Fact = Field(default_factory=Fact)
    # Authoritative income-statement SUBTOTALS tagged directly by many filers — used as
    # C5 cross-check anchors so we can verify NII without reconstructing it from the
    # (filer-specific) expense/tax components. income_before_tax = NII + tax;
    # nii_after_expense_and_tax should equal net_investment_income exactly.
    income_before_tax: Fact = Field(default_factory=Fact)
    nii_after_expense_and_tax: Fact = Field(default_factory=Fact)
    net_realized_gain_loss: Fact = Field(default_factory=Fact)
    net_change_unrealized: Fact = Field(default_factory=Fact)
    net_increase_in_net_assets_ops: Fact = Field(default_factory=Fact)


class StatementOfChanges(BaseModel):                 # Data Dictionary §5 (roll-forward)
    beginning_net_assets: Fact = Field(default_factory=Fact)
    capital_raised: Fact = Field(default_factory=Fact)
    repurchases: Fact = Field(default_factory=Fact)
    distributions_declared: Fact = Field(default_factory=Fact)
    ending_net_assets: Fact = Field(default_factory=Fact)
    # Capital share activity (DURATION) — summed across share classes. Detail behind
    # capital_raised / repurchases; DRIP value also feeds the C6 roll-forward retry.
    shares_issued_new: Fact = Field(default_factory=Fact)        # share count, subscriptions
    proceeds_new_issues: Fact = Field(default_factory=Fact)      # $ raised from new subscriptions
    shares_issued_drip: Fact = Field(default_factory=Fact)       # share count, dividend reinvestment
    value_drip: Fact = Field(default_factory=Fact)               # $ of reinvested distributions
    shares_repurchased: Fact = Field(default_factory=Fact)       # share count, tender repurchases


class CashFlowStatement(BaseModel):                  # Data Dictionary §5b (statement of cash flows)
    # DURATION facts for the primary period. For an investment company, buying/selling
    # investments is an OPERATING activity (there's usually no separate investing section), so
    # investing is often absent (treated as 0 in the C9 footing identity). net_change_in_cash is
    # the bottom-line change INCLUDING the FX effect; effect_of_fx is captured so C9 can foot
    # exactly: operating + investing + financing + fx = net_change_in_cash.
    net_cash_operating: Fact = Field(default_factory=Fact)
    net_cash_investing: Fact = Field(default_factory=Fact)
    net_cash_financing: Fact = Field(default_factory=Fact)
    effect_of_fx: Fact = Field(default_factory=Fact)
    net_change_in_cash: Fact = Field(default_factory=Fact)
    interest_paid: Fact = Field(default_factory=Fact)            # cash interest paid (vs accrued expense)
    investment_purchases: Fact = Field(default_factory=Fact)     # portfolio turnover signal
    investment_sales: Fact = Field(default_factory=Fact)


class FairValueHierarchy(BaseModel):                 # Data Dictionary §6 (4-bucket)
    fv_level_1: Fact = Field(default_factory=Fact)
    fv_level_2: Fact = Field(default_factory=Fact)
    fv_level_3: Fact = Field(default_factory=Fact)
    fv_nav_practical_expedient: Fact = Field(default_factory=Fact)
    fv_total: Fact = Field(default_factory=Fact)


class FinancialHighlights(BaseModel):                # Data Dictionary §7
    # Stored AS-TAGGED from XBRL (confirmed decision); recompute only as a cross-check.
    # Often disclosed per share class in BDCs; modeled at fund-period for v0.1 — a
    # per-class refinement can be added later.
    expense_ratio: Fact = Field(default_factory=Fact)            # NET (after waivers/support)
    gross_expense_ratio: Fact = Field(default_factory=Fact)      # GROSS (true go-forward cost)
    net_investment_income_ratio: Fact = Field(default_factory=Fact)
    total_return: Fact = Field(default_factory=Fact)
    portfolio_turnover: Fact = Field(default_factory=Fact)


class DistributionsLeverage(BaseModel):              # Data Dictionary §8
    distributions_per_share: Fact = Field(default_factory=Fact)
    return_of_capital_distribution: Fact = Field(default_factory=Fact)  # $ ROC (tax character)
    return_of_capital_pct: Fact = Field(default_factory=Fact)  # = ROC $ / distributions_declared
    asset_coverage_ratio: Fact = Field(default_factory=Fact)   # regulatory (I1 check)
    weighted_avg_interest_rate: Fact = Field(default_factory=Fact)  # cost of debt (net_lending_spread)


class FeesExpenseSupport(BaseModel):                 # Data Dictionary §11
    # Surfaces true cost + adviser alignment. A low NET expense ratio can be propped up
    # by temporary adviser expense support (recouped later); expense_support_net exposes it.
    management_fee: Fact = Field(default_factory=Fact)
    incentive_fee: Fact = Field(default_factory=Fact)
    expense_support_net: Fact = Field(default_factory=Fact)    # negative = net recoupment
    # Expense breakdown (DURATION) — decomposes total_expenses. interest_expense is the $ cost
    # of debt; these are GROSS lines (their sum can exceed NET total_expenses when waivers apply).
    interest_expense: Fact = Field(default_factory=Fact)
    administrative_fees: Fact = Field(default_factory=Fact)
    professional_fees: Fact = Field(default_factory=Fact)
    other_g_and_a: Fact = Field(default_factory=Fact)
    director_trustee_fees: Fact = Field(default_factory=Fact)
    amortization_of_financing_costs: Fact = Field(default_factory=Fact)


class TaxBasis(BaseModel):                           # Data Dictionary §13 (tax position)
    # Tax-basis position of the PORTFOLIO (differs from book) + accumulated distributable
    # earnings by tax character. Mostly disclosed in the 10-K (annual). INSTANT facts.
    tax_cost_of_investments: Fact = Field(default_factory=Fact)
    tax_unrealized_appreciation: Fact = Field(default_factory=Fact)   # gross built-in gain
    tax_unrealized_depreciation: Fact = Field(default_factory=Fact)   # gross built-in loss
    tax_unrealized_net: Fact = Field(default_factory=Fact)            # net (C11: ≈ apprec − deprec)
    undistributed_ordinary_income: Fact = Field(default_factory=Fact)
    undistributed_lt_capital_gains: Fact = Field(default_factory=Fact)


class LiquidityObligations(BaseModel):               # Data Dictionary §12
    # Can the fund fund its commitments, and can LPs actually exit? Repurchase fields are
    # partly XBRL / partly SC TO-I and lower-confidence; proration <100% = gated.
    unfunded_commitments: Fact = Field(default_factory=Fact)
    undrawn_debt_capacity: Fact = Field(default_factory=Fact)
    weighted_avg_debt_maturity: Fact = Field(default_factory=Fact)
    repurchase_offered: Fact = Field(default_factory=Fact)
    repurchase_requested: Fact = Field(default_factory=Fact)
    repurchase_repurchased: Fact = Field(default_factory=Fact)
    repurchase_proration_pct: Fact = Field(default_factory=Fact)


class PortfolioSummary(BaseModel):                   # Data Dictionary §9 (holdings deferred)
    num_holdings: Fact = Field(default_factory=Fact)
    composition_by_industry: Composition = Field(default_factory=Composition)
    composition_by_type: Composition = Field(default_factory=Composition)
    top_10_concentration: Fact = Field(default_factory=Fact)
    investments_at_cost: Fact = Field(default_factory=Fact)
    # Non-accruals are captured on BOTH bases: fair value AND amortized cost. The cost
    # basis is usually the higher / more conservative figure because non-accrual
    # positions get marked down in fair value.
    non_accrual_fair_value: Fact = Field(default_factory=Fact)
    non_accrual_at_cost: Fact = Field(default_factory=Fact)
    weighted_avg_portfolio_yield: Fact = Field(default_factory=Fact)   # cost basis preferred
    pct_floating_rate: Fact = Field(default_factory=Fact)
    capitalized_pik_balance: Fact = Field(default_factory=Fact)        # rising = stress
    # Holdings-derived (computed from the schedule of investments; §9 reassessment 2026-06-08).
    weighted_avg_spread: Fact = Field(default_factory=Fact)            # FV-weighted spread over base rate (robust where all-in yield isn't tagged)
    pct_holdings_with_pik: Fact = Field(default_factory=Fact)          # share of positions carrying a PIK rate (count basis)
    pct_affiliated: Fact = Field(default_factory=Fact)                 # FV-weighted share in affiliated/controlled issuers
    # Seniority mix (1st/2nd lien/sub/equity) and internal risk-rating distribution.
    # Internal rating is rarely clean XBRL -> typically an LLM-fallback field.
    composition_by_seniority: Composition = Field(default_factory=Composition)
    composition_by_internal_rating: Composition = Field(default_factory=Composition)


class DerivedMetrics(BaseModel):                     # Data Dictionary §10 (computed)
    # Formulas confirmed 2026-06-03. source should be COMPUTED for all of these.
    leverage_ratio: Fact = Field(default_factory=Fact)        # total_debt / total_net_assets
    asset_coverage_pct: Fact = Field(default_factory=Fact)    # (assets - liab_excl_debt) / debt
    net_debt: Fact = Field(default_factory=Fact)              # total_debt - cash
    pik_income_ratio: Fact = Field(default_factory=Fact)      # pik_interest_income / total_investment_income
    non_accrual_pct_fv: Fact = Field(default_factory=Fact)    # non_accrual_fair_value / investments_at_fair_value
    non_accrual_pct_cost: Fact = Field(default_factory=Fact)  # non_accrual_at_cost / investments_at_cost
    distribution_coverage_ratio: Fact = Field(default_factory=Fact)  # NII / distributions_declared
    portfolio_mark: Fact = Field(default_factory=Fact)        # investments_at_fair_value / investments_at_cost
    net_lending_spread: Fact = Field(default_factory=Fact)    # wtd_avg_portfolio_yield - wtd_avg_interest_rate
    liquidity_coverage: Fact = Field(default_factory=Fact)    # (cash + undrawn_debt_capacity) / unfunded_commitments
    # distribution_yield is per-class -> see ShareClassNAV


# ── Per-share-class section (grain: fund-period-class) ────────────────────────────

class ShareClassNAV(BaseModel):                      # Data Dictionary §3
    class_label: str                                  # e.g. "S", "D", "I", or "single"
    class_net_assets: Fact = Field(default_factory=Fact)
    class_shares_outstanding: Fact = Field(default_factory=Fact)
    class_nav_per_share: Fact = Field(default_factory=Fact)
    distribution_yield: Fact = Field(default_factory=Fact)    # derived, per-class (§10)


# ── Top-level: one extracted filing ──────────────────────────────────────────────

class FilingExtraction(BaseModel):
    """
    Everything we extract from one filing for one reporting period.
    Metadata fields are known facts (not Fact-wrapped); financial sections are.
    """
    # Identity & metadata (Data Dictionary §1) — known, so not Fact-wrapped
    cik: str                                          # 10-digit zero-padded
    fund_name: str
    form_type: str                                    # "10-K" / "10-Q" (pilot)
    # reporting_date is the period-END = the snapshot date for all point-in-time
    # ("stock") fields: balance sheet, NAV, composition, non-accruals. It is the
    # dataset's time key.
    reporting_date: date
    # period_start / period_months describe the period that FLOW fields cover
    # (income statement, distributions): for a 10-Q the primary 3-month quarter,
    # for a 10-K the 12-month year. Captured as-reported (no 10-Q YTD); Q4-standalone
    # and annualized figures are derived later in analysis, not here.
    period_start: date | None = None
    period_months: int | None = None                  # 3 (quarter) or 12 (annual)
    filing_date: date | None = None
    fiscal_period: str | None = None                  # "FY" / "Q1".."Q3"
    vehicle_type: str | None = None                   # from fund_universe.csv
    share_classes: list[str] = Field(default_factory=list)

    # Financial sections (fund-period grain)
    balance_sheet: BalanceSheet = Field(default_factory=BalanceSheet)
    income_statement: IncomeStatement = Field(default_factory=IncomeStatement)
    statement_of_changes: StatementOfChanges = Field(default_factory=StatementOfChanges)
    cash_flow: CashFlowStatement = Field(default_factory=CashFlowStatement)
    fair_value: FairValueHierarchy = Field(default_factory=FairValueHierarchy)
    financial_highlights: FinancialHighlights = Field(default_factory=FinancialHighlights)
    distributions_leverage: DistributionsLeverage = Field(default_factory=DistributionsLeverage)
    fees: FeesExpenseSupport = Field(default_factory=FeesExpenseSupport)
    tax_basis: TaxBasis = Field(default_factory=TaxBasis)
    liquidity: LiquidityObligations = Field(default_factory=LiquidityObligations)
    portfolio_summary: PortfolioSummary = Field(default_factory=PortfolioSummary)
    derived: DerivedMetrics = Field(default_factory=DerivedMetrics)

    # Per-class section (fund-period-class grain)
    share_classes_nav: list[ShareClassNAV] = Field(default_factory=list)

    # Holding-level schedule-of-investments rows (§9). Stored SEPARATELY (per-filing CSV in
    # data/holdings/), NOT serialized into this JSON — kept here only as a transient carrier
    # so the runner can write the CSV. The summary metrics derived from these live in
    # portfolio_summary / liquidity. PrivateAttr => excluded from model_dump_json.
    _holdings: list[dict] = PrivateAttr(default_factory=list)

    # Review queue: human-readable flags from reasonableness/temporal checks.
    # Per the flag-and-keep policy, values are NEVER discarded — concerns land here.
    review_flags: list[str] = Field(default_factory=list)

    # Validation results (populated by src/validation/rules.py).
    # validation_status: "pass" (no failures) | "review" (>=1 failure) | "not_run".
    validation_status: str = "not_run"
    validation_checks: list[ValidationCheck] = Field(default_factory=list)

    # Provenance for the extraction run as a whole
    extraction_source_file: str | None = None         # the .htm filename on disk
    accession_no: str | None = None                   # EDGAR accession


if __name__ == "__main__":
    # Smoke test: build a minimal instance and confirm it validates + round-trips.
    sample = FilingExtraction(
        cik="0001837532",
        fund_name="Apollo Debt Solutions BDC",
        form_type="10-Q",
        reporting_date=date(2026, 3, 31),
        share_classes=["S", "D", "I"],
    )
    sample.balance_sheet.total_assets = Fact(
        value=26_933_918_000.0, source=Source.XBRL, confidence=0.98
    )
    sample.share_classes_nav.append(
        ShareClassNAV(
            class_label="I",
            class_net_assets=Fact(value=11_542_537_000.0, source=Source.XBRL, confidence=0.98),
            class_nav_per_share=Fact(value=23.90, source=Source.XBRL, confidence=0.98),
        )
    )
    print("Schema OK. Sample JSON:")
    print(sample.model_dump_json(indent=2, exclude_none=True))
