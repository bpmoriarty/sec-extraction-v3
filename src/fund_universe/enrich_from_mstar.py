"""
enrich_from_mstar.py — Merge the Morningstar semiliquid fund list into
our fund_universe.csv.

Two-pass approach:
  Pass 1 (CIK match):  198 Morningstar funds have a CIK → direct join
  Pass 2 (name match): 310 Morningstar funds have no CIK → fuzzy name match

Funds in Morningstar that are not in our universe get added.
Funds with no CIK that also don't name-match are added with an empty CIK
and a note so we know they exist.

Run with: uv run python src/fund_universe/enrich_from_mstar.py
"""

import re
import time
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process
from edgar import set_identity, configure_http, Company

# ── Configuration ─────────────────────────────────────────────────────────────

EDGAR_IDENTITY = "brianpmoriarty@gmail.com"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MSTAR_FILE = PROJECT_ROOT / "United States Semiliquid Funds Mstar.xlsx"
UNIVERSE_FILE = PROJECT_ROOT / "data" / "fund_universe.csv"

# Similarity threshold (0–100) for name matching.
# 78 catches most name format differences while rejecting clearly different funds.
FUZZY_THRESHOLD = 78

API_PAUSE_SECONDS = 0.3   # polite rate limit for EDGAR calls


# ── Helpers ───────────────────────────────────────────────────────────────────

# Common legal suffixes that vary between sources (e.g., "Inc" vs absent)
# "Corporation" handled separately so we catch both "corp" and "corporation"
SUFFIX_RE = re.compile(
    r'\b(inc|incorporated|llc|lp|ltd|limited|trust|corp|corporation|co|'
    r'the|series|select|class)\b',
    re.IGNORECASE
)

def normalize(name: str) -> str:
    """Reduce a fund name to core words for fuzzy comparison."""
    if not isinstance(name, str):
        return ""
    name = name.lower()
    name = SUFFIX_RE.sub(" ", name)
    name = re.sub(r"[^a-z0-9 ]", " ", name)   # punctuation → space
    name = re.sub(r"\s+", " ", name).strip()
    return name


def clean_cik(raw) -> str | None:
    """
    Convert a raw CIK value to a zero-padded 10-digit string.
    Returns None if the value is missing or invalid.
    """
    if pd.isna(raw):
        return None
    s = str(raw).strip()
    if s in ("", "-", "nan", "None"):
        return None
    # Remove any decimal point (Excel sometimes stores ints as floats like "1965985.0")
    s = s.split(".")[0]
    # Validate it's numeric
    if not s.isdigit():
        return None
    return s.zfill(10)


def determine_category(cik: str) -> tuple[str, str]:
    """
    Look up form types and SIC for a CIK to assign our category.
    Returns (category, form_types_pipe_string).
    """
    try:
        company = Company(int(cik))
        filings = company.get_filings()
        df = filings.to_pandas()

        forms = set(df["form"].str.strip().unique()) if "form" in df.columns else set()
        files_10k = bool(forms & {"10-K", "10-Q"})
        files_ncsr = bool(forms & {"N-CSR", "N-CSRS"})

        sic = getattr(company, "sic", None)

        if files_10k:
            category = "reit" if sic == 6798 else "bdc"
        elif files_ncsr:
            category = "ncsr_fund"
        else:
            category = "unknown"

        relevant = sorted(forms & {"N-CSR", "N-CSRS", "10-K", "10-Q"})
        form_types_str = "|".join(relevant)
        return category, form_types_str

    except Exception:
        return "unknown", ""


# ── Pass 1: CIK-based matching ────────────────────────────────────────────────

def pass1_cik_match(mstar: pd.DataFrame, universe: pd.DataFrame):
    """
    For the 198 Morningstar rows that have a CIK, check which are already
    in fund_universe and which are genuinely new.

    Returns a list of new-fund dicts to append to the universe.
    """
    existing_ciks = set(universe["cik"].astype(str).str.zfill(10))

    mstar_with_cik = mstar[mstar["_cik"].notna()].copy()
    print(f"  Morningstar rows with a CIK: {len(mstar_with_cik)}")

    already_in = mstar_with_cik[mstar_with_cik["_cik"].isin(existing_ciks)]
    truly_new  = mstar_with_cik[~mstar_with_cik["_cik"].isin(existing_ciks)]
    print(f"  Already in universe (by CIK): {len(already_in)}")
    print(f"  Genuinely new (not in universe): {len(truly_new)}")

    new_funds = []
    for _, row in truly_new.iterrows():
        cik = row["_cik"]
        print(f"  [NEW CIK] {cik}  {row['Fund Legal Name'][:55]}", end="  ", flush=True)

        category, form_types = determine_category(cik)
        time.sleep(API_PAUSE_SECONDS)
        print(f"-> {category}")

        new_funds.append({
            "cik": cik,
            "fund_name": row["Fund Legal Name"],
            "category": category,
            "form_types": form_types,
            "last_filing_date": "",
            "notes": f"added from Morningstar; mstar category: {row['Morningstar Category']}",
        })

    return new_funds


# ── Pass 2: Name-based matching (no-CIK rows) ─────────────────────────────────

def pass2_name_match(mstar: pd.DataFrame, universe: pd.DataFrame,
                     existing_ciks: set):
    """
    For the 310 Morningstar rows without a CIK, fuzzy-match Fund Legal Name
    against fund_universe fund_name.

    Returns a list of new-fund dicts for funds not found in the universe.
    """
    mstar_no_cik = mstar[mstar["_cik"].isna()].copy()
    print(f"  Morningstar rows without a CIK: {len(mstar_no_cik)}")

    # Build normalized universe name list for matching
    universe_norm = {normalize(r["fund_name"]): r for _, r in universe.iterrows()}
    universe_norm_keys = list(universe_norm.keys())

    matched_count = 0
    new_funds = []
    unmatched_names = []

    for _, row in mstar_no_cik.iterrows():
        legal_name = row["Fund Legal Name"]
        norm_name = normalize(legal_name)

        result = process.extractOne(
            norm_name,
            universe_norm_keys,
            scorer=fuzz.token_sort_ratio,
        )

        if result and result[1] >= FUZZY_THRESHOLD:
            matched_count += 1
            # Already in universe — nothing to add
        else:
            best_score = result[1] if result else 0
            best_match = result[0] if result else "—"
            unmatched_names.append((legal_name, best_score, best_match, row))

    print(f"  Matched to existing universe (by name): {matched_count}")
    print(f"  Not matched (will be added with no CIK): {len(unmatched_names)}")

    for legal_name, score, best_match, row in unmatched_names:
        print(f"  [NEW NAME] {legal_name[:60]}  (best match score: {score:.0f}%)")
        new_funds.append({
            "cik": "",   # no CIK available
            "fund_name": legal_name,
            "category": "unknown",
            "form_types": "",
            "last_filing_date": "",
            "notes": (
                f"added from Morningstar (no CIK available); "
                f"mstar category: {row['Morningstar Category']}; "
                f"broad group: {row['Morningstar Category Broad Group']}"
            ),
        })

    return new_funds, unmatched_names


# ── Main ──────────────────────────────────────────────────────────────────────

def enrich_from_mstar():
    set_identity(EDGAR_IDENTITY)
    configure_http(use_system_certs=True)  # OS cert store → works behind corporate SSL inspection

    # Load files
    # Read CIK as str so pandas doesn't strip the leading zeros
    # (e.g., "0001748680" → integer 1748680 → loses leading zeros)
    mstar = pd.read_excel(MSTAR_FILE)
    universe = pd.read_csv(UNIVERSE_FILE, dtype={"cik": str})

    # Normalise the CIK column: convert to clean 10-digit strings or None
    mstar["_cik"] = mstar["Registrant CIK"].apply(clean_cik)

    print(f"Morningstar list: {len(mstar)} funds")
    print(f"  With CIK:    {mstar['_cik'].notna().sum()}")
    print(f"  Without CIK: {mstar['_cik'].isna().sum()}")
    print(f"Current universe: {len(universe)} funds\n")

    # Zero-pad all CIKs to 10 digits for consistent comparison
    universe["cik"] = universe["cik"].astype(str).str.zfill(10)
    existing_ciks = set(universe["cik"])

    # ── Pass 1 ────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("PASS 1: CIK-based matching")
    print("=" * 60)
    new_from_cik = pass1_cik_match(mstar, universe)

    # Add pass-1 new funds to the universe before pass 2 so we don't double-add
    if new_from_cik:
        new_cik_df = pd.DataFrame(new_from_cik)
        universe = pd.concat([universe, new_cik_df], ignore_index=True)
        for f in new_from_cik:
            if f["cik"]:
                existing_ciks.add(f["cik"])

    # ── Pass 2 ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("PASS 2: Name-based matching (no-CIK rows)")
    print("=" * 60)
    new_from_name, unmatched_detail = pass2_name_match(mstar, universe, existing_ciks)

    # ── Summary & save ────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  New funds added via CIK match:  {len(new_from_cik)}")
    print(f"  New funds added via name match: {len(new_from_name)}")
    total_new = len(new_from_cik) + len(new_from_name)

    if total_new == 0:
        print("\n  Universe already covers all Morningstar funds. Nothing added.")
        return

    all_new = pd.DataFrame(new_from_cik + new_from_name, columns=[
        "cik", "fund_name", "category", "form_types",
        "last_filing_date", "notes"
    ])

    # Re-load original to do a clean append (universe may have been modified above)
    original = pd.read_csv(UNIVERSE_FILE)
    updated = pd.concat([original, all_new], ignore_index=True)
    updated = updated.sort_values(["category", "fund_name"]).reset_index(drop=True)
    updated.to_csv(UNIVERSE_FILE, index=False)

    print(f"\n  fund_universe.csv: {len(original)} -> {len(updated)} funds")
    print("\n  Final category breakdown:")
    print(updated["category"].value_counts().to_string())


if __name__ == "__main__":
    enrich_from_mstar()
