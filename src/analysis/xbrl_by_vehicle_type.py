"""
xbrl_by_vehicle_type.py — How many non-BDC funds in the universe carry inline XBRL?

Session 10 established (via parser tests) that interval / tender-offer fund FINANCIALS are
not XBRL-tagged. This checks the question from the cheaper index-flag angle: for each fund of
a given vehicle_type, does ANY of its filings carry the `isInlineXBRL` flag, and on WHICH forms?
Interpretation matters: on a 10-K/10-Q the flag means full financial inline XBRL; on an N-CSR /
N-2 it means thin cover-page tags (cef:/oef:), NOT financial statements.

Reads  : data/fund_universe.csv (vehicle_type + cik)
Writes : data/xbrl_by_vehicle_type.csv  (one row per fund, incremental)

Run: uv run python src/analysis/xbrl_by_vehicle_type.py
"""

from __future__ import annotations

import csv
import time
from collections import Counter
from pathlib import Path

import pandas as pd
from edgar import set_identity, configure_http, Company

ROOT = Path(__file__).resolve().parents[2]
UNIVERSE = ROOT / "data" / "fund_universe.csv"
OUT_CSV = ROOT / "data" / "xbrl_by_vehicle_type.csv"
TYPES = ["Interval Fund", "Tender Offer Fund"]   # REITs have no CIKs in the universe yet
OUT_COLS = ["vehicle_type", "cik", "fund_name", "n_filings", "n_inline",
            "any_inline", "forms_with_inline", "latest_inline_date"]


def check_one(vt: str, cik: str, name: str) -> dict:
    rec = {"vehicle_type": vt, "cik": cik, "fund_name": name, "n_filings": 0, "n_inline": 0,
           "any_inline": False, "forms_with_inline": "", "latest_inline_date": ""}
    for attempt in range(3):
        try:
            fs = Company(int(cik)).get_filings()
            df = fs.to_pandas() if fs is not None else None
            break
        except Exception as ex:
            if attempt < 2 and any(k in repr(ex).lower() for k in
                                   ("timeout", "timed out", "connect", "remoteprotocol")):
                time.sleep(2 * (attempt + 1)); continue
            return rec
    if df is None or len(df) == 0 or "isInlineXBRL" not in df:
        rec["n_filings"] = 0 if df is None else len(df)
        return rec
    rec["n_filings"] = len(df)
    inline = df[df["isInlineXBRL"].fillna(0).astype(int) == 1]
    rec["n_inline"] = len(inline)
    rec["any_inline"] = len(inline) > 0
    if len(inline):
        rec["forms_with_inline"] = "|".join(sorted(set(inline["form"].astype(str))))
        rec["latest_inline_date"] = str(inline["filing_date"].max())[:10]
    return rec


def main() -> None:
    set_identity("brianpmoriarty@gmail.com")
    configure_http(use_system_certs=True)
    df = pd.read_csv(UNIVERSE, dtype={"cik": str})
    df["cik"] = df["cik"].fillna("").str.strip()
    sel = df[df["vehicle_type"].isin(TYPES) & (df["cik"] != "")][["vehicle_type", "cik", "fund_name"]]
    sel = sel.drop_duplicates("cik")
    print(f"Checking {len(sel)} funds ({', '.join(TYPES)}) for inline-XBRL...\n")

    summary: dict[str, Counter] = {vt: Counter() for vt in TYPES}
    form_tally: dict[str, Counter] = {vt: Counter() for vt in TYPES}
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLS); w.writeheader()
        for i, (_, row) in enumerate(sel.iterrows()):
            cik = str(row["cik"]).zfill(10)
            rec = check_one(row["vehicle_type"], cik, str(row["fund_name"]))
            w.writerow(rec); fh.flush()
            vt = rec["vehicle_type"]
            summary[vt]["funds"] += 1
            summary[vt]["with_inline"] += 1 if rec["any_inline"] else 0
            for frm in (rec["forms_with_inline"].split("|") if rec["forms_with_inline"] else []):
                form_tally[vt][frm] += 1
            print(f"  [{i+1:>3}/{len(sel)}] {cik} {str(rec['fund_name'])[:34]:34s} "
                  f"inline={'Y' if rec['any_inline'] else '-'} forms={rec['forms_with_inline'][:40]}")
            time.sleep(0.25)

    print("\n" + "=" * 60)
    for vt in TYPES:
        s = summary[vt]
        print(f"{vt}: {s['with_inline']}/{s['funds']} funds have >=1 inline-XBRL filing")
        for frm, n in form_tally[vt].most_common():
            print(f"    {frm:14s} {n} funds")
    print(f"\nWrote {OUT_CSV}")


if __name__ == "__main__":
    main()
