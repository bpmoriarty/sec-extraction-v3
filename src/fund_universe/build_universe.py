"""
build_universe.py — Build the semiliquid fund universe CSV.

Runs in four phases:
  1. Seed from existing filenames in ../filings/ (instant, no API calls)
  2. Query EDGAR for N-23C3 filers to identify interval funds
  3. Categorize each fund (interval_fund / bdc / reit / ncsr_fund)
  4. Discover any interval funds we don't have filings for yet

Output: data/fund_universe.csv
Run with: uv run python src/fund_universe/build_universe.py
"""

import re
import time
from pathlib import Path
from datetime import date

import pandas as pd
from edgar import set_identity, get_filings, Company

# ── Configuration ─────────────────────────────────────────────────────────────

# edgartools requires you to identify yourself to the SEC EDGAR API
EDGAR_IDENTITY = "brianpmoriarty@gmail.com"

# This file lives at sec-extraction-v3/src/fund_universe/build_universe.py
# So we go up two levels to get to sec-extraction-v3/
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The shared filings folder lives next to sec-extraction-v3/, not inside it
FILINGS_DIR = PROJECT_ROOT.parent / "filings"

OUTPUT_FILE = PROJECT_ROOT / "data" / "fund_universe.csv"

# How far back to search for N-23C3 filings (to catch all active interval funds)
INTERVAL_FUND_LOOKBACK_YEARS = 12

# Rate limit: pause between EDGAR API calls so we're polite to the SEC
API_PAUSE_SECONDS = 0.3  # ~3 requests/second — well under any limit

# Regex pattern to parse our filename convention:
# FundName_CIK_FilingType_Date.htm
# Example: 1WS_Credit_Income_Fund_0001748680_N-CSRS_2020-06-30.htm
# The CIK is always a 10-digit number (zero-padded by SEC convention)
FILENAME_RE = re.compile(
    r"^(.+?)_(\d{10})_(N-CSRS?|10-[KQ])_(\d{4}-\d{2}-\d{2})\.htm$"
)

# SIC codes relevant to semiliquid funds
SIC_REIT = 6798       # Real Estate Investment Trusts
# BDCs typically use SIC 6726 (Investment Offices NEC), but we identify them
# primarily by the fact they file 10-K/10-Q and are not REITs


# ── Phase 1: Seed from existing filenames ─────────────────────────────────────

def seed_from_filenames(filings_dir: Path) -> dict:
    """
    Parse every .htm filename in the filings folder to build a starting fund list.

    This runs instantly with no API calls. It gives us a reliable base of
    funds we already have filings for.

    Returns a dict keyed by CIK string:
    {
      "0001748680": {
        "fund_name": "1WS Credit Income Fund",
        "form_types": {"N-CSR", "N-CSRS"},
        "last_filing_date": "2026-01-09"
      }, ...
    }
    """
    funds = {}
    total_files = 0
    skipped = 0

    for filepath in filings_dir.glob("*.htm"):
        total_files += 1
        match = FILENAME_RE.match(filepath.name)
        if not match:
            skipped += 1
            continue

        fund_name_raw, cik, form_type, filing_date = match.groups()

        # Convert underscore-separated name to readable form
        # e.g., "1WS_Credit_Income_Fund" → "1WS Credit Income Fund"
        fund_name = fund_name_raw.replace("_", " ")

        if cik not in funds:
            funds[cik] = {
                "fund_name": fund_name,
                "form_types": set(),
                "last_filing_date": filing_date,
            }

        funds[cik]["form_types"].add(form_type)

        # Track the most recent filing date for this fund
        if filing_date > funds[cik]["last_filing_date"]:
            funds[cik]["last_filing_date"] = filing_date

    print(f"  Parsed {total_files} files -> {len(funds)} unique funds.")
    if skipped:
        print(f"  Skipped {skipped} files (didn't match expected naming convention)")

    return funds


# ── Phase 2: Get interval fund CIKs from EDGAR ────────────────────────────────

def get_interval_fund_ciks() -> set:
    """
    Query EDGAR for all N-23C3 filers.

    N-23C3 is the SEC form "Notification of Repurchase Offer Under Rule 23c-3."
    ONLY interval funds file this form — it's the cleanest way to identify them.

    Returns a set of CIK strings (zero-padded to 10 digits).
    """
    start_year = date.today().year - INTERVAL_FUND_LOOKBACK_YEARS
    date_range = f"{start_year}-01-01:{date.today().isoformat()}"
    print(f"  Querying EDGAR for N-23C3 filings from {start_year} to today...")

    try:
        # "N-23C3A" is the correct EDGAR form name for repurchase offer notifications.
        # (N-23C3B is continuation filings; N-23C3 alone returns nothing.)
        filings = get_filings(form="N-23C3A", filing_date=date_range)
        df = filings.to_pandas()

        # CIK column is an integer; zero-pad to 10 digits to match our filename convention
        ciks = set(df["cik"].astype(str).str.zfill(10))
        print(f"  Found {len(df)} N-23C3A filings -> {len(ciks)} unique interval fund CIKs.")
        return ciks

    except Exception as e:
        print(f"  WARNING: N-23C3 query failed ({e}). Interval funds won't be auto-identified.")
        return set()


# ── Phase 3: Look up SIC code for 10-K/10-Q filers ───────────────────────────

def classify_10k_filer(cik: str) -> str:
    """
    Look up the SEC SIC industry code for a fund that files 10-K or 10-Q.

    REITs have SIC 6798. Everything else in this universe is treated as a BDC.

    Returns 'reit' or 'bdc'.
    """
    try:
        company = Company(int(cik))
        sic = company.sic
        return "reit" if sic == SIC_REIT else "bdc"
    except Exception:
        # If the lookup fails, default to BDC — most 10-K/10-Q filers here are BDCs
        return "bdc"


# ── Phase 4: Look up fund name for new CIKs ───────────────────────────────────

def lookup_fund_name(cik: str) -> str:
    """
    Get the official fund name from EDGAR for a CIK we don't have a filename for.
    """
    try:
        company = Company(int(cik))
        return company.name
    except Exception:
        return f"Unknown Fund (CIK {cik})"


# ── Main ──────────────────────────────────────────────────────────────────────

def build_universe():
    set_identity(EDGAR_IDENTITY)
    today = date.today().isoformat()

    # ─── Phase 1 ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("PHASE 1: Seeding from existing filenames")
    print("=" * 60)
    funds = seed_from_filenames(FILINGS_DIR)

    # ─── Phase 2 ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("PHASE 2: Identifying interval funds via N-23C3 EDGAR query")
    print("=" * 60)
    interval_ciks = get_interval_fund_ciks()

    # ─── Phase 3 ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("PHASE 3: Categorizing each fund")
    print("=" * 60)

    rows = []
    for cik, info in funds.items():
        form_types = sorted(info["form_types"])
        files_10k = "10-K" in form_types or "10-Q" in form_types
        files_ncsr = "N-CSR" in form_types or "N-CSRS" in form_types

        if cik in interval_ciks:
            # Confirmed interval fund by the N-23C3 EDGAR query
            category = "interval_fund"
        elif files_10k:
            # BDC or REIT — need SIC lookup to distinguish
            category = classify_10k_filer(cik)
            time.sleep(API_PAUSE_SECONDS)
        elif files_ncsr:
            # N-CSR filer that is NOT a confirmed interval fund.
            # Could be a tender offer fund or other non-interval closed-end fund.
            # We'll call it "ncsr_fund" for now — easy to refine later.
            category = "ncsr_fund"
        else:
            category = "unknown"

        rows.append({
            "cik": cik,
            "fund_name": info["fund_name"],
            "category": category,
            "form_types": "|".join(form_types),
            "last_filing_date": info["last_filing_date"],
            "last_checked": today,
            "notes": "",
        })
        print(f"  {cik}  {category:<18}  {info['fund_name'][:55]}")

    # ─── Phase 4 ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("PHASE 4: Adding interval funds not yet in our filings folder")
    print("=" * 60)

    existing_ciks = {r["cik"] for r in rows}
    new_ciks = interval_ciks - existing_ciks
    print(f"  {len(new_ciks)} interval funds discovered that we have no filings for yet.")

    for cik in sorted(new_ciks):
        fund_name = lookup_fund_name(cik)
        time.sleep(API_PAUSE_SECONDS)
        rows.append({
            "cik": cik,
            "fund_name": fund_name,
            "category": "interval_fund",
            # These form types are an educated guess — will be confirmed when we download
            "form_types": "N-CSR|N-CSRS",
            "last_filing_date": "",
            "last_checked": today,
            "notes": "discovered via N-23C3 query; no filings downloaded yet",
        })
        print(f"  Added: {cik}  {fund_name}")

    # ─── Save ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("SAVING")
    print("=" * 60)

    df = pd.DataFrame(rows, columns=[
        "cik", "fund_name", "category", "form_types",
        "last_filing_date", "last_checked", "notes"
    ])
    # Sort so it's easy to review: category first, then alphabetically by name
    df = df.sort_values(["category", "fund_name"]).reset_index(drop=True)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_FILE, index=False)

    print(f"\nSaved {len(df)} funds -> {OUTPUT_FILE}")
    print("\nCategory breakdown:")
    print(df["category"].value_counts().to_string())
    print("\nDone! Review data/fund_universe.csv before running initial_pull.py.")


if __name__ == "__main__":
    build_universe()
