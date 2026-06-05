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

from pydantic import BaseModel, Field


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


class IncomeStatement(BaseModel):                    # Data Dictionary §4
    total_investment_income: Fact = Field(default_factory=Fact)
    total_expenses: Fact = Field(default_factory=Fact)
    net_investment_income: Fact = Field(default_factory=Fact)
    net_realized_gain_loss: Fact = Field(default_factory=Fact)
    net_change_unrealized: Fact = Field(default_factory=Fact)
    net_increase_in_net_assets_ops: Fact = Field(default_factory=Fact)


class StatementOfChanges(BaseModel):                 # Data Dictionary §5 (roll-forward)
    beginning_net_assets: Fact = Field(default_factory=Fact)
    capital_raised: Fact = Field(default_factory=Fact)
    repurchases: Fact = Field(default_factory=Fact)
    distributions_declared: Fact = Field(default_factory=Fact)
    ending_net_assets: Fact = Field(default_factory=Fact)


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
    expense_ratio: Fact = Field(default_factory=Fact)
    net_investment_income_ratio: Fact = Field(default_factory=Fact)
    total_return: Fact = Field(default_factory=Fact)
    portfolio_turnover: Fact = Field(default_factory=Fact)


class DistributionsLeverage(BaseModel):              # Data Dictionary §8
    distributions_per_share: Fact = Field(default_factory=Fact)
    asset_coverage_ratio: Fact = Field(default_factory=Fact)   # regulatory (I1 check)
    weighted_avg_interest_rate: Fact = Field(default_factory=Fact)


class PortfolioSummary(BaseModel):                   # Data Dictionary §9 (holdings deferred)
    num_holdings: Fact = Field(default_factory=Fact)
    composition_by_industry: Composition = Field(default_factory=Composition)
    composition_by_type: Composition = Field(default_factory=Composition)
    top_10_concentration: Fact = Field(default_factory=Fact)
    non_accrual_fair_value: Fact = Field(default_factory=Fact)


class DerivedMetrics(BaseModel):                     # Data Dictionary §10 (computed)
    # Formulas confirmed 2026-06-03. source should be COMPUTED for all of these.
    leverage_ratio: Fact = Field(default_factory=Fact)        # total_debt / total_net_assets
    asset_coverage_pct: Fact = Field(default_factory=Fact)    # (assets - liab_excl_debt) / debt
    net_debt: Fact = Field(default_factory=Fact)              # total_debt - cash
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
    reporting_date: date                              # period-end — the time key
    filing_date: date | None = None
    fiscal_period: str | None = None                  # "FY" / "Q1".."Q3"
    vehicle_type: str | None = None                   # from fund_universe.csv
    share_classes: list[str] = Field(default_factory=list)

    # Financial sections (fund-period grain)
    balance_sheet: BalanceSheet = Field(default_factory=BalanceSheet)
    income_statement: IncomeStatement = Field(default_factory=IncomeStatement)
    statement_of_changes: StatementOfChanges = Field(default_factory=StatementOfChanges)
    fair_value: FairValueHierarchy = Field(default_factory=FairValueHierarchy)
    financial_highlights: FinancialHighlights = Field(default_factory=FinancialHighlights)
    distributions_leverage: DistributionsLeverage = Field(default_factory=DistributionsLeverage)
    portfolio_summary: PortfolioSummary = Field(default_factory=PortfolioSummary)
    derived: DerivedMetrics = Field(default_factory=DerivedMetrics)

    # Per-class section (fund-period-class grain)
    share_classes_nav: list[ShareClassNAV] = Field(default_factory=list)

    # Review queue: human-readable flags from reasonableness/temporal checks.
    # Per the flag-and-keep policy, values are NEVER discarded — concerns land here.
    review_flags: list[str] = Field(default_factory=list)

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
