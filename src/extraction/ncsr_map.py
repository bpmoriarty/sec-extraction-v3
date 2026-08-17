"""
ncsr_map.py — turn one model response into a validated `FilingExtraction`.

This is the seam between "what a model said" and "what the dataset records". Everything
here is deterministic Python over an already-validated pydantic object; no API call, no
network, no parsing of free text. That is deliberate — it means the risky part of the pipeline
(the model) is bounded by a schema on one side and by testable code on the other, and it
means every rule below can be exercised without spending a cent.

FOUR JOBS, IN ORDER:

  1. SCALE. `amounts_scale` / `shares_scale` multiply the as-printed figures into actual
     dollars and actual shares, matching the rest of the dataset (models.py: "All
     monetary values are stored in ACTUAL DOLLARS"). The model never multiplies.
  2. NEST + WRAP. Flat fields go to their section and become `Fact(source=LLM)`.
  3. CROSS-CHECK. Identity is compared against two independent sources — the filename
     (which we trust) and the filer's own inline XBRL (which we also trust, and which
     the model never saw). Disagreement lowers confidence and raises a review flag.
  4. SCORE. `assign_confidence` turns those checks into a per-field number.

RATIO CONVENTION — A STATED ASSUMPTION, NOT A DISCOVERED FACT. The prompt asks the
model for percent numbers ("1.85" for 1.85%) because that is how filings print them and
asking for a conversion invites a silent 100x error. The DATASET, however, follows the
BDC/XBRL side, where `us-gaap:InvestmentCompanyExpenseRatio` is tagged as a decimal
fraction (0.0185) and `build_spreadsheet.DEC_FMT` renders it as-tagged. So the mapper
divides by 100 on the way in, and N-CSR rows land in the same column shape as BDC rows.
If the M4 gold sample shows the BDC column is NOT consistently a fraction, this single
constant is the only thing that has to change — see `_PERCENT_TO_FRACTION`.

WHAT THIS MODULE DELIBERATELY DOES NOT DO: it does not compute derived metrics and does
not run the validation rules. Those are `bdc_xbrl.compute_derived` and
`validation.rules.validate`, unchanged and shared with the BDC path — the whole point of
the flat-intermediate design is that the N-CSR path rejoins the existing spine here
rather than growing a parallel one.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _p in ("schema", "extraction", "validation"):
    sys.path.insert(0, str(PROJECT_ROOT / "src" / _p))

from models import (  # noqa: E402
    Fact,
    FilingExtraction,
    ShareClassNAV,
    Source,
)
from ncsr_anchors import AnchorSet  # noqa: E402
from ncsr_raw import NCSRRawExtraction  # noqa: E402

# ── Confidence levels ─────────────────────────────────────────────────────────────
# Deliberately few and coarse. A finer scale would imply a calibration we have not
# measured; M4's gold sample is what turns these into evidence-backed numbers.
CONF_ANCHORED = 0.97   # an independent XBRL fact from the filer agrees
CONF_BASE = 0.80       # read by the model, nothing contradicts it
CONF_ORPHANED = 0.60   # value came from a statement the model said was not present
CONF_CONTESTED = 0.40  # an independent XBRL fact DISAGREES — keep it, but flag it

# Relative tolerance when comparing a model value against an XBRL anchor. Loose enough
# to absorb rounding in a printed table, tight enough that a wrong column or a missing
# scale factor cannot slip through.
ANCHOR_REL_TOL = 0.01

_PERCENT_TO_FRACTION = 100.0  # see the ratio-convention note in the module docstring

_SCALE_FACTORS = {"units": 1.0, "thousands": 1_000.0, "millions": 1_000_000.0}


# ── The field map: raw field -> (section, target field, kind) ─────────────────────
# `kind` decides the arithmetic:
#   dollar     -> multiply by amounts_scale
#   share      -> multiply by shares_scale
#   percent    -> divide by 100 (see ratio convention above)
#   per_share  -> stored exactly as printed; per-share figures are never scaled
_FIELD_MAP: dict[str, tuple[str, str, str]] = {
    # Statement of Assets and Liabilities
    "total_assets": ("balance_sheet", "total_assets", "dollar"),
    "total_liabilities": ("balance_sheet", "total_liabilities", "dollar"),
    "total_net_assets": ("balance_sheet", "total_net_assets", "dollar"),
    "liabilities_and_equity": ("balance_sheet", "liabilities_and_equity", "dollar"),
    "investments_at_fair_value": ("balance_sheet", "investments_at_fair_value", "dollar"),
    "cash_and_equivalents": ("balance_sheet", "cash_and_equivalents", "dollar"),
    "total_debt": ("balance_sheet", "total_debt", "dollar"),
    "interest_receivable": ("balance_sheet", "interest_receivable", "dollar"),
    "receivable_for_investments": ("balance_sheet", "receivable_for_investments", "dollar"),
    "other_assets": ("balance_sheet", "other_assets", "dollar"),
    "payable_for_investments": ("balance_sheet", "payable_for_investments", "dollar"),
    "interest_payable": ("balance_sheet", "interest_payable", "dollar"),
    "management_fee_payable": ("balance_sheet", "management_fee_payable", "dollar"),
    "distribution_payable": ("balance_sheet", "distribution_payable", "dollar"),
    "additional_paid_in_capital": ("balance_sheet", "additional_paid_in_capital", "dollar"),
    "accumulated_deficit": ("balance_sheet", "accumulated_deficit", "dollar"),
    # investments_at_cost lives in §9 in the schema, not on the balance sheet
    "investments_at_cost": ("portfolio_summary", "investments_at_cost", "dollar"),
    # Statement of Operations
    "interest_income": ("income_statement", "interest_income", "dollar"),
    "pik_interest_income": ("income_statement", "pik_interest_income", "dollar"),
    "dividend_income": ("income_statement", "dividend_income", "dollar"),
    "other_investment_income": ("income_statement", "other_investment_income", "dollar"),
    "total_investment_income": ("income_statement", "total_investment_income", "dollar"),
    "total_expenses": ("income_statement", "total_expenses", "dollar"),
    "income_tax_expense": ("income_statement", "income_tax_expense", "dollar"),
    "net_investment_income": ("income_statement", "net_investment_income", "dollar"),
    "net_realized_gain_loss": ("income_statement", "net_realized_gain_loss", "dollar"),
    "net_change_unrealized": ("income_statement", "net_change_unrealized", "dollar"),
    "net_increase_in_net_assets_ops": (
        "income_statement", "net_increase_in_net_assets_ops", "dollar"),
    # Expense detail (§11)
    "management_fee": ("fees", "management_fee", "dollar"),
    "incentive_fee": ("fees", "incentive_fee", "dollar"),
    "expense_support_net": ("fees", "expense_support_net", "dollar"),
    "interest_expense": ("fees", "interest_expense", "dollar"),
    "administrative_fees": ("fees", "administrative_fees", "dollar"),
    "professional_fees": ("fees", "professional_fees", "dollar"),
    "other_g_and_a": ("fees", "other_g_and_a", "dollar"),
    "director_trustee_fees": ("fees", "director_trustee_fees", "dollar"),
    "amortization_of_financing_costs": (
        "fees", "amortization_of_financing_costs", "dollar"),
    # Statement of Changes in Net Assets
    "beginning_net_assets": ("statement_of_changes", "beginning_net_assets", "dollar"),
    "ending_net_assets": ("statement_of_changes", "ending_net_assets", "dollar"),
    "capital_raised": ("statement_of_changes", "capital_raised", "dollar"),
    "proceeds_new_issues": ("statement_of_changes", "proceeds_new_issues", "dollar"),
    "value_drip": ("statement_of_changes", "value_drip", "dollar"),
    "repurchases": ("statement_of_changes", "repurchases", "dollar"),
    "distributions_declared": ("statement_of_changes", "distributions_declared", "dollar"),
    "shares_issued_new": ("statement_of_changes", "shares_issued_new", "share"),
    "shares_issued_drip": ("statement_of_changes", "shares_issued_drip", "share"),
    "shares_repurchased": ("statement_of_changes", "shares_repurchased", "share"),
    # Statement of Cash Flows
    "net_cash_operating": ("cash_flow", "net_cash_operating", "dollar"),
    "net_cash_investing": ("cash_flow", "net_cash_investing", "dollar"),
    "net_cash_financing": ("cash_flow", "net_cash_financing", "dollar"),
    "effect_of_fx": ("cash_flow", "effect_of_fx", "dollar"),
    "net_change_in_cash": ("cash_flow", "net_change_in_cash", "dollar"),
    "interest_paid": ("cash_flow", "interest_paid", "dollar"),
    "investment_purchases": ("cash_flow", "investment_purchases", "dollar"),
    "investment_sales": ("cash_flow", "investment_sales", "dollar"),
    # Financial Highlights (fund grain)
    "portfolio_turnover": ("financial_highlights", "portfolio_turnover", "percent"),
}

# Which statement each raw field is read from. Used for the "orphaned value" check:
# a number attributed to a statement the model itself said was absent is suspicious,
# because it usually means the value was taken from a NOTE or from the prior period.
_FIELD_STATEMENT: dict[str, str] = {}
for _f, (_sec, _t, _k) in _FIELD_MAP.items():
    _FIELD_STATEMENT[_f] = {
        "balance_sheet": "assets_liabilities",
        "portfolio_summary": "assets_liabilities",
        "income_statement": "operations",
        "fees": "operations",
        "statement_of_changes": "changes_in_net_assets",
        "cash_flow": "cash_flows",
        "financial_highlights": "financial_highlights",
    }[_sec]


@dataclass(frozen=True)
class FilingMeta:
    """What we know about a filing WITHOUT asking a model — filename plus universe.

    Kept separate from the model's output on purpose: these are the values the identity
    cross-check compares against, so they must not be contaminated by anything the model
    said. (The prompt likewise never shows the model any of this.)
    """

    cik: str
    fund_name: str
    form_type: str
    filing_date: str | None = None
    vehicle_type: str | None = None
    source_file: str | None = None
    accession_no: str | None = None


# ── Date handling ─────────────────────────────────────────────────────────────────
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def parse_printed_date(text: str | None) -> date | None:
    """Parse the handful of date shapes filers actually print. None if unrecognised.

    Returning None rather than guessing matters: `reporting_date` is the dataset's time
    key, and a mis-parsed key silently files a fund's year under the wrong year.
    """
    if not text:
        return None
    s = " ".join(text.replace(",", " ").replace(" ", " ").split()).strip().lower()
    m = re.match(r"^([a-z]+)\.?\s+(\d{1,2})\s+(\d{4})$", s)          # September 30 2025
    if m and m.group(1)[:3] in {k[:3] for k in _MONTHS}:
        month = next(v for k, v in _MONTHS.items() if k.startswith(m.group(1)[:3]))
        try:
            return date(int(m.group(3)), month, int(m.group(2)))
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)           # 9/30/2025
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)                 # 2025-09-30
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _period_start(end: date | None, months: int | None) -> date | None:
    """Back out the period start from its end and length.

    Fund fiscal periods end on a month end, so a 12-month period ending 2025-09-30 runs
    from 2024-10-01 and a 6-month period ending 2025-04-30 runs from 2024-11-01. The
    rule is therefore "go back months-1 months, take the 1st" — NOT "subtract months and
    keep the day", which lands a day or a month early depending on month lengths.
    """
    if end is None or not months:
        return None
    y, m = end.year, end.month - (months - 1)
    while m <= 0:
        m += 12
        y -= 1
    try:
        return date(y, m, 1)
    except ValueError:  # pragma: no cover - m is normalised into 1..12 above
        return None


def _close(a: float, b: float, rel: float = ANCHOR_REL_TOL) -> bool:
    """Relative comparison that behaves near zero."""
    scale = max(abs(a), abs(b))
    if scale == 0:
        return True
    return abs(a - b) / scale <= rel


def _norm_name(s: str | None) -> str:
    """Loose fund-name key: lowercase alphanumerics only.

    Filenames, XBRL registrant names and cover pages disagree on punctuation, 'The',
    'Fund' vs 'Fund, Inc.' and so on. Comparing raw strings would flag most of the
    corpus, so this compares only the letters and digits and the caller treats a
    substring match in either direction as agreement.
    """
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ── Identity cross-check ──────────────────────────────────────────────────────────

@dataclass
class IdentityCheck:
    """The result of comparing the model's echoed identity against what we know."""

    reporting_date: date | None
    period_source: str            # "xbrl" | "model" | "none"
    date_conflict: bool
    name_conflict: bool
    cik_conflict: bool
    flags: list[str]

    @property
    def trustworthy(self) -> bool:
        return not (self.date_conflict or self.name_conflict or self.cik_conflict)


def check_identity(
    raw: NCSRRawExtraction, meta: FilingMeta, anchors: AnchorSet | None
) -> IdentityCheck:
    """Compare the model's echoed identity against the filename and the filer's XBRL.

    This is the single most valuable check in the module, because the likeliest failure
    of the whole pipeline is not a garbled number — it is a perfectly-formed extraction
    of the WRONG PERIOD's column. That failure is invisible in the numbers themselves
    and obvious the moment two independent sources of the period end are compared.
    """
    flags: list[str] = []
    model_date = parse_printed_date(raw.period_end_as_printed)
    xbrl_date = parse_printed_date(anchors.document_period_end) if anchors else None

    date_conflict = bool(model_date and xbrl_date and model_date != xbrl_date)
    if date_conflict:
        flags.append(
            f"llm:period_mismatch(model={model_date}, xbrl={xbrl_date})"
        )
    # The filer's own dei tag wins when both exist — it is the filer's assertion about
    # its own document, and it is the thing the model could not have seen.
    reporting_date = xbrl_date or model_date
    period_source = "xbrl" if xbrl_date else ("model" if model_date else "none")
    if reporting_date is None:
        flags.append("llm:no_period_end")
    if raw.period_end_as_printed and model_date is None:
        flags.append(f"llm:unparsed_period({raw.period_end_as_printed[:40]!r})")

    name_conflict = False
    if anchors and anchors.registrant_name and raw.fund_name_as_printed:
        a, b = _norm_name(anchors.registrant_name), _norm_name(raw.fund_name_as_printed)
        # A trust files one document covering several series, so the model's series name
        # is often a SUBSET of the registrant name (or vice versa). Only flag when
        # neither contains the other.
        if a and b and a not in b and b not in a:
            name_conflict = True
            flags.append(
                f"llm:fund_name_mismatch(xbrl={anchors.registrant_name[:40]!r}, "
                f"model={raw.fund_name_as_printed[:40]!r})"
            )

    cik_conflict = False
    if anchors and anchors.cik and meta.cik and anchors.cik.lstrip("0") != meta.cik.lstrip("0"):
        cik_conflict = True
        flags.append(f"llm:cik_mismatch(xbrl={anchors.cik}, filename={meta.cik})")

    return IdentityCheck(
        reporting_date=reporting_date,
        period_source=period_source,
        date_conflict=date_conflict,
        name_conflict=name_conflict,
        cik_conflict=cik_conflict,
        flags=flags,
    )


# ── Confidence ────────────────────────────────────────────────────────────────────

def assign_confidence(
    raw_field: str,
    raw: NCSRRawExtraction,
    identity: IdentityCheck,
    anchor_verdict: str | None = None,
) -> float:
    """Confidence for one extracted value.

    Four inputs, in decreasing order of authority:
      * an XBRL anchor agreed  -> CONF_ANCHORED
      * an XBRL anchor disagreed -> CONF_CONTESTED (the value is KEPT and flagged; the
        flag-and-keep policy applies here exactly as it does elsewhere in this project)
      * the value's own statement was reported absent -> CONF_ORPHANED
      * identity is contested -> everything from this filing drops to CONF_CONTESTED,
        because if we are unsure WHICH period this is, no individual figure is safe
    """
    if anchor_verdict == "disagree":
        return CONF_CONTESTED
    if not identity.trustworthy:
        return CONF_CONTESTED
    if anchor_verdict == "agree":
        return CONF_ANCHORED
    stmt = _FIELD_STATEMENT.get(raw_field)
    if stmt and raw.statements_present and stmt not in raw.statements_present:
        return CONF_ORPHANED
    return CONF_BASE


# ── The mapper ────────────────────────────────────────────────────────────────────

def map_raw_to_extraction(
    raw: NCSRRawExtraction,
    meta: FilingMeta,
    anchors: AnchorSet | None = None,
    *,
    extra_flags: list[str] | None = None,
) -> FilingExtraction:
    """Flat model output -> the nested, Fact-wrapped `FilingExtraction` the spine wants.

    The caller is expected to run `validate()` and `compute_derived()` afterwards; this
    function deliberately does neither, so that the N-CSR path uses the same, unchanged
    validation and derivation code as the BDC path.
    """
    identity = check_identity(raw, meta, anchors)
    flags: list[str] = list(extra_flags or []) + identity.flags

    dollar_mult = _SCALE_FACTORS[raw.amounts_scale]
    share_mult = _SCALE_FACTORS[raw.shares_scale]

    e = FilingExtraction(
        cik=meta.cik,
        fund_name=raw.fund_name_as_printed or meta.fund_name,
        form_type=meta.form_type,
        # A filing with no determinable period end still has to become a row we can see
        # and fix; falling back to the filing date keeps it in the review queue instead
        # of throwing it away, and `llm:no_period_end` says exactly what happened.
        reporting_date=identity.reporting_date or parse_printed_date(meta.filing_date)
        or date(1900, 1, 1),
        period_months=raw.period_months,
        filing_date=parse_printed_date(meta.filing_date),
        fiscal_period="FY" if meta.form_type == "N-CSR" else "H1",
        vehicle_type=meta.vehicle_type,
        extraction_source_file=meta.source_file,
        accession_no=meta.accession_no,
    )
    e.period_start = _period_start(identity.reporting_date, raw.period_months)
    if identity.reporting_date is None:
        flags.append("llm:reporting_date_fallback_to_filing_date")

    # Anchor lookups, always scoped to the resolved period (see ncsr_anchors: a filing
    # tags several years at once and an unscoped lookup compares the wrong one).
    nav_anchors: dict[str, float] = {}
    debt_anchor: float | None = None
    if anchors:
        nav_anchors = anchors.nav_by_class(identity.reporting_date)
        debt_facts = anchors.numeric("cef:LongTermDebtPrincipal", identity.reporting_date)
        if not debt_facts:  # some filers tag it against the duration, not the instant
            debt_facts = anchors.numeric("cef:LongTermDebtPrincipal")
        debt_anchor = debt_facts[0].value if debt_facts else None

    # ── Scalar fields ─────────────────────────────────────────────────────────────
    for raw_field, (section, target, kind) in _FIELD_MAP.items():
        value = getattr(raw, raw_field)
        if value is None:
            continue
        if kind == "dollar":
            value = value * dollar_mult
        elif kind == "share":
            value = value * share_mult
        elif kind == "percent":
            value = value / _PERCENT_TO_FRACTION

        verdict: str | None = None
        if raw_field == "total_debt" and debt_anchor is not None:
            verdict = "agree" if _close(value, debt_anchor) else "disagree"
            if verdict == "disagree":
                flags.append(
                    f"llm:anchor_conflict(total_debt model={value:,.0f} "
                    f"xbrl={debt_anchor:,.0f})"
                )

        conf = assign_confidence(raw_field, raw, identity, verdict)
        setattr(
            getattr(e, section),
            target,
            Fact(value=float(value), source=Source.LLM, confidence=conf),
        )

    # ── Share classes ─────────────────────────────────────────────────────────────
    for rc in raw.share_classes:
        label = (rc.class_label or "single").strip() or "single"
        anchor_nav = nav_anchors.get(label)
        if anchor_nav is None and len(nav_anchors) == 1 and len(raw.share_classes) == 1:
            # One class on the page and one NAV in the XBRL: they are the same class
            # even if the labels are spelled differently.
            anchor_nav = next(iter(nav_anchors.values()))

        verdict = None
        if anchor_nav is not None and rc.nav_per_share is not None:
            verdict = "agree" if _close(rc.nav_per_share, anchor_nav) else "disagree"
            if verdict == "disagree":
                flags.append(
                    f"llm:anchor_conflict(nav[{label}] model={rc.nav_per_share} "
                    f"xbrl={anchor_nav})"
                )
        conf = assign_confidence("nav_per_share", raw, identity, verdict)

        def mk(v: float | None, mult: float = 1.0, c: float = conf) -> Fact:
            return Fact(value=v * mult, source=Source.LLM, confidence=c) if v is not None else Fact()

        e.share_classes_nav.append(
            ShareClassNAV(
                class_label=label,
                class_net_assets=mk(rc.net_assets, dollar_mult),
                class_shares_outstanding=mk(rc.shares_outstanding, share_mult),
                class_nav_per_share=mk(rc.nav_per_share),
            )
        )
    e.share_classes = [sc.class_label for sc in e.share_classes_nav]

    # ── Fund-grain Financial Highlights from the per-class ratios ─────────────────
    # The schema models highlights at fund-period while filings print them per class.
    # With one class there is no ambiguity. With several, taking any one of them would
    # silently label one class's expense ratio as the fund's — so we leave the section
    # empty and say so. M2 (the multi-series slicer) is where per-class highlights get
    # a home of their own.
    hl_conf = assign_confidence("portfolio_turnover", raw, identity)
    if len(raw.share_classes) == 1:
        rc = raw.share_classes[0]
        for src, dst in (
            ("expense_ratio", "expense_ratio"),
            ("gross_expense_ratio", "gross_expense_ratio"),
            ("net_investment_income_ratio", "net_investment_income_ratio"),
            ("total_return", "total_return"),
        ):
            v = getattr(rc, src)
            if v is not None:
                setattr(e.financial_highlights, dst,
                        Fact(value=v / _PERCENT_TO_FRACTION, source=Source.LLM,
                             confidence=hl_conf))
        if rc.distributions_per_share is not None:
            e.distributions_leverage.distributions_per_share = Fact(
                value=rc.distributions_per_share, source=Source.LLM, confidence=hl_conf)
    elif len(raw.share_classes) > 1:
        flags.append(
            f"llm:multi_class_highlights_not_aggregated(n={len(raw.share_classes)})")
    else:
        flags.append("llm:no_share_classes")

    # ── Diagnostics that reviewers care about ─────────────────────────────────────
    if raw.extraction_notes:
        flags.append(f"llm:note({raw.extraction_notes[:180]})")
    if raw.amounts_scale != "units":
        flags.append(f"llm:scale({raw.amounts_scale})")
    if anchors is None or not anchors:
        flags.append("llm:no_xbrl_anchors")
    else:
        flags.append(f"llm:anchored(period_source={identity.period_source})")
    for expected in ("assets_liabilities", "operations", "changes_in_net_assets"):
        if raw.statements_present and expected not in raw.statements_present:
            flags.append(f"llm:statement_absent({expected})")

    e.review_flags = flags
    return e


if __name__ == "__main__":
    # Self-test: no API key, no network. Exercises the three behaviours most likely to
    # break silently — scaling, the anchor cross-check, and identity conflict handling.
    meta = FilingMeta(cik="0001467631", fund_name="ACAP Strategic Fund",
                      form_type="N-CSR", filing_date="2025-12-01",
                      vehicle_type="Tender Offer Fund")

    raw = NCSRRawExtraction.empty()
    raw.fund_name_as_printed = "ACAP Strategic Fund"
    raw.period_end_as_printed = "September 30, 2025"
    raw.period_months = 12
    raw.amounts_scale = "thousands"          # figures printed in thousands …
    raw.shares_scale = "units"               # … while shares are printed whole
    raw.statements_present = ["assets_liabilities", "operations", "changes_in_net_assets"]
    raw.total_assets = 1_234.5               # i.e. $1,234,500
    raw.shares_repurchased = 10_000.0        # i.e. 10,000 shares, NOT 10,000,000
    raw.portfolio_turnover = 42.0            # 42% -> 0.42

    from ncsr_raw import RawShareClass
    raw.share_classes = [RawShareClass(
        class_label="Class A", net_assets=800.0, shares_outstanding=27_000.0,
        nav_per_share=29.22, distributions_per_share=1.10, expense_ratio=2.35,
        gross_expense_ratio=2.60, net_investment_income_ratio=0.85, total_return=11.4)]

    anchors = None
    acap = PROJECT_ROOT.parent / "filings" / "ACAP_Strategic_Fund_0001467631_N-CSR_2025-12-01.htm"
    if acap.exists():
        from ncsr_anchors import anchors_for_file
        anchors = anchors_for_file(acap)

    e = map_raw_to_extraction(raw, meta, anchors)
    print("--- scaling ---")
    print(f"  total_assets      {e.balance_sheet.total_assets.value:,.0f}  (expect 1,234,500)")
    print(f"  shares_repurchased {e.statement_of_changes.shares_repurchased.value:,.0f}"
          f"  (expect 10,000)")
    print(f"  portfolio_turnover {e.financial_highlights.portfolio_turnover.value}"
          f"  (expect 0.42)")
    print(f"  expense_ratio      {e.financial_highlights.expense_ratio.value}  (expect 0.0235)")
    print("--- identity ---")
    print(f"  reporting_date {e.reporting_date}   period_start {e.period_start}")
    print(f"  NAV conf       {e.share_classes_nav[0].class_nav_per_share.confidence}"
          f"  (expect {CONF_ANCHORED} when the ACAP filing is present)")
    print("--- flags ---")
    for f in e.review_flags:
        print(f"  {f}")

    print("\n--- negative control: model read the PRIOR year ---")
    raw2 = raw.model_copy(deep=True)
    raw2.period_end_as_printed = "September 30, 2024"
    e2 = map_raw_to_extraction(raw2, meta, anchors)
    print(f"  reporting_date {e2.reporting_date}  "
          f"total_assets confidence {e2.balance_sheet.total_assets.confidence}")
    for f in e2.review_flags:
        if "mismatch" in f or "conflict" in f:
            print(f"  {f}")
