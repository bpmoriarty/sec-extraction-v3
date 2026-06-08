# SEC Filing Extraction — Project Status

## Project Goal

Extract structured financial data from SEC filings (HTML format) across a broad
set of filers and filing types. The long-term aim is a scalable pipeline that can
handle many filers with minimal per-filer manual work.

---

## Current State

**Phase: C4 cash/MMF plug landed (session 6) — Brian's hypothesis confirmed. Clear-rate now
251 pass / 49 review of 300 (84%). C4: 233 pass / 15 fail / 52 skip (was 220/28/52); the
near-miss gap was the measured-at-NAV bucket (money-market / alternative investments) tagged
OUTSIDE the L1/L2/L3 hierarchy — now captured. Cleared Blue Owl Tech (6) + Crescent (7);
remaining 15 fails = First Eagle 10 (only L3 tagged — LLM territory), PGIM 4 (tiny non-cash
residual ~0.3%), Antares 1 (levels OVERSHOOT — different issue). C6 roll-forward: extracted the
§5 DATA (beginning/ending net assets, capital_raised, repurchases) but DROPPED the check — the
roll-forward isn't reconstructable from XBRL (94/100 inputs-present filings failed, median gap
2.3%); data kept for the spreadsheet. All session-5 wins hold; 0 regressions. Use `--revalidate`
for rule-only changes; extractor changes need a CLEAN full re-run (delete data/extracted first —
the runner SKIPS existing JSONs).
Next: SPREADSHEET ASSEMBLER (Brian wants a clean confident dataset to analyze BEFORE the LLM/HTML
fallback expands coverage). Flag-and-keep values (C4/C5/etc.) must be visibly marked in output.**
**Last Session: 2026-06-07 (session 6)**

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
  try/except. CLI: `--max-funds`, `--max-filings`, `--since-year`, **`--revalidate`**
  (re-run validation over existing JSONs in place — no re-extraction/network — for
  validation-rule changes; rewrites validation fields + regenerates the review index).
  **Two full runs done (2026-06-05 session 4); see "Run 2" below.**
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

### Full Volume Run Results (2026-06-05 session 4)

Ran `run_extraction.py` (no limits) over all BDCs × 10 years. Run summary:
`written 295 | skipped 8 | review 151 | no_xbrl 126 | errors 1`. **24 BDC funds**
produced output. Analysis of `data/extracted/` + `_errors.log` + `review_queue/index.txt`:

- **126 `no_xbrl` are NOT a bug — they are pre-inline-XBRL filings.** Every single
  failed filing is dated **2022 or earlier**; each fund extracts cleanly from the point
  it began tagging inline XBRL (Blackstone 2022-06+, Apollo 2023-03+, HPS 2022-12+) and
  fails only on its earliest filings. **Only 1 of 24 funds (NC SLF Inc, CIK 0001844684)
  has zero XBRL anywhere**; the other 13 are partial (early-life only). → recent-data
  XBRL coverage is essentially complete; the historical gap is LLM/HTML-fallback territory.
- **The review queue (151) is two very different populations:**
  - **~79 filings flag *only* C5 NII** — the known gross-vs-net-of-waiver + excise-tax
    issue. Values are correct; the identity check is naïve. **The 2 refinements below
    clear these.**
  - **16 filings are *structurally broken*** (C1 balance-sheet off by hundreds of
    millions, negative net assets, negative computed NAV) — concentrated in **3 filers**:
    First Eagle Private Credit (CIK 0001890107, ~8+7), Terra Income Fund 6 (0001577134,
    ~5), Bain Capital Private Credit (0001899017, ~3). Per-filer concept-mapping misses —
    the long tail. **These are the only wrong *values*** and are the next fix after the refinements.
  - Remainder: NAV mismatch / income-component-sum / NAV-range flags (mix of cosmetic + real).
- **The 1 real `error`:** Golub Capital Private Credit Fund 10-Q 2024-12-31 →
  `KeyError('concept')` — a *recent* filing whose XBRL facts dataframe lacks the expected
  `concept` column. One filing; genuine bug to investigate.
- **Code observations:** `extract_filing` ([bdc_xbrl.py:365-368]) accesses `xbrl.facts`
  where `filing.xbrl()` can be `None`; the runner reclassifies the resulting
  `AttributeError` as `no_xbrl` by string-matching `"NoneType"` — works, but a
  `if xbrl is None:` guard would be cleaner.

### Run 2 — cumulative result of all session-4 fixes (2026-06-05)

After committing the C5 levers (`9918097`/`f98a7f9`/`c642efd`) + net-assets fix (`923c29b`),
re-ran the full extraction. **Authoritative counts read from the 300 JSONs** (the runner's
summary + `review_queue/index.txt` were skewed by a mid-run restart — trust the JSONs):

- **192 pass / 108 review of 300** (64% clean, up from 51% at session start).
- Arc across the 3 re-runs: pass ~144 → 176 → **192**; review 151 → 124 → **108**.
- **Failing checks (filings flagging each rule, deduped per filing):**
  C2 NAV-per-share **33** · C5 NII **30** · C7 income-components **27** · A2 NAV-range **18**
  · A1 net-assets-positive **5** · C3 class-sum **4** · C1 balance-sheet **3**.
- **Qualitative win:** the hard correctness failures cleared — C1 12→3, A1 11→5, C5 51→30.
  Terra now *truly* passes; First Eagle's C1/A1 fixed (its ~10 remaining = the per-class
  NAV sign residual we left flag-and-keep). Remaining queue is **softer** (derived NAV /
  completeness), not wrong balance sheets.
- `no_xbrl` steady at 126 (pre-2022 filings); `errors` 0.
- Run 1 (`9918097`) outputs preserved at `data/extracted_run1_9918097/` +
  `review_queue/index_run1_9918097.txt`; original run at `data/extracted_OLD_pre-9918097/`.

**C2 fix landed (`d9aac9e`) — rounding-aware NAV tolerance.** Diagnosis: ~43 of the 51
flagged classes were rounding artifacts, not errors — filers tag net assets and/or shares
rounded to thousands, so for small classes (few thousand shares, or exact small counts with
thousand-rounded net assets) the recomputed NAV can't match the filer's exact reported NAV
to the cent. Replaced the flat $0.01 tolerance with one that propagates each input's own
rounding (`_nav_tol` in rules.py). Re-validated in place: **C2 fail checks 51→9, review
108→82, 0 regressions**; the 9 remaining are First Eagle's per-class sign issue (8, the
flag-and-keep residual) + 1 Oaktree borderline. Genuine errors (sign, ~1000× unit) stay
flagged.

**`--revalidate` runner mode (`5c6b622`).** Validation-rule changes only touch `validate()`,
not the extracted data — so `uv run python src/extraction/run_extraction.py --revalidate`
re-runs validation over the existing JSONs in place (rewrites validation fields + regenerates
the review index fresh), offline in seconds. Used it to land the C2 fix. Use this for any
future C-rule tweak instead of a full network re-run.

**Remaining review queue (49 filings, after session-6 C4 cash/MMF plug):** C5 30 (Antares-type,
no authoritative anchor — NII value correct, can't self-verify), C4 15 (First Eagle 10 = only L3
tagged; PGIM 4 = tiny non-cash residual; Antares 1 = levels overshoot), C2 9 (8 First Eagle
per-class sign residual + 1 Oaktree), A1 5, C3 4, C1 3, A2 1 (Ares unit error), C7 1 (Ares missing
cash-dividend). All flag-and-keep. Next: C6 roll-forward, then spreadsheet assembler.

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

### C5 validation — evolution & the three independent levers (2026-06-05 session 4)

C5 (NII reconciles) went through three changes this session. **Each is a separate
commit so any one can be reverted independently** (`git revert <sha>`):

1. **`9918097` — net-of-waiver expenses + tax-aware C5 (OR-logic).** Reordered the
   `total_expenses` candidates so the net-of-waiver concept beats the GROSS
   `InvestmentIncomeInvestmentExpense`; added the `income_tax_expense` field; made C5
   accept whichever of `(TII-exp)` / `(TII-exp-tax)` reconciles (tax is above-NII for AB,
   below-NII/on-gains for Blackstone). Cleared the AB-style waiver+tax flags. *Original
   "pending refinements 1 & 2" — now DONE.*
2. **`f98a7f9` — LEVER 1: tax capture component fix (extractor only).** Some filers
   (Oaktree) tag a tiny/partial `IncomeTaxExpenseBenefit` while the real tax is split
   `Current` + `Deferred`; use that sum when larger. Added `ExciseAndSalesTaxes` (Crescent)
   to the tax candidate list. **Cleared Oaktree 10-K.** *Deliberately did NOT reorder
   expenses to prefer the net concept over `OperatingExpenses` — diagnosis showed Antares
   tags `InvestmentCompanyExpenseAfterReductionOfFeeWaiverAndReimbursement`=1.3M (garbage
   vs ~60M real), so that would BREAK Antares.* **Roll back: `git revert f98a7f9`.**
3. **`c642efd` — LEVER 2: anchor cross-check (model + extractor + rules).** Recomputing NII
   from components is fragile for BDCs (expense support, offering-cost amortization,
   multi-component tax). C5 now also cross-checks NII against the filer's authoritative
   tagged subtotals — `InvestmentIncomeOperatingAfterExpenseAndTax` and
   `IncomeLossFromContinuingOperationsBeforeIncomeTaxes − tax` (two new IncomeStatement
   fields) — and passes if ANY reconciliation holds. **Cleared Crescent, Oaktree 10-Q,
   John Hancock.** **Roll back: `git revert c642efd`.**

**Safety property of levers 1 & 2:** both only ADD pass paths, so no filing that passed
C5 before can start failing — verified live on AB/Blackstone/HPS/Apollo (no regressions).

**Known residual:** Antares-style funds (no authoritative anchor tagged + gross-only
expense concept) still flag C5. Their NII *value* is correct (from `NetInvestmentIncome`);
the identity just can't self-verify. Flag-and-keep → value retained. This is the
irreducible long tail, not a data error.

> **OUTPUT REQUIREMENT (future — spreadsheet assembler, idea logged 2026-06-05 session 4):**
> Flag-and-keep means some values ship as "kept but flagged" (validation_status=`review`
> with `review_flags`) and may never get a code fix. **These MUST be visibly marked in the
> final spreadsheet output** so a consumer never mistakes a flagged value for a clean one.
> Options to decide later: conditional formatting (e.g. amber cells), a per-cell comment/
> note, and/or a companion "flags" column or status column per row. The data is already
> there to drive it — every `Fact` carries source/confidence and each filing carries
> `validation_status` + `review_flags`. Decision deferred; do NOT solve now.

### Net-assets concept fix — the 3 "structurally broken" filers (2026-06-05 session 4)

Diagnosis showed the 3 filers were NOT one shared bug but three situations, all resolved
with generic concept-list work (no per-CIK hacks):

- **`923c29b` — net-assets candidate reorder + LLC concepts (extractor only).** Both lists
  (`total_net_assets` + per-class `class_net_assets`) changed from `[AssetsNet,
  StockholdersEquity]` to `[StockholdersEquity, MembersCapital, MembersEquity, AssetsNet]`.
  - **First Eagle** mis-signs `us-gaap:AssetsNet` (−301.88M vs correct `StockholdersEquity`
    +301.88M) → now reconciles (C1) with positive net assets (A1).
  - **Terra Income Fund 6 LLC** tags `MembersCapital`/`MembersEquity` (not AssetsNet/SE) →
    now captured (was silently `None` → C1 skipped = a fake "pass").
  - Regression-tested across all 24 working funds: 22 byte-identical, **0 regressions**.
  - **Roll back: `git revert 923c29b`.**
- **First Eagle per-class residual (FLAG-AND-KEEP, not fixed):** First Eagle ALSO mis-signs
  its share-class-dimensioned `AssetsNet` (D=−102k, I=−301,778k), and tags no per-class
  `StockholdersEquity`, so C2/C3 still flag it. **The reported per-share NAV (24.23) is
  correct and stored** — only the derived per-class net-assets sign is wrong. Left flagged
  rather than adding a per-filer sign-flip heuristic (anti-fragility; revisit only if the
  same negative-per-class pattern shows up across multiple filers).
- **Bain (deferred):** its current 10-K is clean (AssetsNet = StockholdersEquity, positive,
  passes). Only some earlier-year filings flagged — a separate, lower-priority year-by-year
  check, not a concept-mapping bug.

### C7 income-completeness — PIK-band anchor (2026-06-07 session 5)

C7 originally demanded the income components (interest + PIK + dividend + other) sum EXACTLY
to total_investment_income. 27 filings failed, concentrated in 2 filers with OPPOSITE causes,
both about PIK (paid-in-kind) income:

- **Ares Strategic Income (10 filings): PIK double-counted.** Ares's interest line
  (`InterestIncomeOperating`) is **PIK-inclusive** — it already contains PIK interest — so
  adding `InterestIncomeOperatingPaidInKind` on top overshoots by exactly the PIK amount
  (gap = −PIK, the fingerprint across all 10).
- **Blue Owl Credit Income (12 filings): PIK dividend uncaptured.** Blue Owl's interest line
  is **PIK-exclusive**, and it breaks PIK out across overlapping concepts (us-gaap split +
  us-gaap combined `InterestAndDividendIncomeOperatingPaidInKind` + a custom `orcic:` combined).
  We captured only `InterestIncomeOperatingPaidInKind`, missing the **PIK dividend**
  (`DividendIncomeOperatingPaidInKind`) — exactly the shortfall.

**Key lesson:** the same PIK concept means different things to different filers (inclusive vs
exclusive of the cash interest line), so no single concept-list edit fixes both — they pull
opposite directions. **Fix = anchor on the authoritative total + PIK band (mirrors C5):**
let `core = interest + dividend + other` (PIK-free, consistent) and pass if
`-tol <= (total - core) <= pik_avail + tol`, where `pik_avail = max(pik_interest + pik_dividend,
pik_combined)`. PIK-inclusive filers → shortfall ~0 → pass; PIK-exclusive → shortfall ~ tagged
PIK → pass; a genuine NON-PIK missing income line → shortfall exceeds PIK → still fails.

- **Extractor:** added `pik_dividend_income` + `pik_income_combined` fields/concepts (additive;
  feed the C7 band only, not C5/derived). Gating in C7 unchanged (same 4 components required)
  so the rule only ADDS pass paths — nothing passing can regress.
- **Result:** C7 27→1, review 82→60, pass 218→240 (80%). The 1 residual = Ares 2026-03-31,
  which has `dividend_income = 0` (a genuinely missing cash-dividend line, NOT PIK) → correctly
  still flagged. **Regression-verified:** reverted C7, re-validated in place → every other rule
  (A1/A2/C1/C2/C3/C5) byte-identical; only C7 moved.

### A2 NAV-range — skip dormant share classes (2026-06-07 session 5)

A2 (per-class NAV within $1–$100) flagged 27 share-class rows. Diagnosis: **26 were
dormant/unfunded classes** — funds register multiple classes (A/D/I/S) on the XBRL share-class
axis, but some carry 0 shares + 0/None net assets, so their NAV extracts as **0.00**. A NAV of
0 on an empty class is not an out-of-range anomaly (the funded classes extract correctly —
$4.50/$24.40/$27.62/$20.22). The 27th was a genuine **~1000× unit error** (Ares 2023-06-30
class I, NAV 26,750 = 26.75×1000, with no net assets/shares).
**Fix (rule-only):** A2 now skips NAV of 0/None (`if not nav: continue`). Safe because a
genuinely funded class that mis-extracted to 0 would still be caught by the C2 identity
(net_assets/shares != 0). **A2 27→1** (keeps the Ares unit error), review 60→43, pass 240→257
(86%). Rule-only → applied via `--revalidate`; every other rule byte-identical (0 regressions).

### C4 fair-value hierarchy — now extracting (2026-06-07 session 5)

C4 (L1+L2+L3+NAV-practical-expedient = total) was always SKIPPED — the extractor never pulled the
hierarchy. Now it does. The data is dimensional and the hardest we've handled: investment fair
value is tagged on `dim_us-gaap_FairValueByFairValueHierarchyLevelAxis` (Level1/2/3 +
NetAssetValuePerShare members), CROSS-TABBED with asset-type and valuation-technique axes, and the
instant date lives in `period_instant` (`period_end` is NULL for these facts).

**Extraction (`FactSet.fv_hierarchy`, concept `us-gaap:InvestmentOwnedAtFairValue` →
`InvestmentsFairValueDisclosure`):** per level, PREFER the per-level TOTAL row (hierarchy axis is the
ONLY dimension — reconciles exactly); else FALL BACK to summing the asset-type breakdown (hierarchy +
exactly one other dim). The breakdown is often cross-tabbed, so a naive sum double-counts → the
fallback is SELF-CHECKED against the undimensioned total (= investments_at_fair_value, which doubles
as fv_total) and DISCARDED if it doesn't reconcile (TPG summed to 1.9× → skipped, not stored).
Result: **C4 220 pass / 28 fail / 52 skip; 18 funds reconcile; 0 regressions** (the change only ADDS
fair-value fields). The NAV-practical-expedient 4th bucket populates (HPS, Blackstone). **Reminder:
an extractor change needs a CLEAN full re-run — the runner SKIPS existing JSONs, so delete
`data/extracted/` first** (a no-op run cost us a cycle here).

**The 28 fails — KEPT-and-flagged (Brian's decision, 2026-06-07):** 4 funds with incompletely-tagged
hierarchies. First Eagle (10) tags only L3 (gaps 7–52%) = genuine LLM-fallback territory. Crescent
(7, missing L1), Blue Owl Tech (6), PGIM (4), Antares (1) are NEAR-misses (gap ~0–5%, have most/all
levels). **Leading hypothesis (Brian, from prior data-product experience): the reconciling plug is
cash / money-market funds / cash-equivalents, which filers often EXCLUDE from the fair-value table.**
We keep the partial buckets and flag C4=fail (rather than skip) so the gap stays visible and can be
plugged later. **OUTPUT REQUIREMENT: C4 non-reconcilers MUST be marked in the final spreadsheet like
every other flag-and-keep value** (conditional formatting / status column — see the output-requirement
note above).

**NAV-practical-expedient plug (2026-06-07 session 6) — Brian's cash/MMF hypothesis CONFIRMED.**
Of the 28 fails, the near-misses were the measured-at-NAV bucket (money-market / alternative
investments) tagged OUTSIDE the L1/L2/L3 hierarchy, under a DIFFERENT concept than the one we
extract: `us-gaap:AlternativeInvestment` (Blue Owl Tech — on the hierarchy NAV member) or a custom
`...MeasuredAtNetAssetValue` line (Crescent — undimensioned). `FactSet._nav_practical_expedient`
now finds it. SAFE BY CONSTRUCTION: the plug fires only when our buckets UNDERSHOOT the total, and
is kept ONLY if it then reconciles — so a filing that already balances can never break. (Tolerance
aligned to C4's 0.1%: an earlier 0.5% trigger left a DEAD ZONE that stranded 3 Blue Owl Tech filings
whose ~0.3% gap was too small to trigger the plug but too big to pass C4.) **Result: C4 28→15 fails
(Blue Owl Tech 6 + Crescent 7 cleared), pass 220→233, review 62→49, 0 regressions.** Remaining 15:
First Eagle 10 (only L3 tagged — genuine LLM/HTML territory), PGIM 4 (tiny ~0.3% non-cash residual,
no matching concept), Antares 1 (levels OVERSHOOT the total — a "what is fv_total measuring"
question, not a missing bucket).

### C6 net-asset roll-forward — DATA captured, CHECK dropped (2026-06-07 session 6)

Attempted C6 (`beg + capital_raised - repurchases + net-ops - distributions = end`). Extracted the
4 missing §5 inputs: beginning_net_assets (equity at the instant before period_start, via new
`FactSet.instant_scalar_at`), ending_net_assets (= total_net_assets), capital_raised
(`ProceedsFromIssuanceOfCommonStock`), repurchases (`StockRepurchasedDuringPeriodValue` /
`PaymentsForRepurchaseOfCommonStock`). Coverage: beg 246/300, end 285/300, capital 255, repurchases
166. BUT the strict identity is NOT reconstructable from XBRL — even clean filers (Apollo, HPS) miss
by ~0.2–0.9%, and a full run failed 94 of 100 inputs-present filings (median gap 2.3%, 35 over 5%).
The unrecoverable terms: DRIP reinvestment, gross-vs-net repurchases (incl. unpaid payables),
offering costs, early-repurchase deductions — many under custom `ck:` concepts. **Decision (Brian):
KEEP the captured DATA (useful line items for the spreadsheet) but DROP the C6 check entirely** (no
validation_check emitted) rather than flood the queue with 94 incomplete-extraction flags. Review
returned 140→49. The extractor still populates statement_of_changes; only the rule was removed.
Re-add C6 ONLY if an authoritative tagged roll-forward SUBTOTAL surfaces to anchor against (cf. C5).

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
7. ~~**Full volume run**~~ — Done 2026-06-05 session 4. 295 filings / 24 funds. Results
   analyzed (see "Full Volume Run Results" above): failures concentrated and explainable.
8. **Two validation refinements** (IN PROGRESS, session 4 — see "Pending validation
   refinements"): prefer net-of-waiver expenses; make C5 tax-aware. Clears ~79 review flags.
   After: fix the 3 structurally-broken filers (First Eagle / Terra / Bain) and the Golub
   `KeyError`, then re-run (resumable — only re-does fixed filings).
9. **After that:** harder XBRL (fair value C4, roll-forward C6, composition) and/or LLM
   fallback, informed by the volume run; then gold-standard set + spreadsheet assembler.
8. *(Optional)* Review borderline ISIN/ticker on the ~5 fuzzy master/feeder matches.

---

## Session Log

| Date | What Happened |
|------|---------------|
| 2026-06-07 (session 6 — C6) | **C6 net-asset roll-forward: data captured, check dropped.** Mapped the 4 missing §5 inputs — beginning_net_assets (new `FactSet.instant_scalar_at`: equity at the instant before period_start), ending_net_assets (= total_net_assets), capital_raised (`ProceedsFromIssuanceOfCommonStock`), repurchases (`StockRepurchasedDuringPeriodValue` / `PaymentsForRepurchaseOfCommonStock`). Coverage beg 246 / end 285 / capital 255 / repurchases 166 of 300. Full run showed the strict roll-forward identity is NOT reconstructable from XBRL: 94 of 100 inputs-present filings failed, median gap 2.3% (35 > 5%), driven by uncapturable terms (DRIP, gross/net repurchases incl. payables, offering costs, custom `ck:` concepts) — not tolerance-fixable. **Brian's call: keep the captured DATA, DROP the C6 check** (review 140→49, back to the post-C4 state). 0 regressions. Next: spreadsheet assembler. |
| 2026-06-07 (session 6) | **C4 cash/MMF plug — Brian's hypothesis confirmed.** Diagnosed the 28 C4 fails: the near-misses (Blue Owl Tech, Crescent) were the measured-at-NAV bucket (money-market / alternative investments) tagged OUTSIDE the L1/L2/L3 hierarchy under a different concept — `us-gaap:AlternativeInvestment` (on the hierarchy NAV member) or a custom `...MeasuredAtNetAssetValue` line (undimensioned). Added `FactSet._nav_practical_expedient`: fires only when buckets undershoot the total, kept only if it then reconciles (so no passing filing can break). First pass cleared 10; found a tolerance dead zone (plug trigger 0.5% vs C4 0.1%) stranding 3 Blue Owl Tech filings → aligned plug tol to 0.1% → cleared. **C4 28→15 fails, pass 238→251 (84%), review 62→49, 0 regressions.** Remaining 15: First Eagle 10 (only L3 — LLM territory), PGIM 4 (tiny non-cash residual), Antares 1 (overshoot). Next: C6 net-asset roll-forward. |
| 2026-06-07 (session 5 — C4) | **Fair-value hierarchy C4 now extracts (0 → 220 pass).** Built `FactSet.fv_hierarchy`: per-level TOTAL rows preferred (hierarchy axis only); asset-type-sum fallback self-checked against the undimensioned total and discarded if it double-counts (TPG 1.9× → skipped). Wired into `extract_filing` (fv_total = investments_at_fair_value; NAV-practical-expedient 4th bucket populates for HPS/Blackstone). Validated end-to-end before the run. NOTE: first full run was a no-op — forgot to clear `data/extracted/` (runner skips existing JSONs); cleared and re-ran. Result: **C4 220 pass / 28 fail / 52 skip, review 43→62, 0 regressions on other rules.** Brian's call: KEEP the 28 honest fails (4 funds — First Eagle only-L3 + Crescent/Blue Owl Tech/PGIM/Antares near-misses) rather than skip; **leading hypothesis is the reconciling plug is cash / money-market / cash-equivalents excluded from the FV table** (Brian, from prior data-product work). These (like all flag-and-keep values) must be marked in the final spreadsheet. Next: identify the cash/MMF plugs. |
| 2026-06-07 (session 5) | **C7 income-completeness fix (PIK-band anchor).** Pulled latest code on the home machine; re-ran the full extraction to rebuild `data/extracted/` (reproduced 218/82 exactly — confirms the dataset is regenerable and the JSONs are correctly gitignored, not pushed). Diagnosed C7 (27 fails): concentrated in 2 filers with OPPOSITE PIK causes — Ares (10) double-counts PIK (PIK-inclusive interest line) and Blue Owl (12) misses PIK dividend (PIK-exclusive, PIK split across overlapping us-gaap + custom `orcic:` concepts). Same PIK concept means different things per filer → no single concept edit fixes both. Implemented the PIK-band anchor (mirrors C5): `core = int+div+oth` must reconcile to the authoritative total once tagged PIK is allowed as a band. Added `pik_dividend_income` + `pik_income_combined` (schema + extractor, additive); rewrote C7 with gating unchanged so it only ADDS pass paths. Full re-run → **240 pass / 60 review (80%), C7 27→1.** The 1 residual = Ares 2026-03-31 (genuine missing cash-dividend, not PIK) — correctly still flagged. **Regression-verified by reverting C7 + re-validating in place: every other rule byte-identical (A1 5, A2 27, C1 3, C2 9, C3 4, C5 30) — only C7 moved.** Also corrected the stale "A2 18" in this doc → A2 was always 27. **Then fixed A2 (same session):** diagnosed the 27 fails as 26 dormant/unfunded share classes (0 shares + 0/None net assets → NAV 0.00) + 1 genuine ~1000× unit error (Ares 2023-06-30 class I, NAV 26,750); rule-only fix skips NAV 0/None (C2 still catches any funded-class zero). **A2 27→1, review 60→43, pass 257/300 (86%), 0 regressions** (rule-only, verified via `--revalidate` — every other rule byte-identical). Remaining 43 is now the irreducible tail (C5 Antares 30, First Eagle residuals, 2 genuine Ares anomalies). |
| 2026-06-05 (session 4) | **Full volume run + results analysis + C5 rework.** Ran the extractor over all BDCs × 10 yrs: 295 written / 24 funds, 151 review, 126 no_xbrl, 1 error. Diagnosed buckets: all 126 no_xbrl are pre-2022 (pre-inline-XBRL) filings — not a bug; only NC SLF has zero XBRL anywhere. Review queue = cosmetic C5 flags + 16 genuinely-broken extractions in 3 filers (First Eagle/Terra/Bain) + NAV/component remainder. The 1 error = Golub 10-Q 2024-12-31 `KeyError('concept')`. Applied the 2 refinements (net-of-waiver + tax-aware C5, commit `9918097`); re-ran full → C5 fails 107→51, errors 1→0. Diagnosed the residual 51 across 4 funds (Crescent/Antares/Oaktree/John Hancock): NOT waiver/tax — it's expense support + gross-vs-net expenses + split Current/Deferred tax, and the extracted NII *values* are correct. Added 2 more independent levers: tax-capture fix (`f98a7f9`) + anchor cross-check (`c642efd`). Verified no regressions; clears Crescent/Oaktree/John Hancock, leaves Antares-type (no anchor) as flag-and-keep residual. Then diagnosed the 3 "structurally-broken" filers → 3 distinct situations, all fixed generically (net-assets concept reorder + LLC concepts, `923c29b`): First Eagle mis-signed AssetsNet → prefer StockholdersEquity; Terra LLC → add MembersCapital/MembersEquity; Bain already clean (earlier years deferred). Regression-tested across 24 funds, 0 regressions. First Eagle per-class sign residual left flag-and-keep. **Ran the 2nd full re-run: 192 pass / 108 review of 300 (64%); C1 12→3, A1 11→5, C5 51→30.** Then diagnosed C2 (NAV-per-share) → ~43 of 51 flags were rounding artifacts on small share classes (net assets/shares rounded to thousands → can't match reported NAV to the cent); fixed with a rounding-aware tolerance (`d9aac9e`). Added a `--revalidate` runner mode (`5c6b622`) to re-run validation in place with no re-extraction, and used it to land C2: **final 218 pass / 82 review (73%)**, C2 51→9 (8 = First Eagle sign residual), 0 regressions. All committed & pushed. |
| 2026-06-05 (session 3) | **Set up the project on the Morningstar corporate machine + synced to GitHub.** Installed git (winget). Cloned the repo. Verified the venv/deps. Smoke-tested the runner (`--max-funds 1 --max-filings 2`) → 0 filings: diagnosed as corporate SSL inspection blocking EDGAR (`CERTIFICATE_VERIFY_FAILED`). Fixed with `configure_http(use_system_certs=True)` after `set_identity()` in all 6 EDGAR-touching scripts (+ rules.py self-test); re-ran smoke test → 4 AB Private Lending filings extracted & validated (balance sheet reconciles; status=review as expected per the known C5 waiver/tax item). Committed (`5f4d321`) and pushed to `origin/master`; set local git identity + upstream tracking. Pandas 3.0.3 noted as a watch item. Next: Brian kicks off the full volume run. |
| 2026-06-05 (session 2) | **Built the pipeline.** Added `src/validation/rules.py` (C1–C7 + reasonableness, flag-and-keep; missing inputs = skipped) and `src/extraction/run_extraction.py` (resumable runner → per-filing JSON in `data/extracted/`, gitignored). Tested on 2 funds (8 filings written; a new fund, AB Private Lending, extracted & passed — generalization signal). Volume test surfaced 3 coverage gaps, all fixed: combined class members ('SDAndI'), missing total_debt (debt sum fallback), expense concepts (recovered AB + Blackstone). New finding logged: C5 gross-vs-net-of-waiver expenses + income tax (2 pending refinements). Full volume run not yet executed — Brian to kick off. |
| 2026-06-05 (session 1) | **Built BDC XBRL extractor (`bdc_xbrl.py`), increments 1–5.** Concept-mapped balance sheet, per-class NAV, income statement + components (incl. PIK), fees, investments_at_cost, asset coverage, interest rate, distributions; computed derived metrics. Hand-validated C1/C2/C3/C5/C7 on Apollo/Blackstone/Ares/HPS latest 10-Q. Handled two dimension patterns (share-class, investment-affiliation) and fixed two bugs (affiliation-split income; instant-vs-duration period leakage). Decided to pivot to the pipeline (validation + JSON output + runner) to de-risk time/fund-set/volume coverage before more (harder) XBRL or LLM fallback. See "XBRL learnings" section. |
| 2026-06-04 | **Extraction Phase 0.** Ran an XBRL spike (Apollo/Blackstone/Ares/HPS 10-Qs) confirming the data is comprehensively tagged — incl. per-class NAV/shares, statement of changes (roll-forward), financial-highlights ratios, fair-value hierarchy, and full schedule of investments. Decided holdings = summary now / detail later. Drafted `docs/DATA_DICTIONARY.md` and built `src/schema/models.py` (pydantic, Fact-wrapped values w/ provenance+confidence). Confirmed derived-field formulas (leverage, asset coverage, distribution yield, net debt) and as-tagged highlights. One open item: trim/add field review. |
| 2026-06-03 (session 2) | **Categorization + extraction planning.** Confirmed initial pull complete (7,229 files). Built `add_vehicle_type.py` → added `vehicle_type` + Morningstar fields (ISIN, category, etc.) to all funds by CIK-first/name-fallback matching against the 4-tab categorization workbook. Found 18 Morningstar funds unrepresented in the universe; added 15 genuinely-new ones (blank CIK, needs sourcing), skipped 3 as duplicates/feeders. Universe 532 → 547. Separately, **locked the extraction plan** (hybrid XBRL-first BDC pilot; comprehensive schema; gold-set accuracy; flag-and-keep validation; new identity rules incl. 4-bucket fair-value and net-asset roll-forward). |
| 2026-06-03 (session 1) | Initial pull confirmed complete — 7,229 filings downloaded. |
| 2026-06-02 (session 2) | Built `initial_pull.py` and `update_pull.py`. `initial_pull.py` started and running — downloads 10 years of filings for ~334 funds (N-CSR/N-CSRS/N-23C3A for interval funds; N-CSR/N-CSRS for ncsr; 10-K/10-Q/SC TO-I for BDCs and REITs). `update_pull.py` ready for periodic use. Added `README.md` with setup and usage docs. Next: wait for initial pull to finish, then begin extraction work. |
| 2026-06-02 (session 1) | Built fund universe pipeline. `build_universe.py` seeds from existing filenames + queries EDGAR N-23C3A → 324 funds. `enrich_from_mstar.py` merges 508-fund Morningstar list by CIK then by name → 532 funds total. 198 funds have no CIK and are marked "unknown". Downloader not started — paused to commit. |
| 2026-06-01 | Project started. Read all old code. Created this folder and status file. |
