"""
ncsr_raw.py — the FLAT intermediate the model fills in, one per statements block.

WHY A SECOND SCHEMA AT ALL? `models.FilingExtraction` (the thing the rest of the
pipeline consumes) is deeply nested and wraps every number in a `Fact` carrying
provenance and confidence. That structure is right for storage and wrong for asking
a model to fill in: nesting costs output tokens, invites the model to put a value in
the wrong sub-object, and asks it to invent provenance it has no way to know. So the
model fills in this FLAT record of plain numbers, and `ncsr_map.map_raw_to_extraction`
does the nesting, the scaling, and the Fact-wrapping in ordinary Python where it can
be tested.

THREE DESIGN RULES, each of which exists because the alternative fails quietly:

1. VALUES AS PRINTED + A SEPARATE SCALE. Fund statements are usually printed "in
   thousands". We do NOT ask the model to multiply — a model that silently drops a
   1000x is indistinguishable from a fund that is 1000x smaller, and that class of
   error already bit this project once (the Prospect holdings mis-scale). The model
   reports the number exactly as printed and tells us the scale; the mapper multiplies.
   Dollars and share counts get SEPARATE scales because filings routinely print one
   in thousands and the other in units.

2. IDENTITY IS ECHOED BACK, NOT ASSUMED. N-CSR statements print the current AND prior
   year side by side, and a multi-series trust prints several funds. The single most
   likely failure is reading the wrong COLUMN or the wrong FUND. So the model must
   echo `period_end_as_printed` and `fund_name_as_printed`; the mapper compares them
   against what we already know from the filename and the universe. A wrong column
   becomes a detectable, flagged failure instead of a silently wrong row.

3. EVERY FIELD IS REQUIRED — absence is spelled `null`. A field with a default could
   be silently omitted by the model and we would never know whether it looked and
   found nothing or never looked. Requiring an explicit `null` costs a few hundred
   output tokens per filing and buys an explicit "not present" for every line item.
   Use `NCSRRawExtraction.empty()` when you need a blank instance in a test.

STRUCTURED-OUTPUT CONSTRAINTS (why there are no ge=/le= bounds below): the Claude
structured-outputs JSON-schema subset does NOT support numeric bounds (`minimum`,
`maximum`), string-length bounds, or recursive schemas, and requires
`additionalProperties: false` on every object. The Python SDK strips unsupported
keywords for `messages.parse()`, but the BATCH path sends the raw schema we generate
here, so this model must stay inside the subset on its own. Range checks belong in
`validation/rules.py`, where they already live.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# The scale a statement is printed at. "units" means the figures are actual dollars.
# Kept as a Literal (renders as a JSON-schema `enum`, which structured outputs DOES
# support) rather than a free string, so an unexpected value fails validation loudly.
Scale = Literal["units", "thousands", "millions"]
ShareScale = Literal["units", "thousands"]


class RawShareClass(BaseModel):
    """One share class as printed in the Financial Highlights / net-asset tables.

    Interval and tender-offer funds commonly run several classes (I, A, S, D, ...)
    with their own NAV, expense ratios and total return. Funds with a single
    unnamed class are reported here as one entry labelled "single".
    """

    model_config = ConfigDict(extra="forbid")

    class_label: str = Field(
        description="Share class name exactly as printed (e.g. 'Class I', 'Class A', "
        "'Institutional'). Use 'single' if the fund has only one unnamed class."
    )
    net_assets: float | None = Field(
        description="Net assets attributable to this class, as printed. null if not shown."
    )
    shares_outstanding: float | None = Field(
        description="Shares outstanding for this class, as printed. null if not shown."
    )
    nav_per_share: float | None = Field(
        description="Net asset value per share for this class at period end, in DOLLARS "
        "per share (per-share figures are never scaled - report them exactly as printed)."
    )
    distributions_per_share: float | None = Field(
        description="Total distributions declared per share for the period, in dollars "
        "per share. null if not shown."
    )
    expense_ratio: float | None = Field(
        description="NET expense ratio for this class (after any waiver or expense "
        "support), as a PERCENT number: report 1.85 for 1.85%, not 0.0185."
    )
    gross_expense_ratio: float | None = Field(
        description="GROSS expense ratio before waivers or expense support, as a PERCENT "
        "number. null if the filing shows only one ratio."
    )
    net_investment_income_ratio: float | None = Field(
        description="Ratio of net investment income to average net assets, as a PERCENT "
        "number."
    )
    total_return: float | None = Field(
        description="Total return for the period, as a PERCENT number (e.g. -4.2 for a "
        "4.2% loss). Use the return based on NAV if both NAV and market-price returns "
        "are shown."
    )


class NCSRRawExtraction(BaseModel):
    """Everything read out of ONE financial-statements block of an N-CSR / N-CSRS."""

    model_config = ConfigDict(extra="forbid")

    # ── Identity echo-back (the wrong-column / wrong-fund tripwire) ────────────────
    fund_name_as_printed: str | None = Field(
        description="The fund or series name printed at the top of these statements, "
        "copied exactly. If the statements cover a single fund with no separate series "
        "name, give the fund name."
    )
    period_end_as_printed: str | None = Field(
        description="The period-END date of the column you extracted, copied exactly as "
        "printed (e.g. 'October 31, 2023'). CRITICAL: these statements usually show the "
        "current period NEXT TO one or more PRIOR periods. Extract only the MOST RECENT "
        "period, and report its date here so we can verify which column you used."
    )
    period_months: int | None = Field(
        description="Number of months the income-statement / cash-flow figures cover: 12 "
        "for an annual report (N-CSR), 6 for a semi-annual report (N-CSRS). Use the "
        "period the statements actually state, not the form type."
    )
    amounts_scale: Scale = Field(
        description="The scale DOLLAR amounts are printed at. Look for a heading such as "
        "'(in thousands)' or '(amounts in 000s)' above or beside the statements. If the "
        "figures are actual dollars, answer 'units'. Do NOT multiply anything yourself - "
        "report figures exactly as printed and tell us the scale here."
    )
    shares_scale: ShareScale = Field(
        description="The scale SHARE COUNTS are printed at. Filings often print dollars "
        "in thousands while printing shares in whole units - check the share columns "
        "separately from the dollar columns before answering."
    )
    statements_present: list[str] = Field(
        description="Which statements you actually found in this text. Use only these "
        "labels: 'assets_liabilities', 'operations', 'changes_in_net_assets', "
        "'cash_flows', 'financial_highlights'."
    )

    # ── Statement of Assets and Liabilities (balance sheet) ───────────────────────
    total_assets: float | None = Field(description="Total assets.")
    total_liabilities: float | None = Field(description="Total liabilities.")
    total_net_assets: float | None = Field(
        description="Total net assets (also printed as 'Net assets' or "
        "'Net assets applicable to shares outstanding')."
    )
    liabilities_and_equity: float | None = Field(
        description="The 'Total liabilities and net assets' line, if the filing prints "
        "one. This should equal total assets; we use it as a free cross-check."
    )
    investments_at_fair_value: float | None = Field(
        description="Investments at fair value (market value). If affiliated and "
        "unaffiliated investments are listed separately with no total, sum them."
    )
    investments_at_cost: float | None = Field(
        description="Amortized cost of investments. Usually printed parenthetically on "
        "the investments line, e.g. '(cost $878,282,596)'."
    )
    cash_and_equivalents: float | None = Field(
        description="Cash and cash equivalents, including cash held at broker. Exclude "
        "restricted cash if it is shown as a separate line."
    )
    total_debt: float | None = Field(
        description="Total borrowings: credit facility payable, notes payable, or loans "
        "payable, net of deferred financing costs if presented that way. null if the "
        "fund has no debt."
    )
    interest_receivable: float | None = Field(description="Interest receivable.")
    receivable_for_investments: float | None = Field(
        description="Receivable for investments sold (unsettled trades)."
    )
    other_assets: float | None = Field(
        description="Other assets / prepaid expenses, as a single line if shown."
    )
    payable_for_investments: float | None = Field(
        description="Payable for investments purchased (unsettled trades)."
    )
    interest_payable: float | None = Field(description="Interest payable / accrued interest.")
    management_fee_payable: float | None = Field(
        description="Management or advisory fee payable."
    )
    distribution_payable: float | None = Field(
        description="Distributions payable to shareholders."
    )
    additional_paid_in_capital: float | None = Field(
        description="Paid-in capital / additional paid-in capital in the net-assets "
        "composition section."
    )
    accumulated_deficit: float | None = Field(
        description="Total distributable earnings (accumulated deficit). Report the sign "
        "as printed - a loss is negative."
    )

    # ── Statement of Operations (income statement) ────────────────────────────────
    interest_income: float | None = Field(
        description="Interest income. If the filing shows a combined "
        "'Interest and dividend income' line only, put it here and leave dividend_income "
        "null."
    )
    pik_interest_income: float | None = Field(
        description="Payment-in-kind (PIK) interest income, if broken out separately."
    )
    dividend_income: float | None = Field(description="Dividend income.")
    other_investment_income: float | None = Field(
        description="Other income / fee income / miscellaneous income."
    )
    total_investment_income: float | None = Field(
        description="Total investment income (the income subtotal before expenses)."
    )
    management_fee: float | None = Field(description="Management or advisory fee expense.")
    incentive_fee: float | None = Field(
        description="Incentive fee / performance fee expense."
    )
    interest_expense: float | None = Field(
        description="Interest and debt-financing expense."
    )
    administrative_fees: float | None = Field(
        description="Administration / accounting / custodian / transfer-agent fees. If "
        "shown as several lines, sum them here."
    )
    professional_fees: float | None = Field(
        description="Professional fees (legal, audit, tax)."
    )
    director_trustee_fees: float | None = Field(description="Trustee or director fees.")
    amortization_of_financing_costs: float | None = Field(
        description="Amortization of deferred financing or offering costs."
    )
    other_g_and_a: float | None = Field(
        description="Other general and administrative expenses."
    )
    expense_support_net: float | None = Field(
        description="Net expense waiver, reimbursement or support from the adviser. "
        "Report as printed: a waiver that REDUCES expenses is normally shown negative, "
        "and a recoupment is positive."
    )
    total_expenses: float | None = Field(
        description="Total expenses. If both gross and net (after waiver) totals are "
        "shown, give the NET total here."
    )
    income_tax_expense: float | None = Field(
        description="Income tax or excise tax expense. null for most RIC-compliant funds."
    )
    net_investment_income: float | None = Field(
        description="Net investment income (loss), after expenses and taxes."
    )
    net_realized_gain_loss: float | None = Field(
        description="Net realized gain (loss) on investments, including foreign currency "
        "if combined in one line. Report the sign as printed."
    )
    net_change_unrealized: float | None = Field(
        description="Net change in unrealized appreciation (depreciation). Report the "
        "sign as printed."
    )
    net_increase_in_net_assets_ops: float | None = Field(
        description="Net increase (decrease) in net assets resulting from operations - "
        "the bottom line of the Statement of Operations."
    )

    # ── Statement of Changes in Net Assets ────────────────────────────────────────
    beginning_net_assets: float | None = Field(
        description="Net assets at the BEGINNING of the most recent period."
    )
    ending_net_assets: float | None = Field(
        description="Net assets at the END of the most recent period. This should match "
        "total_net_assets on the balance sheet."
    )
    capital_raised: float | None = Field(
        description="Total proceeds from shares sold / subscriptions during the period, "
        "across all classes."
    )
    proceeds_new_issues: float | None = Field(
        description="Proceeds from NEW subscriptions only, excluding reinvested "
        "distributions. Leave null if the filing does not separate them."
    )
    value_drip: float | None = Field(
        description="Dollar value of distributions reinvested (dividend reinvestment plan)."
    )
    repurchases: float | None = Field(
        description="Dollar value of shares repurchased / tendered during the period. "
        "Report as a positive number."
    )
    distributions_declared: float | None = Field(
        description="Total distributions declared to shareholders during the period. "
        "Report as a positive number."
    )
    shares_issued_new: float | None = Field(
        description="Number of shares issued for new subscriptions (a SHARE COUNT - see "
        "shares_scale)."
    )
    shares_issued_drip: float | None = Field(
        description="Number of shares issued through dividend reinvestment (a SHARE COUNT)."
    )
    shares_repurchased: float | None = Field(
        description="Number of shares repurchased or tendered (a SHARE COUNT), as a "
        "positive number."
    )

    # ── Statement of Cash Flows (absent in many unlevered funds) ──────────────────
    net_cash_operating: float | None = Field(
        description="Net cash provided by (used in) operating activities. For an "
        "investment company, buying and selling investments is an OPERATING activity."
    )
    net_cash_investing: float | None = Field(
        description="Net cash from investing activities. Usually absent for funds - "
        "leave null rather than guessing."
    )
    net_cash_financing: float | None = Field(
        description="Net cash provided by (used in) financing activities."
    )
    effect_of_fx: float | None = Field(
        description="Effect of exchange-rate changes on cash."
    )
    net_change_in_cash: float | None = Field(
        description="Net increase (decrease) in cash for the period."
    )
    interest_paid: float | None = Field(
        description="Cash interest paid during the period (a supplemental disclosure)."
    )
    investment_purchases: float | None = Field(
        description="Purchases of investments during the period, as a positive number."
    )
    investment_sales: float | None = Field(
        description="Proceeds from sales / maturities / repayments of investments, as a "
        "positive number."
    )

    # ── Financial Highlights ──────────────────────────────────────────────────────
    portfolio_turnover: float | None = Field(
        description="Portfolio turnover rate for the period, as a PERCENT number. This "
        "is normally one figure for the whole fund."
    )
    share_classes: list[RawShareClass] = Field(
        description="One entry per share class. Take the ratios and per-share figures "
        "from the MOST RECENT period column of the Financial Highlights table. If the "
        "fund has a single unnamed class, return exactly one entry labelled 'single'. "
        "Return an empty list only if no per-class information appears anywhere."
    )

    # ── Free-text escape hatch ────────────────────────────────────────────────────
    extraction_notes: str | None = Field(
        description="Anything a reviewer should know: a line you were unsure about, an "
        "unusual presentation, statements that appear truncated, or two funds' figures "
        "that were hard to tell apart. Leave null if the extraction was unambiguous. Do "
        "NOT put numbers we asked for here - put them in their own field."
    )

    # ── Convenience constructors ──────────────────────────────────────────────────

    @classmethod
    def empty(cls) -> "NCSRRawExtraction":
        """A fully-null instance. Every field on this model is REQUIRED (see the module
        docstring), which is right for the model but tedious in tests — so tests and
        fixtures build from here and set only the fields they care about."""
        blank: dict[str, object] = {}
        for name, field in cls.model_fields.items():
            ann = str(field.annotation)
            if name == "amounts_scale":
                blank[name] = "units"
            elif name == "shares_scale":
                blank[name] = "units"
            elif name in ("statements_present", "share_classes"):
                blank[name] = []
            elif "None" in ann:
                blank[name] = None
            else:  # pragma: no cover - defensive; every other field is optional-typed
                blank[name] = None
        return cls.model_validate(blank)


if __name__ == "__main__":
    # Self-test: no API key, no network. Proves three things we depend on later —
    # the model round-trips, the generated JSON schema stays inside the structured-
    # outputs subset, and empty() actually validates.
    import json

    schema = NCSRRawExtraction.model_json_schema()
    sample = NCSRRawExtraction.empty()
    sample.total_assets = 832_647_460.0
    sample.amounts_scale = "units"

    n_fields = len(NCSRRawExtraction.model_fields)
    print(f"NCSRRawExtraction: {n_fields} fields")
    print(f"round-trip OK: total_assets={sample.total_assets:,.0f}")

    # The subset check that matters: unsupported keywords anywhere in the schema would
    # be silently stripped by messages.parse() but sent verbatim on the batch path.
    banned = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
              "multipleOf", "minLength", "maxLength", "pattern", "minItems", "maxItems"}
    found = sorted(k for k in banned if f'"{k}"' in json.dumps(schema))
    print(f"unsupported schema keywords: {found or 'none'}")

    objects = [schema] + list(schema.get("$defs", {}).values())
    open_objects = [o.get("title") for o in objects
                    if o.get("type") == "object" and o.get("additionalProperties") is not False]
    print(f"objects missing additionalProperties:false: {open_objects or 'none'}")
