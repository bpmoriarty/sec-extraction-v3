"""
build_spreadsheet.py — assemble the extracted per-filing JSONs into one analysis workbook.

Reads every JSON in data/extracted/ and writes data/dataset/semiliquid_bdc_dataset.xlsx with
four tabs:

  Data         — one row per filing (fund-period grain), ~65 fields grouped by section.
                 Flag-and-keep values are marked: a `status`/`flags` column, an amber tint on
                 any review row, AND a cell-level highlight on the specific fields tied to each
                 failing rule (C4 -> the fair-value cells, C5 -> NII, etc.).
  ShareClasses — one row per filing x share class (NAV / shares / net assets / yield).
  Review       — every filing with a validation failure + the rules + messages.
  Check (Gold) — a hand-verification view for ~15 representative filings: each filing's key
                 fields stacked tall, with provenance + blank verdict columns. Fill the
                 ✓ (Y/N) column against the actual SEC filing; the accuracy % computes itself.

Run:  uv run python src/output/build_spreadsheet.py
"""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = Path(__file__).resolve().parents[2]
EXTRACTED = ROOT / "data" / "extracted"
OUT = ROOT / "data" / "dataset" / "semiliquid_bdc_dataset.xlsx"

# ── Column spec for the Data tab ────────────────────────────────────────────────
# (section, column_name, section_obj, field) — section_obj is the top-level JSON key
# whose Fact lives at [section_obj][field]["value"]. Meta columns are handled separately.
DATA_FIELDS: list[tuple[str, str, str, str]] = [
    # Balance sheet (§2)
    ("Balance", "total_assets", "balance_sheet", "total_assets"),
    ("Balance", "total_liabilities", "balance_sheet", "total_liabilities"),
    ("Balance", "total_net_assets", "balance_sheet", "total_net_assets"),
    ("Balance", "investments_at_fair_value", "balance_sheet", "investments_at_fair_value"),
    ("Balance", "cash_and_equivalents", "balance_sheet", "cash_and_equivalents"),
    ("Balance", "total_debt", "balance_sheet", "total_debt"),
    # Income statement (§4)
    ("Income", "interest_income", "income_statement", "interest_income"),
    ("Income", "pik_interest_income", "income_statement", "pik_interest_income"),
    ("Income", "dividend_income", "income_statement", "dividend_income"),
    ("Income", "other_investment_income", "income_statement", "other_investment_income"),
    ("Income", "total_investment_income", "income_statement", "total_investment_income"),
    ("Income", "total_expenses", "income_statement", "total_expenses"),
    ("Income", "income_tax_expense", "income_statement", "income_tax_expense"),
    ("Income", "net_investment_income", "income_statement", "net_investment_income"),
    ("Income", "net_realized_gain_loss", "income_statement", "net_realized_gain_loss"),
    ("Income", "net_change_unrealized", "income_statement", "net_change_unrealized"),
    ("Income", "net_increase_in_net_assets_ops", "income_statement", "net_increase_in_net_assets_ops"),
    # Fair-value hierarchy (§6)
    ("FairValue", "fv_level_1", "fair_value", "fv_level_1"),
    ("FairValue", "fv_level_2", "fair_value", "fv_level_2"),
    ("FairValue", "fv_level_3", "fair_value", "fv_level_3"),
    ("FairValue", "fv_nav_practical_expedient", "fair_value", "fv_nav_practical_expedient"),
    ("FairValue", "fv_total", "fair_value", "fv_total"),
    # Statement of changes (§5)
    ("Changes", "beginning_net_assets", "statement_of_changes", "beginning_net_assets"),
    ("Changes", "capital_raised", "statement_of_changes", "capital_raised"),
    ("Changes", "repurchases", "statement_of_changes", "repurchases"),
    ("Changes", "distributions_declared", "statement_of_changes", "distributions_declared"),
    ("Changes", "ending_net_assets", "statement_of_changes", "ending_net_assets"),
    # Fees (§11)
    ("Fees", "management_fee", "fees", "management_fee"),
    ("Fees", "incentive_fee", "fees", "incentive_fee"),
    ("Fees", "expense_support_net", "fees", "expense_support_net"),
    # Financial highlights (§7)
    ("Highlights", "expense_ratio", "financial_highlights", "expense_ratio"),
    ("Highlights", "gross_expense_ratio", "financial_highlights", "gross_expense_ratio"),
    ("Highlights", "net_investment_income_ratio", "financial_highlights", "net_investment_income_ratio"),
    ("Highlights", "total_return", "financial_highlights", "total_return"),
    ("Highlights", "portfolio_turnover", "financial_highlights", "portfolio_turnover"),
    # Distributions & leverage (§8)
    ("DistLev", "distributions_per_share", "distributions_leverage", "distributions_per_share"),
    ("DistLev", "return_of_capital_pct", "distributions_leverage", "return_of_capital_pct"),
    ("DistLev", "asset_coverage_ratio", "distributions_leverage", "asset_coverage_ratio"),
    ("DistLev", "weighted_avg_interest_rate", "distributions_leverage", "weighted_avg_interest_rate"),
    # Portfolio summary (§9)
    ("Portfolio", "num_holdings", "portfolio_summary", "num_holdings"),
    ("Portfolio", "investments_at_cost", "portfolio_summary", "investments_at_cost"),
    ("Portfolio", "non_accrual_fair_value", "portfolio_summary", "non_accrual_fair_value"),
    ("Portfolio", "non_accrual_at_cost", "portfolio_summary", "non_accrual_at_cost"),
    ("Portfolio", "weighted_avg_portfolio_yield", "portfolio_summary", "weighted_avg_portfolio_yield"),
    ("Portfolio", "pct_floating_rate", "portfolio_summary", "pct_floating_rate"),
    ("Portfolio", "capitalized_pik_balance", "portfolio_summary", "capitalized_pik_balance"),
    ("Portfolio", "top_10_concentration", "portfolio_summary", "top_10_concentration"),
    # Liquidity & obligations (§12)
    ("Liquidity", "unfunded_commitments", "liquidity", "unfunded_commitments"),
    ("Liquidity", "undrawn_debt_capacity", "liquidity", "undrawn_debt_capacity"),
    ("Liquidity", "weighted_avg_debt_maturity", "liquidity", "weighted_avg_debt_maturity"),
    ("Liquidity", "repurchase_offered", "liquidity", "repurchase_offered"),
    ("Liquidity", "repurchase_requested", "liquidity", "repurchase_requested"),
    ("Liquidity", "repurchase_repurchased", "liquidity", "repurchase_repurchased"),
    ("Liquidity", "repurchase_proration_pct", "liquidity", "repurchase_proration_pct"),
    # Derived metrics (§10)
    ("Derived", "leverage_ratio", "derived", "leverage_ratio"),
    ("Derived", "asset_coverage_pct", "derived", "asset_coverage_pct"),
    ("Derived", "net_debt", "derived", "net_debt"),
    ("Derived", "pik_income_ratio", "derived", "pik_income_ratio"),
    ("Derived", "non_accrual_pct_fv", "derived", "non_accrual_pct_fv"),
    ("Derived", "non_accrual_pct_cost", "derived", "non_accrual_pct_cost"),
    ("Derived", "distribution_coverage_ratio", "derived", "distribution_coverage_ratio"),
    ("Derived", "portfolio_mark", "derived", "portfolio_mark"),
    ("Derived", "net_lending_spread", "derived", "net_lending_spread"),
    ("Derived", "liquidity_coverage", "derived", "liquidity_coverage"),
]

META_COLS = ["cik", "fund_name", "form_type", "reporting_date", "period_months",
             "vehicle_type", "status", "flags"]

# Which Data columns each validation rule implicates (for cell-level highlighting). Per-class
# rules (C2/A2) highlight in the ShareClasses tab instead, so they're not here.
RULE_FIELDS: dict[str, list[str]] = {
    "C1": ["total_assets", "total_liabilities", "total_net_assets"],
    "C3": ["total_net_assets"],
    "C4": ["fv_level_1", "fv_level_2", "fv_level_3", "fv_nav_practical_expedient", "fv_total"],
    "C5": ["total_investment_income", "total_expenses", "net_investment_income"],
    "C7": ["interest_income", "pik_interest_income", "dividend_income",
           "other_investment_income", "total_investment_income"],
    "A1": ["total_net_assets"],
    "I1": ["asset_coverage_ratio"],
    "I2": ["leverage_ratio"],
}

# Gold sample (~15): clean + messy filers, single + multi-class, LLC + corp. Each entry is a
# (fund_name prefix, preferred form) — we take that fund's latest filing of the preferred form.
GOLD_SELECTION = [
    ("Apollo Debt Solutions", "10-K"),       # clean, annual (verify full-year flows)
    ("Blackstone Private Credit", "10-Q"),   # clean, large, NAV-PE bucket
    ("HPS Corporate Lending", "10-Q"),       # clean, 4-class, fair-value sum2
    ("AB Private Lending", "10-Q"),          # excise-tax / C5 waiver case
    ("Blue Owl Credit Income", "10-Q"),      # PIK-dividend (C7) case
    ("First Eagle Private Credit", "10-Q"),  # messy: C2/C3/C4 — verify flags are correct
    ("Antares Private Credit", "10-Q"),      # C5 flag — verify NII value is right anyway
    ("PGIM Private Credit", "10-Q"),         # C4 residual
    ("Golub Capital Private Credit", "10-K"),# clean, 4-class, annual
    ("John Hancock Comvest", "10-Q"),        # most share classes (5)
    ("Terra Income Fund 6", "10-Q"),         # LLC structure, single-class
    ("Fidelity Private Credit", "10-Q"),     # LLC, single-class
    ("Oaktree Strategic Credit", "10-Q"),    # C5 split-tax case
    ("Bain Capital Private Credit", "10-Q"), # clean, 4-class
    ("Crescent Private Credit", "10-Q"),     # C4 cleared via NAV-PE — verify
]

# Key fields shown in the gold check view (curated subset, in reading order).
GOLD_FIELDS = [
    ("balance_sheet", "total_assets"), ("balance_sheet", "total_liabilities"),
    ("balance_sheet", "total_net_assets"), ("balance_sheet", "investments_at_fair_value"),
    ("balance_sheet", "cash_and_equivalents"), ("balance_sheet", "total_debt"),
    ("income_statement", "total_investment_income"), ("income_statement", "total_expenses"),
    ("income_statement", "net_investment_income"), ("income_statement", "pik_interest_income"),
    ("fair_value", "fv_level_1"), ("fair_value", "fv_level_2"), ("fair_value", "fv_level_3"),
    ("fair_value", "fv_nav_practical_expedient"), ("fair_value", "fv_total"),
    ("portfolio_summary", "num_holdings"), ("portfolio_summary", "investments_at_cost"),
]

# ── Styling ─────────────────────────────────────────────────────────────────────
HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(bold=True, color="FFFFFF", size=10)
SECTION_FILL = PatternFill("solid", fgColor="2E5496")
REVIEW_ROW_FILL = PatternFill("solid", fgColor="FFF2CC")   # light amber row tint
FLAG_CELL_FILL = PatternFill("solid", fgColor="F4B183")    # stronger amber for flagged cells
PASS_FILL = PatternFill("solid", fgColor="E2EFDA")
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
MONEY_FMT = "#,##0"


def fact_value(j: dict, section: str, field: str):
    """Return the numeric .value for a Fact field, or None."""
    sec = j.get(section) or {}
    f = sec.get(field) or {}
    return f.get("value")


def fact_source(j: dict, section: str, field: str) -> str:
    sec = j.get(section) or {}
    f = sec.get(field) or {}
    return f.get("raw_text") or (f.get("source") or "")


def failing_rule_bases(j: dict) -> list[str]:
    return sorted({c["rule"].split("[")[0] for c in j.get("validation_checks", [])
                   if c["status"] == "fail"})


def load_filings() -> list[dict]:
    out = []
    for p in sorted(EXTRACTED.glob("*.json")):
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


def sec_url(j: dict) -> str:
    acc = j.get("accession_no") or ""
    cik = j.get("cik", "").lstrip("0")
    if not acc or not cik:
        return ""
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{acc}-index.htm"


def style_header(ws, row_idx: int, ncols: int):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row_idx, column=c)
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER


def build_data_tab(wb, filings: list[dict]):
    ws = wb.create_sheet("Data")
    headers = META_COLS + [name for _, name, _, _ in DATA_FIELDS]
    # Row 1: section bands; Row 2: column names
    ws.append(["META"] * len(META_COLS) + [sec for sec, _, _, _ in DATA_FIELDS])
    ws.append(headers)
    style_header(ws, 2, len(headers))
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = SECTION_FILL
        cell.font = Font(bold=True, color="FFFFFF", size=9)
        cell.alignment = Alignment(horizontal="center")

    field_col = {name: META_COLS.__len__() + i + 1 for i, (_, name, _, _) in enumerate(DATA_FIELDS)}
    for j in filings:
        fails = failing_rule_bases(j)
        row = [
            j.get("cik"), j.get("fund_name"), j.get("form_type"), j.get("reporting_date"),
            j.get("period_months"), j.get("vehicle_type"), j.get("validation_status"),
            ",".join(fails),
        ]
        for _, _, sec, fld in DATA_FIELDS:
            row.append(fact_value(j, sec, fld))
        ws.append(row)
        r = ws.max_row
        is_review = j.get("validation_status") == "review"
        # row tint + number formats
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=r, column=c)
            cell.border = BORDER
            if is_review:
                cell.fill = REVIEW_ROW_FILL
            if c > len(META_COLS) and isinstance(cell.value, (int, float)):
                cell.number_format = MONEY_FMT
        # cell-level highlight on fields implicated by each failing rule
        flagged_fields: set[str] = set()
        for rule in fails:
            flagged_fields.update(RULE_FIELDS.get(rule, []))
        for fld in flagged_fields:
            col = field_col.get(fld)
            if col:
                ws.cell(row=r, column=col).fill = FLAG_CELL_FILL
    ws.freeze_panes = "C3"
    _autosize(ws, headers, start_row=2)
    return ws


def build_shareclasses_tab(wb, filings: list[dict]):
    ws = wb.create_sheet("ShareClasses")
    headers = ["cik", "fund_name", "reporting_date", "class", "net_assets",
               "shares", "nav_per_share", "distribution_yield", "flags"]
    ws.append(headers)
    style_header(ws, 1, len(headers))
    for j in filings:
        fails = failing_rule_bases(j)
        # per-class fails (C2/A2) carry the class label in brackets
        class_fail = {}
        for c in j.get("validation_checks", []):
            if c["status"] == "fail" and "[" in c["rule"]:
                lbl = c["rule"].split("[")[1].rstrip("]")
                class_fail.setdefault(lbl, set()).add(c["rule"].split("[")[0])
        for sc in j.get("share_classes_nav", []):
            lbl = sc.get("class_label")
            ws.append([
                j.get("cik"), j.get("fund_name"), j.get("reporting_date"), lbl,
                (sc.get("class_net_assets") or {}).get("value"),
                (sc.get("class_shares_outstanding") or {}).get("value"),
                (sc.get("class_nav_per_share") or {}).get("value"),
                (sc.get("distribution_yield") or {}).get("value"),
                ",".join(sorted(class_fail.get(lbl, set()))),
            ])
            r = ws.max_row
            for c in range(1, len(headers) + 1):
                ws.cell(row=r, column=c).border = BORDER
            for col in (5, 6, 7):
                cell = ws.cell(row=r, column=col)
                if isinstance(cell.value, (int, float)):
                    cell.number_format = MONEY_FMT
            if class_fail.get(lbl):
                ws.cell(row=r, column=7).fill = FLAG_CELL_FILL  # NAV cell
    ws.freeze_panes = "A2"
    _autosize(ws, headers, start_row=1)
    return ws


def build_review_tab(wb, filings: list[dict]):
    ws = wb.create_sheet("Review")
    headers = ["cik", "fund_name", "reporting_date", "status", "failing_rules", "messages"]
    ws.append(headers)
    style_header(ws, 1, len(headers))
    for j in filings:
        if j.get("validation_status") != "review":
            continue
        fails = [c for c in j.get("validation_checks", []) if c["status"] == "fail"]
        ws.append([
            j.get("cik"), j.get("fund_name"), j.get("reporting_date"), "review",
            ",".join(sorted({c["rule"] for c in fails})),
            " | ".join(c["message"] for c in fails if c.get("message")),
        ])
        r = ws.max_row
        for c in range(1, len(headers) + 1):
            ws.cell(row=r, column=c).fill = REVIEW_ROW_FILL
            ws.cell(row=r, column=c).border = BORDER
    ws.freeze_panes = "A2"
    _autosize(ws, headers, start_row=1, max_w=70)
    return ws


def pick_gold(filings: list[dict]) -> list[dict]:
    chosen = []
    for prefix, form in GOLD_SELECTION:
        cands = [j for j in filings if j.get("fund_name", "").startswith(prefix)
                 and j.get("form_type") == form]
        if not cands:  # fall back to any form
            cands = [j for j in filings if j.get("fund_name", "").startswith(prefix)]
        if cands:
            chosen.append(max(cands, key=lambda x: x.get("reporting_date", "")))
    return chosen


def build_gold_tab(wb, filings: list[dict]):
    ws = wb.create_sheet("Check (Gold)")
    gold = pick_gold(filings)
    # Accuracy summary block (formulas fill in as you complete the verdict column F).
    ws.append(["GOLD ACCURACY CHECK — fill column F (Y/N) against each filing on SEC.gov"])
    ws["A1"].font = Font(bold=True, size=12)
    ws.append(["Checked:", "=COUNTIF(F:F,\"Y\")+COUNTIF(F:F,\"N\")",
               "Correct:", "=COUNTIF(F:F,\"Y\")",
               "Accuracy:", "=IFERROR(COUNTIF(F:F,\"Y\")/(COUNTIF(F:F,\"Y\")+COUNTIF(F:F,\"N\")),\"-\")"])
    ws["F2"].number_format = "0.0%"
    for c in ("A2", "C2", "E2"):
        ws[c].font = Font(bold=True)
    ws.append([])
    headers = ["field", "extracted", "source (xbrl concept)", "confidence", "filing / link", "✓ (Y/N)", "true value", "note"]
    hdr_row = ws.max_row + 1
    ws.append(headers)
    style_header(ws, hdr_row, len(headers))

    dv = DataValidation(type="list", formula1='"Y,N"', allow_blank=True)
    ws.add_data_validation(dv)

    for j in gold:
        # filing header band
        label = f"{j.get('fund_name')}  |  {j.get('form_type')}  {j.get('reporting_date')}  (classes: {', '.join(j.get('share_classes') or ['single'])})"
        ws.append([label])
        br = ws.max_row
        ws.cell(row=br, column=1).font = Font(bold=True, color="FFFFFF")
        for c in range(1, len(headers) + 1):
            ws.cell(row=br, column=c).fill = SECTION_FILL
        ws.cell(row=br, column=5).value = sec_url(j)
        ws.cell(row=br, column=5).font = Font(color="FFFFFF", underline="single")
        # fund-level key fields
        for sec, fld in GOLD_FIELDS:
            val = fact_value(j, sec, fld)
            ws.append([fld, val, fact_source(j, sec, fld),
                       (j.get(sec, {}).get(fld, {}) or {}).get("confidence"), "", "", "", ""])
            r = ws.max_row
            if isinstance(val, (int, float)):
                ws.cell(row=r, column=2).number_format = MONEY_FMT
            dv.add(ws.cell(row=r, column=6))
            for c in range(1, len(headers) + 1):
                ws.cell(row=r, column=c).border = BORDER
        # per-class NAV rows
        for sc in j.get("share_classes_nav", []):
            nav = (sc.get("class_nav_per_share") or {}).get("value")
            ws.append([f"NAV (class {sc.get('class_label')})", nav,
                       (sc.get("class_nav_per_share") or {}).get("raw_text", ""),
                       (sc.get("class_nav_per_share") or {}).get("confidence"), "", "", "", ""])
            r = ws.max_row
            dv.add(ws.cell(row=r, column=6))
            for c in range(1, len(headers) + 1):
                ws.cell(row=r, column=c).border = BORDER
    ws.freeze_panes = "A" + str(hdr_row + 1)
    _autosize(ws, headers, start_row=hdr_row, max_w=55)
    return ws, len(gold)


def _autosize(ws, headers, start_row: int, max_w: int = 22):
    for i, h in enumerate(headers, start=1):
        width = max(len(str(h)), 10)
        for r in range(start_row, min(ws.max_row, start_row + 60) + 1):
            v = ws.cell(row=r, column=i).value
            if v is not None:
                width = max(width, min(len(str(v)), max_w))
        ws.column_dimensions[get_column_letter(i)].width = min(width + 2, max_w)


def main():
    filings = load_filings()
    if not filings:
        raise SystemExit(f"No JSONs found in {EXTRACTED}")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # drop the default sheet
    build_data_tab(wb, filings)
    build_shareclasses_tab(wb, filings)
    build_review_tab(wb, filings)
    _, n_gold = build_gold_tab(wb, filings)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT)
    n_review = sum(1 for j in filings if j.get("validation_status") == "review")
    print(f"Wrote {OUT}")
    print(f"  Data: {len(filings)} filings | Review: {n_review} | Gold sample: {n_gold} filings")


if __name__ == "__main__":
    main()
