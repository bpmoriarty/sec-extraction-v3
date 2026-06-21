"""
churn_enrich.py — Enrich the BDC churn census (Phase 2 prep + mechanism classification).

For every BDC in the census it pulls EDGAR submission metadata once and derives:
  • listed vs unlisted   — does the entity carry an exchange ticker?
  • manager (family)      — curated map first, else a name-derived family token
  • death mechanism       — for dead funds, a heuristic over the filing trail:
        conversion         (kept filing substantive reports AFTER withdrawal)
        scheduled_winddown (drawdown / vintage vehicles built to close on schedule)
        merger             (target-side merger forms: 425 / DEFM14A / N-14 / S-4 …)
        liquidation        (deregistered/delisted, no merger signal, no successor)
        unknown            (no clear signal — flagged for the verification pass)
    plus a confidence flag; low-confidence rows are the manual/LLM verification queue.

Reads : data/dataset/bdc_churn_census.csv, data/dataset/fund_manager_map.csv
Writes: data/dataset/bdc_churn_census_enriched.csv  (incremental)

Run: uv run python src/analysis/churn_enrich.py
"""

from __future__ import annotations

import csv
import re
import time
from pathlib import Path

import pandas as pd
from edgar import set_identity, configure_http, Company

ROOT = Path(__file__).resolve().parents[2]
CENSUS = ROOT / "data" / "dataset" / "bdc_churn_census.csv"
MANAGERS = ROOT / "data" / "dataset" / "fund_manager_map.csv"
OUT = ROOT / "data" / "dataset" / "bdc_churn_census_enriched.csv"

MERGER_FORMS = {"425", "DEFM14A", "PREM14A", "DEFA14A", "N-14", "N-14/A", "N-14 8C",
                "N-14 8C/A", "S-4", "S-4/A"}
DEREG_FORMS = {"15-12B", "15-12G", "15-15D", "25", "25-NSE"}
SUBSTANTIVE = {"10-K", "10-Q", "N-CSR", "N-CSRS"}
# Periodic financial reports — a bona-fide operating BDC files these over a multi-year span.
PERIODIC = {"10-K", "10-K/A", "10-Q", "10-Q/A", "N-CSR", "N-CSRS", "N-CSR/A", "N-CSRS/A",
            "10-KSB", "10-QSB", "10-KSB/A", "N-30D"}
# Forms that only an EXCHANGE-LISTED entity files (registration on / removal from an exchange).
# Current `tickers` is empty for delisted funds, so the filing trail is the reliable signal.
LISTED_FORMS = {"8-A12B", "25", "25-NSE", "15-12B"}
# Form N-2 is the INVESTMENT-COMPANY registration statement. A bona-fide (publicly-registered)
# BDC files it; operating companies that merely elected BDC status register on S-1/SB-2 and do
# not. High precision; slight under-recall on non-offering BDC subsidiaries (acceptable).
N2_FORMS = {"N-2", "N-2/A", "N-2 8C", "N-2 8C/A", "N-2MEF", "N-2ASR", "N-2 POS"}

# Vintage / drawdown vehicles that are built to wind down on schedule (not failures).
SCHEDULED_RX = re.compile(
    r"venture lending\s*&?\s*leasing|"          # WTI's serial VLL funds
    r"credit income fund\s*20\d\d|"             # Guggenheim 2016T / 2019 / 2021 …
    r"\b(19|20)\d\d[- ]?t?\b.*fund|"            # vintage-year-tagged funds
    r"liquidat", re.I)

# Boilerplate stripped when deriving a family token from a fund name.
_SUFFIX_RX = re.compile(
    r"\b(inc|corp|corporation|company|co|llc|l\.?l\.?c|lp|l\.?p|ltd|"
    r"capital|investment|investments|finance|financial|lending|credit|income|"
    r"fund|funds|trust|bdc|partners|holdings|group|the|of|and|&|"
    r"i|ii|iii|iv|v|vi|vii|viii|ix|x)\b", re.I)


def family_token(name: str) -> str:
    """A coarse manager-family label from the fund name (leading distinctive words)."""
    if not name:
        return ""
    n = re.sub(r"[.,/\\&]", " ", str(name))
    n = re.sub(r"\b\d{4}\b", " ", n)                 # drop vintage years
    toks = [t for t in n.split() if t and not _SUFFIX_RX.fullmatch(t)]
    return " ".join(toks[:2]).title() if toks else str(name).split()[0].title()


def enrich_one(cik: str, name: str, status: str, death_date: str) -> dict:
    rec = {"listed": "", "exchange": "", "sic": "", "sic_desc": "", "has_n2": False,
           "n_periodic": 0, "first_report": "", "last_report": "", "span_years": 0,
           "mechanism": "", "mech_confidence": "", "mech_signals": "",
           "name_family": family_token(name)}
    for attempt in range(3):
        try:
            c = Company(int(cik))
            tickers = list(getattr(c, "tickers", None) or [])
            exchanges = [e for e in (getattr(c, "exchanges", None) or []) if e]
            rec["exchange"] = "|".join(str(e) for e in exchanges)[:40]
            rec["sic"] = str(getattr(c, "sic", "") or "")
            rec["sic_desc"] = str(getattr(c, "sic_description", None)
                                  or getattr(c, "industry", "") or "")[:40]
            # Listed = currently has a ticker (survivors) OR ever filed an exchange
            # listing/delisting form (catches dead funds whose tickers are now empty).
            df = c.get_filings().to_pandas()
            forms = set(df["form"].astype(str)) if (df is not None and "form" in df) else set()
            rec["listed"] = "listed" if (tickers or (forms & LISTED_FORMS)) else "unlisted"
            rec["has_n2"] = bool(forms & N2_FORMS)
            # periodic-report footprint (bona-fide operating history proxy)
            if df is not None and "form" in df:
                p = df[df["form"].astype(str).isin(PERIODIC)]
                rec["n_periodic"] = len(p)
                if len(p):
                    dts = sorted(p["filing_date"].astype(str))
                    rec["first_report"], rec["last_report"] = dts[0], dts[-1]
                    rec["span_years"] = round(
                        (pd.Timestamp(dts[-1]) - pd.Timestamp(dts[0])).days / 365.25, 1)
            if status == "dead":
                _classify(rec, df, name, death_date)
            return rec
        except Exception as ex:
            if attempt < 2 and any(k in repr(ex).lower() for k in
                                   ("timeout", "timed out", "connect", "remoteprotocol")):
                time.sleep(2 * (attempt + 1)); continue
            return rec
    return rec


def _classify(rec: dict, df, name: str, death_date: str) -> None:
    if df is None or len(df) == 0 or "form" not in df:
        rec.update(mechanism="unknown", mech_confidence="low", mech_signals="no filings")
        return
    forms = set(df["form"].astype(str))
    merger_hits = sorted(forms & MERGER_FORMS)
    dereg_hits = sorted(forms & DEREG_FORMS)
    # SUSTAINED substantive filing after withdrawal (latest such report > 1 year past the
    # N-54C) => the entity kept operating in another form (a conversion), not a slow wind-down
    # that files a couple of interim reports before disappearing.
    post = False
    post_days = 0
    if death_date:
        sub = df[df["form"].astype(str).isin(SUBSTANTIVE)]
        dates = [d for d in sub["filing_date"].astype(str) if d > death_date]
        if dates:
            post_days = (pd.Timestamp(max(dates)) - pd.Timestamp(death_date)).days
            post = post_days > 365
    scheduled = bool(SCHEDULED_RX.search(str(name)))

    sig = []
    if merger_hits: sig.append("merger:" + ",".join(merger_hits))
    if dereg_hits: sig.append("dereg:" + ",".join(dereg_hits))
    if post: sig.append("post-death-filings")
    if scheduled: sig.append("scheduled-name")

    # Order matters: continued substantive filing under the fund's OWN CIK means the entity
    # lived on (a conversion / reorg), even if merger forms were used as the mechanism.
    if post:
        mech, conf = "conversion", ("high" if post_days > 730 else "med")
    elif scheduled:
        mech, conf = "scheduled_winddown", "med"
    elif merger_hits:
        mech, conf = "merger", ("high" if dereg_hits else "med")
    elif dereg_hits:
        mech, conf = "liquidation", "med"
    else:
        mech, conf = "unknown", "low"
    rec.update(mechanism=mech, mech_confidence=conf, mech_signals=" ; ".join(sig) or "none")


def main() -> None:
    set_identity("brianpmoriarty@gmail.com")
    configure_http(use_system_certs=True)
    census = pd.read_csv(CENSUS, dtype={"cik": str})
    census["cik"] = census["cik"].str.zfill(10)

    # curated manager map takes precedence over the name-derived family token
    cur = {}
    if MANAGERS.exists():
        mg = pd.read_csv(MANAGERS, dtype={"cik": str})
        mg["cik"] = mg["cik"].str.zfill(10)
        cur = mg.drop_duplicates("cik").set_index("cik")["manager"].to_dict()

    cols = list(census.columns) + ["listed", "exchange", "sic", "sic_desc", "has_n2",
                                    "n_periodic", "first_report", "last_report", "span_years",
                                    "manager_family", "mechanism", "mech_confidence", "mech_signals"]
    print(f"Enriching {len(census)} BDCs (listed/unlisted + mechanism for {int((census['status']=='dead').sum())} dead)...\n")
    with open(OUT, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols); w.writeheader()
        for i, row in census.iterrows():
            cik = str(row["cik"]).zfill(10)
            e = enrich_one(cik, str(row.get("fund_name", "")), row["status"],
                           str(row.get("death_date", "") or ""))
            rec = row.to_dict()
            rec["listed"] = e["listed"]
            rec["exchange"] = e["exchange"]
            rec["sic"] = e["sic"]
            rec["sic_desc"] = e["sic_desc"]
            rec["has_n2"] = e["has_n2"]
            rec["n_periodic"] = e["n_periodic"]
            rec["first_report"] = e["first_report"]
            rec["last_report"] = e["last_report"]
            rec["span_years"] = e["span_years"]
            rec["manager_family"] = cur.get(cik) or e["name_family"]
            rec["mechanism"] = e["mechanism"]
            rec["mech_confidence"] = e["mech_confidence"]
            rec["mech_signals"] = e["mech_signals"]
            w.writerow(rec); fh.flush()
            if (i + 1) % 25 == 0:
                print(f"  [{i+1:>3}/{len(census)}] ...")
            time.sleep(0.2)
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
