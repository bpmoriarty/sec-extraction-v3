"""
download_state.py — Per-machine record of when each fund was last checked for new filings.

WHY THIS EXISTS (read this if you're ever confused about cross-machine syncing):

`last_checked` used to live in data/fund_universe.csv, which is tracked in git. That caused
a subtle bug when working from two machines. The downloader uses last_checked only as the
START of the EDGAR query window, and a separate on-disk check (dest.exists()) skips files
already downloaded. The on-disk check only protects files *inside* that window. So:

    Machine A downloads new filings into ITS OWN local filings/ folder, bumps last_checked,
    and pushes the CSV. Machine B pulls the CSV, now believes those filings were "checked,"
    and on its next update only queries filings AFTER that date -- so the ones A downloaded
    (which never reached B's disk) fall outside B's window and are skipped forever.

The two pieces of state had drifted apart: last_checked was shared (git) but filings/ was
not. The fix: download progress is LOCAL state. It lives in this file (data/download_state.csv),
which is gitignored. Each machine tracks only what IT has actually downloaded, so the date
window and the on-disk files can never disagree.

A fresh machine with no state file falls back to the default start date and the on-disk
check, which is exactly right: it has no filings, so it should query the full history and
download everything (re-downloads are still skipped by dest.exists()).

The key is the 10-digit zero-padded CIK (the same form the downloaders use), e.g. "0001837532".
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# This file lives at sec-extraction-v3/src/downloader/download_state.py
# parents[2] = sec-extraction-v3/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = PROJECT_ROOT / "data" / "download_state.csv"


def load_state() -> dict[str, str]:
    """
    Return {cik: last_checked_iso} for this machine.

    Returns an empty dict if the state file doesn't exist yet (a fresh clone). Callers
    treat a missing CIK as "never checked" and fall back to the default start date.
    """
    if not STATE_FILE.exists():
        return {}
    df = pd.read_csv(STATE_FILE, dtype=str).fillna("")
    return {
        row["cik"].strip(): row["last_checked"].strip()
        for _, row in df.iterrows()
        if row["cik"].strip()
    }


def save_state(state: dict[str, str]) -> None:
    """
    Write {cik: last_checked} back to the state file.

    Sorted by CIK so the file is stable/diff-friendly. Created on first write; the
    data/ folder is guaranteed to exist (it holds fund_universe.csv) but we make it
    anyway so a fresh machine can't trip on a missing directory.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(sorted(state.items()), columns=["cik", "last_checked"])
    df.to_csv(STATE_FILE, index=False)
