"""
rules.py — The validation layer (Data Dictionary "Validation rules" section).

Turns a raw FilingExtraction into a trusted record by checking internal consistency.
Two kinds of rule:
  - IDENTITY (accounting must-hold): a failure means extraction is wrong.
  - REASONABLENESS: a failure might be a real-world anomaly -> per the flag-and-keep
    policy we KEEP the value and raise a review flag (never discard).

`validate(extraction)` runs every applicable rule, fills `extraction.validation_checks`
and `extraction.review_flags`, sets `extraction.validation_status`, and returns the
extraction. Rules whose inputs are missing are recorded as "skipped" (not failures) —
so C4 (fair value) and C6 (roll-forward) sit ready for when those fields are extracted.

Run a quick demo:  uv run python src/validation/rules.py
"""

from __future__ import annotations

import sys
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"
sys.path.insert(0, str(SCHEMA_DIR))
from models import FilingExtraction, ValidationCheck  # noqa: E402


def _tol(x: float, rel: float = 0.001, floor: float = 1000.0) -> float:
    """Tolerance = greater of rel*|x| or an absolute floor (for rounding noise)."""
    return max(abs(x) * rel, floor)


def validate(e: FilingExtraction) -> FilingExtraction:
    checks: list[ValidationCheck] = []

    def add(rule, name, tier, status, message=None):
        checks.append(ValidationCheck(rule=rule, name=name, tier=tier,
                                      status=status, message=message))

    def v(*facts):
        """Return the .value of each Fact (None if missing)."""
        return [f.value for f in facts]

    bs = e.balance_sheet
    inc = e.income_statement
    d = e.derived

    # ── C1: balance sheet equation (identity) ────────────────────────────────
    ta, tl, tna = v(bs.total_assets, bs.total_liabilities, bs.total_net_assets)
    if None in (ta, tl, tna):
        add("C1", "Balance sheet equation", "identity", "skipped", "missing inputs")
    else:
        diff = ta - (tl + tna)
        if abs(diff) <= _tol(ta):
            add("C1", "Balance sheet equation", "identity", "pass")
        else:
            add("C1", "Balance sheet equation", "identity", "fail",
                f"assets {ta:,.0f} != liab+NA {tl + tna:,.0f} (diff {diff:,.0f})")

    # ── C2: NAV per share class (identity, with unit-error auto-detect) ───────
    for sc in e.share_classes_nav:
        na, sh, nav = v(sc.class_net_assets, sc.class_shares_outstanding, sc.class_nav_per_share)
        rid = f"C2[{sc.class_label}]"
        if None in (na, sh, nav) or not sh:
            add(rid, "NAV = net assets / shares", "identity", "skipped", "missing inputs")
            continue
        computed = na / sh
        if abs(nav - computed) <= 0.01:
            add(rid, "NAV = net assets / shares", "identity", "pass")
        elif computed and 0.5 < (computed / nav if nav else 0) / 1000 < 1.5:
            # reported ~1000x the computed -> units mismatch (thousands vs actual)
            add(rid, "NAV = net assets / shares", "identity", "fail",
                f"likely UNIT error: reported {nav:.2f} vs computed {computed:,.2f} (~1000x)")
        else:
            add(rid, "NAV = net assets / shares", "identity", "fail",
                f"reported {nav:.2f} vs computed {computed:.2f}")

    # ── C3: class net assets sum to total (identity) ─────────────────────────
    class_nas = [sc.class_net_assets.value for sc in e.share_classes_nav
                 if sc.class_net_assets.value is not None]
    if tna is None or not class_nas:
        add("C3", "Class net assets sum to total", "identity", "skipped", "missing inputs")
    else:
        diff = sum(class_nas) - tna
        status = "pass" if abs(diff) <= _tol(tna) else "fail"
        add("C3", "Class net assets sum to total", "identity", status,
            None if status == "pass" else f"sum {sum(class_nas):,.0f} != total {tna:,.0f}")

    # ── C4: fair-value hierarchy sum (identity) — ready for when §6 extracts ──
    fv = e.fair_value
    l1, l2, l3, npe, tot = v(fv.fv_level_1, fv.fv_level_2, fv.fv_level_3,
                             fv.fv_nav_practical_expedient, fv.fv_total)
    if tot is None or all(x is None for x in (l1, l2, l3, npe)):
        add("C4", "Fair-value hierarchy sum", "identity", "skipped", "missing inputs")
    else:
        s = sum(x for x in (l1, l2, l3, npe) if x is not None)
        status = "pass" if abs(s - tot) <= _tol(tot) else "fail"
        add("C4", "Fair-value hierarchy sum", "identity", status,
            None if status == "pass" else f"L1+L2+L3+NAV {s:,.0f} != total {tot:,.0f}")

    # ── C5: NII reconciles (identity) ─────────────────────────────────────────
    # Reconstructing NII from TII - expenses (- tax) is fragile for BDCs: expense support,
    # offering-cost amortization, and multi-component tax sit between TII and NII in
    # filer-specific ways. So we ALSO cross-check NII against the filer's own authoritative
    # tagged subtotals, and pass if ANY reconciliation holds:
    #   (anchor)  NII == InvestmentIncomeOperatingAfterExpenseAndTax
    #   (anchor)  NII == income-before-tax - tax
    #   (recompute) NII == TII - exp           (tax below the line, e.g. Blackstone)
    #   (recompute) NII == TII - exp - tax      (tax above the line, e.g. AB)
    # All branches only ADD ways to pass, so no filing that passed before can start failing.
    tii, exp, nii = v(inc.total_investment_income, inc.total_expenses, inc.net_investment_income)
    tax = inc.income_tax_expense.value or 0.0
    anchor_after = inc.nii_after_expense_and_tax.value     # should equal NII exactly
    pre_tax = inc.income_before_tax.value                  # NII = pre_tax - tax
    if None in (tii, exp, nii):
        add("C5", "NII reconciles", "identity", "skipped", "missing inputs")
    else:
        candidates = {
            "after-exp&tax anchor": anchor_after,
            "pre-tax anchor - tax": (pre_tax - tax) if pre_tax is not None else None,
            "TII-exp": tii - exp,
            "TII-exp-tax": tii - exp - tax,
        }
        diffs = {k: abs(val - nii) for k, val in candidates.items() if val is not None}
        tol = _tol(tii)
        best = min(diffs, key=diffs.get)
        if diffs[best] <= tol:
            add("C5", "NII reconciles", "identity", "pass")
        else:
            tax_note = f"; with tax {tii - exp - tax:,.0f}" if tax else ""
            add("C5", "NII reconciles", "identity", "fail",
                f"TII-exp {tii - exp:,.0f}{tax_note} != NII {nii:,.0f} "
                f"(closest '{best}' off by {diffs[best]:,.0f})")

    # ── C6: net-asset roll-forward (identity) — ready for when §5 extracts ────
    soc = e.statement_of_changes
    beg, cap, rep, dist, end = v(soc.beginning_net_assets, soc.capital_raised,
                                 soc.repurchases, soc.distributions_declared, soc.ending_net_assets)
    nio = inc.net_increase_in_net_assets_ops.value
    if None in (beg, cap, rep, dist, end, nio):
        add("C6", "Net-asset roll-forward", "identity", "skipped", "missing inputs")
    else:
        calc = beg + cap - rep + nio - dist
        status = "pass" if abs(calc - end) <= _tol(end) else "fail"
        add("C6", "Net-asset roll-forward", "identity", status,
            None if status == "pass" else f"rolled {calc:,.0f} != ending {end:,.0f}")

    # ── C7: income components sum to total (completeness, flag-keep) ──────────
    comps = v(inc.interest_income, inc.pik_interest_income, inc.dividend_income,
              inc.other_investment_income)
    if tii is None or any(c is None for c in comps):
        add("C7", "Income components sum to total", "identity", "skipped",
            "not all components present")
    else:
        diff = sum(comps) - tii
        status = "pass" if abs(diff) <= _tol(tii) else "fail"
        add("C7", "Income components sum to total", "identity", status,
            None if status == "pass" else f"components {sum(comps):,.0f} != total {tii:,.0f} "
            f"(diff {diff:,.0f}) - likely an uncaptured income line")

    # ── Reasonableness (flag-and-keep) ───────────────────────────────────────
    acov = d.asset_coverage_pct.value
    if acov is None:
        add("I1", "Asset coverage >= 150%", "reasonableness", "skipped", "missing inputs")
    elif acov >= 1.5:
        add("I1", "Asset coverage >= 150%", "reasonableness", "pass")
    else:
        add("I1", "Asset coverage >= 150%", "reasonableness", "fail",
            f"asset coverage {acov:.1%} below 150% regulatory minimum")

    lev = d.leverage_ratio.value
    if lev is None:
        add("I2", "Leverage in range", "reasonableness", "skipped", "missing inputs")
    elif 0 <= lev <= 2.0:
        add("I2", "Leverage in range", "reasonableness", "pass")
    else:
        add("I2", "Leverage in range", "reasonableness", "fail", f"leverage {lev:.2f} out of 0-2 range")

    if tna is None:
        add("A1", "Net assets positive", "reasonableness", "skipped", "missing inputs")
    else:
        add("A1", "Net assets positive", "reasonableness",
            "pass" if tna > 0 else "fail", None if tna > 0 else f"net assets {tna:,.0f} <= 0")

    for sc in e.share_classes_nav:
        nav = sc.class_nav_per_share.value
        if nav is None:
            continue
        if not (1.0 <= nav <= 100.0):
            add(f"A2[{sc.class_label}]", "NAV in plausible range", "reasonableness", "fail",
                f"NAV {nav:.2f} outside $1-$100")

    # ── Roll up ──────────────────────────────────────────────────────────────
    e.validation_checks = checks
    fails = [c for c in checks if c.status == "fail"]
    for c in fails:
        e.review_flags.append(f"{c.rule} {c.name}: {c.message}")
    e.validation_status = "review" if fails else "pass"
    return e


def summarize(e: FilingExtraction) -> str:
    counts = {"pass": 0, "fail": 0, "skipped": 0}
    for c in e.validation_checks:
        counts[c.status] = counts.get(c.status, 0) + 1
    lines = [f"{e.fund_name} | {e.reporting_date} | status={e.validation_status} | "
             f"pass={counts['pass']} fail={counts['fail']} skipped={counts['skipped']}"]
    for c in e.validation_checks:
        if c.status == "fail":
            lines.append(f"    FAIL {c.rule} {c.name}: {c.message}")
    return "\n".join(lines)


if __name__ == "__main__":
    # Demo: extract a fund and validate it.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extraction"))
    from edgar import set_identity, configure_http
    from bdc_xbrl import extract_bdc

    set_identity("brianpmoriarty@gmail.com")
    configure_http(use_system_certs=True)  # OS cert store → works behind corporate SSL inspection
    for cik in (1837532, 1803498, 1918712, 1838126):
        print(summarize(validate(extract_bdc(cik, form="10-Q"))))
