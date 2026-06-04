"""
add_vehicle_type.py — Tag each fund in fund_universe.csv with a Vehicle Type
and Morningstar identifier/category data, sourced from the four tabs of
"semiliquid fund categorization Mstar.xlsx".

The four tabs map to vehicle types:
    Interval Funds      -> "Interval Fund"      (has Ticker + CIK)
    Tender Offer Funds  -> "Tender Offer Fund"  (has Ticker + CIK)
    Unlisted BDCs       -> "Unlisted BDC"       (name only, no CIK)
    Unlisted REITs      -> "Unlisted REIT"      (name only, no CIK)

Matching strategy for each universe fund:
    1. CIK match (authoritative) against the two tabs that carry CIKs.
    2. If no CIK match, fuzzy name match across ALL four tabs. This is the
       only option for the BDC/REIT tabs (which have no CIK), and a fallback
       for the others.
Funds that match nothing get vehicle_type = "unknown".

New columns appended to fund_universe.csv:
    vehicle_type, mstar_ticker, isin, morningstar_category,
    us_category_group, morningstar_category_broad_group

Run with: uv run python src/fund_universe/add_vehicle_type.py
"""

import re
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process

# ── Configuration ─────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATEGORIZATION_FILE = PROJECT_ROOT / "semiliquid fund categorization Mstar.xlsx"
UNIVERSE_FILE = PROJECT_ROOT / "data" / "fund_universe.csv"

# Map each tab name to the Vehicle Type label we want to store.
TAB_TO_VEHICLE_TYPE = {
    "Interval Funds": "Interval Fund",
    "Tender Offer Funds": "Tender Offer Fund",
    "Unlisted BDCs": "Unlisted BDC",
    "Unlisted REITs": "Unlisted REIT",
}

# Name-match similarity threshold (0-100). A name match mislabels the vehicle
# type if it's wrong, so this is set higher than the 78 used when *adding*
# funds. Matches between this and REVIEW_CEILING are printed for spot-checking.
FUZZY_THRESHOLD = 85
REVIEW_CEILING = 93


# ── Helpers (mirror enrich_from_mstar.py for consistency) ──────────────────────

SUFFIX_RE = re.compile(
    r'\b(inc|incorporated|llc|lp|ltd|limited|trust|corp|corporation|co|'
    r'the|series|select|class)\b',
    re.IGNORECASE,
)


def normalize(name: str) -> str:
    """Reduce a fund name to core words for fuzzy comparison."""
    if not isinstance(name, str):
        return ""
    name = name.lower()
    name = SUFFIX_RE.sub(" ", name)
    name = re.sub(r"[^a-z0-9 ]", " ", name)   # punctuation -> space
    name = re.sub(r"\s+", " ", name).strip()
    return name


def clean_cik(raw) -> str | None:
    """Convert a raw CIK value to a zero-padded 10-digit string, or None."""
    if pd.isna(raw):
        return None
    s = str(raw).strip()
    if s in ("", "-", "nan", "None"):
        return None
    s = s.split(".")[0]
    if not s.isdigit():
        return None
    return s.zfill(10)


def clean_text(raw) -> str:
    """Normalize a cell to a plain string; Morningstar uses '-' for missing."""
    if pd.isna(raw):
        return ""
    s = str(raw).strip()
    return "" if s in ("-", "nan", "None") else s


# ── Build the lookup from the categorization workbook ──────────────────────────

def build_lookups():
    """
    Read all four tabs and return:
      cik_index:  {cik: record}
      name_records: list of (normalized_name, record)
    where each `record` carries vehicle_type + the copied data fields.
    """
    cik_index = {}
    name_records = []

    for tab, vehicle_type in TAB_TO_VEHICLE_TYPE.items():
        df = pd.read_excel(CATEGORIZATION_FILE, sheet_name=tab)
        has_cik = "Registrant CIK" in df.columns
        has_ticker = "Ticker" in df.columns

        for _, row in df.iterrows():
            legal_name = clean_text(row.get("Fund Legal Name"))
            if not legal_name:
                continue

            record = {
                "vehicle_type": vehicle_type,
                "mstar_ticker": clean_text(row.get("Ticker")) if has_ticker else "",
                "isin": clean_text(row.get("ISIN")),
                "morningstar_category": clean_text(row.get("Morningstar Category")),
                "us_category_group": clean_text(row.get("US Category Group")),
                "morningstar_category_broad_group": clean_text(
                    row.get("Morningstar Category Broad Group")
                ),
                "_source_name": legal_name,
                "_matched": False,   # track which tab rows never matched a fund
            }

            if has_cik:
                cik = clean_cik(row.get("Registrant CIK"))
                if cik:
                    if cik in cik_index:
                        print(f"  [WARN] duplicate CIK {cik} across tabs "
                              f"('{cik_index[cik]['_source_name']}' vs '{legal_name}')")
                    else:
                        cik_index[cik] = record

            name_records.append((normalize(legal_name), record))

    return cik_index, name_records


# ── Main ───────────────────────────────────────────────────────────────────────

def add_vehicle_type():
    universe = pd.read_csv(UNIVERSE_FILE, dtype={"cik": str})
    universe["cik"] = universe["cik"].fillna("").astype(str)

    cik_index, name_records = build_lookups()
    name_keys = [n for n, _ in name_records]

    total_mstar = len(name_records)
    print(f"Categorization workbook: {total_mstar} fund rows across 4 tabs")
    print(f"  with usable CIK: {len(cik_index)}")
    print(f"Universe: {len(universe)} funds\n")

    new_cols = {
        "vehicle_type": [],
        "mstar_ticker": [],
        "isin": [],
        "morningstar_category": [],
        "us_category_group": [],
        "morningstar_category_broad_group": [],
    }

    n_cik, n_name, n_unknown = 0, 0, 0
    review_matches = []   # borderline name matches to print for spot-checking

    for _, fund in universe.iterrows():
        cik = fund["cik"].strip()
        record = None
        how = ""

        # 1) CIK match (authoritative)
        if cik and cik in cik_index:
            record = cik_index[cik]
            how = "cik"
        else:
            # 2) Fuzzy name match across all tabs
            norm = normalize(fund["fund_name"])
            if norm:
                result = process.extractOne(
                    norm, name_keys, scorer=fuzz.token_sort_ratio
                )
                if result and result[1] >= FUZZY_THRESHOLD:
                    record = name_records[result[2]][1]
                    how = "name"
                    if result[1] < REVIEW_CEILING:
                        review_matches.append(
                            (fund["fund_name"], record["_source_name"], result[1])
                        )

        if record is None:
            new_cols["vehicle_type"].append("unknown")
            for c in ("mstar_ticker", "isin", "morningstar_category",
                      "us_category_group", "morningstar_category_broad_group"):
                new_cols[c].append("")
            n_unknown += 1
        else:
            record["_matched"] = True
            new_cols["vehicle_type"].append(record["vehicle_type"])
            new_cols["mstar_ticker"].append(record["mstar_ticker"])
            new_cols["isin"].append(record["isin"])
            new_cols["morningstar_category"].append(record["morningstar_category"])
            new_cols["us_category_group"].append(record["us_category_group"])
            new_cols["morningstar_category_broad_group"].append(
                record["morningstar_category_broad_group"]
            )
            if how == "cik":
                n_cik += 1
            else:
                n_name += 1

    for col, values in new_cols.items():
        universe[col] = values

    universe.to_csv(UNIVERSE_FILE, index=False)

    # ── Report ──────────────────────────────────────────────────────────────
    print("=" * 60)
    print("MATCH SUMMARY")
    print("=" * 60)
    print(f"  Matched by CIK:        {n_cik}")
    print(f"  Matched by name:       {n_name}")
    print(f"  Unmatched (unknown):   {n_unknown}")
    print(f"  Total universe funds:  {len(universe)}\n")

    print("Vehicle Type breakdown:")
    print(universe["vehicle_type"].value_counts().to_string())

    n_unmatched_mstar = sum(1 for _, r in name_records if not r["_matched"])
    print(f"\nCategorization rows with NO universe match (not added): "
          f"{n_unmatched_mstar} of {total_mstar}")

    if review_matches:
        print(f"\nBorderline name matches to spot-check "
              f"(score {FUZZY_THRESHOLD}-{REVIEW_CEILING-1}):")
        for uni_name, mstar_name, score in sorted(review_matches, key=lambda x: x[2]):
            print(f"  [{score:.0f}%] universe: {uni_name[:45]:45s} <- mstar: {mstar_name[:45]}")

    print(f"\nSaved -> {UNIVERSE_FILE}")


if __name__ == "__main__":
    add_vehicle_type()
