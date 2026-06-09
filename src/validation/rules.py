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


def _thousand_rounded(x: float) -> bool:
    """True if x looks XBRL-rounded to thousands (a multiple of 1000, magnitude >= 1000).
    Filers tag net assets / shares with decimals=-3 → the stored value is rounded."""
    return abs(x) >= 1000 and abs(round(x / 1000.0) * 1000.0 - x) < 0.5


def _nav_tol(na: float, sh: float, computed: float) -> float:
    """Tolerance for the per-share NAV identity (C2). A flat $0.01 is unrealistic when
    net assets and/or shares are XBRL-rounded to thousands: for a small class the rounding
    swings the recomputed NAV by cents-to-dollars even though the filer's REPORTED NAV is
    exact (e.g. net assets 10,000 / shares 382 → 26.18 vs a reported 25.13 whose true net
    assets were ~9,600). Propagate EACH input's own rounding independently:
        ΔNAV ≈ (step_na/2)/sh           (net-assets rounding → NAV)
             + |computed|·(step_sh/2)/sh (share rounding → NAV)
             + 0.005                      (reported NAV is itself rounded to the cent)
    step_x = 1000 if that input looks thousand-rounded, else 1. Floored at $0.01 so fully
    precise large classes keep a tight check. Genuine errors (sign flips, ~1000x unit
    mismatches) are far outside this band and still fail."""
    step_na = 1000.0 if _thousand_rounded(na) else 1.0
    step_sh = 1000.0 if _thousand_rounded(sh) else 1.0
    return max(0.01,
               (step_na / 2.0) / abs(sh)
               + abs(computed) * (step_sh / 2.0) / abs(sh)
               + 0.005)


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

    # ── C1b: tagged "total liabilities and net assets" == total assets (cross-check) ──
    # A free redundancy: filers tag LiabilitiesAndStockholdersEquity (the bottom-of-balance-sheet
    # total), which must equal total_assets. Additive reasonableness check (flag-and-keep) — only
    # fires when both are present, so it never regresses C1.
    lae = bs.liabilities_and_equity.value
    if ta is None or lae is None:
        add("C1b", "Liab+equity total = assets", "reasonableness", "skipped", "missing inputs")
    elif abs(lae - ta) <= _tol(ta):
        add("C1b", "Liab+equity total = assets", "reasonableness", "pass")
    else:
        add("C1b", "Liab+equity total = assets", "reasonableness", "fail",
            f"liab+equity total {lae:,.0f} != total assets {ta:,.0f} (diff {lae - ta:,.0f})")

    # ── C2: NAV per share class (identity, with unit-error auto-detect) ───────
    for sc in e.share_classes_nav:
        na, sh, nav = v(sc.class_net_assets, sc.class_shares_outstanding, sc.class_nav_per_share)
        rid = f"C2[{sc.class_label}]"
        if None in (na, sh, nav) or not sh:
            add(rid, "NAV = net assets / shares", "identity", "skipped", "missing inputs")
            continue
        computed = na / sh
        nav_tol = _nav_tol(na, sh, computed)
        if abs(nav - computed) <= nav_tol:
            add(rid, "NAV = net assets / shares", "identity", "pass")
        elif computed and 0.5 < (computed / nav if nav else 0) / 1000 < 1.5:
            # reported ~1000x the computed -> units mismatch (thousands vs actual)
            add(rid, "NAV = net assets / shares", "identity", "fail",
                f"likely UNIT error: reported {nav:.2f} vs computed {computed:,.2f} (~1000x)")
        else:
            add(rid, "NAV = net assets / shares", "identity", "fail",
                f"reported {nav:.2f} vs computed {computed:.2f} (tol {nav_tol:.2f})")

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

    # ── C6: net-asset roll-forward — DROPPED (2026-06-07 session 6) ───────────
    # The statement-of-changes roll-forward (beg + capital_raised - repurchases + net-ops -
    # distributions = end) is NOT reconstructable from XBRL: filers tag many components we
    # cannot capture reliably (DRIP reinvestment, gross-vs-net repurchases incl. unpaid
    # payables, offering costs, early-repurchase deductions), often under custom ck: concepts.
    # A full run showed 94 of 100 filings with all six inputs failing — median gap 2.3%, 35
    # over 5% — NOT tolerance-fixable. Decision (Brian): KEEP the captured statement-of-changes
    # DATA (beginning/ending net assets, capital_raised, repurchases — useful for the
    # spreadsheet) but emit NO C6 check. Re-add only if an authoritative tagged roll-forward
    # SUBTOTAL becomes available to anchor against (the way C5 anchors NII).

    # ── C7: income components reconcile to total (completeness, flag-keep) ─────
    # BDC PIK (paid-in-kind) tagging is inconsistent: some filers fold PIK INTO the
    # interest line (PIK-inclusive), others break it out separately (PIK-exclusive) —
    # and the breakout scatters across overlapping us-gaap + custom concepts. So instead
    # of demanding the components sum EXACTLY, we anchor on the filer's authoritative
    # total and treat PIK as a band: the clean PIK-free components (interest+dividend+
    # other) must not overshoot the total, and any shortfall must be explainable by the
    # filer's tagged PIK. A genuine uncaptured NON-PIK income line still fails (mirrors
    # the C5 anchor strategy). Gating is unchanged from the old strict rule (same four
    # components required) so this only ADDS pass paths — nothing passing can start to fail.
    int_i, pik_i, div_i, oth_i = v(inc.interest_income, inc.pik_interest_income,
                                   inc.dividend_income, inc.other_investment_income)
    if tii is None or any(c is None for c in (int_i, pik_i, div_i, oth_i)):
        add("C7", "Income components reconcile to total", "identity", "skipped",
            "not all components present")
    else:
        core = int_i + div_i + oth_i                       # PIK-free, consistent across filers
        pik_div, pik_comb = v(inc.pik_dividend_income, inc.pik_income_combined)
        pik_split = pik_i + (pik_div or 0.0)               # PIK interest + PIK dividend
        pik_avail = max(pik_split, pik_comb or 0.0)        # best estimate of total PIK income
        shortfall = tii - core
        tol = _tol(tii)
        # PIK-inclusive filer -> shortfall ~ 0; PIK-exclusive -> shortfall ~ pik_avail.
        if -tol <= shortfall <= pik_avail + tol:
            add("C7", "Income components reconcile to total", "identity", "pass")
        else:
            add("C7", "Income components reconcile to total", "identity", "fail",
                f"core (int+div+oth) {core:,.0f} vs total {tii:,.0f} (shortfall {shortfall:,.0f}, "
                f"tagged PIK {pik_avail:,.0f}) - unexplained income gap")

    # ── C9: statement of cash flows foots (identity) ─────────────────────────
    # operating + investing + financing + fx = net change in cash. For an investment company,
    # investing is often absent (investments are an operating activity) and FX is usually zero —
    # both default to 0. Tolerance is relative to the GROSS flows (op/fin run to billions while the
    # net change can be small), so dollar-level rounding doesn't trip it.
    cf = e.cash_flow
    op, fin, nch = v(cf.net_cash_operating, cf.net_cash_financing, cf.net_change_in_cash)
    inv = cf.net_cash_investing.value or 0.0
    fx = cf.effect_of_fx.value or 0.0
    if None in (op, fin, nch):
        add("C9", "Cash flow statement foots", "identity", "skipped", "missing inputs")
    else:
        diff = (op + inv + fin + fx) - nch
        tol = max(_tol(nch), 0.005 * max(abs(op), abs(fin), abs(nch)))
        if abs(diff) <= tol:
            add("C9", "Cash flow statement foots", "identity", "pass")
        else:
            add("C9", "Cash flow statement foots", "identity", "fail",
                f"op {op:,.0f} + inv {inv:,.0f} + fin {fin:,.0f} + fx {fx:,.0f} "
                f"!= net change {nch:,.0f} (diff {diff:,.0f})")

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
        # NAV of 0/None = a DORMANT share class: registered on the XBRL share-class axis
        # but unfunded (0 shares, 0/None net assets), so it has no meaningful NAV — not an
        # out-of-range anomaly. (A genuinely funded class that mis-extracted to 0 would be
        # caught by the C2 identity, net_assets/shares != 0, so skipping 0 here is safe.)
        if not nav:
            continue
        if not (1.0 <= nav <= 100.0):
            add(f"A2[{sc.class_label}]", "NAV in plausible range", "reasonableness", "fail",
                f"NAV {nav:.2f} outside $1-$100")

    # ── C8: undrawn debt capacity plausible (reasonableness, flag-and-keep) ───
    # We take the UNDIMENSIONED LineOfCreditFacilityRemainingBorrowingCapacity (a clean
    # fund-level figure across the probed filers). The dimensioned per-facility rows are
    # cross-tabbed across CreditFacility + LegalEntity axes and the MaximumBorrowingCapacity
    # total double-counts them, so a max-vs-(drawn+undrawn) reconciliation isn't reliable.
    # Instead we bound the figure: undrawn capacity above the fund's TOTAL ASSETS would signal
    # a double-count / mis-tag. Negative is impossible. Value is KEPT either way (flag-and-keep).
    undrawn = e.liquidity.undrawn_debt_capacity.value
    if undrawn is None:
        add("C8", "Undrawn debt capacity plausible", "reasonableness", "skipped", "not tagged")
    elif undrawn < 0:
        add("C8", "Undrawn debt capacity plausible", "reasonableness", "fail",
            f"undrawn capacity {undrawn:,.0f} is negative")
    elif ta is not None and undrawn > ta:
        add("C8", "Undrawn debt capacity plausible", "reasonableness", "fail",
            f"undrawn capacity {undrawn:,.0f} exceeds total assets {ta:,.0f} (possible double-count)")
    else:
        add("C8", "Undrawn debt capacity plausible", "reasonableness", "pass")

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
