"""
initial_pull.py — Download all historical filings for funds in fund_universe.csv.

For each fund that has a CIK, this script:
  1. Asks EDGAR for all relevant filings since START_DATE
  2. Downloads the HTML for each filing
  3. Saves it to the shared filings/ folder using the standard naming convention
  4. Skips any file that already exists (safe to interrupt and re-run)
  5. Updates last_checked in fund_universe.csv after each fund

Forms downloaded per category:
  interval_fund  ->  N-CSR, N-CSRS, N-23C3A
  ncsr_fund      ->  N-CSR, N-CSRS
  bdc            ->  10-K, 10-Q, SC TO-I
  reit           ->  10-K, 10-Q, SC TO-I
  unknown        ->  (skipped — no CIK available)

Run with: uv run python src/downloader/initial_pull.py
"""

import re
import time
from pathlib import Path
from datetime import date

import pandas as pd
from edgar import set_identity, Company

# ── Configuration ─────────────────────────────────────────────────────────────

# Your email is required by SEC EDGAR for API access identification
EDGAR_IDENTITY = "brianpmoriarty@gmail.com"

# This file lives at sec-extraction-v3/src/downloader/initial_pull.py
# parents[0] = downloader/   parents[1] = src/   parents[2] = sec-extraction-v3/
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# filings/ lives next to sec-extraction-v3/, not inside it
FILINGS_DIR = PROJECT_ROOT.parent / "filings"

UNIVERSE_FILE = PROJECT_ROOT / "data" / "fund_universe.csv"

# Pull all filings from this date forward (~10 years of history)
START_DATE = "2016-01-01"

# Pause between individual file downloads — keeps us well under SEC rate limits
API_PAUSE_SECONDS = 0.3

# Set to a small number (e.g., 3) to do a quick test run on just the first N funds.
# Set to None to process all funds.
TEST_MODE_LIMIT = None

# What to download for each fund category.
# Adjust this dict if you want to add or remove form types later.
FORMS_BY_CATEGORY = {
    "interval_fund": ["N-CSR", "N-CSRS", "N-23C3A"],
    "ncsr_fund":     ["N-CSR", "N-CSRS"],
    "bdc":           ["10-K", "10-Q", "SC TO-I"],
    "reit":          ["10-K", "10-Q", "SC TO-I"],
    "unknown":       [],   # no CIK — nothing to download
}

# Characters that are illegal in Windows/Mac filenames — replaced with underscore
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\s]+')


# ── Filename helpers ───────────────────────────────────────────────────────────

def safe(s: str) -> str:
    """Replace any filename-unsafe characters (including spaces) with underscores."""
    return _BAD_CHARS.sub("_", s).strip("_")


def build_filename(fund_name: str, cik: str, form_type: str, filing_date) -> str:
    """
    Build our standard filename for one filing.

    Format:  FundName_CIK_FormType_YYYY-MM-DD.htm
    Example: Cliffwater_Corporate_Lending_Fund_0001735964_N-CSRS_2022-12-09.htm

    Note: form types with spaces (like "SC TO-I") become "SC_TO-I" in the filename.
    """
    return f"{safe(fund_name)}_{cik}_{safe(form_type)}_{filing_date}.htm"


# ── Per-fund download logic ────────────────────────────────────────────────────

def download_fund(fund_name: str, cik: str, category: str,
                  filings_dir: Path) -> tuple[int, int]:
    """
    Download all relevant filings for one fund since START_DATE.

    Returns (downloaded_count, skipped_count).
    Skipped means the file already existed — not an error.
    """
    forms = FORMS_BY_CATEGORY.get(category, [])
    if not forms:
        return 0, 0

    downloaded = 0
    skipped = 0

    try:
        # Get the filing index for this company — filtered to our form types and date range.
        # edgartools fetches the index (fast), then we download HTML lazily per filing.
        filings = (
            Company(int(cik))
            .get_filings(form=forms)
            .filter(filing_date=f"{START_DATE}:")
        )

        for filing in filings:
            filename = build_filename(fund_name, cik, filing.form, filing.filing_date)
            dest = filings_dir / filename

            # Skip if we already have this file
            if dest.exists():
                skipped += 1
                continue

            # Download the filing's primary HTML document
            try:
                html = filing.html()
                if html:
                    dest.write_text(html, encoding="utf-8")
                    print(f"      + {filename}")
                    downloaded += 1
                else:
                    # Some old filings exist only as plain text — skip them for now
                    print(f"      ~ No HTML available: {filename}")

            except Exception as e:
                print(f"      ERROR downloading {filename}: {e}")

            # Pause between downloads to be a polite API consumer
            time.sleep(API_PAUSE_SECONDS)

    except Exception as e:
        print(f"  ERROR fetching filings for {fund_name} (CIK {cik}): {e}")

    return downloaded, skipped


# ── Main ──────────────────────────────────────────────────────────────────────

def initial_pull():
    set_identity(EDGAR_IDENTITY)
    today = date.today().isoformat()

    # Make sure the filings directory exists
    FILINGS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Load fund universe ──
    # Read CIK as string so pandas doesn't strip leading zeros
    # (e.g., "0001748680" would become integer 1748680 if read without dtype)
    universe = pd.read_csv(UNIVERSE_FILE, dtype={"cik": str})

    # Zero-pad valid CIKs to 10 digits; keep empty CIKs empty (don't pad them)
    universe["cik"] = universe["cik"].fillna("").apply(
        lambda x: x.strip().zfill(10) if x.strip() else ""
    )

    # ── Filter to downloadable funds ──
    # A fund is eligible if it has a CIK and a category that has forms to download
    downloadable_categories = [cat for cat, forms in FORMS_BY_CATEGORY.items() if forms]
    to_process = universe[
        (universe["cik"] != "") &
        universe["category"].isin(downloadable_categories)
    ].copy()

    # ── Summary before starting ──
    print("=" * 60)
    print("INITIAL PULL — SEC Filing Downloader")
    print("=" * 60)
    print(f"Universe total:       {len(universe)} funds")
    print(f"Eligible for download: {len(to_process)} funds")
    print(f"Skipped (no CIK):     {len(universe) - len(to_process)} funds")
    print(f"\nPulling filings from {START_DATE} onward")
    print(f"\nBreakdown by category:")
    print(to_process["category"].value_counts().to_string())
    print()
    print(f"Files already in filings/: {len(list(FILINGS_DIR.glob('*.htm')))}")
    print("=" * 60)
    print()

    total_downloaded = 0
    total_skipped = 0

    # ── Process each fund ──
    if TEST_MODE_LIMIT:
        print(f"*** TEST MODE: processing only the first {TEST_MODE_LIMIT} funds ***\n")
        to_process = to_process.head(TEST_MODE_LIMIT)

    for i, (df_idx, row) in enumerate(to_process.iterrows(), start=1):
        cik = row["cik"]
        fund_name = row["fund_name"]
        category = row["category"]
        forms = FORMS_BY_CATEGORY.get(category, [])

        print(f"[{i}/{len(to_process)}] {fund_name}")
        print(f"  CIK: {cik}  |  Category: {category}  |  Forms: {', '.join(forms)}")

        downloaded, skipped = download_fund(fund_name, cik, category, FILINGS_DIR)

        total_downloaded += downloaded
        total_skipped += skipped

        # Mark this fund as checked today
        universe.loc[df_idx, "last_checked"] = today

        # Save the updated universe after every fund — if the script crashes,
        # we won't lose progress on funds already processed
        universe.to_csv(UNIVERSE_FILE, index=False)

        print(f"  -> {downloaded} new, {skipped} already existed")
        print()

    # ── Final summary ──
    print("=" * 60)
    print("DONE")
    print(f"  Total new files downloaded: {total_downloaded}")
    print(f"  Total files already existed: {total_skipped}")
    print(f"  Files now in filings/:       {len(list(FILINGS_DIR.glob('*.htm')))}")
    print("=" * 60)


if __name__ == "__main__":
    initial_pull()
