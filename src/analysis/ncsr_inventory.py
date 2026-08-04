"""
ncsr_inventory.py — M1 gate for the N-CSR LLM extraction path: a free, no-API census of
the whole N-CSR/N-CSRS corpus.

WHY RUN THIS BEFORE SPENDING ANYTHING
-------------------------------------
The plan's expensive step (M5) is one Claude call per filing across ~3,000 filings. That
only makes sense if the cheap deterministic step actually works, so this module answers
three questions for free, before any API key is involved:

  1. COVERAGE — in what fraction of filings can we locate the financial statements at all?
     The plan's gate is >=95%. Below that, the locator is the wrong tool and the fallback
     (evaluating `sec-parser`) is on the table instead.
  2. SIZE — how many tokens does a located block actually come to? That converts the cost
     estimate from an assumption into a measurement.
  3. SHAPE — which filings are multi-block (multi-series trusts or attached master-fund
     statements), which lack a recognisable balance-sheet title, which carry inline XBRL
     that can cross-check the LLM later.

Nothing here calls an API or costs money. It only reads local HTML.

WHAT IT WRITES
--------------
  data/dataset/ncsr_inventory.csv   one row per filing (appended as it goes, so a crash
                                    or Ctrl-C loses nothing and --resume picks up)
and prints a summary ending in an explicit PASS/FAIL against the >=95% gate.

Run:
    uv run python src/analysis/ncsr_inventory.py                   # full corpus
    uv run python src/analysis/ncsr_inventory.py --max-files 200   # quick smoke test
    uv run python src/analysis/ncsr_inventory.py --summary-only    # re-print from the CSV
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src" / "extraction"))
from ncsr_sections import (  # noqa: E402
    Kind,
    extract_sections,
    iter_ncsr_filings,
    parse_filing_name,
)

# The filings corpus is a sibling of the project directory, not inside it (7.8 GB of HTML
# has no business in a git repo).
DEFAULT_FILINGS_DIR = PROJECT_ROOT.parent / "filings"
UNIVERSE = PROJECT_ROOT / "data" / "fund_universe.csv"
OUT_CSV = PROJECT_ROOT / "data" / "dataset" / "ncsr_inventory.csv"

# The vehicle types this workstream exists to cover. Other N-CSR filers (plain closed-end
# funds that wandered into the download) are reported separately rather than counted
# against the gate.
IN_SCOPE_VEHICLES = {"Interval Fund", "Tender Offer Fund"}

# The plan's M1 gate.
GATE_MIN_HIT_RATE = 0.95

# Files at or above this size are processed with fewer workers, because each one needs a
# few hundred MB of memory and the largest in this corpus is 150 MB.
LARGE_FILE_BYTES = 20 * 1024 * 1024

# --- cost model (for turning measured tokens into dollars) -----------------------------
# Sonnet 5 introductory pricing, halved for the Message Batches API. Intro pricing ends
# 2026-08-31; see the plan's cost table.
SONNET_BATCH_IN_PER_MTOK = 1.00
SONNET_BATCH_OUT_PER_MTOK = 5.00
# Prompt overhead per call: system prompt + field dictionary + per-filing metadata header.
PROMPT_OVERHEAD_TOKENS = 4_000
# Expected structured-output size per filing (values + short raw_text snippets).
EXPECTED_OUTPUT_TOKENS = 5_000

FIELDNAMES = [
    "filename",
    "fund_name",
    "cik",
    "form",
    "filing_date",
    "is_amendment",
    "in_universe",
    "vehicle_type",
    "file_mb",
    "raw_chars",
    "located",
    "n_blocks",
    "n_kinds",
    "block_kinds",
    # Every statement heading found anywhere in the document, whether or not it ended up in
    # a block. This is what separates "we failed to locate the statements" from "this
    # document has no statements in it" (stub amendments, cover letters).
    "heading_kinds",
    "has_statements",
    "has_balance_sheet",
    "has_cash_flows",
    "has_highlights",
    "block_chars",
    "serialized_chars",
    "est_tokens",
    "has_inline_xbrl",
    "flags",
    "error",
]


def _process_one(path_str: str) -> dict[str, object]:
    """Census one filing. Runs in a worker process, so it must be importable and total.

    Never raises: a filing that blows up is recorded with its error text and counted as a
    miss, because a crashed worker in the middle of a 3,000-file run is worse than a row
    that says what went wrong.
    """
    path = Path(path_str)
    row: dict[str, object] = {k: "" for k in FIELDNAMES}
    row["filename"] = path.name
    try:
        row["file_mb"] = round(path.stat().st_size / (1024 * 1024), 3)
    except OSError:
        row["file_mb"] = ""

    try:
        res = extract_sections(path)
    except Exception as exc:  # noqa: BLE001 - deliberately total
        fn = parse_filing_name(path)
        if fn is not None:
            row.update(
                fund_name=fn.fund_name,
                cik=fn.cik,
                form=fn.form,
                filing_date=fn.filing_date,
                is_amendment=int(fn.is_amendment),
            )
        row["located"] = 0
        row["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return row

    f = res.filing
    kinds = res.block_kinds
    row.update(
        fund_name=f.fund_name,
        cik=f.cik,
        form=f.form,
        filing_date=f.filing_date,
        is_amendment=int(f.is_amendment),
        raw_chars=res.raw_chars,
        located=int(res.located),
        n_blocks=len(res.blocks),
        n_kinds=len(kinds),
        block_kinds="|".join(kinds),
        heading_kinds="|".join(res.anchor_kinds),
        # Regulation S-X requires both of these of every registered fund, so a document
        # containing neither title anywhere is not a financial report at all.
        has_statements=int(
            Kind.OPERATIONS in res.anchor_kinds and Kind.CHANGES in res.anchor_kinds
        ),
        has_balance_sheet=int(Kind.ASSETS_LIAB in kinds),
        has_cash_flows=int(Kind.CASH_FLOWS in kinds),
        has_highlights=int(Kind.HIGHLIGHTS in kinds),
        block_chars=sum(b.chars for b in res.blocks),
        serialized_chars=len(res.text),
        est_tokens=res.est_tokens,
        has_inline_xbrl=int(res.has_inline_xbrl),
        flags="|".join(res.flags),
    )
    return row


def load_universe() -> dict[str, dict[str, str]]:
    """CIK -> universe attributes, keyed on 10-digit zero-padded CIK.

    CIK is the canonical join key throughout this project; fund_name is display-only
    (it has casing inconsistencies between sources). See PROJECT_STATUS session 15.
    """
    if not UNIVERSE.exists():
        return {}
    df = pd.read_csv(UNIVERSE, dtype=str).fillna("")
    df["cik"] = df["cik"].str.strip().str.zfill(10)
    return {
        r["cik"]: {"vehicle_type": r.get("vehicle_type", "")}
        for _, r in df.iterrows()
        if r["cik"] and r["cik"] != "0000000000"
    }


def _run_phase(
    paths: list[Path],
    workers: int,
    writer: csv.DictWriter,
    handle,  # noqa: ANN001 - open file object
    universe: dict[str, dict[str, str]],
    label: str,
) -> int:
    """Process one batch of files across `workers` processes, writing rows as they land."""
    if not paths:
        return 0
    print(f"\n[{label}] {len(paths):,} files across {workers} worker process(es)")
    started = time.time()
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_process_one, str(p)): p for p in paths}
        for fut in as_completed(futures):
            row = fut.result()
            cik = str(row.get("cik") or "")
            info = universe.get(cik)
            row["in_universe"] = int(info is not None)
            row["vehicle_type"] = info["vehicle_type"] if info else ""
            writer.writerow(row)
            handle.flush()  # per-file durability: a crash never loses completed work
            done += 1
            if done % 100 == 0 or done == len(paths):
                rate = done / max(0.001, time.time() - started)
                remaining = (len(paths) - done) / max(0.001, rate)
                print(
                    f"  {done:,}/{len(paths):,}  ({rate:.1f} files/s, "
                    f"~{remaining / 60:.1f} min left)",
                    flush=True,
                )
    return done


def _pct(numerator: int, denominator: int) -> str:
    if not denominator:
        return "n/a"
    return f"{100.0 * numerator / denominator:.1f}%"


def summarize(csv_path: Path) -> None:
    """Print the census, ending with the explicit gate verdict."""
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    for col in (
        "located",
        "n_blocks",
        "n_kinds",
        "est_tokens",
        "raw_chars",
        "block_chars",
        "serialized_chars",
        "has_balance_sheet",
        "has_cash_flows",
        "has_highlights",
        "has_inline_xbrl",
        "is_amendment",
        "in_universe",
        "has_statements",
    ):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    df["file_mb"] = pd.to_numeric(df["file_mb"], errors="coerce").fillna(0.0)

    n = len(df)
    print("\n" + "=" * 78)
    print(f"N-CSR CORPUS INVENTORY  —  {n:,} filings")
    print("=" * 78)

    print("\n--- by form type ---")
    for form, grp in df.groupby("form"):
        amd = int(grp["is_amendment"].sum())
        print(
            f"  {form:<8} {len(grp):>6,} filings  "
            f"({amd} amendments)  located {_pct(int(grp['located'].sum()), len(grp))}"
        )

    # Amendments supersede the original for the same (cik, form, period), so the number of
    # filings we would actually pay to extract is lower than the raw file count.
    df["dedupe_key"] = df["cik"] + "_" + df["form"] + "_" + df["filing_date"]
    unique_periods = df.drop_duplicates(subset="dedupe_key")
    print(f"\n  distinct (cik, form, date) periods: {len(unique_periods):,}")

    print("\n--- LOCATION HIT-RATE (the gate) ---")
    overall = int(df["located"].sum())
    print(f"  all filings                 : {overall:,}/{n:,}  = {_pct(overall, n)}")

    in_scope = df[df["vehicle_type"].isin(IN_SCOPE_VEHICLES)]
    scope_hits = int(in_scope["located"].sum())
    print(
        f"  interval + tender (all)     : {scope_hits:,}/{len(in_scope):,}  "
        f"= {_pct(scope_hits, len(in_scope))}"
    )
    other = df[~df["vehicle_type"].isin(IN_SCOPE_VEHICLES)]
    print(
        f"  other / unmapped CIK        : {int(other['located'].sum()):,}/{len(other):,}  "
        f"= {_pct(int(other['located'].sum()), len(other))}"
    )

    # The gate measures the LOCATOR, so documents that contain no statements at all (stub
    # amendments, cover letters) do not belong in the denominator — there is nothing in them
    # to find. They are reported immediately below rather than quietly dropped, and any
    # sizeable one is called out for inspection, because "no statements" in a 2 MB annual
    # report would mean unrecognised title wording, which IS a locator failure.
    gate_pop = in_scope[in_scope["has_statements"] == 1]
    gate_hits = int(gate_pop["located"].sum())
    print(
        f"  interval + tender w/ stmts  : {gate_hits:,}/{len(gate_pop):,}  "
        f"= {_pct(gate_hits, len(gate_pop))}   <-- THE GATE"
    )

    no_stmt = in_scope[in_scope["has_statements"] == 0]
    stubs = no_stmt[no_stmt["file_mb"] < 0.05]
    suspicious = no_stmt[no_stmt["file_mb"] >= 0.05]
    print(
        f"\n  excluded from the gate: {len(no_stmt):,} in-scope filings contain no statement"
        f" titles at all\n    {len(stubs):,} are <0.05 MB (stub amendments / cover letters —"
        " genuinely no financials)"
    )
    print(f"    {len(suspicious):,} are >=0.05 MB  <-- INSPECT: may be unrecognised wording")
    # A known benign cause: a fund that had not commenced operations by its fiscal year end
    # files a balance sheet and nothing else — there was no activity to report. Those show a
    # balance-sheet title with no Operations/Changes, which is correct, not a locator miss.
    bs_only = suspicious[suspicious["heading_kinds"].str.contains(Kind.ASSETS_LIAB, na=False)]
    if len(bs_only):
        print(
            f"      of which {len(bs_only):,} have a balance-sheet title only "
            "(pre-operational / seed-stage funds — expected, not a miss)"
        )
    for _, r in suspicious.nlargest(8, "file_mb").iterrows():
        tag = " [bs-only]" if Kind.ASSETS_LIAB in str(r["heading_kinds"]) else ""
        print(f"      {r['file_mb']:>7.2f} MB  {r['filename'][:60]}{tag}")

    print("\n--- statement coverage (located filings only) ---")
    loc = df[df["located"] == 1]
    if len(loc):
        print(f"  balance sheet found  : {_pct(int(loc['has_balance_sheet'].sum()), len(loc))}")
        print(f"  cash-flow statement  : {_pct(int(loc['has_cash_flows'].sum()), len(loc))}")
        print(f"  financial highlights : {_pct(int(loc['has_highlights'].sum()), len(loc))}")
        print(f"  inline XBRL present  : {_pct(int(loc['has_inline_xbrl'].sum()), len(loc))}"
              "  (independent cross-check anchors, rule X1)")

    print("\n--- serialized block size (located filings, estimated tokens) ---")
    if len(loc):
        t = loc["est_tokens"]
        for q in (0.05, 0.25, 0.50, 0.75, 0.90, 0.99):
            print(f"  p{int(q * 100):<3} {int(t.quantile(q)):>9,} tokens")
        print(f"  max  {int(t.max()):>9,} tokens")
        print(f"  mean {int(t.mean()):>9,} tokens")
        compression = 1.0 - (loc["serialized_chars"].sum() / max(1, loc["raw_chars"].sum()))
        print(f"  raw HTML -> serialized text: {100 * compression:.2f}% smaller")

        # Cost from MEASURED tokens rather than the plan's assumption.
        billable = unique_periods[unique_periods["located"] == 1]
        mean_in = float(t.mean()) + PROMPT_OVERHEAD_TOKENS
        cost = len(billable) * (
            mean_in / 1e6 * SONNET_BATCH_IN_PER_MTOK
            + EXPECTED_OUTPUT_TOKENS / 1e6 * SONNET_BATCH_OUT_PER_MTOK
        )
        print(
            f"\n  MEASURED cost estimate for {len(billable):,} distinct located periods:"
            f"\n    Sonnet 5 batch (intro pricing, ~{int(mean_in):,} in / "
            f"{EXPECTED_OUTPUT_TOKENS:,} out per filing): ${cost:,.0f}"
        )

    print("\n--- shape flags ---")
    flag_counts: dict[str, int] = {}
    for raw in df["flags"]:
        for fl in str(raw).split("|"):
            if fl:
                # Collapse "multi_block_2/3/4..." into one bucket for the tally.
                key = "preprocess:multi_block_N" if fl.startswith("preprocess:multi_block_") else fl
                flag_counts[key] = flag_counts.get(key, 0) + 1
    for fl, cnt in sorted(flag_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {cnt:>6,}  {fl}")

    multi = df[df["n_blocks"] > 1].sort_values("n_blocks", ascending=False)
    print(f"\n--- multi-block filings: {len(multi):,} (M2 input) ---")
    for _, r in multi.head(15).iterrows():
        print(f"  {int(r['n_blocks']):>3} blocks  {r['file_mb']:>8.1f} MB  {r['filename'][:76]}")
    if len(multi) > 15:
        print(f"  ... and {len(multi) - 15:,} more (see the CSV)")

    # True misses: statements are demonstrably in the document, but we failed to locate them.
    misses = df[(df["located"] == 0) & (df["has_statements"] == 1)]
    print(f"\n--- TRUE MISSES: {len(misses):,} filings that HAVE statements we failed to locate ---")
    if len(misses):
        print("  by CIK (top 15):")
        for cik, grp in sorted(
            misses.groupby("cik"), key=lambda kv: -len(kv[1])
        )[:15]:
            name = grp["fund_name"].iloc[0][:44]
            vt = grp["vehicle_type"].iloc[0] or "(unmapped)"
            print(f"    {len(grp):>4}x  {cik}  {name:<44} {vt}")
        errs = misses[misses["error"] != ""]
        if len(errs):
            print(f"\n  of which {len(errs)} raised errors:")
            for _, r in errs.head(10).iterrows():
                print(f"    {r['filename'][:60]}  {r['error'][:90]}")
        print("\n  smallest misses are usually not real reports (cover letters, stub filings):")
        for _, r in misses.nsmallest(5, "file_mb").iterrows():
            print(f"    {r['file_mb']:>7.3f} MB  {r['filename'][:70]}")

    print("\n" + "=" * 78)
    rate = gate_hits / len(gate_pop) if len(gate_pop) else 0.0
    verdict = "PASS" if rate >= GATE_MIN_HIT_RATE else "FAIL"
    print(
        f"M1 GATE (>= {GATE_MIN_HIT_RATE:.0%} located on interval + tender filings that "
        f"contain statements): {rate:.1%}  -> {verdict}"
    )
    all_scope_rate = scope_hits / len(in_scope) if len(in_scope) else 0.0
    print(f"  (unfiltered interval + tender rate, for reference: {all_scope_rate:.1%})")
    if verdict == "FAIL":
        print("  Plan's pre-identified fallback: evaluate sec-parser as the locator.")
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--filings-dir", type=Path, default=DEFAULT_FILINGS_DIR)
    ap.add_argument("--out", type=Path, default=OUT_CSV)
    ap.add_argument("--max-files", type=int, help="process only the first N (smoke test)")
    ap.add_argument("--workers", type=int, default=6, help="workers for normal-size files")
    ap.add_argument(
        "--large-workers",
        type=int,
        default=2,
        help="workers for files >20MB (each needs several hundred MB of memory)",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help=(
            "skip filings already in the CSV. Use this ONLY to finish an interrupted run — "
            "after any change to ncsr_sections.py the existing rows are stale, so re-run "
            "from scratch instead or the census will mix two versions of the locator."
        ),
    )
    ap.add_argument("--summary-only", action="store_true", help="just re-print the summary")
    args = ap.parse_args(argv)

    if args.summary_only:
        if not args.out.exists():
            print(f"no inventory at {args.out}")
            return 1
        summarize(args.out)
        return 0

    if not args.filings_dir.exists():
        print(f"filings directory not found: {args.filings_dir}")
        return 1

    filings = iter_ncsr_filings(args.filings_dir)
    if args.max_files:
        filings = filings[: args.max_files]
    print(f"found {len(filings):,} N-CSR/N-CSRS filings in {args.filings_dir}")

    already: set[str] = set()
    if args.resume and args.out.exists():
        prior = pd.read_csv(args.out, dtype=str).fillna("")
        already = set(prior["filename"])
        print(f"resuming: {len(already):,} already in {args.out.name}")

    todo = [f.path for f in filings if f.path.name not in already]
    if not todo:
        print("nothing to do.")
        summarize(args.out)
        return 0

    small = [p for p in todo if p.stat().st_size < LARGE_FILE_BYTES]
    large = [p for p in todo if p.stat().st_size >= LARGE_FILE_BYTES]
    print(f"to process: {len(small):,} standard + {len(large):,} large (>20MB)")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_header = not (args.out.exists() and already)
    universe = load_universe()
    print(f"universe: {len(universe):,} CIKs")

    with args.out.open("a" if already else "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        # Large files run last and with fewer workers so peak memory stays bounded.
        _run_phase(small, args.workers, writer, fh, universe, "standard")
        _run_phase(large, args.large_workers, writer, fh, universe, "large")

    print(f"\nwrote {args.out}")
    summarize(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
