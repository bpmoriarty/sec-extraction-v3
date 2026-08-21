# SEC Filing Extraction — v3

A pipeline for extracting structured financial data from SEC EDGAR filings across a
broad set of semiliquid funds (interval funds, tender offer funds, BDCs, and unlisted
REITs) — and a **research layer** that turns that data into cross-fund analyses.

The project has three layers over **one shared fund universe**:

1. **Fund universe & downloads** — the master `fund_universe.csv` (CIKs, vehicle types,
   Morningstar identifiers) that every other layer reads, plus the EDGAR filing downloads.
   See [Workflow 2](#workflow-2--fund-universe--filing-download).
2. **Extraction** — turning filings into typed, validated per-filing records. Two paths:
   - **XBRL extraction** — for funds whose filings are machine-readable (structured XBRL).
     **Live today** for **BDCs** (listed + unlisted + deregistered): 107 funds in scope,
     **1,353 filings**, data current through **2026-06-30**. See
     [Workflow 1](#workflow-1--bdc-xbrl-extraction).
   - **LLM-over-clean-text** — for interval/tender-offer funds, whose N-CSR financial
     statements are **not** XBRL-tagged. **Built through M3, not yet run** — the section
     locator, schema, prompt, XBRL anchors, mapper and API module all exist and the
     end-to-end spine is verified, but **nothing has been spent**. See
     [Interval & tender-offer extraction](#interval--tender-offer-extraction-built-through-m3)
     and the runbook at [`docs/NCSR_LLM_PLAN.md`](docs/NCSR_LLM_PLAN.md).
3. **Research & analysis** — cross-fund studies built on the extracted data: holdings/mark
   comparison, trend ownership, credit migration, manager marking bias, portfolio overlap,
   BDC churn/survival. **Live today.** See
   [Workflow 3](#workflow-3--research--analysis-layer) for how to run them and
   [Research — What This Data Can Do](#research--what-this-data-can-do) for what they answer.

> **Current state lives in [`PROJECT_STATUS.md`](PROJECT_STATUS.md), not here.** Read its
> `⏩ RESUME HERE` block first — it carries the live counts, the quarterly-refresh runbook, and
> the known follow-ups. This README describes the *architecture*, which changes far more slowly
> than the numbers.

---

## Project Structure

```
sec-extraction-v3/
├── data/
│   ├── fund_universe.csv          # Master list of all funds + CIKs + categories (shared by every layer)
│   ├── extracted/                 # [W1] One JSON per filing (gitignored, regenerable)
│   ├── holdings/                  # [W1] One CSV per filing — schedule of investments (gitignored)
│   ├── review_queue/              # [W1] index.txt of filings flagged for review (gitignored)
│   ├── download_state.csv         # [W2] PER-MACHINE download progress (cik → last_checked); gitignored
│   ├── dataset/                   # [W1/W3] Assembled workbooks + intermediate CSVs (gitignored, rebuildable)
│   │   ├── semiliquid_bdc_dataset.xlsx        #   [W1] the core per-filing dataset (6 tabs)
│   │   ├── holdings_consolidated.csv          #   [W3] all holdings cleaned into one table
│   │   ├── holdings_matched.csv               #   [W3] each holding tagged to its loan/issue
│   │   ├── issues.csv                         #   [W3] one row per distinct loan (issue)
│   │   ├── holdings_marks_comparison.xlsx     #   [W3] cross-BDC marks, trend ownership, loan history (12 tabs)
│   │   ├── credit_migration.xlsx              #   [W3] credit migration + fund attribution (9 tabs, standalone)
│   │   ├── portfolio_overlap.xlsx             #   [W3] pairwise fund overlap + co-lending network
│   │   ├── marking_bias.xlsx                  #   [W3] manager rich/cheap marking bias
│   │   ├── bdc_churn.xlsx                     #   [W3] BDC births/deaths/survival (7 tabs)
│   │   ├── bdc_churn_census*.csv              #   [W3] one row per BDC ever elected (+ enriched)
│   │   ├── ncsr_inventory.csv                 #   [W4] N-CSR section-locator census (free, no API)
│   │   └── fund_manager_map.csv               #   [W3] curated CIK → manager map
│   ├── churn_births_raw.csv / churn_deaths_raw.csv  # [W3] N-54A / N-54C pulls (EDGAR index)
│   ├── survivorship_gap_candidates.csv        # Deregistered-BDC CIKs (N-54C), survivorship workstream
│   ├── survivorship_gap_enriched.csv          #   …enriched with XBRL availability
│   └── xbrl_by_vehicle_type.csv               # Inline-XBRL coverage by vehicle type (probe output)
├── docs/
│   ├── DATA_DICTIONARY.md         # [W1] What financial data we extract (the spec)
│   ├── XBRL_EXPANSION_PLAN.md     # [W1] Roadmap for additional XBRL data themes
│   ├── LISTED_BDC_PLAN.md         # [W1] Plan for incorporating listed BDCs
│   ├── NCSR_LLM_PLAN.md           # [W4] THE RUNBOOK for the N-CSR LLM path (M0–M6)
│   ├── HOLDINGS_COMPARISON_PLAN.md# [W3] Technical plan for the holdings/mark matcher (7 phases)
│   ├── HOLDINGS_RESEARCH_EXPLAINER.md # [W3] Plain-English guide to the marks comparison
│   ├── MARKING_BIAS_PLAN.md       # [W3] Plan + methodology for manager marking bias
│   ├── PORTFOLIO_OVERLAP_PLAN.md  # [W3] Plan for portfolio overlap analysis
│   └── CREDIT_MIGRATION_PLAN.md   # [W3] Plan + the five measured guards behind credit_migration
├── src/
│   ├── fund_universe/             # [W2] Build & maintain the shared fund list
│   │   ├── build_universe.py      #   Build the initial fund list from filenames + EDGAR
│   │   ├── enrich_from_mstar.py   #   Merge the Morningstar fund list in
│   │   └── add_vehicle_type.py    #   Tag funds with Vehicle Type + Morningstar data
│   ├── downloader/                # [W2] Pull filing HTML down from EDGAR
│   │   ├── initial_pull.py        #   Download all historical filings (one-time)
│   │   └── update_pull.py         #   Check for new filings (run periodically)
│   ├── schema/
│   │   ├── models.py              # [W1] Typed schema for extracted data (pydantic)
│   │   └── ncsr_raw.py            # [W4] Flat intermediate the LLM fills in (64 fields)
│   ├── extraction/
│   │   ├── bdc_xbrl.py            # [W1] BDC XBRL extractor (maps us-gaap concepts → schema)
│   │   ├── run_extraction.py      # [W1] Resumable runner: extract → validate → per-filing JSON
│   │   ├── ncsr_sections.py       # [W4] M1: locate the financial statements in N-CSR HTML
│   │   ├── ncsr_prompt.py         # [W4] M3: prompt built FROM the schema so they can't drift
│   │   ├── ncsr_anchors.py        # [W4] M3: inline-XBRL cover-page anchors (identity checks)
│   │   ├── ncsr_map.py            # [W4] M3: map the LLM's raw record into the shared schema
│   │   ├── ncsr_llm.py            # [W4] M3: Claude API module (interactive + batch)
│   │   └── api_smoke_test.py      # [W4] One cheap call to prove the API key works
│   ├── validation/
│   │   └── rules.py               # [W1] Validation layer (identity checks + reasonableness)
│   ├── output/
│   │   └── build_spreadsheet.py   # [W1] Assemble the JSONs into the core dataset workbook
│   └── analysis/                  # [W3] Research layer (reads data/holdings/ + data/extracted/)
│       ├── holdings_compare.py    #   Cross-BDC holdings & marks; trend ownership; loan history
│       ├── credit_migration.py    #   Credit migration + fund attribution (standalone workbook)
│       ├── portfolio_overlap.py   #   Pairwise fund overlap + co-lending network
│       ├── marking_bias.py        #   Manager rich/cheap marking bias (stats)
│       ├── managers.py            #   Curated fund → manager map
│       ├── bdc_churn.py           #   BDC churn: census, births/deaths, survival, workbook
│       ├── churn_sizing.py        #   EDGAR N-54A/N-54C pulls that feed the churn census
│       ├── churn_enrich.py        #   Death-mechanism classification (merger/liquidation/…)
│       ├── ncsr_inventory.py      #   [W4] Free corpus census of N-CSR locator coverage
│       ├── survivorship_enrich.py #   Deregistered-BDC gap list + XBRL availability
│       └── xbrl_by_vehicle_type.py#   Inline-XBRL coverage probe across vehicle types
├── Listed BDCs Mstar.xlsx                          # [W1] Morningstar input (listed-BDC universe)
├── United States Semiliquid Funds Mstar.xlsx       # [W2] Morningstar input (universe build)
├── semiliquid fund categorization Mstar.xlsx       # [W2] Morningstar input (vehicle types)
├── pyproject.toml / uv.lock / .python-version   # Pinned, reproducible environment (uv sync)
└── PROJECT_STATUS.md              # Running log of decisions and progress — START HERE
```

`[W1]` = XBRL extraction · `[W2]` = fund universe & download · `[W3]` = research & analysis ·
`[W4]` = N-CSR LLM extraction (interval / tender-offer funds).

The `filings/` folder (where downloaded HTML files are saved) lives **one level up**
from this folder, at `SEC Filing Extraction/filings/`. It is not inside `sec-extraction-v3/`
because it can grow very large and is shared across project versions. Note that the live
XBRL workflow does **not** read these files — it fetches XBRL directly from EDGAR (see
Workflow 1). The downloaded HTML is for the future interval/tender extraction path.

---

## First-Time Setup

### 1. Build the environment

The project declares its own dependencies in `pyproject.toml`, pins exact versions in
`uv.lock`, and pins the interpreter in `.python-version`. So the whole environment comes from
one command, run from inside the `sec-extraction-v3/` folder:

```bash
uv sync
```

That is the only setup step. Do **not** hand-install with `uv pip install` — it records no
versions and drifts from the lock. To add a package, use `uv add <pkg>`, which updates
`pyproject.toml` and `uv.lock` together.

> **If `uv sync` fails with a hardlink error**, this folder is inside OneDrive/iCloud (which
> block the hardlinks `uv` prefers). Use `uv sync --link-mode=copy`.

> **Why versions are pinned with `==`:** the BDC pipeline was hand-validated against these
> exact versions (notably `edgartools==5.35.1`, whose XBRL parsing the extractor's concept
> maps were verified against). Upgrading is a deliberate act that needs re-verification, not a
> routine refresh. `anthropic` is the one floor-pinned (`>=`) dependency, since the N-CSR path
> wants current structured-output support.

### 2. Corporate networks (SSL inspection)

On a corporate network that does SSL inspection (e.g. the Morningstar machine), EDGAR
HTTPS calls fail with `SSLVerificationError: CERTIFICATE_VERIFY_FAILED`. Every
EDGAR-touching script already handles this by calling `configure_http(use_system_certs=True)`
right after `set_identity()` — this uses the Windows certificate store (which trusts the
corporate root CA) and is harmless on home networks. No action needed; just be aware that
this line is required and shouldn't be removed.

The N-CSR LLM path needs the same treatment for the Anthropic SDK, which uses `httpx` rather
than `edgartools`' HTTP layer: `ncsr_llm.py` calls `truststore.inject_into_ssl()` **before**
importing `anthropic`. Order matters — injecting afterwards has no effect.

### 3. Anthropic API key (only for the N-CSR LLM path)

Nothing in Workflows 1–3 needs it. The N-CSR path does:

```bash
# set ANTHROPIC_API_KEY in the environment, then prove it works for ~$0.0002:
uv run python src/extraction/api_smoke_test.py
```

The smoke test verifies its own result (checks `stop_reason` for refusal/truncation and that
the reply is non-empty and correct) rather than printing PASSED unconditionally.

---

## Orientation — which extraction path applies to which funds

The right extraction path depends on how a fund files.

- **BDCs** (listed and unlisted) and **unlisted REITs** are operating-company filers: they
  file **10-K / 10-Q**, which the SEC requires to be tagged in **inline XBRL** (structured,
  machine-readable). Their balance sheet, income, per-class NAV, fair-value hierarchy, and
  schedule of investments come down as typed data — no HTML scraping or LLM needed.
- **Interval and tender-offer funds** are registered investment companies: they file
  **N-CSR / N-CSRS**, whose **financial statements are NOT XBRL-tagged** (confirmed at the
  raw-fact level — those filings carry only thin `cef:`/`oef:` cover-page tags, not the
  statements). So their financials need a different extractor (LLM-over-clean-text). Their
  **portfolio holdings**, however, are available separately as structured **Form N-PORT**
  data (already collected in-house), so holdings don't need extraction at all for them.

| Fund type | Filing forms | Financials source | Holdings source | Status |
|---|---|---|---|---|
| **Unlisted BDC** | 10-K / 10-Q | **XBRL** | XBRL (schedule of investments) | **Live** — part of the 107-fund / 1,353-filing run |
| **Listed BDC** | 10-K / 10-Q | **XBRL** | XBRL (schedule of investments) | **Live** — 55 funds added (session 11) |
| **Deregistered BDC** | 10-K / 10-Q | **XBRL** | XBRL | **Live** — 26 dead BDCs added for survivorship correction; extraction capped at the N-54C withdrawal date |
| Unlisted REIT | 10-K / 10-Q | XBRL | XBRL | Not built (XBRL-tagged, but needs CIK sourcing + REIT-specific concept maps) |
| Interval Fund | N-CSR / N-CSRS | **LLM-over-clean-text** (no XBRL) | **N-PORT** (in-house) | **Built through M3, not yet run** (financials); holdings available |
| Tender Offer Fund | N-CSR / N-CSRS | **LLM-over-clean-text** (no XBRL) | **N-PORT** (in-house) | **Built through M3, not yet run** (financials); holdings available |

---

## Workflow 1 — BDC XBRL Extraction

This is the **live** extraction pipeline. It targets funds tagged `Unlisted BDC` /
`Listed BDC` / `Deregistered BDC` (or `category = bdc`) in `fund_universe.csv`, pulls every
10-K / 10-Q since 2016, extracts the financial data, validates it, and writes one JSON per
filing. As of 2026-08-18 it covers **107 BDC CIKs in scope → 1,353 filings
(829 pass / 524 review)**, current through the **2026-06-30** reporting period.

For deregistered BDCs, extraction is **capped at the fund's `deregistration_date`** (its N-54C
withdrawal) — filings whose period ends after that are post-BDC data and are skipped.

> **Refreshing each quarter:** the runner is additive by construction — it keys output on
> `{cik}_{form}_{period}.json` and skips what exists, with a fixed `SINCE_YEAR = 2016`, so no
> window ever rolls forward and prior filings are never lost. The full nine-step refresh order
> (extraction through every downstream workbook) is in
> [`PROJECT_STATUS.md`](PROJECT_STATUS.md)'s `🔁 BDC quarterly refresh runbook`.

**What XBRL gives us:** because BDCs file 10-K/10-Q, their balance sheet, income statement,
per-class NAV, fair-value hierarchy, schedule of investments, cash flow, tax basis, expense
breakdown, credit-facility capacity, and more come down as typed data. (Unlisted REITs also
file XBRL-tagged 10-K/10-Q and could be added later, but `bdc_xbrl.py`'s concept mappings are
BDC-specific. Interval/tender-offer funds are a separate path — see below.)

> **Prerequisites:** `fund_universe.csv` must exist and have CIKs for the BDCs (built in
> [Workflow 2](#workflow-2--fund-universe--filing-download)). That's the only thing this
> workflow needs from Workflow 2 — **it fetches the XBRL itself, live from EDGAR via
> `edgartools`**, and never touches the downloaded HTML in `filings/`.

### The spec and the schema

- **`docs/DATA_DICTIONARY.md`** — the spec for every field we collect (balance sheet,
  per-class NAV, income incl. PIK breakout, fair-value hierarchy, schedule of investments,
  etc.), with units, sources, and the validation rules.
- **`docs/XBRL_EXPANSION_PLAN.md`** — the roadmap for additional XBRL data themes
  (credit-facility capacity, cash flow, capital share activity, etc.).
- **`src/schema/models.py`** — the typed (pydantic) version of the spec; validates extracted
  data and generates the output columns. `pydantic` ships with `edgartools`, so no extra
  install is needed.

### Data flow (extraction → core dataset)

Per-filing JSON is the staging layer / source of truth; the spreadsheet is a derived view
that can be rebuilt anytime without re-extracting. The per-filing holdings CSVs are the raw
material the [research layer](#workflow-3--research--analysis-layer) builds on.

```
  filing  (XBRL fetched live from EDGAR)
      │
      ▼  [extractor: bdc_xbrl.py]  — map us-gaap XBRL facts into the schema + compute derived
  FilingExtraction object   (pydantic validates structure)
      │
      ▼  [validation: rules.py]  — run identity + reasonableness checks; attach results + review_flags
      │
      ├─ write  data/extracted/<cik>_<form>_<reporting_date>.json    ← one file per filing
      ├─ write  data/holdings/<cik>_<form>_<reporting_date>.csv       ← schedule-of-investments rows
      └─ append flagged filings →  data/review_queue/index.txt
      │
      ▼  [assembler: build_spreadsheet.py]  — read ALL extracted JSONs, pivot
  data/dataset/semiliquid_bdc_dataset.xlsx   ← derived, rebuildable view (6 tabs)
```

Why the JSON layer: crash-safe incremental runs, idempotent re-runs (skip already-extracted
filings), full auditability (every cell traces to a source filing with its provenance +
confidence), and decoupling (restructure the spreadsheet without re-extracting). Point-in-time
fields are keyed on `reporting_date`; flow fields (income, distributions) carry `period_start`
/ `period_months` (3 = quarter, 12 = annual). Holding-level rows are stored SEPARATELY
(per-filing CSVs in `data/holdings/`) so the validated core JSON stays lean; the §9 summary
metrics derived from them live in the JSON.

### `src/extraction/run_extraction.py` — the runner

**What it does:**
For every BDC fund with a CIK, pulls all 10-K / 10-Q filings since 2016, extracts each
(`bdc_xbrl.extract_filing`), validates it (`rules.validate`), and writes one JSON per filing
to `data/extracted/` plus a holdings CSV to `data/holdings/`. Resumable (skips filings whose
JSON already exists), crash-safe (writes per filing), and robust (per-filing try/except;
filings without XBRL are logged and skipped; transient EDGAR timeouts are retried).

```bash
uv run python src/extraction/run_extraction.py            # full run (network-bound, 1-2+ hrs)
uv run python src/extraction/run_extraction.py --max-funds 2 --max-filings 2   # quick test
```

**⚠️ After changing the extractor, you MUST clear `data/extracted/` before re-running** —
the runner skips filings whose JSON already exists, so a re-run over existing output is a
no-op and your changes won't take effect:

```powershell
Remove-Item data\extracted -Recurse -Force
Remove-Item data\holdings  -Recurse -Force
Remove-Item data\review_queue\index.txt -Force
uv run python src/extraction/run_extraction.py
```

**`--revalidate` (no re-extraction):** when you change only a *validation rule* (not the
extractor), re-run validation over the existing JSONs in place — instant, no network. It
rewrites each filing's validation fields and regenerates the review index:

```bash
uv run python src/extraction/run_extraction.py --revalidate
```

The run prints a summary: `written / skipped / review / no_xbrl / errors`. The authoritative
pass/review counts come from the JSONs themselves (the printed `review` counter can be skewed
if a run is interrupted and restarted, since the index appends).

> **What `no_xbrl` means:** filings dated 2022 or earlier predate inline-XBRL tagging, so
> they have no structured data to extract and are skipped. These are LLM-fallback territory,
> not a bug — recent-year coverage is essentially complete.

### `src/output/build_spreadsheet.py` — the core dataset workbook

**What it does:**
Reads every JSON in `data/extracted/` and writes `data/dataset/semiliquid_bdc_dataset.xlsx`
with six tabs: **Data** (one row per filing, ~70+ fields), **ShareClasses** (one row per
filing × class), **Review** (flagged filings + a validation-code key), **Check (Gold)** (a
hand-verification view for ~15 representative filings with a self-computing accuracy %),
**Holdings (Gold)** (the holding-level schedule of investments for the gold funds), and
**Definitions** (every derived/calculated field with its formula + methodology). Flag-and-keep
values are visibly marked (status/flags columns, amber row tint, amber on the specific cells
tied to each failing rule).

```bash
uv run python src/output/build_spreadsheet.py
```

> Close the workbook in Excel before rebuilding — Windows won't let the script overwrite an
> open file (`PermissionError`).

---

## Workflow 2 — Fund Universe & Filing Download

This workflow builds and maintains the shared fund list, and downloads filing HTML from
EDGAR. The fund list (`fund_universe.csv`) feeds **every** layer — the XBRL pipeline reads
CIKs from it. The downloaded HTML in `filings/` is the raw material for the **future**
interval/tender-offer extraction (see the last subsection).

Run the universe scripts in order the first time; re-run individual ones when the Morningstar
inputs change.

### `src/fund_universe/build_universe.py` — build the initial list

**What it does:** Builds `data/fund_universe.csv` from scratch, in two steps:
1. Scans any existing `.htm` files in the `filings/` folder and extracts fund names and CIKs
   from the filenames (fast, no internet needed)
2. Queries SEC EDGAR for all N-23C3A filers (repurchase offer notifications) — only interval
   funds file this form, making it the cleanest way to identify them

**When to run:** Only once to create the initial fund list, or to rebuild from scratch.

```bash
uv run python src/fund_universe/build_universe.py
```

### `src/fund_universe/enrich_from_mstar.py` — merge in the Morningstar list

**What it does:** Merges the Morningstar semiliquid fund list (`United States Semiliquid
Funds Mstar.xlsx`) into `fund_universe.csv`, in two passes:
1. **CIK match:** funds that have a CIK in Morningstar are joined directly
2. **Name match:** remaining funds (no CIK) are fuzzy-matched by name against the existing
   universe; any not found are added as `category = unknown`

Funds added as "unknown" have no CIK and will be skipped by the downloader until a CIK is
sourced for them.

**When to run:** After `build_universe.py`, and again whenever the Morningstar Excel file is
updated with new funds or newly added CIKs.

```bash
uv run python src/fund_universe/enrich_from_mstar.py
```

### `src/fund_universe/add_vehicle_type.py` — tag vehicle types

**What it does:** Tags each fund with a **Vehicle Type** and copies over Morningstar
identifier/category data, sourced from the four tabs of `semiliquid fund categorization
Mstar.xlsx` (Interval Funds, Tender Offer Funds, Unlisted BDCs, Unlisted REITs). Matching is
CIK-first (where the workbook has a CIK), then fuzzy name match. Funds on no tab get
`vehicle_type = unknown`. Adds: `vehicle_type`, `mstar_ticker`, `isin`,
`morningstar_category`, `us_category_group`, `morningstar_category_broad_group`.

This is the step that makes [Workflow 1](#workflow-1--bdc-xbrl-extraction) possible — the
XBRL runner selects funds by `vehicle_type` (`Unlisted BDC` / `Listed BDC`) or `category == bdc`.

**When to run:** After the universe exists, and again whenever the categorization workbook is
updated. (Listed BDCs were added from `Listed BDCs Mstar.xlsx` — see `docs/LISTED_BDC_PLAN.md`.)

```bash
uv run python src/fund_universe/add_vehicle_type.py
```

### `src/downloader/initial_pull.py` — download all historical filings

**What it does:** Downloads all historical filings (back to 2016) for every fund in
`fund_universe.csv` that has a CIK. Files are saved to the `filings/` folder using the naming
convention:

```
FundName_CIK_FormType_YYYY-MM-DD.htm
```

Example: `Cliffwater_Corporate_Lending_Fund_0001735964_N-CSRS_2022-12-09.htm`

Forms downloaded per fund category:

| Category | Forms Downloaded |
|---|---|
| interval_fund | N-CSR, N-CSRS, N-23C3A |
| ncsr_fund | N-CSR, N-CSRS |
| bdc | 10-K, 10-Q, SC TO-I |
| reit | 10-K, 10-Q, SC TO-I |
| unknown | (skipped — no CIK) |

**Key behaviors:**
- Skips files that already exist (safe to interrupt and re-run)
- Saves progress to `data/download_state.csv` after each fund — if the script crashes, restart
  it and it picks up where it left off. (It no longer writes `last_checked` into
  `fund_universe.csv`; see the note under `update_pull.py`.)
- `TEST_MODE_LIMIT = 3` at the top of the file lets you test on 3 funds before committing to
  the full run; set it to `None` for the full download

**When to run:** Once, after the universe is built. Expect 1.5–3 hours for a full run.

```bash
uv run python src/downloader/initial_pull.py
```

### `src/downloader/update_pull.py` — fetch new filings

**What it does:** Checks for new filings filed since the last time each fund was checked, and
downloads anything new. If a fund has never been checked, it falls back to 2016-01-01 so no
filings are missed.

> **`last_checked` lives in `data/download_state.csv`, NOT in `fund_universe.csv`.** Download
> progress is **per-machine** (this repo is worked from two machines and `data/` is gitignored),
> so keeping it in the shared, committed universe file would have made one machine's progress
> silently suppress the other's downloads. A fresh clone has no state file, queries full
> history, and re-downloads nothing thanks to the on-disk existence check. See
> `src/downloader/download_state.py`.

**When to run:** Periodically — once a month is usually enough for this type of fund.

```bash
uv run python src/downloader/update_pull.py
```

### `fund_universe.csv` — the master fund list

This CSV is the backbone of the whole project. Every script reads from or writes to it.

| Column | Description |
|---|---|
| `cik` | 10-digit zero-padded SEC EDGAR identifier. Empty if unknown. |
| `fund_name` | Full legal name of the fund |
| `category` | `interval_fund`, `ncsr_fund`, `bdc`, `reit`, or `unknown` (derived from EDGAR forms/SIC) |
| `form_types` | Pipe-separated list of forms this fund has been seen filing |
| `last_filing_date` | Most recent filing date found in our filings folder (ISO `YYYY-MM-DD`) |
| `notes` | Free-text notes, e.g., data source or quirks |
| `deregistered` | Set for BDCs that filed an N-54C (withdrew their BDC election) |
| `deregistration_date` | N-54C date (ISO `YYYY-MM-DD`). **The XBRL runner caps extraction here** — filings whose period ends later are post-BDC data. |
| `vehicle_type` | `Interval Fund`, `Tender Offer Fund`, `Unlisted BDC`, `Listed BDC`, `Unlisted REIT`, or `unknown` (from the categorization workbook — see `add_vehicle_type.py`) |
| `mstar_ticker` | Morningstar ticker, where available |
| `isin` | ISIN identifier, where available |
| `morningstar_category` | Morningstar category (e.g. "Private Debt - Direct Lending") |
| `us_category_group` | Morningstar US category group |
| `morningstar_category_broad_group` | Morningstar broad group |

**Important — reading the file:** Always read with `dtype={"cik": str}` in pandas, otherwise
leading zeros in CIKs are stripped (e.g., `"0001748680"` → `1748680`).

**Important — do NOT open this CSV in Excel and save it.** Excel auto-reformats the date
columns (`last_filing_date`, `deregistration_date`) from ISO `YYYY-MM-DD` into US `M/D/YYYY` on
save, corrupting the format. To view it in a spreadsheet, open a *copy* or import it as text.
Dates in this file should always be ISO `YYYY-MM-DD`.

### Interval & tender-offer extraction (built through M3)

**Runbook: [`docs/NCSR_LLM_PLAN.md`](docs/NCSR_LLM_PLAN.md) — read that, not this section, for
the architecture and milestones.** This is a summary.

Unlike BDCs, these funds' **financial statements are not XBRL-tagged** — their N-CSR / N-CSRS
filings carry only thin `cef:` / `oef:` cover-page tags (NAV/share, expense ratios), not the
balance sheet, income statement, cash flow, or schedule of investments. This was verified at
the raw-fact level, not assumed.

**Status: M0–M3 built and verified; nothing has been spent.** Measured full-corpus cost is
**$96** on Sonnet batch.

| Milestone | State |
|---|---|
| **M1** — section locator (`ncsr_sections.py`) + free corpus census (`ncsr_inventory.py`) | **Done.** Locator gate **99.1%** (2,323/2,343), 3,026 filings located, 27 true misses |
| **M3** — schema, prompt, XBRL anchors, mapper, API module | **Done.** `ncsr_raw.py`, `ncsr_prompt.py`, `ncsr_anchors.py`, `ncsr_map.py`, `ncsr_llm.py` |
| **M4** — ~25-filing hand-verified gold sample | **Next. Gates all spend.** |
| **M5** — batch backfill (~$96) | Blocked on M4 |
| **M2** — multi-series slicer (429 multi-block filings) | Deliberately not an M5 prerequisite |

Two design points worth knowing before touching it:

- **The prompt is generated FROM the schema** (`ncsr_prompt.py` reads `ncsr_raw.py`'s
  `Field(description=...)`), so the two cannot drift apart.
- **Only the middle of the pipeline is new, and that was tested rather than asserted.** A
  hand-built raw record runs through the mapper into the **unchanged** `validate()` and
  `compute_derived()` from Workflow 1 — same schema, same C-rules, same review queue.

The path therefore splits in two:

- **Holdings → in-house N-PORT data.** These funds file **Form N-PORT**, which carries their
  full portfolio holdings as structured XML (issuer, CUSIP, fair value, par, coupon,
  maturity). That data is already collected in-house, so holdings need **no extraction** — and
  it can feed the same [research layer](#workflow-3--research--analysis-layer), extending the
  cross-fund analyses well beyond BDCs.
- **Financial statements → LLM-over-clean-text.** The intended approach (replacing the old
  "group filers by HTML table structure" plan, which was brittle) uses `edgartools`' document
  tooling — `filing.text()` / `filing.markdown()` to get clean, readable text (not raw HTML),
  `filing.get_section()` to isolate the financial statements, `filing.chunk_text()` to fit
  context windows — then feeds that to an LLM (Claude) with a structured-extraction prompt
  targeting the **same schema**, and validates the output with the **same identity C-rules**
  as the XBRL path (so the balance sheet must balance, NAV must reconcile, etc.). One LLM
  extractor generalizes across filer layouts that would break a deterministic parser; the
  validation layer keeps it honest. The thin `cef:`/`oef:` cover-page tags that *do* exist are
  harvested as high-confidence cross-check anchors.

> **Note:** `edgartools`' AI/agent integration (`to_llm_context`, `to_agent_tools`, the MCP
> server) is **not** the lever here — it serializes data `edgartools` has *already parsed*
> (XBRL facts, company financials) for an LLM/agent. For untagged N-CSR financials there is
> nothing parsed to serialize. The document-text tooling above is the actual path.

It plugs into the same schema, validation layer, review queue, and spreadsheet assembler as
Workflow 1 — only the front-end extractor differs.

The census that measures locator coverage is **free** (no API calls), so it can be re-run
freely after any locator change:

```bash
uv run python src/analysis/ncsr_inventory.py       # → data/dataset/ncsr_inventory.csv
```

> **Never pass `--resume` after changing the locator** — it would mix results from two code
> versions in one census. Back up `ncsr_inventory.csv`, re-run clean, and diff both the
> located/not-located flips **and** `serialized_chars` (the gate measures whether a block was
> *found*, never whether it was read *correctly*).

---

## Workflow 3 — Research & Analysis Layer

The research scripts in `src/analysis/` read the extracted data (mainly the per-filing
holdings CSVs in `data/holdings/`, plus the core JSONs) and produce cross-fund analyses. They
are independent of the extractor and rebuildable anytime. For *what each analysis answers and
why it matters*, see [Research — What This Data Can Do](#research--what-this-data-can-do); this
section is the mechanics.

**Run order** (later steps consume earlier outputs):

```bash
# 1. (prereq) The core dataset, from Workflow 1
uv run python src/output/build_spreadsheet.py

# 2. Holdings matcher: consolidate + clean + cluster + match  (~30 min, the expensive step)
uv run python src/analysis/holdings_compare.py --build      # writes holdings_consolidated/matched/issues.csv
#   (diagnostics: --diagnose | --cluster | --issues ; --threshold N tunes the fuzzy merge)

# 3. Fund → manager map (needed by marking bias); reads step 2's consolidated CSV
uv run python src/analysis/managers.py                      # writes fund_manager_map.csv + prints VERIFY flags

# 4. The marks workbook — reuse step 2's output instead of re-clustering  (SECONDS, not 70 min)
uv run python src/analysis/holdings_compare.py --workbook --from-cache   # → holdings_marks_comparison.xlsx (12 tabs)

# 5. Cross-fund studies  (each still re-clusters internally, ~70 min apiece)
uv run python src/analysis/marking_bias.py                  # writes marking_bias.xlsx
uv run python src/analysis/portfolio_overlap.py             # writes portfolio_overlap.xlsx

# 6. Credit migration + fund attribution (standalone workbook, its own window)
uv run python src/analysis/credit_migration.py --gate       # CHECK THIS FIRST (see below)
uv run python src/analysis/credit_migration.py --build      # → credit_migration.xlsx (9 tabs)

# 7. BDC churn / survival (independent EDGAR pull; not driven by holdings)
uv run python src/analysis/churn_sizing.py                  # N-54A/N-54C pulls → churn_*_raw.csv
uv run python src/analysis/bdc_churn.py                     # census + workbook → bdc_churn.xlsx (7 tabs)
uv run python src/analysis/churn_enrich.py                  # death-mechanism classification
```

> **`--from-cache` (step 4) reads the CSVs step 2 already wrote** rather than re-running the
> clustering, turning a workbook-only change from ~70 minutes into seconds. It is valid **only**
> while those CSVs are current — re-run `--build` after any parsing change. It raises rather
> than falling back if the cache is missing, so it can never silently run on stale input.
> `marking_bias.py` and `portfolio_overlap.py` have no cache path yet.

> **Always run `credit_migration.py --gate` before `--build`.** It reports how many funds'
> dollar figures reconcile against their own XBRL-tagged portfolio total; the expected result is
> **~43 usable funds / ~88.9% of BDC assets**. Materially lower means the dollar answers are
> unsafe and the fallback is issuer counts only.

> **A `PermissionError` writing any workbook is usually OneDrive sync, not Excel — retry once**
> before closing anything. A genuine Excel lock leaves a `~$` file in `data/dataset/`.

Supporting probes (not part of the main pipeline): `survivorship_enrich.py` (deregistered-BDC
gap list + XBRL availability → `survivorship_gap_*.csv`) and `xbrl_by_vehicle_type.py`
(inline-XBRL coverage by vehicle type → `xbrl_by_vehicle_type.csv`).

---

## Research — What This Data Can Do

The pipeline turns thousands of filings into a comparable, validated dataset. **Nine** research
workflows are **live today**; several more are scoped as **future** work.

### Live today

**1. Single-fund financial analytics — the core dataset.**
Every BDC filing becomes one row of comparable, validated financials: balance sheet, per-class
NAV, income (including the PIK / non-cash breakout), fees, the fair-value hierarchy (Level
1/2/3 + NAV-practical-expedient), cash flow, tax basis, expense breakdown, credit-facility
capacity, leverage and asset-coverage, distributions — plus derived ratios (portfolio mark,
distribution coverage, PIK income ratio, net lending spread, liquidity coverage) and §9
holdings-summary metrics (number of holdings, top-10 concentration, % floating-rate,
weighted-average yield/spread, PIK exposure, unfunded commitments). With it you can ask: *how
levered is this fund? is it covering its distribution or eating into NAV? how much of its
income is non-cash PIK? where does it sit versus peers on yield, leverage, and non-accruals?
how have its marks and NAV trended over time?* → `data/dataset/semiliquid_bdc_dataset.xlsx`.

**2. Cross-BDC holdings & mark comparison.**
The same private loan is often held by several BDCs at once (a "club deal"). Because each
manager has to *estimate* what that illiquid loan is worth — its "mark," in cents on the
dollar — we can line up the identical loan across every fund that holds it and ask: *do they
value it the same, and when they disagree, who's the outlier — is a credit being quietly
marked down before the others catch up?* The matcher cleans 375k+ messy holding entries,
groups the same borrower across a dozen spellings, separates each borrower's distinct loans,
and compares the marks — surfacing real situations (e.g. Pluralsight: Ares 73.5 vs four Blue
Owl funds 97.7) and quarter-by-quarter deterioration (First Brands −54 points) as early
warnings. → `holdings_compare.py` → `data/dataset/holdings_marks_comparison.xlsx` (12 tabs).
Plain-English guide: `docs/HOLDINGS_RESEARCH_EXPLAINER.md`.

**3. Who owns the moving credits, and the arc of each holding.**
Identifying the credits that moved most is only half the question; the median that makes a
trend legible discards *who holds it*. The `TrendOwners` tab puts the holders back — each one's
own mark, its deviation from the cross-holder consensus, the position in dollars, and the
position as a share of that holder's portfolio — and `LoanHistory` shows the whole arc, one row
per (issuer, fund) across six semiannual as-of dates. Three guards keep it honest, each of them
measured: credits whose series *stopped* are quarantined to `TrendEnded` rather than ranked
beside live declines; each move is recomputed on a **constant holder set**, because a fund with
a later fiscal year-end drops out and takes its mark with it (one credit reads −38.7pts raw but
−13.0 once holders are held constant); and a wide spread across current holders is flagged as a
likely tranche mis-merge rather than real disagreement. → same workbook.

**4. Credit migration & fund attribution.**
For a fixed window, how much of each fund went from healthy to impaired, which borrowers did the
most damage to the whole BDC book, and what that cost each fund. A start-bucket × end-bucket
migration matrix (in dollars and issuer counts, with an **exited** column so a fund that *sold*
its deteriorating loans isn't read as having underwritten well), per-issuer exposure-weighted
price change ranked by contribution to the entire universe, and a per-fund valuation drag against
both an asset-weighted universe benchmark and the fund's **actual reported total return**. That
last pairing matters: the drag is *not* a return — funds here carry −2 to −4 points of valuation
drag and still reported returns above +10%, because interest income dominates. →
`credit_migration.py` → `data/dataset/credit_migration.xlsx` (9 tabs, including a **Definitions**
tab that is validated against the columns actually written). Plan + the five guards:
`docs/CREDIT_MIGRATION_PLAN.md`.

**5. Manager marking bias.**
Aggregating across every loan two or more managers share, does a given manager *systematically*
mark richer or cheaper than its peers on the same credits? Using a within-loan leave-one-out
deviation (each manager's mark vs. the others on the identical loan), with bootstrap confidence
intervals and a multiple-testing (FDR) correction, this separates real, persistent bias from
noise — revealing which managers are consistently aggressive vs. conservative valuers (e.g.
Prospect / CION mark rich; Barings / Goldman mark cheap). → `marking_bias.py` (with the curated
fund→manager map from `managers.py`) → `data/dataset/marking_bias.xlsx`.

**6. Portfolio overlap & co-lending network.**
Which funds hold the same borrowers, and how concentrated is that overlap? This computes
pairwise fund overlap at both the issuer and the individual-loan grain (common count,
directional share, Jaccard, dollar-weighted overlap, and a hypergeometric "lift" that flags
overlap beyond chance), flags same-manager sister funds, tracks overlap over time, and draws
the co-lending relationships as a network graph. It surfaces both the obvious (same-manager
sister funds nearly identical) and the interesting (cross-manager club deals — e.g. Blackstone
and HPS sharing 121 actual loans). → `portfolio_overlap.py` →
`data/dataset/portfolio_overlap.xlsx`.

**7. BDC churn & survival analysis.**
The full life-cycle of the BDC universe from EDGAR's filing index alone — **N-54A = births**
(election to be a BDC), **N-54C = deaths** (withdrawal), `fund_universe` = current survivors.
Births/deaths and active-count over time, survival rates and observed median lifespan, and the
**death-mechanism split** (liquidation vs. merger vs. conversion vs. scheduled wind-down — where
merging a weak fund into a stronger sibling is itself a survivorship-bias mechanism, not a benign
event), with low-confidence classifications flagged for manual review. Mirrors Morningstar's
fund-survival methodology (a fund "doesn't survive" if liquidated *or* merged). →
`churn_sizing.py` → `bdc_churn.py` → `churn_enrich.py` → `data/dataset/bdc_churn.xlsx` (7 tabs).

**8. Survivorship-bias correction (the extractable subset).**
The universe was survivor-only. The 26 deregistered BDCs with extractable XBRL are now **in the
main extraction run**, capped at each fund's N-54C date so no post-BDC data leaks in. The wider
gap list is reconstructed in `survivorship_gap_*.csv` (76 candidates, 30 with extractable XBRL);
the pre-XBRL remainder is LLM-only territory.

**9. Manager-level rollups.** Every fund-level cut above also rolls up to the parent asset
manager via the curated `managers.py` CIK map, which is where family-level concentration shows
up — several managers run five or more vehicles, and a per-fund table buries that.

### Future research ideas

- **C. Extend the holdings analyses to interval & tender-offer funds.** Their holdings are already
  available as structured **N-PORT** data (in-house), and their financials come from the
  **LLM-over-clean-text** path once M4/M5 run. This would extend the holdings/marks/overlap/bias
  engine well beyond BDCs to the much larger registered-fund universe.

- **D. Unlisted REITs.** They file XBRL-tagged 10-K/10-Q, so they're extractable — but they
  have no CIKs in the universe yet (need sourcing) and need REIT-specific concept maps.

- **E. Holdings-matcher enhancements.** Cross-fiscal-date alignment (today the comparison is
  exact-date only, so funds with different quarter-ends don't line up), and folding the matcher
  output back into the main dataset workbook.

- **F. Tabled XBRL themes.** Derivatives and the Level-3 fair-value roll-forward were
  deliberately deferred in the XBRL expansion (see `docs/XBRL_EXPANSION_PLAN.md`).

---

## Notes for New Users

- **Start with `PROJECT_STATUS.md`'s `⏩ RESUME HERE` block**, not this README. It carries the
  live counts, the quarterly-refresh runbook, and the open follow-ups. One warning it makes
  about itself is worth repeating: the history below that block **records hypotheses in the same
  confident voice as measured results**, and several recorded "diagnoses" have turned out to be
  wrong. From session 21 on it uses explicit **MEASURED** / **ASSUMED** markers. Before acting on
  any diagnosis in that history, check which it is — re-measuring is cheap here.
- `uv sync` builds the environment from the lock file. All scripts run as
  `uv run python <script>` **from inside `sec-extraction-v3/`** — not from the parent directory.
- The `filings/` folder is **not** included if you received only the code. Run `initial_pull.py`
  to populate it (1.5–3 hours). It is needed only for the **N-CSR** path; the BDC XBRL workflow
  fetches from EDGAR directly and never reads it.
- EDGAR requires you to identify yourself with an email address for API access. The scripts use
  a hardcoded `EDGAR_IDENTITY` — if you're running this yourself, update that constant at the
  top of each EDGAR-touching script to your own email.
- `data/` is gitignored in its entirety and fully regenerable. Nothing there is a source of
  truth except the filings themselves; on a fresh clone you rebuild it.
- **Windows/PowerShell quirks that have each cost real time** (the full list is in
  `PROJECT_STATUS.md`): use `git commit -F <file>` for multiline messages, never `-m` with a
  here-string, which leaks its delimiters; never background a long run with a shell `&` (it makes
  the *shell* the tracked process and reports success while the real work continues); don't
  diagnose file encoding in PowerShell, whose `Get-Content` reads UTF-8 as ANSI and once produced
  a false mojibake diagnosis; and avoid non-ASCII in printed output, which the console codepage
  mangles into something that reads like data corruption.
