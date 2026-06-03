"""
update_pull.py — Check for and download new filings for all funds in fund_universe.csv.

For each fund with a CIK, queries EDGAR for filings filed AFTER that fund's
last_checked date. Downloads anything new, then updates last_checked to today.

Run this periodically (e.g., once a month) to keep the filings folder current.
After the initial_pull.py run completes, last_checked is set for every fund,
so subsequent update runs will only look at the most recent window.

Run with: uv run python src/downloader/update_pull.py
"""

import re
import time
from pathlib import Path
from datetime import date

import pandas as pd
from edgar import set_identity, Company

# ── Configuration ─────────────────────────────────────────────────────────────

EDGAR_IDENTITY = "brianpmoriarty@gmail.com"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FILINGS_DIR = PROJECT_ROOT.parent / "filings"
UNIVERSE_FILE = PROJECT_ROOT / "data" / "fund_universe.csv"

API_PAUSE_SECONDS = 0.3

# Fallback start date for any fund that has never been checked.
# Matches the START_DATE in initial_pull.py so behavior is consistent.
DEFAULT_START_DATE = "2016-01-01"

# Same form mapping as initial_pull.py — edit both if you add a new form type
FORMS_BY_CATEGORY = {
    "interval_fund": ["N-CSR", "N-CSRS", "N-23C3A"],
    "ncsr_fund":     ["N-CSR", "N-CSRS"],
    "bdc":           ["10-K", "10-Q", "SC TO-I"],
    "reit":          ["10-K", "10-Q", "SC TO-I"],
    "unknown":       [],
}

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\s]+')


# ── Helpers (identical to initial_pull.py) ────────────────────────────────────

def safe(s: str) -> str:
    """Replace filename-unsafe characters with underscores."""
    return _BAD_CHARS.sub("_", s).strip("_")


def build_filename(fund_name: str, cik: str, form_type: str, filing_date) -> str:
    """FundName_CIK_FormType_YYYY-MM-DD.htm"""
    return f"{safe(fund_name)}_{cik}_{safe(form_type)}_{filing_date}.htm"


# ── Per-fund check ─────────────────────────────────────────────────────────────

def check_fund(fund_name: str, cik: str, category: str,
               since_date: str, filings_dir: Path) -> tuple[int, int]:
    """
    Look for filings filed after since_date and download any that are new.
    Returns (downloaded, skipped_already_exists).
    """
    forms = FORMS_BY_CATEGORY.get(category, [])
    if not forms:
        return 0, 0

    downloaded = 0
    skipped = 0

    try:
        filings = (
            Company(int(cik))
            .get_filings(form=forms)
            .filter(filing_date=f"{since_date}:")
        )

        for filing in filings:
            filename = build_filename(fund_name, cik, filing.form, filing.filing_date)
            dest = filings_dir / filename

            if dest.exists():
                skipped += 1
                continue

            try:
                html = filing.html()
                if html:
                    dest.write_text(html, encoding="utf-8")
                    print(f"      + {filename}")
                    downloaded += 1
                else:
                    print(f"      ~ No HTML available: {filename}")
            except Exception as e:
                print(f"      ERROR {filename}: {e}")

            time.sleep(API_PAUSE_SECONDS)

    except Exception as e:
        print(f"  ERROR fetching filings for {fund_name} (CIK {cik}): {e}")

    return downloaded, skipped


# ── Main ──────────────────────────────────────────────────────────────────────

def update_pull():
    set_identity(EDGAR_IDENTITY)
    today = date.today().isoformat()

    universe = pd.read_csv(UNIVERSE_FILE, dtype={"cik": str})
    universe["cik"] = universe["cik"].fillna("").apply(
        lambda x: x.strip().zfill(10) if x.strip() else ""
    )

    downloadable_categories = [cat for cat, forms in FORMS_BY_CATEGORY.items() if forms]
    to_process = universe[
        (universe["cik"] != "") &
        universe["category"].isin(downloadable_categories)
    ].copy()

    print("=" * 60)
    print("UPDATE PULL — Check for new SEC filings")
    print("=" * 60)
    print(f"Checking {len(to_process)} funds  |  Today: {today}")
    print("=" * 60)
    print()

    total_new = 0
    funds_with_new = 0

    for i, (df_idx, row) in enumerate(to_process.iterrows(), start=1):
        cik = row["cik"]
        fund_name = row["fund_name"]
        category = row["category"]

        # Use last_checked as the since-date cutoff.
        # If the fund was never checked (empty or NaN), fall back to DEFAULT_START_DATE.
        raw = str(row.get("last_checked", "")).strip()
        since_date = raw if (raw and raw != "nan") else DEFAULT_START_DATE

        print(f"[{i}/{len(to_process)}] {fund_name}  (since {since_date})")

        downloaded, skipped = check_fund(fund_name, cik, category, since_date, FILINGS_DIR)

        total_new += downloaded
        if downloaded > 0:
            funds_with_new += 1
            print(f"  -> {downloaded} new file(s)")
        else:
            print(f"  -> up to date")

        # Update last_checked to today and save after every fund
        universe.loc[df_idx, "last_checked"] = today
        universe.to_csv(UNIVERSE_FILE, index=False)

        print()

    print("=" * 60)
    print("DONE")
    print(f"  Funds with new filings: {funds_with_new}")
    print(f"  Total new files:        {total_new}")
    print(f"  Already up to date:     {len(to_process) - funds_with_new} funds")
    print("=" * 60)


if __name__ == "__main__":
    update_pull()
