# SEC Filing Extraction — v3

A pipeline for downloading and eventually extracting financial data from SEC EDGAR
filings across a broad set of semiliquid funds (interval funds, tender offer funds,
unlisted BDCs, and unlisted REITs).

---

## Project Structure

```
sec-extraction-v3/
├── data/
│   └── fund_universe.csv          # Master list of all known funds + CIKs
├── src/
│   ├── fund_universe/
│   │   ├── build_universe.py      # Step 1: Build the initial fund list
│   │   └── enrich_from_mstar.py   # Step 2: Merge Morningstar fund list in
│   └── downloader/
│       ├── initial_pull.py        # Step 3: Download all historical filings
│       └── update_pull.py         # Step 4: Check for new filings (run periodically)
├── United States Semiliquid Funds Mstar.xlsx   # Morningstar input data
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
| `category` | `interval_fund`, `ncsr_fund`, `bdc`, `reit`, or `unknown` |
| `form_types` | Pipe-separated list of forms this fund has been seen filing |
| `last_filing_date` | Most recent filing date found in our filings folder |
| `last_checked` | Date this fund was last queried against EDGAR |
| `notes` | Free-text notes, e.g., data source or quirks |

**Important:** Always read this file with `dtype={"cik": str}` in pandas, otherwise
leading zeros in CIKs will be stripped (e.g., `"0001748680"` becomes `1748680`).

---

## Notes for New Users

- The `filings/` folder is **not** included if you received only the code. You will
  need to run `initial_pull.py` to populate it (this takes 1.5–3 hours).
- EDGAR requires you to identify yourself with an email address for API access.
  The scripts use `brianpmoriarty@gmail.com` — if you're running this yourself,
  update the `EDGAR_IDENTITY` constant at the top of each script to your own email.
- All scripts are run with `uv run python <script>` from inside the `sec-extraction-v3/`
  folder. Do not run them from the parent directory.
