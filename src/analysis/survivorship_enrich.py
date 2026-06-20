"""
survivorship_enrich.py — Enrich the survivorship-gap candidate CIKs (deregistered BDCs,
sourced from N-54C withdrawals) with their 10-K/10-Q XBRL availability.

For each candidate CIK it pulls the 10-K + 10-Q filing INDEX (not the documents — cheap,
one network call per form) and reads the index's own `isInlineXBRL` / `isXBRL` flags. That
tells us, without extracting anything, how many in-window (2016+) filings the pipeline could
actually read. The point: split the raw 76 into "extractable today" vs "pre-XBRL / LLM-only",
so Path A (survivorship correction) can be scoped on real numbers.

Reads  : data/survivorship_gap_candidates.csv  (cik, company, date)
Writes : data/survivorship_gap_enriched.csv     (one row per CIK, written incrementally)

Run: uv run python src/analysis/survivorship_enrich.py
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

import pandas as pd
from edgar import set_identity, configure_http, Company

ROOT = Path(__file__).resolve().parents[2]
IN_CSV = ROOT / "data" / "survivorship_gap_candidates.csv"
OUT_CSV = ROOT / "data" / "survivorship_gap_enriched.csv"
SINCE_YEAR = 2016
FORMS = ["10-K", "10-Q"]

OUT_COLS = ["cik", "company", "n54c_date", "n_10k", "n_10q", "n_total",
            "n_inline_xbrl", "n_xbrl_any", "earliest_xbrl_year", "latest_filing_date",
            "extractable"]


def _year(s) -> int | None:
    try:
        return int(str(s)[:4])
    except Exception:
        return None


def filings_for(company: Company, form: str) -> pd.DataFrame:
    """In-window (2016+) filings of one form as a dataframe, with the XBRL flags. Empty on miss."""
    for attempt in range(3):
        try:
            fs = company.get_filings(form=form, amendments=False)
            df = fs.to_pandas() if fs is not None else None
            if df is None or len(df) == 0:
                return pd.DataFrame()
            df = df.copy()
            df["__yr"] = df["filing_date"].map(_year)
            return df[df["__yr"].fillna(0) >= SINCE_YEAR]
        except Exception as ex:
            if attempt < 2 and any(k in repr(ex).lower() for k in
                                   ("timeout", "timed out", "connect", "remoteprotocol")):
                time.sleep(2 * (attempt + 1))
                continue
            return pd.DataFrame()
    return pd.DataFrame()


def enrich_one(cik: str, company_name: str, n54c_date: str) -> dict:
    rec = {"cik": cik, "company": company_name, "n54c_date": n54c_date,
           "n_10k": 0, "n_10q": 0, "n_total": 0, "n_inline_xbrl": 0, "n_xbrl_any": 0,
           "earliest_xbrl_year": "", "latest_filing_date": "", "extractable": False}
    try:
        company = Company(int(cik))
    except Exception:
        return rec
    frames = []
    for form in FORMS:
        df = filings_for(company, form)
        rec["n_10k" if form == "10-K" else "n_10q"] = len(df)
        if len(df):
            frames.append(df)
        time.sleep(0.3)
    if not frames:
        return rec
    allf = pd.concat(frames, ignore_index=True)
    rec["n_total"] = len(allf)
    # The index flags inline-XBRL (what our extractor reads) and any-XBRL (incl. old exhibits).
    inline = allf[allf["isInlineXBRL"].fillna(0).astype(int) == 1] if "isInlineXBRL" in allf else allf.iloc[0:0]
    anyx = allf[allf["isXBRL"].fillna(0).astype(int) == 1] if "isXBRL" in allf else allf.iloc[0:0]
    rec["n_inline_xbrl"] = len(inline)
    rec["n_xbrl_any"] = len(anyx)
    rec["latest_filing_date"] = str(allf["filing_date"].max())[:10]
    if len(inline):
        rec["earliest_xbrl_year"] = min(_year(d) for d in inline["filing_date"] if _year(d))
    rec["extractable"] = len(inline) > 0
    return rec


def main() -> None:
    set_identity("brianpmoriarty@gmail.com")
    configure_http(use_system_certs=True)
    cands = pd.read_csv(IN_CSV, dtype={"cik": str})
    print(f"Enriching {len(cands)} candidate CIKs with XBRL availability...\n")

    with open(OUT_CSV, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLS)
        w.writeheader()
        for i, row in cands.iterrows():
            cik = str(row["cik"]).zfill(10)
            rec = enrich_one(cik, str(row.get("company", "")), str(row.get("date", "")))
            w.writerow(rec)
            fh.flush()  # incremental: a crash keeps everything done so far
            flag = "EXTRACTABLE" if rec["extractable"] else "no-inline-xbrl"
            print(f"  [{i+1:>2}/{len(cands)}] {cik} {str(rec['company'])[:38]:38s} "
                  f"10K={rec['n_10k']:>2} 10Q={rec['n_10q']:>2} "
                  f"inlineXBRL={rec['n_inline_xbrl']:>2} ({flag})")

    print(f"\nWrote {OUT_CSV}")


if __name__ == "__main__":
    main()
