"""
churn_sizing.py — Size the BDC birth/death population from EDGAR (full history).

Pulls EVERY N-54A (election to be a BDC = "birth") and N-54C (withdrawal = "death")
across all available EDGAR years, dedupes to one row per CIK (earliest birth, latest
death), and reports the census: ever-born, dead, survivors, and the left-censored set
(filed N-54C but no electronic N-54A = born before EDGAR). This is the read-only sizing
step for the churn analysis; it also bootstraps the Phase-0 census (saves raw CSVs and
caches the EDGAR indexes so the full build doesn't re-download them).

Writes (incremental): data/churn_births_raw.csv, data/churn_deaths_raw.csv

Run: uv run python src/analysis/churn_sizing.py
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

import pandas as pd
from edgar import set_identity, configure_http, get_filings

ROOT = Path(__file__).resolve().parents[2]
BIRTHS = ROOT / "data" / "churn_births_raw.csv"
DEATHS = ROOT / "data" / "churn_deaths_raw.csv"
START_YEAR = 1994            # EDGAR electronic filings begin ~1994
END_YEAR = 2026


def pull(form: str, out_path: Path) -> pd.DataFrame:
    """All filings of `form` across START..END, written incrementally; returns the rows."""
    rows: list[dict] = []
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["cik", "company", "date"])
        w.writeheader()
        for yr in range(START_YEAR, END_YEAR + 1):
            df = None
            for attempt in range(3):
                try:
                    fs = get_filings(form=form, year=yr)
                    df = fs.to_pandas() if fs is not None else None
                    break
                except Exception as ex:
                    if attempt < 2 and any(k in repr(ex).lower() for k in
                                           ("timeout", "timed out", "connect", "remoteprotocol")):
                        time.sleep(2 * (attempt + 1)); continue
                    df = None
            n = 0
            if df is not None and len(df):
                for _, r in df.iterrows():
                    rec = {"cik": str(r.get("cik")).zfill(10),
                           "company": r.get("company"),
                           "date": str(r.get("filing_date"))[:10]}
                    rows.append(rec); w.writerow(rec); n += 1
                fh.flush()
            print(f"  {form} {yr}: {n}")
    return pd.DataFrame(rows)


def main() -> None:
    set_identity("brianpmoriarty@gmail.com")
    configure_http(use_system_certs=True)

    print("=== Pulling N-54A (births) ===")
    births = pull("N-54A", BIRTHS)
    print("\n=== Pulling N-54C (deaths) ===")
    deaths = pull("N-54C", DEATHS)

    # Dedupe to one row per CIK
    b = births.sort_values("date").drop_duplicates("cik", keep="first") if len(births) else births
    d = deaths.sort_values("date").drop_duplicates("cik", keep="last") if len(deaths) else deaths
    born = set(b["cik"]) if len(b) else set()
    died = set(d["cik"]) if len(d) else set()

    print("\n" + "=" * 60)
    print("BDC CENSUS SIZING")
    print(f"  N-54A filings total: {len(births)}  -> distinct CIKs (ever-born): {len(born)}")
    print(f"  N-54C filings total: {len(deaths)}  -> distinct CIKs (dead):      {len(died)}")
    print(f"  Survivors (born, not dead):              {len(born - died)}")
    print(f"  Dead WITH a birth on record:             {len(born & died)}")
    print(f"  Dead WITHOUT a birth (pre-EDGAR / left-censored): {len(died - born)}")
    if len(b):
        print(f"  Birth date range: {b['date'].min()} .. {b['date'].max()}")
    if len(d):
        print(f"  Death date range: {d['date'].min()} .. {d['date'].max()}")

    # Births & deaths by 5-year bucket (rough shape of the time series)
    def bucket(frame, label):
        if not len(frame):
            return
        f = frame.copy()
        f["yr"] = f["date"].str[:4].astype(int)
        f["bkt"] = (f["yr"] // 5) * 5
        print(f"\n  {label} by 5-year bucket:")
        for bkt, c in f.groupby("bkt").size().items():
            print(f"    {bkt}-{bkt+4}: {c}")

    bucket(b, "Births (distinct CIK, first N-54A)")
    bucket(d, "Deaths (distinct CIK, last N-54C)")


if __name__ == "__main__":
    main()
