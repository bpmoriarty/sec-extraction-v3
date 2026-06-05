# SEC Filing Extraction — Project Status

## Project Goal

Extract structured financial data from SEC filings (HTML format) across a broad
set of filers and filing types. The long-term aim is a scalable pipeline that can
handle many filers with minimal per-filer manual work.

---

## Current State

**Phase: Pipeline built end-to-end (extractor → validation → per-filing JSON via a resumable runner). Now set up & verified on a second (Morningstar corporate) machine; repo synced to GitHub. Pending: full volume run (Brian to kick off) + 2 validation refinements.**
**Last Session: 2026-06-05 (session 3)**

### What's Working
- Virtual environment set up (`uv venv` inside `sec-extraction-v3/`)
- **Runs on the Morningstar corporate machine** (set up 2026-06-05 session 3). The
  corporate network does SSL inspection, which blocked all EDGAR HTTPS calls
  (`SSLVerificationError: CERTIFICATE_VERIFY_FAILED`). Fixed by adding
  `configure_http(use_system_certs=True)` right after `set_identity()` in every
  EDGAR-touching script (uses the Windows cert store, which trusts the corporate
  root CA; harmless on home networks). Smoke test passed: AB Private Lending,
  4 filings extracted + validated, balance sheet reconciles.
- **Git + GitHub set up on this machine** — repo cloned from
  `github.com/bpmoriarty/sec-extraction-v3`, git installed via winget, local git
  identity set, credentials saved via Git Credential Manager, `master` tracks
  `origin/master`. The SSL fix is committed (`5f4d321`) and pushed.
- `src/fund_universe/build_universe.py` — seeds from 1,895 existing filenames +
  queries EDGAR N-23C3A form type → outputs 324 funds (202 interval, 98 ncsr, 24 BDC)
- `src/fund_universe/enrich_from_mstar.py` — merges Morningstar's 508-fund list
  against universe by CIK (pass 1) then by name (pass 2) → 532 funds total
- `data/fund_universe.csv` — master fund list, **547 funds** (15 added 2026-06-03
  from the Morningstar categorization workbook; see below)
- `src/fund_universe/add_vehicle_type.py` — matches funds against the four tabs of
  `semiliquid fund categorization Mstar.xlsx` (CIK-first, fuzzy-name fallback) and
  writes a **`vehicle_type`** column plus `mstar_ticker`, `isin`,
  `morningstar_category`, `us_category_group`, `morningstar_category_broad_group`.
  Vehicle Type breakdown: Interval Fund 171, Tender Offer Fund 155, Unlisted BDC 73,
  Unlisted REIT 42, unknown 106
- `src/extraction/bdc_xbrl.py` — **BDC XBRL extractor (pilot front-end), increments 1–5.**
  Maps us-gaap concepts → the FilingExtraction schema, pulling actual-dollar values for
  the current period. Hand-validated across Apollo/Blackstone/Ares/HPS latest 10-Q.
  Run: `uv run python src/extraction/bdc_xbrl.py` (prints a coverage report).
  **Extracting & validated:** metadata/dates/period; balance sheet (C1); per-class NAV
  (C2, C3); income statement + components incl. PIK (C5, C7); fees (mgmt, incentive);
  investments_at_cost; asset_coverage_ratio (tagged) + weighted_avg_interest_rate;
  distributions_declared; derived = leverage, net_debt, asset_coverage_pct,
  portfolio_mark, pik_income_ratio, distribution_coverage_ratio.
  **Finding:** all 4 funds show distribution coverage 0.89–0.98 (slightly over-distributing).
- `src/validation/rules.py` — **validation layer.** `validate(extraction)` runs identity
  checks C1–C7 (+ unit-error auto-detect in C2) and reasonableness checks (I1 asset
  coverage, I2 leverage, A1 net assets, A2 NAV range), fills `validation_checks` +
  `review_flags`, sets `validation_status` (pass/review). Flag-and-keep: identity fails are
  serious, reasonableness fails keep the value. Missing-input rules are "skipped" (so C4/C6
  are ready when fair value / roll-forward extract).
- `src/extraction/run_extraction.py` — **resumable runner.** Iterates BDC funds → all
  10-K/10-Q since 2016 → extract → validate → one JSON per filing in `data/extracted/`
  (gitignored). Skips existing (resumable), writes per-filing (crash-safe), per-filing
  try/except. CLI: `--max-funds`, `--max-filings`, `--since-year`.
  **Full run not yet executed** — `uv run python src/extraction/run_extraction.py`.
- `src/downloader/initial_pull.py` — downloads all historical filings since 2016
  for ~334 funds with CIKs; **complete as of 2026-06-03 — 7,229 files downloaded**
- `src/downloader/update_pull.py` — periodic check for new filings since each
  fund's `last_checked` date; ready to use after initial pull completes
- `README.md` — full setup and usage instructions for the pipeline
- `docs/DATA_DICTIONARY.md` — the **what-we-collect spec** (Extraction Phase 0).
  Grounded in a 2026-06-04 XBRL spike on Apollo/Blackstone/Ares/HPS 10-Qs that
  confirmed balance sheet (incl. per-class NAV/shares), income, statement of changes,
  financial highlights, fair-value hierarchy, and schedule of investments are all
  tagged. Derived-field formulas confirmed; holdings = summary now / detail later.
- `src/schema/models.py` — pydantic schema mirroring the data dictionary; every value
  wrapped in a `Fact` (value + source xbrl/llm/computed + confidence). Validates and
  round-trips (`uv run python src/schema/models.py`).

### What's Not Done Yet
- No-CIK funds still excluded from downloader until CIKs are sourced. This now
  includes the 15 funds added 2026-06-03 (blank CIK + "needs CIK sourcing" note);
  automated name→CIK lookup via edgartools was tested and found unreliable for
  these unlisted/private funds, so CIKs must be sourced manually.
- 106 funds remain `vehicle_type = unknown` (not on any Morningstar tab).
- Borderline ISIN/ticker: ~5 fuzzy master/feeder name matches (e.g. TCW
  Spirit←Star) have correct vehicle_type but possibly a sibling entity's
  ISIN/ticker — not yet reviewed.
- Extraction work not yet started (plan locked — see "Extraction Plan" below).

---

## What We Learned from Prior Work

### Old project (`old - SEC Filing Extraction/`)

This is the most complete prior attempt. Key things that were built and worked well:

**Format classification system** — The single most valuable idea from prior work.
195 filer profiles were grouped into 6 extraction groups based on how their HTML
is structured:

| Group | Name | Count | Approach |
|-------|------|-------|----------|
| 1 | BDC XBRL (10-K/10-Q) | 23 | HTML table + optional XBRL fallback |
| 2 | N-CSR Standard Inline NAV | 130 | Single BS table, NAV at bottom |
| 3 | N-CSR Separate NAV Table | 19 | BS table + separate NAV table |
| 4 | N-CSR Embedded NAV Formula | 13 | Regex to parse NAV from label text |
| 5 | N-CSR No Per-Share NAV | 7 | BS fields only |
| 6 | N-CSR Text-Dense | 3 | Custom text parser (Fundrise) |

The key insight: group by structure, not by filer name. Within a group, only
small per-filer config differences remain (amounts_in_thousands, label variants,
share class count, etc.).

**Working extractor code:**
- `group1_bdc_xbrl_extractor.py` — handles BDC 10-K/10-Q filings
- `group2_ncsr_inline_nav_extractor.py` — handles most N-CSR filings
- Both include: HTML parsing, field extraction with confidence scoring,
  multi-class NAV, thousands scaling, and validation checks (C1–C5)

**Format detector** (`format_detector.py`) — maps filenames to filer names
and format profiles using a prefix lookup table.

**195 format profiles** in `skills/sec-extraction/formats/` — one .md file per
filer documenting the exact structure, quirks, and config for that filer.

**Validation rules** (C1–C5):
- C1: Balance sheet equation (Assets = Liabilities + Net Assets)
- C2: NAV calculation per share class (Net Assets / Shares = NAV)
- C3: Sum of class net assets = total net assets
- C4: Income statement equation (Income - Expenses = Net Investment Income)
- C5: Fair value hierarchy sum (L1 + L2 + L3 = Total)

**What data was extracted:**
- Balance sheet: total assets, total liabilities, net assets, investments at
  fair value, cash, total debt
- NAV per share: per class — net assets, shares outstanding, NAV per share
- Income statement: total investment income, expenses, net investment income,
  plus optional line items
- Fair value hierarchy: Level 1/2/3 totals for investments

**Filename convention used:** `FundName_CIK_FilingType_Date.htm`
(e.g., `Cliffwater_Corporate_Lending_Fund_0001735964_N-CSRS_2022-12-09.htm`)

### Archived pandas project (`archived - SEC Filing Extraction - Pandas/`)

Earlier, simpler attempt focused on Cliffwater filings only. Used pandas for
table extraction. Good as a starting point but too narrow in scope.

---

## What to Bring Forward (Do Not Rewrite from Scratch)

The following are worth adapting into the new codebase:

1. **The 6-group classification framework** — keep as the core design principle
2. **The extraction logic from group1 and group2 extractors** — battle-tested
   against 195 real filers; adapt rather than rewrite
3. **The validation rules (C1–C5)** — already correct and reusable
4. **The format profiles** — 195 .md files document real filer quirks; reference
   when onboarding new filers

---

## What to Do Differently This Time

- Better folder structure and habits from the start
- Clearer scope — define what we're building before writing code
- Incremental — get one group working end-to-end before expanding
- Better output — decide on the target format (CSV, database, etc.) upfront

---

## Decisions Made

- Old folders kept as reference, not deleted
- New project folder: `sec-extraction-v3/`
- Shared filings data stays in the parent `filings/` folder
- edgartools is a Python library (not an MCP) — installed via `uv pip install edgartools`
- CIK is the primary identifier for all funds — name-based EDGAR lookup is too
  unreliable for production use
- Interval funds discovered via N-23C3A EDGAR form query (only interval funds
  file this form)
- 198 no-CIK Morningstar funds added as category "unknown" — excluded from
  downloader until CIKs are obtained
- Sub-category classification (BDC / REIT / interval / tender offer) deferred
  until after the downloader is built — classification matters for extraction,
  not for downloading
- Vehicle Type classification source of truth = the 4 tabs of `semiliquid fund
  categorization Mstar.xlsx` (Interval Funds, Tender Offer Funds, Unlisted BDCs,
  Unlisted REITs). Stored in the `vehicle_type` column; funds on no tab = "unknown"
- Confirmed (2026-06-03): edgartools `find_company` / full-text search are
  unreliable for unlisted/private fund name→CIK lookup → CIKs sourced manually
- OneDrive requires `--link-mode=copy` for all `uv pip install` commands
- Form types per category: `interval_fund` → N-CSR, N-CSRS, N-23C3A;
  `ncsr_fund` → N-CSR, N-CSRS; `bdc`/`reit` → 10-K, 10-Q, SC TO-I
- 10-year lookback (START_DATE = 2016-01-01) for initial pull
- `update_pull.py` uses per-fund `last_checked` as the since-date cutoff;
  falls back to 2016-01-01 if a fund has never been checked
- Corporate-network EDGAR access uses `configure_http(use_system_certs=True)`
  (the secure, portable option) rather than disabling SSL verification — chosen
  over `verify_ssl=False` and over a manual CA bundle path

---

## Extraction Plan (locked 2026-06-03)

Full plan file: `C:\Users\brian\.claude\plans\while-i-work-on-concurrent-stardust.md`

- **Methodology: Hybrid** — locate financial tables deterministically → extract with
  Claude → validate with math (the C-rules). For **BDCs, XBRL-first** via edgartools
  (10-K/10-Q are XBRL-tagged), LLM/HTML as fallback.
- **Two pipelines, one spine:** BDC (XBRL) and interval-fund (HTML) are different
  extraction front-ends that share schema, validation, review-queue, and output.
- **Pilot: BDCs first** (~25 funds). Interval funds are a later, harder phase.
- **Comprehensive schema** defined first (Phase 0), keyed on **reporting date
  (period-end)** sourced from EDGAR `period_of_report` / XBRL metadata — NOT the
  filing date in the filename. A one-time manifest maps file → CIK → both dates.
- **Accuracy:** hand-checked gold set (~15–25 filings) as the yardstick.
- **Anomaly policy: flag-and-keep** — only accounting-identity failures are
  rejected/fixed; reasonableness failures keep the value + raise a review flag.
- **Validation identities to build:** C1 balance sheet, C2 NAV (with auto unit-error
  detection), C3 class sums, **4-bucket fair-value sum (L1+L2+L3+NAV-practical-
  expedient=total)**, **income-statement identity (promoted to Critical)**, **net-asset
  roll-forward**. Broad reasonableness set (expense ratios, yields, repurchase bands)
  = future.
- **Format-routing idea (future, low priority):** filing agent / financial printer
  (DFIN, Toppan, Broadridge) and auditor as signals for HTML format family.

---

## Extractor — XBRL learnings (for resuming `bdc_xbrl.py`)

Hard-won quirks discovered while building. Read before extending the extractor.

- **`numeric_value` is actual dollars** — XBRL scale is already applied; no thousands
  normalization needed.
- **Map by us-gaap CONCEPT, not rendered label.** `by_concept` is fuzzy → match exactly.
  Use a candidate list per field (first hit wins); leave null for the LLM fallback.
- **`scalar()` must filter `period_type=='instant'`** — otherwise a duration concept's
  prior-period row gets mis-picked (this bug hit distributions + interest rate). Use
  `scalar_any()` for ratio fields that may be instant OR duration.
- **Duration facts:** pick the row with `period_end == reporting_date` and length closest
  to target_months (3 for 10-Q, 12 for 10-K). Ignores 10-Q year-to-date rows.
- **Dimensioned facts — two patterns handled:**
  - **Share class** (`dim_us-gaap_StatementClassOfStockAxis`): per-class NAV/shares/NAV-PS.
    Filter to single-axis rows so class×consolidation cross-tabs don't leak. Normalize
    members like `bcred:CommonClassIMember` → `I`.
  - **Investment affiliation** (`dim_us-gaap_InvestmentIssuerAffiliationAxis`): some filers
    (Blackstone) split income components by affiliation instead of one total → sum across
    members. `duration_scalar` tries undimensioned total first, then affiliation-sum.
- **Cash sum fallback:** filers reporting `us-gaap:Cash` + `CashEquivalentsAtCarryingValue`
  separately (HPS) are summed when the combined concept is absent.
- **Combined share-class members:** some filers tag a combined member (e.g. AB Private
  Lending's `ClassSDAndISharesMember` → "SDAndI"). `_normalize_class` drops members whose
  normalized form contains "and".
- **Debt / cash sum fallbacks:** filers reporting components separately instead of one
  combined line are summed — cash = `Cash` + `CashEquivalentsAtCarryingValue` (HPS); debt
  = `LineOfCredit` + `OtherLongTermDebt` + `NotesPayable` (AB Private Lending).

### Pending validation refinements (found 2026-06-05, not yet done)
1. **Expenses gross vs net of waivers.** Dictionary defines `total_expenses` as net of
   waivers. For AB the candidate order grabbed the GROSS line (`InvestmentIncomeInvestmentExpense`,
   5,104,973) but NII uses the NET-of-waiver figure (`InvestmentCompanyExpenseAfterReductionOfFeeWaiverAndReimbursement`,
   4,584,326). Prefer the net concept — but verify it doesn't shift Blackstone/HPS (which
   currently pass). The waiver itself is the §11 `expense_support_net` field.
2. **C5 ignores income tax.** NII = TII − expenses − **tax**. RIC-compliant BDCs have ≈0
   tax, but funds with excise tax (AB: 44,575) fail C5 by the tax amount. Either extract
   income tax and make C5 tax-aware, or treat tax-payers as expected flags.
   (AB C5 currently fails: 8,165,022 − 5,104,973 = 3,060,049 vs NII 3,536,121; the gap =
   waiver 520,647 + tax 44,575 − reconciles exactly.)

- **Per-filer / dimensional long tail = LLM-fallback territory:** income components &
  total_expenses on some filers; fair-value hierarchy (§6, dimensional + custom `ck...`
  concepts); statement-of-changes roll-forward (custom repurchase concepts); per-class
  financial highlights (§7 — turnover/total-return are share-class-dimensioned, expense/NII
  ratios often 10-K only). C7/C-checks flag the unreliable cases.

---

## Next Steps

1. ~~**Wait for initial pull to complete**~~ — Done. 7,229 files downloaded 2026-06-03.
2. ~~**Apply sub-category labels**~~ — Done 2026-06-03. `vehicle_type` column added
   from the Morningstar categorization workbook.
3. **Source CIKs** for no-CIK funds (now includes the 15 added 2026-06-03). Manual
   lookup needed — automated name→CIK confirmed unreliable for these funds.
4. ~~**Phase 0 (schema/data dictionary)**~~ — Done 2026-06-04. One open item: Brian
   reviewing filings for fields to trim/add (decision #3 in the data dictionary).
5. ~~Build the BDC XBRL extractor~~ — In progress, increments 1–5 done (see "What's
   Working" + "XBRL learnings"). Core financials extract & hand-validate on 4 funds.
6. ~~**Pipeline:** validation + JSON output + resumable runner~~ — Done 2026-06-05.
   Surfaced coverage fixes (class junk, debt sum, expense concepts) also applied.
7. **Full volume run (Brian to kick off):** `uv run python src/extraction/run_extraction.py`
   (no limits) over all BDCs × 10 years → the coverage/failure distribution that tells us
   what to fix next. Long-running (network-bound). Then analyze `data/extracted/`.
8. **Two validation refinements** (see "Pending validation refinements" above): prefer
   net-of-waiver expenses; make C5 tax-aware.
9. **After that:** harder XBRL (fair value C4, roll-forward C6, composition) and/or LLM
   fallback, informed by the volume run; then gold-standard set + spreadsheet assembler.
8. *(Optional)* Review borderline ISIN/ticker on the ~5 fuzzy master/feeder matches.

---

## Session Log

| Date | What Happened |
|------|---------------|
| 2026-06-05 (session 3) | **Set up the project on the Morningstar corporate machine + synced to GitHub.** Installed git (winget). Cloned the repo. Verified the venv/deps. Smoke-tested the runner (`--max-funds 1 --max-filings 2`) → 0 filings: diagnosed as corporate SSL inspection blocking EDGAR (`CERTIFICATE_VERIFY_FAILED`). Fixed with `configure_http(use_system_certs=True)` after `set_identity()` in all 6 EDGAR-touching scripts (+ rules.py self-test); re-ran smoke test → 4 AB Private Lending filings extracted & validated (balance sheet reconciles; status=review as expected per the known C5 waiver/tax item). Committed (`5f4d321`) and pushed to `origin/master`; set local git identity + upstream tracking. Pandas 3.0.3 noted as a watch item. Next: Brian kicks off the full volume run. |
| 2026-06-05 (session 2) | **Built the pipeline.** Added `src/validation/rules.py` (C1–C7 + reasonableness, flag-and-keep; missing inputs = skipped) and `src/extraction/run_extraction.py` (resumable runner → per-filing JSON in `data/extracted/`, gitignored). Tested on 2 funds (8 filings written; a new fund, AB Private Lending, extracted & passed — generalization signal). Volume test surfaced 3 coverage gaps, all fixed: combined class members ('SDAndI'), missing total_debt (debt sum fallback), expense concepts (recovered AB + Blackstone). New finding logged: C5 gross-vs-net-of-waiver expenses + income tax (2 pending refinements). Full volume run not yet executed — Brian to kick off. |
| 2026-06-05 (session 1) | **Built BDC XBRL extractor (`bdc_xbrl.py`), increments 1–5.** Concept-mapped balance sheet, per-class NAV, income statement + components (incl. PIK), fees, investments_at_cost, asset coverage, interest rate, distributions; computed derived metrics. Hand-validated C1/C2/C3/C5/C7 on Apollo/Blackstone/Ares/HPS latest 10-Q. Handled two dimension patterns (share-class, investment-affiliation) and fixed two bugs (affiliation-split income; instant-vs-duration period leakage). Decided to pivot to the pipeline (validation + JSON output + runner) to de-risk time/fund-set/volume coverage before more (harder) XBRL or LLM fallback. See "XBRL learnings" section. |
| 2026-06-04 | **Extraction Phase 0.** Ran an XBRL spike (Apollo/Blackstone/Ares/HPS 10-Qs) confirming the data is comprehensively tagged — incl. per-class NAV/shares, statement of changes (roll-forward), financial-highlights ratios, fair-value hierarchy, and full schedule of investments. Decided holdings = summary now / detail later. Drafted `docs/DATA_DICTIONARY.md` and built `src/schema/models.py` (pydantic, Fact-wrapped values w/ provenance+confidence). Confirmed derived-field formulas (leverage, asset coverage, distribution yield, net debt) and as-tagged highlights. One open item: trim/add field review. |
| 2026-06-03 (session 2) | **Categorization + extraction planning.** Confirmed initial pull complete (7,229 files). Built `add_vehicle_type.py` → added `vehicle_type` + Morningstar fields (ISIN, category, etc.) to all funds by CIK-first/name-fallback matching against the 4-tab categorization workbook. Found 18 Morningstar funds unrepresented in the universe; added 15 genuinely-new ones (blank CIK, needs sourcing), skipped 3 as duplicates/feeders. Universe 532 → 547. Separately, **locked the extraction plan** (hybrid XBRL-first BDC pilot; comprehensive schema; gold-set accuracy; flag-and-keep validation; new identity rules incl. 4-bucket fair-value and net-asset roll-forward). |
| 2026-06-03 (session 1) | Initial pull confirmed complete — 7,229 filings downloaded. |
| 2026-06-02 (session 2) | Built `initial_pull.py` and `update_pull.py`. `initial_pull.py` started and running — downloads 10 years of filings for ~334 funds (N-CSR/N-CSRS/N-23C3A for interval funds; N-CSR/N-CSRS for ncsr; 10-K/10-Q/SC TO-I for BDCs and REITs). `update_pull.py` ready for periodic use. Added `README.md` with setup and usage docs. Next: wait for initial pull to finish, then begin extraction work. |
| 2026-06-02 (session 1) | Built fund universe pipeline. `build_universe.py` seeds from existing filenames + queries EDGAR N-23C3A → 324 funds. `enrich_from_mstar.py` merges 508-fund Morningstar list by CIK then by name → 532 funds total. 198 funds have no CIK and are marked "unknown". Downloader not started — paused to commit. |
| 2026-06-01 | Project started. Read all old code. Created this folder and status file. |
