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
     **Live today** for **BDCs** (listed + unlisted): 81 funds, 1,088 filings. See
     [Workflow 1](#workflow-1--bdc-xbrl-extraction).
   - **LLM-over-clean-text** — for interval/tender-offer funds, whose N-CSR financial
     statements are **not** XBRL-tagged. **Not built yet** (scoped). See
     [Interval & tender-offer extraction](#interval--tender-offer-extraction-not-yet-built).
3. **Research & analysis** — cross-fund studies built on the extracted data: holdings/mark
   comparison, manager marking bias, portfolio overlap. **Live today.** See
   [Workflow 3](#workflow-3--research--analysis-layer) for how to run them and
   [Research — What This Data Can Do](#research--what-this-data-can-do) for what they answer.

---

## Project Structure

```
sec-extraction-v3/
├── data/
│   ├── fund_universe.csv          # Master list of all funds + CIKs + categories (shared by every layer)
│   ├── extracted/                 # [W1] One JSON per filing (gitignored, regenerable)
│   ├── holdings/                  # [W1] One CSV per filing — schedule of investments (gitignored)
│   ├── review_queue/              # [W1] index.txt of filings flagged for review (gitignored)
│   ├── dataset/                   # [W1/W3] Assembled workbooks + intermediate CSVs (gitignored, rebuildable)
│   │   ├── semiliquid_bdc_dataset.xlsx        #   [W1] the core per-filing dataset (6 tabs)
│   │   ├── holdings_consolidated.csv          #   [W3] all holdings cleaned into one table
│   │   ├── holdings_matched.csv               #   [W3] each holding tagged to its loan/issue
│   │   ├── issues.csv                         #   [W3] one row per distinct loan (issue)
│   │   ├── holdings_marks_comparison.xlsx     #   [W3] cross-BDC mark comparison (9 tabs)
│   │   ├── portfolio_overlap.xlsx             #   [W3] pairwise fund overlap + co-lending network
│   │   ├── marking_bias.xlsx                  #   [W3] manager rich/cheap marking bias
│   │   └── fund_manager_map.csv               #   [W3] curated CIK → manager map
│   ├── survivorship_gap_candidates.csv        # Deregistered-BDC CIKs (N-54C), survivorship workstream
│   ├── survivorship_gap_enriched.csv          #   …enriched with XBRL availability
│   └── xbrl_by_vehicle_type.csv               # Inline-XBRL coverage by vehicle type (probe output)
├── docs/
│   ├── DATA_DICTIONARY.md         # [W1] What financial data we extract (the spec)
│   ├── XBRL_EXPANSION_PLAN.md     # [W1] Roadmap for additional XBRL data themes
│   ├── LISTED_BDC_PLAN.md         # [W1] Plan for incorporating listed BDCs
│   ├── HOLDINGS_COMPARISON_PLAN.md# [W3] Technical plan for the holdings/mark matcher
│   ├── HOLDINGS_RESEARCH_EXPLAINER.md # [W3] Plain-English guide to the marks comparison
│   ├── MARKING_BIAS_PLAN.md       # [W3] Plan + methodology for manager marking bias
│   └── PORTFOLIO_OVERLAP_PLAN.md  # [W3] Plan for portfolio overlap analysis
├── src/
│   ├── fund_universe/             # [W2] Build & maintain the shared fund list
│   │   ├── build_universe.py      #   Build the initial fund list from filenames + EDGAR
│   │   ├── enrich_from_mstar.py   #   Merge the Morningstar fund list in
│   │   └── add_vehicle_type.py    #   Tag funds with Vehicle Type + Morningstar data
│   ├── downloader/                # [W2] Pull filing HTML down from EDGAR
│   │   ├── initial_pull.py        #   Download all historical filings (one-time)
│   │   └── update_pull.py         #   Check for new filings (run periodically)
│   ├── schema/
│   │   └── models.py              # [W1] Typed schema for extracted data (pydantic)
│   ├── extraction/
│   │   ├── bdc_xbrl.py            # [W1] BDC XBRL extractor (maps us-gaap concepts → schema)
│   │   └── run_extraction.py      # [W1] Resumable runner: extract → validate → per-filing JSON
│   ├── validation/
│   │   └── rules.py               # [W1] Validation layer (identity checks + reasonableness)
│   ├── output/
│   │   └── build_spreadsheet.py   # [W1] Assemble the JSONs into the core dataset workbook
│   └── analysis/                  # [W3] Research layer (reads data/holdings/ + data/extracted/)
│       ├── holdings_compare.py    #   Cross-BDC holdings & mark comparison (5-phase matcher)
│       ├── portfolio_overlap.py   #   Pairwise fund overlap + co-lending network
│       ├── marking_bias.py        #   Manager rich/cheap marking bias (stats)
│       ├── managers.py            #   Curated fund → manager map
│       ├── survivorship_enrich.py #   Deregistered-BDC gap list + XBRL availability
│       └── xbrl_by_vehicle_type.py#   Inline-XBRL coverage probe across vehicle types
├── Listed BDCs Mstar.xlsx                          # [W1] Morningstar input (listed-BDC universe)
├── United States Semiliquid Funds Mstar.xlsx       # [W2] Morningstar input (universe build)
├── semiliquid fund categorization Mstar.xlsx       # [W2] Morningstar input (vehicle types)
└── PROJECT_STATUS.md              # Running log of decisions and progress
```

`[W1]` = XBRL extraction · `[W2]` = fund universe & download · `[W3]` = research & analysis.

The `filings/` folder (where downloaded HTML files are saved) lives **one level up**
from this folder, at `SEC Filing Extraction/filings/`. It is not inside `sec-extraction-v3/`
because it can grow very large and is shared across project versions. Note that the live
XBRL workflow does **not** read these files — it fetches XBRL directly from EDGAR (see
Workflow 1). The downloaded HTML is for the future interval/tender extraction path.

---

## First-Time Setup

### 1. Create a virtual environment

From inside the `sec-extraction-v3/` folder:

```bash
uv venv
```

### 2. Install dependencies

```bash
uv pip install edgartools pandas openpyxl rapidfuzz scipy statsmodels matplotlib networkx --link-mode=copy
```

> **Note on `--link-mode=copy`:** This flag is required if this folder is inside
> OneDrive, iCloud, or any cloud-synced directory (these block the hardlinks that
> `uv` uses by default). If the project is on a regular local drive, you can omit
> `--link-mode=copy` and just run `uv pip install edgartools pandas openpyxl rapidfuzz scipy statsmodels matplotlib networkx`.

> **Note on analysis dependencies:** `rapidfuzz`, `scipy`, `statsmodels`, `matplotlib`,
> and `networkx` are required only by the **Workflow-3 research scripts** (`src/analysis/`).
> The extraction pipeline (`run_extraction.py`) and the core spreadsheet builder do not
> need them.

### 3. Corporate networks (SSL inspection)

On a corporate network that does SSL inspection (e.g. the Morningstar machine), EDGAR
HTTPS calls fail with `SSLVerificationError: CERTIFICATE_VERIFY_FAILED`. Every
EDGAR-touching script already handles this by calling `configure_http(use_system_certs=True)`
right after `set_identity()` — this uses the Windows certificate store (which trusts the
corporate root CA) and is harmless on home networks. No action needed; just be aware that
this line is required and shouldn't be removed.

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
| **Unlisted BDC** | 10-K / 10-Q | **XBRL** | XBRL (schedule of investments) | **Live** — part of the 81-fund / 1,088-filing run |
| **Listed BDC** | 10-K / 10-Q | **XBRL** | XBRL (schedule of investments) | **Live** — 55 funds added (session 11) |
| Unlisted REIT | 10-K / 10-Q | XBRL | XBRL | Not built (XBRL-tagged, but needs CIK sourcing + REIT-specific concept maps) |
| Interval Fund | N-CSR / N-CSRS | **LLM-over-clean-text** (no XBRL) | **N-PORT** (in-house) | Not built (financials); holdings available |
| Tender Offer Fund | N-CSR / N-CSRS | **LLM-over-clean-text** (no XBRL) | **N-PORT** (in-house) | Not built (financials); holdings available |

---

## Workflow 1 — BDC XBRL Extraction

This is the **live** extraction pipeline. It targets funds tagged `Unlisted BDC` /
`Listed BDC` (or `category = bdc`) in `fund_universe.csv`, pulls every 10-K / 10-Q since
2016, extracts the financial data, validates it, and writes one JSON per filing. A full run
covers **81 BDC funds → ~1,088 filings (642 pass / 446 review)**.

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
- Saves progress to `fund_universe.csv` after each fund — if the script crashes, restart it
  and it picks up where it left off
- `TEST_MODE_LIMIT = 3` at the top of the file lets you test on 3 funds before committing to
  the full run; set it to `None` for the full download

**When to run:** Once, after the universe is built. Expect 1.5–3 hours for a full run.

```bash
uv run python src/downloader/initial_pull.py
```

### `src/downloader/update_pull.py` — fetch new filings

**What it does:** Checks for new filings filed since the last time each fund was checked. It
reads the `last_checked` date from `fund_universe.csv` for each fund and asks EDGAR for
anything filed after that date. Downloads new filings and updates `last_checked` to today. If
a fund has never been checked, it falls back to 2016-01-01 so no filings are missed.

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
| `last_checked` | Date this fund was last queried against EDGAR (ISO `YYYY-MM-DD`) |
| `notes` | Free-text notes, e.g., data source or quirks |
| `vehicle_type` | `Interval Fund`, `Tender Offer Fund`, `Unlisted BDC`, `Listed BDC`, `Unlisted REIT`, or `unknown` (from the categorization workbook — see `add_vehicle_type.py`) |
| `mstar_ticker` | Morningstar ticker, where available |
| `isin` | ISIN identifier, where available |
| `morningstar_category` | Morningstar category (e.g. "Private Debt - Direct Lending") |
| `us_category_group` | Morningstar US category group |
| `morningstar_category_broad_group` | Morningstar broad group |

**Important — reading the file:** Always read with `dtype={"cik": str}` in pandas, otherwise
leading zeros in CIKs are stripped (e.g., `"0001748680"` → `1748680`).

**Important — do NOT open this CSV in Excel and save it.** Excel auto-reformats the
`last_filing_date` / `last_checked` columns from ISO `YYYY-MM-DD` into US `M/D/YYYY` on save,
corrupting the format. To view it in a spreadsheet, open a *copy* or import it as text. Dates
in this file should always be ISO `YYYY-MM-DD`.

### Interval & tender-offer extraction (not yet built)

Extracting financial data from interval and tender-offer funds is the next major extraction
phase and is **not started**. Unlike BDCs, these funds' **financial statements are not
XBRL-tagged** — their N-CSR / N-CSRS filings carry only thin `cef:` / `oef:` cover-page tags
(NAV/share, expense ratios), not the balance sheet, income statement, cash flow, or schedule
of investments. This was verified at the raw-fact level, not assumed.

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

When this phase begins, it plugs into the same schema, validation layer, review queue, and
spreadsheet assembler as Workflow 1 — only the front-end extractor differs.

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

# 2. Holdings matcher: consolidate + clean + cluster + match, then build the workbook
uv run python src/analysis/holdings_compare.py --build      # writes holdings_consolidated/matched/issues.csv
uv run python src/analysis/holdings_compare.py --workbook   # writes holdings_marks_comparison.xlsx (9 tabs)
#   (diagnostics: --diagnose | --cluster | --issues ; --threshold N tunes the fuzzy merge)

# 3. Fund → manager map (needed by marking bias)
uv run python src/analysis/managers.py                      # writes fund_manager_map.csv  (--review prints flags)

# 4. Cross-fund studies
uv run python src/analysis/portfolio_overlap.py             # writes portfolio_overlap.xlsx
uv run python src/analysis/marking_bias.py                 # writes marking_bias.xlsx
```

Supporting probes (not part of the main pipeline): `survivorship_enrich.py` (deregistered-BDC
gap list + XBRL availability → `survivorship_gap_*.csv`) and `xbrl_by_vehicle_type.py`
(inline-XBRL coverage by vehicle type → `xbrl_by_vehicle_type.csv`).

---

## Research — What This Data Can Do

The pipeline turns thousands of filings into a comparable, validated dataset. Four research
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
warnings. → `holdings_compare.py` → `data/dataset/holdings_marks_comparison.xlsx` (9 tabs).
Plain-English guide: `docs/HOLDINGS_RESEARCH_EXPLAINER.md`.

**3. Manager marking bias.**
Aggregating across every loan two or more managers share, does a given manager *systematically*
mark richer or cheaper than its peers on the same credits? Using a within-loan leave-one-out
deviation (each manager's mark vs. the others on the identical loan), with bootstrap confidence
intervals and a multiple-testing (FDR) correction, this separates real, persistent bias from
noise — revealing which managers are consistently aggressive vs. conservative valuers (e.g.
Prospect / CION mark rich; Barings / Goldman mark cheap). → `marking_bias.py` (with the curated
fund→manager map from `managers.py`) → `data/dataset/marking_bias.xlsx`.

**4. Portfolio overlap & co-lending network.**
Which funds hold the same borrowers, and how concentrated is that overlap? This computes
pairwise fund overlap at both the issuer and the individual-loan grain (common count,
directional share, Jaccard, dollar-weighted overlap, and a hypergeometric "lift" that flags
overlap beyond chance), flags same-manager sister funds, tracks overlap over time, and draws
the co-lending relationships as a network graph. It surfaces both the obvious (same-manager
sister funds nearly identical) and the interesting (cross-manager club deals — e.g. Blackstone
and HPS sharing 121 actual loans). → `portfolio_overlap.py` →
`data/dataset/portfolio_overlap.xlsx`.

### Future research ideas

- **A. BDC churn & survival analysis.** Quantify the full life-cycle of the BDC universe
  (listed + unlisted) using EDGAR's filing index alone — **N-54A = births** (election to be a
  BDC), **N-54C = deaths** (withdrawal), `fund_universe` = current survivors. Births/deaths and
  active-count over time; overall and per-cohort **survival rate**; a Kaplan–Meier survival
  curve and median lifespan; the **death-mechanism split** (liquidation vs. merger — where
  merging a weak fund into a stronger sibling is itself a survivorship-bias mechanism, not a
  benign event); whether attrition was an early wave (2016–2020 non-traded-BDC mortality) or a
  recent consolidation wave; listed vs. unlisted survival; and manager-level churn. Mirrors
  Morningstar's fund-survival methodology (a fund "doesn't survive" if liquidated *or* merged).

- **B. Survivorship-bias correction.** The current universe is survivor-only. Add the
  deregistered BDCs back (the gap list is reconstructed in `survivorship_gap_*.csv`: 76
  candidates, 30 with extractable XBRL), re-run, and document how the headline averages (yields,
  marks, returns) shift once the dead funds are included. Tightly coupled to (A).

- **C. Extend analyses 1–4 to interval & tender-offer funds.** Their holdings are already
  available as structured **N-PORT** data (in-house), and their financials can be added via the
  **LLM-over-clean-text** path. This would extend the holdings/marks/overlap/bias engine well
  beyond BDCs to the much larger registered-fund universe.

- **D. Unlisted REITs.** They file XBRL-tagged 10-K/10-Q, so they're extractable — but they
  have no CIKs in the universe yet (need sourcing) and need REIT-specific concept maps.

- **E. Holdings-matcher enhancements.** Cross-fiscal-date alignment (today the comparison is
  exact-date only, so funds with different quarter-ends don't line up), and folding the matcher
  output back into the main dataset workbook.

- **F. Tabled XBRL themes.** Derivatives and the Level-3 fair-value roll-forward were
  deliberately deferred in the XBRL expansion (see `docs/XBRL_EXPANSION_PLAN.md`).

---

## Notes for New Users

- The `filings/` folder is **not** included if you received only the code. You will need to
  run `initial_pull.py` to populate it (this takes 1.5–3 hours). Note this is only required for
  the future interval/tender extraction — the live BDC XBRL workflow fetches from EDGAR directly
  and does not need it.
- EDGAR requires you to identify yourself with an email address for API access. The scripts use
  a hardcoded `EDGAR_IDENTITY` — if you're running this yourself, update that constant at the
  top of each EDGAR-touching script to your own email.
- All scripts are run with `uv run python <script>` from inside the `sec-extraction-v3/`
  folder. Do not run them from the parent directory.
- `PROJECT_STATUS.md` is the running log of decisions, progress, and hard-won quirks — read it
  to understand the current state and why things are the way they are.
