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
import csv
import json
import sys
import time
from pathlib import Path

import pandas as pd
from edgar import set_identity, configure_http, Company

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src" / "extraction"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "validation"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "schema"))
from bdc_xbrl import extract_filing  # noqa: E402
from rules import validate  # noqa: E402
from models import FilingExtraction  # noqa: E402

EDGAR_IDENTITY = "brianpmoriarty@gmail.com"
UNIVERSE_FILE = PROJECT_ROOT / "data" / "fund_universe.csv"
OUT_DIR = PROJECT_ROOT / "data" / "extracted"
HOLDINGS_DIR = PROJECT_ROOT / "data" / "holdings"   # per-filing schedule-of-investments CSVs
REVIEW_INDEX = PROJECT_ROOT / "data" / "review_queue" / "index.txt"
ERROR_LOG = OUT_DIR / "_errors.log"

# Column order for the per-filing holdings CSV (§9 schedule of investments).
HOLDING_COLS = ["issuer", "affiliation", "fair_value", "cost", "principal", "rate", "spread",
                "pik_rate", "floor", "shares", "commitment", "pct_na"]
FORMS = ["10-K", "10-Q"]
SINCE_YEAR = 2016
API_PAUSE = 0.3


def bdc_funds() -> list[dict]:
    """BDC funds with a CIK, from the universe. Each record carries a NORMALIZED vehicle_type:
    'Listed BDC' when the universe tags it so, else 'Unlisted BDC'. The BDC-extraction segment is
    the unlisted set today, so funds selected via category=='bdc' but tagged 'unknown' / 'Tender
    Offer Fund' (e.g. the non-traded BDCs Kennedy Lewis / NC SLF / Terra / Fidelity Private Credit)
    are correctly labeled 'Unlisted BDC' rather than left mislabeled. Future listed BDCs carry the
    'Listed BDC' tag in the universe and are respected here."""
    df = pd.read_csv(UNIVERSE_FILE, dtype={"cik": str})
    df["cik"] = df["cik"].fillna("").str.strip()
    mask = ((df["vehicle_type"].isin(["Unlisted BDC", "Listed BDC"])) | (df["category"] == "bdc")) \
        & (df["cik"] != "")
    recs = df[mask][["cik", "fund_name", "vehicle_type"]].drop_duplicates("cik").to_dict("records")
    for r in recs:
        r["vehicle_type"] = "Listed BDC" if r.get("vehicle_type") == "Listed BDC" else "Unlisted BDC"
    return recs


def _year(filing) -> int | None:
    try:
        return int(str(filing.filing_date)[:4])
    except Exception:
        return None


def run(max_funds=None, max_filings=None, since_year=SINCE_YEAR) -> None:
    set_identity(EDGAR_IDENTITY)
    # Use the OS (Windows) certificate store so EDGAR requests succeed on
    # corporate networks that do SSL inspection. Harmless on home networks —
    # the system store also contains the standard public CAs. See README/SSL notes.
    configure_http(use_system_certs=True)
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
                # amendments=False -> take the ORIGINAL 10-K/10-Q, not a 10-K/A (amendments often
                # lack full XBRL, e.g. ARES CAPITAL's latest filing). Good hygiene for all BDCs.
                filings = company.get_filings(form=form, amendments=False)
            except Exception:
                continue
            done = 0
            for filing in filings:
                yr = _year(filing)
                if yr is not None and yr < since_year:
                    continue
                # Resolve period_of_report (a NETWORK fetch) + process the filing inside the
                # try, with a small retry for transient EDGAR timeouts. Previously period_of_report
                # sat OUTSIDE the try, so one slow EDGAR response aborted the whole run.
                for attempt in range(3):
                    try:
                        rd = str(getattr(filing, "period_of_report", "") or "")[:10] \
                            or str(filing.accession_no)
                        out = OUT_DIR / f"{cik}_{form}_{rd}.json"
                        if out.exists():
                            stats["skipped"] += 1
                            break
                        e = extract_filing(company, filing, cik, form)
                        e.vehicle_type = f.get("vehicle_type")  # fund metadata from the universe
                        validate(e)
                        out.write_text(e.model_dump_json(indent=2, exclude_none=False),
                                       encoding="utf-8")
                        _write_holdings(e, cik, form, rd)
                        stats["written"] += 1
                        if e.validation_status == "review":
                            stats["review"] += 1
                            with open(REVIEW_INDEX, "a", encoding="utf-8") as fh:
                                fh.write(f"{out.name}: {'; '.join(e.review_flags)}\n")
                        print(f"  [{form}] {name[:32]:32s} {rd}  "
                              f"status={e.validation_status}")
                        break
                    except Exception as ex:
                        msg = repr(ex)
                        transient = any(k in msg.lower() for k in
                                        ("timeout", "timed out", "connect", "readerror",
                                         "remoteprotocol", "temporarily"))
                        if transient and attempt < 2:
                            time.sleep(2 * (attempt + 1))   # backoff, then retry
                            continue
                        if "xbrl" in msg.lower() or "NoneType" in msg:
                            stats["no_xbrl"] += 1
                        else:
                            stats["errors"] += 1
                        _log_error(f"{cik} {form}: {msg[:200]}")
                        break
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


def revalidate() -> None:
    """Re-run validation over the existing JSONs in place — NO re-extraction, NO network.

    For validation-rule changes (e.g. a C-rule tweak): reload each filing, re-run validate(),
    and rewrite its validation_status / validation_checks / review_flags. The review index is
    regenerated fresh (not appended). Far cheaper than a full network re-run when only the
    validation logic changed and the extracted data is unchanged.

    Also RE-SYNCS fund metadata from the universe (currently vehicle_type) into each JSON, so a
    metadata-only change (e.g. correcting/adding a vehicle_type tag) lands without re-extraction.
    """
    files = sorted(OUT_DIR.glob("*.json"))
    if not files:
        print(f"No JSONs to revalidate in {OUT_DIR}")
        return
    vt_by_cik = {str(f["cik"]).zfill(10): f["vehicle_type"] for f in bdc_funds()}
    REVIEW_INDEX.parent.mkdir(parents=True, exist_ok=True)
    stats = {"revalidated": 0, "pass": 0, "review": 0, "changed": 0}
    review_lines: list[str] = []
    for jf in files:
        d = json.loads(jf.read_text(encoding="utf-8"))
        old_status = d.get("validation_status")
        e = FilingExtraction.model_validate(d)
        vt = vt_by_cik.get(str(e.cik).zfill(10))
        if vt:
            e.vehicle_type = vt  # re-sync fund metadata from the universe
        e.review_flags = []  # validate() appends — start clean so we don't duplicate
        validate(e)
        jf.write_text(e.model_dump_json(indent=2, exclude_none=False), encoding="utf-8")
        stats["revalidated"] += 1
        stats[e.validation_status] = stats.get(e.validation_status, 0) + 1
        if e.validation_status != old_status:
            stats["changed"] += 1
        if e.validation_status == "review":
            review_lines.append(f"{jf.name}: {'; '.join(e.review_flags)}")
    REVIEW_INDEX.write_text("\n".join(review_lines) + ("\n" if review_lines else ""),
                            encoding="utf-8")

    print("\n" + "=" * 50)
    print("REVALIDATE SUMMARY (no re-extraction)")
    for k, v in stats.items():
        print(f"  {k:12s} {v}")
    print(f"  review index: {REVIEW_INDEX}")


def _write_holdings(e, cik: str, form: str, rd: str) -> None:
    """Write the filing's holding-level schedule-of-investments rows to a per-filing CSV in
    data/holdings/. No-op when the filing has no holdings (older / LLC filers, no axis). Stored
    SEPARATELY from the core JSON so the validated dataset stays lean (§9 reassessment)."""
    holdings = getattr(e, "_holdings", None)
    if not holdings:
        return
    HOLDINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = HOLDINGS_DIR / f"{cik}_{form}_{rd}.csv"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["cik", "fund_name", "form_type", "reporting_date"] + HOLDING_COLS)
        for h in holdings:
            w.writerow([cik, e.fund_name, form, rd] + [h.get(c) for c in HOLDING_COLS])


def _log_error(line: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(ERROR_LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-funds", type=int, default=None)
    ap.add_argument("--max-filings", type=int, default=None, help="per form, per fund")
    ap.add_argument("--since-year", type=int, default=SINCE_YEAR)
    ap.add_argument("--revalidate", action="store_true",
                    help="re-run validation over existing JSONs in place (no re-extraction)")
    args = ap.parse_args()
    if args.revalidate:
        revalidate()
    else:
        run(max_funds=args.max_funds, max_filings=args.max_filings, since_year=args.since_year)
