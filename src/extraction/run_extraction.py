"""
run_extraction.py — Orchestrate BDC XBRL extraction over many filings.

For each BDC fund (from fund_universe.csv) it pulls every 10-K / 10-Q since SINCE_YEAR,
extracts it (bdc_xbrl.extract_filing), validates it (validation.rules.validate), and
writes one JSON per filing to data/extracted/. The JSON is the staging layer / source of
truth; the spreadsheet (later) is rebuilt from it.

Design:
  - RESUMABLE: skips filings whose JSON already exists (re-run anytime; only new work runs).
  - INCREMENTAL: writes per filing, so a crash mid-run keeps everything done so far.
  - ROBUST: per-filing try/except; filings without XBRL (often older ones) are logged and
    skipped, not fatal.

Tunables (CLI flags): --max-funds, --max-filings, --since-year. Omit for the full run.
Example test:  uv run python src/extraction/run_extraction.py --max-funds 2 --max-filings 2
Full run:      uv run python src/extraction/run_extraction.py
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
from edgar import set_identity, Company

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src" / "extraction"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "validation"))
from bdc_xbrl import extract_filing  # noqa: E402
from rules import validate  # noqa: E402

EDGAR_IDENTITY = "brianpmoriarty@gmail.com"
UNIVERSE_FILE = PROJECT_ROOT / "data" / "fund_universe.csv"
OUT_DIR = PROJECT_ROOT / "data" / "extracted"
REVIEW_INDEX = PROJECT_ROOT / "data" / "review_queue" / "index.txt"
ERROR_LOG = OUT_DIR / "_errors.log"
FORMS = ["10-K", "10-Q"]
SINCE_YEAR = 2016
API_PAUSE = 0.3


def bdc_funds() -> list[dict]:
    """BDC funds with a CIK, from the universe."""
    df = pd.read_csv(UNIVERSE_FILE, dtype={"cik": str})
    df["cik"] = df["cik"].fillna("").str.strip()
    mask = ((df["vehicle_type"] == "Unlisted BDC") | (df["category"] == "bdc")) & (df["cik"] != "")
    return df[mask][["cik", "fund_name"]].drop_duplicates("cik").to_dict("records")


def _year(filing) -> int | None:
    try:
        return int(str(filing.filing_date)[:4])
    except Exception:
        return None


def run(max_funds=None, max_filings=None, since_year=SINCE_YEAR) -> None:
    set_identity(EDGAR_IDENTITY)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REVIEW_INDEX.parent.mkdir(parents=True, exist_ok=True)

    funds = bdc_funds()
    if max_funds:
        funds = funds[:max_funds]
    print(f"BDC funds to process: {len(funds)} | since {since_year} | forms {FORMS}\n")

    stats = {"written": 0, "skipped": 0, "review": 0, "no_xbrl": 0, "errors": 0}

    for f in funds:
        cik, name = f["cik"], f["fund_name"]
        try:
            company = Company(int(cik))
        except Exception as ex:
            _log_error(f"{cik} {name}: company load failed: {ex!r}")
            stats["errors"] += 1
            continue

        for form in FORMS:
            try:
                filings = company.get_filings(form=form)
            except Exception:
                continue
            done = 0
            for filing in filings:
                yr = _year(filing)
                if yr is not None and yr < since_year:
                    continue
                rd = str(getattr(filing, "period_of_report", "") or "")[:10] or str(filing.accession_no)
                out = OUT_DIR / f"{cik}_{form}_{rd}.json"
                if out.exists():
                    stats["skipped"] += 1
                else:
                    try:
                        e = extract_filing(company, filing, cik, form)
                        validate(e)
                        out.write_text(e.model_dump_json(indent=2, exclude_none=False),
                                       encoding="utf-8")
                        stats["written"] += 1
                        if e.validation_status == "review":
                            stats["review"] += 1
                            with open(REVIEW_INDEX, "a", encoding="utf-8") as fh:
                                fh.write(f"{out.name}: {'; '.join(e.review_flags)}\n")
                        print(f"  [{form}] {name[:32]:32s} {rd}  "
                              f"status={e.validation_status}")
                    except Exception as ex:
                        msg = repr(ex)
                        if "xbrl" in msg.lower() or "NoneType" in msg:
                            stats["no_xbrl"] += 1
                        else:
                            stats["errors"] += 1
                        _log_error(f"{cik} {form} {rd}: {msg[:200]}")
                    time.sleep(API_PAUSE)
                done += 1
                if max_filings and done >= max_filings:
                    break

    print("\n" + "=" * 50)
    print("RUN SUMMARY")
    for k, v in stats.items():
        print(f"  {k:10s} {v}")
    print(f"  output dir: {OUT_DIR}")
    if stats["errors"] or stats["no_xbrl"]:
        print(f"  see {ERROR_LOG}")


def _log_error(line: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(ERROR_LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-funds", type=int, default=None)
    ap.add_argument("--max-filings", type=int, default=None, help="per form, per fund")
    ap.add_argument("--since-year", type=int, default=SINCE_YEAR)
    args = ap.parse_args()
    run(max_funds=args.max_funds, max_filings=args.max_filings, since_year=args.since_year)
