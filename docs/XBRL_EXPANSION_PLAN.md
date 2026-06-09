# XBRL Coverage Expansion — Plan (pre-LLM increment)

_Scoped 2026-06-09 (session 8). Motivation: a tag-universe audit (12-fund sample of
latest 10-Ks) found we extract ~83 us-gaap concepts out of ~1,897 present; ~562 untracked
us-gaap **numeric** line items remain (after filtering narrative TextBlocks). Several are
high-prevalence, fully-structured, and would complete things we already half-built
(`liquidity_coverage`, the dropped C6 roll-forward). The LLM stage stays reserved for what
is genuinely NOT in XBRL: industry, security seniority, maturity dates, non-accrual $._

## Principles (unchanged from prior work)
- Use the existing `FactSet` machinery (`scalar` / `duration_scalar` / `per_class`); no LLM, no
  workflow change.
- Candidate concept lists (first-hit), current-period filtering, OR-logic on validation (only
  ADD pass paths so nothing regresses), degrade-to-null on thin coverage. No per-CIK code.
- Each theme = its own commit, independently revertable via `git revert <sha>`.
- Validate each theme on a ~3-fund spot re-extract as we go; ONE clean full re-run +
  spreadsheet rebuild at the end (not six).

## Fact shapes confirmed by probe (Apollo / Blackstone / HPS latest 10-K)
- **Cash flow** — undimensioned `duration`, 3 comparative periods per 10-K → existing
  `duration_scalar(concepts, target_months)` current-period filter handles it. LOW risk.
- **Share activity** — `duration`, dimensioned by share-class axis (already handled) → sum
  across classes. LOW risk.
- **Credit facility** — `LineOfCreditFacilityRemainingBorrowingCapacity` IS tagged (direct
  undrawn amount), but cross-tabbed across TWO axes (CreditFacility + LegalEntity/SPV) with an
  undimensioned total that DOUBLE-COUNTS (Apollo: 12.9B undimensioned vs ~3.45B real revolver).
  HIGHEST risk — needs a reconciliation gate.

---

## Theme 1 — Credit-facility capacity  (priority #1; HIGHEST risk)
- **Schema:** `LiquidityObligations.undrawn_debt_capacity` (exists, empty). Populating it
  auto-computes `DerivedMetrics.liquidity_coverage = (cash + undrawn) / unfunded_commitments`
  (already defined).
- **Concepts:** `us-gaap:LineOfCreditFacilityRemainingBorrowingCapacity` (undrawn),
  `…MaximumBorrowingCapacity` (total), `us-gaap:LineOfCredit` (drawn — already summed).
- **Approach (anti-fragile):** prefer the UNDIMENSIONED `RemainingBorrowingCapacity` only if it
  reconciles: `max ≈ drawn + remaining` within tolerance. If the undimensioned value
  double-counts (fails reconciliation), fall back to summing the CreditFacility-axis values (ONE
  axis only, never both), re-check; if still off → NULL (degrade). `liquidity_coverage` computes
  only when `undrawn` is trustworthy.
- **Validation:** new **C8 (reasonableness, flag-and-keep):** `max_capacity ≈ drawn + undrawn`.
- **Risk:** high — review a few funds before trusting broadly.

## Theme 2 — Cash-flow statement  (priority #2; LOW risk)
- **Schema:** NEW `CashFlowStatement` section: `net_cash_operating`, `net_cash_financing`,
  `net_cash_investing`, `interest_paid`, `investment_purchases`, `investment_sales`,
  `net_change_in_cash`.
- **Concepts:** `NetCashProvidedByUsedInOperating/Financing/InvestingActivities`,
  `InterestPaidNet`, `PaymentsForPurchaseOfInvestmentOperatingActivity`,
  `ProceedsFromDispositionOfInvestmentOperatingActivity`,
  `CashCashEquivalents…PeriodIncreaseDecrease…`.
- **Validation:** new **C9 (identity):** `operating + investing + financing ≈ net_change_in_cash`.
- **Bonus:** purchases/sales give a real portfolio-turnover signal.

## Theme 3 — Capital share activity  (priority #3; LOW risk)
- **Schema:** extend `StatementOfChanges`: `shares_issued_new`, `proceeds_new_issues`,
  `shares_issued_drip`, `value_drip`, `shares_repurchased`.
- **Concepts:** `StockIssuedDuringPeriodShares/ValueNewIssues`, `…DividendReinvestmentPlan`,
  `StockRepurchasedDuringPeriodShares` (sum across share classes).
- **Stretch:** these are the DRIP + issuance pieces missing when C6 failed. After capturing them,
  re-test whether C6 reconstructs; revive as flag-and-keep if it does for a healthy share of
  filers, else keep the data and leave C6 dropped (no regression either way).

## Theme 4 — Balance-sheet detail  (priority #4; LOW risk)
- **Schema:** extend `BalanceSheet`: `interest_receivable`, `distribution_payable`,
  `payable_for_investments`, `receivable_for_investments`, `management_fee_payable`,
  `interest_payable`, `other_assets`, `additional_paid_in_capital`, `accumulated_deficit`.
- **Validation:** capture `us-gaap:LiabilitiesAndStockholdersEquity` as a free C1 cross-check
  (should equal `total_assets`).

## Theme 5 — Expense breakdown  (priority #5; LOW risk)
- **Schema:** extend `FeesExpenseSupport`: `administrative_fees`, `professional_fees`,
  `other_g_and_a`, `interest_expense`, `amortization_of_financing_costs`.
- **Concepts:** `AdministrativeFeesExpense`, `ProfessionalFees`,
  `OtherGeneralAndAdministrativeExpense`, `InterestExpenseDebt`, `AmortizationOfFinancingCosts`.
- **Validation:** new **C10 (reasonableness):** captured components sum ≤ `total_expenses`.
  `interest_expense` ($) independently useful as the dollar cost of debt.

## Theme 6 — Tax-basis investment data  (priority #6; LOW risk)
- **Schema:** NEW `TaxBasis` section: `tax_cost_of_investments`, `tax_unrealized_appreciation`,
  `tax_unrealized_depreciation`, `tax_unrealized_net`, + distributable-earnings components
  (undistributed ordinary income, LT cap gains).
- **Mostly 10-K** (annual). **Cross-check:** `gross_apprec − gross_deprec ≈ net`.

---

## Tabled for now (per Brian, 2026-06-09)
- **Derivatives** (Derivative Asset/Liability NotionalAmount, UnrealizedGainLossOnDerivatives).
- **Level-3 fair-value roll-forward** (UnobservableInputsReconciliation: purchases, transfers
  in/out of L3, gain/loss in earnings).

## Cross-cutting deliverables (every theme)
- Spreadsheet: new columns under a section header on the Data tab + Definitions-tab entries
  (formula in exact column names).
- Validation codes (C8/C9/C10) added to the Review-tab key, tagged Identity vs Reasonableness.

## Sequencing & rollback
1. Implement themes 1→6 in priority order, each its own commit.
2. After each: spot re-extract ~3 funds to confirm + check no regression.
3. After all six: ONE clean full re-run + spreadsheet rebuild + PROJECT_STATUS update.
4. Rollback per-theme via `git revert <sha>`.
