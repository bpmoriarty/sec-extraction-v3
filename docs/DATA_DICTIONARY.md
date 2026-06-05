# SEC Filing Extraction — Data Dictionary (v0.1 DRAFT)

This is the **what-we-collect** spec. The extraction prompt, the validation rules, and
the output spreadsheet columns all derive from this document. It is the source of truth;
`src/schema/models.py` (pydantic) will encode it in code so the two never drift.

**Status:** v0.1 draft for review. Items marked **[CONFIRM]** are research-judgment calls
that need Brian's decision before we lock them. Built/validated against the BDC pilot
(10-K / 10-Q, XBRL) — see `## Source grounding` below.

---

## Conventions

- **Time key = reporting date (period-end)**, sourced from EDGAR `period_of_report` /
  XBRL context — NOT the filing date in the filename.
- **Two grains** (so the output has two natural tables):
  - **fund-period** — one row per filing (balance sheet totals, income, composition).
  - **fund-period-class** — one row per share class within a filing (NAV, shares).
- **Point-in-time vs flow.** *Snapshot* fields (balance sheet, NAV, composition,
  non-accruals) are as of `reporting_date` and are directly comparable across 10-K and
  10-Q. *Flow* fields (income statement, distributions) cover `period_start` →
  `reporting_date`, with `period_months` = 3 for a 10-Q quarter or 12 for a 10-K year.
  We capture the filing's **primary period as-reported only** — no 10-Q year-to-date
  figures; standalone Q4 (annual − 9-mo YTD) and annualized values are derived later in
  analysis, not at extraction time.
- **Units** are captured from XBRL ("in thousands" etc.) and normalized to actual dollars
  on the way in; everything stored in the dataset is in **actual dollars / actual shares**.
- **Every value carries provenance + confidence**: `source` ∈ {`xbrl`, `llm`, `computed`}
  and a 0–1 confidence. This is how we trust + audit the data.
- **Anomaly policy:** accounting-identity failures are rejected/fixed; reasonableness-check
  failures keep the value and raise a review flag (we never discard a real anomaly).
- Scope is **comprehensive**, but fields not present in a given form are simply null.

---

## Source grounding (from the 2026-06-03 XBRL spike)

Verified on Apollo, Blackstone, Ares, HPS 10-Qs. Confirmed available & structured:
balance sheet (incl. per-class net assets/shares/NAV), income statement, statement of
changes in net assets, financial highlights, fair-value hierarchy (dimensional),
schedule of investments. Per-filer variation exists (e.g. HPS tags NAV/share in a
different location) → XBRL-first with LLM/secondary-location fallback.

---

## 1. Identity & metadata  *(grain: fund-period)*

| Field | Definition | Source |
|---|---|---|
| cik | 10-digit zero-padded CIK | metadata |
| fund_name | Registrant legal name | metadata |
| form_type | 10-K / 10-Q (pilot); N-CSR/N-CSRS later | metadata |
| reporting_date | Period-**end** — the time key; snapshot date for point-in-time fields | XBRL `period_of_report` |
| period_start | Start of the period that **flow** fields cover | XBRL context |
| period_months | Length of the flow period: 3 (10-Q quarter) or 12 (10-K annual) | derived |
| filing_date | Date filed (secondary) | metadata |
| fiscal_period | FY / Q1–Q3 | derived from period |
| vehicle_type | Unlisted BDC / Interval Fund / … | fund_universe.csv |
| share_classes | List of class labels present (e.g. S, D, I) | XBRL dimensions |

## 2. Balance sheet  *(grain: fund-period)*

| Field | Definition | Source |
|---|---|---|
| total_assets | Total assets | XBRL `us-gaap:Assets` |
| total_liabilities | Total liabilities | XBRL `us-gaap:Liabilities` |
| total_net_assets | Net assets (equity) | XBRL |
| investments_at_fair_value | Total investments at fair value | XBRL |
| cash_and_equivalents | Cash + foreign currency + equivalents | XBRL |
| total_debt | Debt, net of deferred financing costs | XBRL |

## 3. NAV per share class  *(grain: fund-period-class)*

| Field | Definition | Source |
|---|---|---|
| class_label | Share class (S / D / I / single) | XBRL dimension |
| class_net_assets | Net assets attributable to the class | XBRL |
| class_shares_outstanding | Shares outstanding for the class | XBRL |
| class_nav_per_share | NAV per share for the class | XBRL (fallback: parenthetical/LLM) |

## 4. Income statement  *(grain: fund-period)*

| Field | Definition | Source |
|---|---|---|
| interest_income | Cash interest income | XBRL |
| pik_interest_income | Payment-in-kind (PIK) interest — accrued, not paid in cash (key credit-stress signal) | XBRL |
| dividend_income | Dividend income | XBRL |
| other_investment_income | Other / fee / misc. investment income | XBRL |
| total_investment_income | Total investment income (= sum of components above) | XBRL |
| total_expenses | Total expenses (net of waivers) | XBRL |
| net_investment_income | NII | XBRL |
| net_realized_gain_loss | Net realized gains/(losses) | XBRL |
| net_change_unrealized | Net change in unrealized appn/(depn) | XBRL |
| net_increase_in_net_assets_ops | Net increase from operations | XBRL |

## 5. Statement of changes in net assets  *(grain: fund-period)* — enables roll-forward

| Field | Definition | Source |
|---|---|---|
| beginning_net_assets | Net assets, start of period | XBRL |
| capital_raised | Proceeds from share issuance | XBRL |
| repurchases | Shares repurchased / tendered | XBRL |
| distributions_declared | Distributions to shareholders | XBRL |
| ending_net_assets | Net assets, end of period (= total_net_assets) | XBRL |

## 6. Fair-value hierarchy  *(grain: fund-period)*

| Field | Definition | Source |
|---|---|---|
| fv_level_1 | Investments measured at Level 1 | XBRL (dimensional) |
| fv_level_2 | Level 2 | XBRL |
| fv_level_3 | Level 3 | XBRL |
| fv_nav_practical_expedient | Investments measured at NAV (outside L1/2/3) | XBRL |
| fv_total | Total investments at fair value | XBRL |

## 7. Financial highlights  *(grain: fund-period, often per-class)*

| Field | Definition | Source |
|---|---|---|
| expense_ratio | Ratio of expenses to average net assets | XBRL highlights (tagged) |
| net_investment_income_ratio | NII to average net assets | XBRL highlights |
| total_return | Total return for the period | XBRL highlights |
| portfolio_turnover | Portfolio turnover rate | XBRL highlights |

## 8. Distributions & leverage  *(grain: fund-period)*

| Field | Definition | Source |
|---|---|---|
| distributions_per_share | Distributions declared per share | XBRL |
| asset_coverage_ratio | Regulatory 1940-Act asset coverage | XBRL (often tagged) / computed |
| weighted_avg_interest_rate | Weighted average rate on debt | XBRL debt detail |

## 9. Portfolio — summary composition  *(grain: fund-period; holdings deferred)*

| Field | Definition | Source |
|---|---|---|
| num_holdings | Count of portfolio positions | XBRL schedule of investments |
| composition_by_industry | {industry: fair_value} map | XBRL |
| composition_by_type | {security type: fair_value} map | XBRL |
| top_10_concentration | Σ fair value of 10 largest ÷ investments | computed |
| investments_at_cost | Total investments at amortized cost | XBRL |
| non_accrual_fair_value | Fair value of non-accrual investments | XBRL / LLM |
| non_accrual_at_cost | Amortized cost of non-accrual investments | XBRL / LLM |

> **Deferred to a later phase:** full **holding-level table** (issuer, industry, security
> type, rate, maturity, par, cost, fair value, % of net assets, non-accrual flag).

## 10. Derived fields  *(computed by us, not extracted)* — **confirmed 2026-06-03**

| Field | Formula | Status |
|---|---|---|
| leverage_ratio | total_debt ÷ total_net_assets | ✅ Confirmed |
| asset_coverage_pct | (total_assets − liabilities-excl-debt) ÷ total_debt | ✅ Confirmed |
| distribution_yield | (distributions_per_share ÷ class_nav_per_share), annualized | ✅ Confirmed (grain: fund-period-class) |
| net_debt | total_debt − cash_and_equivalents | ✅ Confirmed (include) |
| pik_income_ratio | pik_interest_income ÷ total_investment_income | Added 2026-06-05 (credit-stress signal) |
| non_accrual_pct_fv | non_accrual_fair_value ÷ investments_at_fair_value | Added 2026-06-05 |
| non_accrual_pct_cost | non_accrual_at_cost ÷ investments_at_cost | Added 2026-06-05 (usually higher; more conservative) |

> Note: `asset_coverage_pct` here is the leverage-analysis ratio. It is distinct from the
> regulatory `asset_coverage_ratio` in §8 used for the I1 reasonableness check — keep both.

---

## Validation rules → fields they touch

| Rule | Tier | Fields |
|---|---|---|
| C1 balance sheet | identity | total_assets = total_liabilities + total_net_assets |
| C2 NAV math (+ unit auto-detect) | identity | class_nav_per_share = class_net_assets ÷ class_shares_outstanding |
| C3 class sums | identity | Σ class_net_assets = total_net_assets |
| C4 fair-value sum | identity | fv_level_1+2+3 + fv_nav_practical_expedient = fv_total |
| C5 income identity | identity | net_investment_income = total_investment_income − total_expenses |
| C6 net-asset roll-forward | identity | ending = beginning + capital_raised − repurchases + net_increase_ops − distributions_declared |
| C7 income-components sum | completeness (flag-keep) | interest + pik_interest + dividend + other = total_investment_income. **Flag-keep, not reject**: a shortfall usually means a filer broke out an income line we didn't capture (e.g. fee income), so it doubles as a "missing component" detector rather than proof of error. |
| I1 asset coverage | reasonableness (flag-keep) | asset_coverage_pct ≥ 150% |
| I2 leverage range | reasonableness | leverage_ratio |
| A1–A3, T1–T3 | reasonableness / temporal | net assets > 0; NAV & share-count ranges; period-over-period swings |

---

## Open decisions before locking v1

1. ~~Derived-field formulas in §10~~ — ✅ Confirmed 2026-06-03.
2. ~~Financial-highlights ratios as-tagged vs recomputed~~ — ✅ Confirmed 2026-06-03:
   store **as-tagged** from XBRL; recompute only as a cross-check.
3. **OPEN** — Brian reviewing filings for fields to trim or add:
   - Any fields above **not** wanted for the pilot (trim scope)?
   - Anything missing wanted for comparative research?
   - Added 2026-06-05: investment-income components (interest, PIK interest, dividend,
     other) + C7 sum check + pik_income_ratio derived metric.
   - Added 2026-06-05: non-accruals on both bases (investments_at_cost,
     non_accrual_at_cost) + non_accrual_pct_fv and non_accrual_pct_cost derived metrics.
