# SEC Filing Extraction — v3

A pipeline for downloading and extracting structured financial data from SEC EDGAR
filings across a broad set of semiliquid funds (interval funds, tender offer funds,
unlisted BDCs, and unlisted REITs). Extraction is live for the BDC pilot (XBRL-based);
interval/HTML funds are a later phase.

---

## Project Structure

```
sec-extraction-v3/
├── data/
│   ├── fund_universe.csv          # Master list of all funds + CIKs + categories
│   ├── extracted/                 # One JSON per filing (gitignored, regenerable)
│   ├── holdings/                  # One CSV per filing — schedule of investments (gitignored)
│   ├── review_queue/              # index.txt of filings flagged for review (gitignored)
│   └── dataset/                   # Assembled analysis workbook (gitignored, rebuildable)
├── docs/
│   └── DATA_DICTIONARY.md         # What financial data we extract (the spec)
├── src/
│   ├── fund_universe/
│   │   ├── build_universe.py      # Step 1: Build the initial fund list
│   │   ├── enrich_from_mstar.py   # Step 2: Merge Morningstar fund list in
│   │   └── add_vehicle_type.py    # Step 3: Tag funds with Vehicle Type + Mstar data
│   ├── downloader/
│   │   ├── initial_pull.py        # Step 4: Download all historical filings
│   │   └── update_pull.py         # Step 5: Check for new filings (run periodically)
│   ├── schema/
│   │   └── models.py              # Typed schema for extracted data (pydantic)
│   ├── extraction/
│   │   ├── bdc_xbrl.py            # BDC XBRL extractor (maps us-gaap concepts → schema)
│   │   └── run_extraction.py      # Resumable runner: extract → validate → per-filing JSON
│   ├── validation/
│   │   └── rules.py               # Validation layer (identity checks C1–C5/C7 + reasonableness)
│   └── output/
│       └── build_spreadsheet.py   # Assemble the JSONs into the analysis workbook
├── United States Semiliquid Funds Mstar.xlsx       # Morningstar input (universe build)
├── semiliquid fund categorization Mstar.xlsx       # Morningstar input (vehicle types)
└── PROJECT_STATUS.md              # Running log of decisions and progress
```

The `filings/` folder (where downloaded HTML files are saved) lives **one level up**
from this folder, at `SEC Filing Extraction/filings/`. It is not inside `sec-extraction-v3/`
because it can grow very large and is shared across project versions.

---

## First-Time Setup

### 1. Create a virtual environment

From inside the `sec-extraction-v3/` folder:

```bash
uv venv
```

### 2. Install dependencies

```bash
uv pip install edgartools pandas openpyxl rapidfuzz --link-mode=copy
```

> **Note on `--link-mode=copy`:** This flag is required if this folder is inside
> OneDrive, iCloud, or any cloud-synced directory (these block the hardlinks that
> `uv` uses by default). If the project is on a regular local drive, you can omit
> `--link-mode=copy` and just run `uv pip install edgartools pandas openpyxl rapidfuzz`.

### 3. Corporate networks (SSL inspection)

On a corporate network that does SSL inspection (e.g. the Morningstar machine), EDGAR
HTTPS calls fail with `SSLVerificationError: CERTIFICATE_VERIFY_FAILED`. Every
EDGAR-touching script already handles this by calling `configure_http(use_system_certs=True)`
right after `set_identity()` — this uses the Windows certificate store (which trusts the
corporate root CA) and is harmless on home networks. No action needed; just be aware that
this line is required and shouldn't be removed.

---

## Scripts — What They Do and When to Run Them

### `src/fund_universe/build_universe.py`

**What it does:**
Builds `data/fund_universe.csv` from scratch. It works in two steps:
1. Scans any existing `.htm` files in the `filings/` folder and extracts fund names
   and CIKs from the filenames (fast, no internet needed)
2. Queries SEC EDGAR for all N-23C3A filers (repurchase offer notifications) — only
   interval funds file this form, making it the cleanest way to identify them

**When to run:**
Only needed once to create the initial fund list, or if you want to completely
rebuild it from scratch.

```bash
uv run python src/fund_universe/build_universe.py
```

---

### `src/fund_universe/enrich_from_mstar.py`

**What it does:**
Merges the Morningstar semiliquid fund list (`United States Semiliquid Funds Mstar.xlsx`)
into `fund_universe.csv`. It does this in two passes:
1. **CIK match:** Funds that have a CIK in Morningstar are joined directly
2. **Name match:** Remaining funds (no CIK) are fuzzy-matched by name against the
   existing universe; any not found are added as `category = unknown`

Funds added as "unknown" have no CIK and will be skipped by the downloader until
a CIK is sourced for them.

**When to run:**
After `build_universe.py`, and again whenever the Morningstar Excel file is updated
with new funds or newly added CIKs.

```bash
uv run python src/fund_universe/enrich_from_mstar.py
```

---

### `src/fund_universe/add_vehicle_type.py`

**What it does:**
Tags each fund with a **Vehicle Type** and copies over Morningstar identifier/category
data, sourced from the four tabs of `semiliquid fund categorization Mstar.xlsx`
(Interval Funds, Tender Offer Funds, Unlisted BDCs, Unlisted REITs). Matching is
CIK-first (where the workbook has a CIK), then fuzzy name match. Funds on no tab get
`vehicle_type = unknown`. Adds the columns: `vehicle_type`, `mstar_ticker`, `isin`,
`morningstar_category`, `us_category_group`, `morningstar_category_broad_group`.

**When to run:**
After the universe exists, and again whenever the categorization workbook is updated.

```bash
uv run python src/fund_universe/add_vehicle_type.py
```

---

### `src/downloader/initial_pull.py`

**What it does:**
Downloads all historical filings (back to 2016) for every fund in `fund_universe.csv`
that has a CIK. Files are saved to the `filings/` folder using the naming convention:

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
- Saves progress to `fund_universe.csv` after each fund — if the script crashes,
  restart it and it picks up where it left off
- `TEST_MODE_LIMIT = 3` at the top of the file lets you test on 3 funds before
  committing to the full run; set it to `None` for the full download

**When to run:**
Once, after `build_universe.py` and `enrich_from_mstar.py`. Expect 1.5–3 hours
for a full run across ~334 funds.

```bash
uv run python src/downloader/initial_pull.py
```

---

### `src/downloader/update_pull.py`

**What it does:**
Checks for new filings filed since the last time each fund was checked. It reads the
`last_checked` date from `fund_universe.csv` for each fund and asks EDGAR for anything
filed after that date. Downloads new filings and updates `last_checked` to today.

If a fund has never been checked, it falls back to 2016-01-01 (same as the initial
pull) so no filings are missed.

The summary at the end shows how many funds had new filings vs. were already up to date.

**When to run:**
Periodically — once a month is usually enough for this type of fund. Run it the same
way as the initial pull.

```bash
uv run python src/downloader/update_pull.py
```

---

## fund_universe.csv — The Master Fund List

This CSV is the backbone of the whole pipeline. Every script reads from or writes to it.

| Column | Description |
|---|---|
| `cik` | 10-digit zero-padded SEC EDGAR identifier. Empty if unknown. |
| `fund_name` | Full legal name of the fund |
| `category` | `interval_fund`, `ncsr_fund`, `bdc`, `reit`, or `unknown` (derived from EDGAR forms/SIC) |
| `form_types` | Pipe-separated list of forms this fund has been seen filing |
| `last_filing_date` | Most recent filing date found in our filings folder (ISO `YYYY-MM-DD`) |
| `last_checked` | Date this fund was last queried against EDGAR (ISO `YYYY-MM-DD`) |
| `notes` | Free-text notes, e.g., data source or quirks |
| `vehicle_type` | `Interval Fund`, `Tender Offer Fund`, `Unlisted BDC`, `Unlisted REIT`, or `unknown` (from the categorization workbook — see `add_vehicle_type.py`) |
| `mstar_ticker` | Morningstar ticker, where available |
| `isin` | ISIN identifier, where available |
| `morningstar_category` | Morningstar category (e.g. "Private Debt - Direct Lending") |
| `us_category_group` | Morningstar US category group |
| `morningstar_category_broad_group` | Morningstar broad group |

**Important — reading the file:** Always read with `dtype={"cik": str}` in pandas,
otherwise leading zeros in CIKs are stripped (e.g., `"0001748680"` → `1748680`).

**Important — do NOT open this CSV in Excel and save it.** Excel auto-reformats the
`last_filing_date` / `last_checked` columns from ISO `YYYY-MM-DD` into US `M/D/YYYY`
on save, corrupting the format. To view it in a spreadsheet, open a *copy* or import
it as text. Dates in this file should always be ISO `YYYY-MM-DD`.

---

## Extraction & Output

The extraction pipeline is built and running for the **BDC pilot** (~24 funds, 300
filings). Data is pulled from structured **XBRL** via `edgartools`; interval funds
(HTML, no XBRL) are a later phase. Foundation:

- **`docs/DATA_DICTIONARY.md`** — the spec for every field we collect (balance sheet,
  per-class NAV, income incl. PIK breakout, fair-value hierarchy, schedule of
  investments, etc.), with units, sources, and the validation rules.
- **`src/schema/models.py`** — the typed (pydantic) version of that spec; validates
  extracted data and generates the output columns. `pydantic` ships with `edgartools`,
  so no extra install is needed.

### Data flow (extraction → analysis workbook)

Per-filing JSON is the staging layer / source of truth; the spreadsheet is a derived
view that can be rebuilt anytime without re-extracting.

```
  filing  (XBRL fetched from EDGAR)
      │
      ▼  [extractor: bdc_xbrl.py]  — map us-gaap XBRL facts into the schema + compute derived
  FilingExtraction object   (pydantic validates structure)
      │
      ▼  [validation: rules.py]  — run C1–C5/C7 + reasonableness; attach results + review_flags
      │
      ├─ write  data/extracted/<cik>_<form>_<reporting_date>.json    ← one file per filing
      ├─ write  data/holdings/<cik>_<form>_<reporting_date>.csv       ← schedule-of-investments rows
      └─ append flagged filings →  data/review_queue/index.txt
      │
      ▼  [assembler: build_spreadsheet.py]  — read ALL extracted JSONs, pivot
  data/dataset/semiliquid_bdc_dataset.xlsx   ← derived, rebuildable view (5 tabs)
```

Why the JSON layer: crash-safe incremental runs, idempotent re-runs (skip already-
extracted filings), full auditability (every cell traces to a source filing with its
provenance + confidence), and decoupling (restructure the spreadsheet without
re-extracting). Point-in-time fields are keyed on `reporting_date`; flow fields
(income, distributions) carry `period_start` / `period_months` (3 = quarter, 12 = annual).
Holding-level rows are stored SEPARATELY (per-filing CSVs in `data/holdings/`) so the
validated core JSON stays lean; the §9 summary metrics derived from them live in the JSON.

---

### `src/extraction/run_extraction.py` — the runner

**What it does:**
For every BDC fund with a CIK, pulls all 10-K / 10-Q filings since 2016, extracts each
(`bdc_xbrl.extract_filing`), validates it (`rules.validate`), and writes one JSON per
filing to `data/extracted/` plus a holdings CSV to `data/holdings/`. Resumable
(skips filings whose JSON already exists), crash-safe (writes per filing), and robust
(per-filing try/except; filings without XBRL are logged and skipped).

```bash
uv run python src/extraction/run_extraction.py            # full run (network-bound, minutes)
uv run python src/extraction/run_extraction.py --max-funds 2 --max-filings 2   # quick test
```

**⚠️ After changing the extractor, you MUST clear `data/extracted/` before re-running** —
the runner skips filings whose JSON already exists, so a re-run over the existing output is
a no-op and your changes won't take effect:

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

The run prints a summary: `written / skipped / review / no_xbrl / errors`. The
authoritative pass/review counts come from the JSONs themselves (the printed `review`
counter can be skewed if a run is interrupted and restarted, since the index appends).

---

### `src/output/build_spreadsheet.py` — the analysis workbook

**What it does:**
Reads every JSON in `data/extracted/` and writes `data/dataset/semiliquid_bdc_dataset.xlsx`
with five tabs: **Data** (one row per filing, ~70 fields), **ShareClasses** (one row per
filing × class), **Review** (flagged filings + a validation-code key), **Check (Gold)** (a
hand-verification view for ~15 representative filings with a self-computing accuracy %), and
**Definitions** (every derived/calculated field with its formula + methodology). Flag-and-keep
values are visibly marked (status/flags columns, amber row tint, amber on the specific cells
tied to each failing rule).

```bash
uv run python src/output/build_spreadsheet.py
```

> Close the workbook in Excel before rebuilding — Windows won't let the script overwrite an
> open file (`PermissionError`).

---

## Notes for New Users

- The `filings/` folder is **not** included if you received only the code. You will
  need to run `initial_pull.py` to populate it (this takes 1.5–3 hours).
- EDGAR requires you to identify yourself with an email address for API access.
  The scripts use `brianpmoriarty@gmail.com` — if you're running this yourself,
  update the `EDGAR_IDENTITY` constant at the top of each script to your own email.
- All scripts are run with `uv run python <script>` from inside the `sec-extraction-v3/`
  folder. Do not run them from the parent directory.
