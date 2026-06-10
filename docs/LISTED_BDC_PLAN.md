# Listed BDC Incorporation — Plan

Scoped 2026-06-09 (session 10). Empirically validated against 10 major listed BDCs before
writing. **No code written yet** — this is the plan of record.

---

## 1. Goal & framing

Extend the extraction/collection pipeline to cover **exchange-listed BDCs** (publicly traded
business development companies — e.g. ARCC, FSK, MAIN, OBDC, GBDC, HTGC, TSLX, BXSL, CSWC), in
addition to the ~24 **unlisted** BDCs already covered.

**Framing:** listed BDCs are *public-market* vehicles, distinct from the project's semiliquid/
unlisted focus. Their value here is as a **benchmark / comparison universe** — same asset class
(direct-lending credit), same financial structure, but with observable market pricing. They slot
into the existing dataset as additional rows, tagged by `vehicle_type` so consumers can filter.

**Core finding (the reason this is cheap):** listed BDCs file the *same* 10-K / 10-Q against the
*same* us-gaap XBRL taxonomy as unlisted BDCs. The existing extractor already handles them — see
§3. The expensive 90% (XBRL extraction, validation, holdings, the 6 data themes) is done and
proven to generalize. Remaining work is small and generic.

---

## 2. Architecture decision — ONE extractor, not a fork

**We do NOT create a second `listed_bdc_xbrl.py`.** The existing `src/extraction/bdc_xbrl.py` is
extended with a generic fallback. Rationale:

- The XBRL is identical (same forms, same taxonomy) — 9 of 10 listed BDCs extracted with zero
  changes in the scoping probe.
- The only real delta (single common class) is a *single-class* concern, not a *listed* concern —
  fixed by a generic fallback that benefits any single-class fund.
- A copy-paste fork would drift: every future theme/fix would have to be applied twice. This
  violates the project's "two front-ends, one spine" design and its anti-fragility rules.
- The genuine front-end seam is **XBRL vs HTML** (the future interval/N-CSR path warrants a second
  extractor — different data source). **Listed vs unlisted BDC is the same XBRL source**, so it
  stays one extractor. The distinction lives in the universe (`vehicle_type`) + runner selection.

---

## 3. Evidence (scoping probe, 2026-06-09)

Ran the **existing** `extract_filing` + `validate` on 10 listed BDCs' latest 10-K, unchanged:

| Result | Detail |
|---|---|
| 9/10 extracted full financials | balance sheet, net assets, income, cash flow, tax basis all populated |
| Holdings extracted for most | ARCC 1,446 · GBDC 1,763 · MAIN 747 · BXSL 674 · OBDC 635 · CSWC 554 · HTGC 365 · TSLX 217 |
| Validation profile mirrors unlisted | ~half clean `pass`, half `review` with 1–2 familiar flag-and-keep items (C5/C4 family) |
| ARCC "empty" row was a red herring | `.latest()` grabbed a **10-K/A** (amendments lack full XBRL). From the original 10-K: TA $31.2B, TNA $14.3B, TII $3.05B, 1,446 holdings — clean. |
| NAVcls = 0 for ALL | single common class, not on `StatementClassOfStockAxis` — see §4.2 |
| Undimensioned NAV/share IS tagged | `NetAssetValuePerShare` + `CommonStockSharesOutstanding` present (ARCC $19.89 / 718M sh, MAIN $32.78, OBDC $15.26) — makes the single-class synthesis trivial |
| FSK, PSEC | 0 holdings extracted — SOI likely on a different axis/structure (see §6) |

---

## 4. The three changes

### 4.1 Universe (`fund_universe.csv` + sourcing)

- **Input:** Brian provides the listed-BDC list (CIK, ticker, name). Ingest into
  `fund_universe.csv` with a new `vehicle_type = "Listed BDC"`. **Dedupe against existing rows by
  CIK** (some names may already be present).
- **Sourcing cross-check (optional, mirrors the interval-fund approach):** the **N-54A** form
  (notification of election to be a BDC) is the EDGAR signal for BDC status — the exact analog of
  N-23C3A for interval funds (~22 new elections/yr; a multi-year sweep enumerates the full BDC
  universe). To isolate *listed* BDCs, cross-reference exchange tickers (SEC `company_tickers.json`,
  wrapped by edgartools). Use this to validate/complete Brian's list, not as the primary source.
- **CIK hygiene:** zero-padded 10-digit strings; read with `dtype={"cik": str}` (existing rule).

### 4.2 Extractor (`bdc_xbrl.py`) — generic single-class fallback

The one meaningful code change. Today `per_class()` (line ~602) returns `{}` when there is no
`StatementClassOfStockAxis`, so `share_classes_nav` (built ~line 923) is empty for single-class
filers → no NAV, and C2/C3 skip.

**Fallback (additive, fires only when the class axis yields nothing):** synthesize a single class
labeled `"common"` from the UNDIMENSIONED facts at `reporting_date`:
- `class_net_assets` ← undimensioned total net assets (already extracted as `total_net_assets`)
- `class_shares_outstanding` ← undimensioned `CommonStockSharesOutstanding` (instant at reporting_date)
- `class_nav_per_share` ← undimensioned `us-gaap:NetAssetValuePerShare` (instant at reporting_date)

Safe by construction (OR-logic, like the other levers): multi-class unlisted funds already
populate via the axis, so the fallback never fires for them → 0 regressions. C2 (NAV identity) and
C3 (class sums) then work for the synthesized single class. Watch the instant-period filter (OBDC
showed `0` / `-0.46` noise rows from other periods/members — pick the reporting-date instant, which
the existing scalar logic already does).

### 4.3 Runner (`run_extraction.py`)

- **Exclude amendments:** select filings with `get_filings(form=..., amendments=False)` so we take
  the original 10-K/10-Q, not a 10-K/A that lacks full XBRL (ARCC). Good hygiene for unlisted too.
- **Broaden `bdc_funds()`:** the selector is currently `vehicle_type == "Unlisted BDC"` OR
  `category == "bdc"`. Add `vehicle_type == "Listed BDC"`. (Consider a `--segment` flag to run
  listed / unlisted / both, so each can be re-run independently.)

### 4.4 Validation & spreadsheet — unchanged

No rule changes. Expect a similar pass/review profile to the unlisted set. The spreadsheet gains
rows; surface `vehicle_type` as a column/filter so listed vs unlisted is separable. Holdings,
themes, and all C-rules apply as-is.

---

## 5. Anti-fragility principles (carried from the existing plan)

- **Generic over per-CIK.** No filer-specific hacks. The single-class fallback is keyed on
  structure (no class axis), not on a name/CIK list.
- **Additive / OR-logic.** New paths only ADD coverage; nothing that passes today can regress.
  Verify by re-running the unlisted set and confirming 248/52 is byte-identical.
- **Degrade to null.** If an input is missing (e.g. FSK holdings), leave the metric null and let
  the existing gates/flags surface it — never emit a partial/corrupted number.
- **Independent commits.** Each increment below is its own commit, independently revertable.

---

## 6. Implementation sequence (small steps, each a checkpoint + commit)

1. **Universe ingest.** Load Brian's list → `fund_universe.csv` (vehicle_type = "Listed BDC"),
   dedupe by CIK. Checkpoint: counts by vehicle_type; spot-check a few CIKs resolve on EDGAR.
2. **Single-class fallback in the extractor.** Implement + unit-probe on ARCC/MAIN/OBDC (NAVPS,
   shares, net assets populate; C2/C3 pass). Re-run a couple of UNLISTED multi-class funds to
   confirm byte-identical output (0 regressions). Commit.
3. **Runner: amendments=False + selector.** Verify ARCC now picks the original 10-K. Commit.
4. **Investigate FSK / PSEC holdings (0 rows).** Probe their SOI tagging (which axis/members). If a
   generic fix exists (e.g. an alternate identifier axis), add it; if not, document and let the
   metrics degrade to null. Commit only if a generic fix lands.
5. **Full run over the combined universe** (or `--segment listed` first). Capture written / review /
   no_xbrl / errors. Rebuild the spreadsheet. Verify the unlisted counts are unchanged.
6. **Document** in PROJECT_STATUS (session entry + log row).

---

## 7. Risks & open questions

- **Single-class edge cases.** A few listed BDCs may have a second class or units; the fallback
  handles the common case — confirm during step 2 and leave multi-class to the existing axis path.
- **FSK / PSEC holdings.** Unknown why 0 rows; may be a different SOI axis. Non-blocking (metrics
  degrade to null) but worth a look (step 4).
- **Amendments / restatements.** `amendments=False` takes the original; if a fund only has a 10-K/A
  for a period, that period is skipped (acceptable — matches "no full XBRL" handling).
- **Pre-2022 history.** Same as unlisted — pre-inline-XBRL filings come back `no_xbrl`; coverage is
  recent-years-complete.
- **Workbook size / segmentation.** Adding ~40–50 funds × ~10 years grows the dataset materially.
  Decide whether listed + unlisted share one workbook (with a vehicle_type filter) or split.

---

## 8. Rollback

- Extractor fallback: `git revert <single-class commit>` — the axis path is untouched, so unlisted
  output is unaffected.
- Runner selector: revert to restore the unlisted-only selection.
- Universe: the listed rows are tagged `vehicle_type = "Listed BDC"` and can be filtered out or
  removed without touching unlisted rows.

---

## 9. Downstream analysis this unlocks — cross-BDC holdings & mark comparison

**(Stretch / future analysis — separate from the incorporation mechanics above. Idea logged
2026-06-09 session 10, Brian's request.)**

### The idea & why it's valuable

Once the **full BDC universe** (listed + unlisted) is extracted, pull every BDC's holding-level
schedule of investments and **match the same underlying credit across different BDCs**, then
**compare each holder's mark** on it. BDC fair values are manager estimates on illiquid Level-3
loans with no observable market price — so the *same* loan held by several BDCs can be marked
differently. That dispersion is the signal: aggressive vs. conservative valuation, a possible early
credit-deterioration warning when one holder marks down before others, and outlier detection. This
is novel cross-sectional analysis that the structured holdings data makes possible.

### The comparison unit

Raw `fair_value` isn't comparable (position sizes differ). The comparable unit is **price as a
percent of par** — `fair_value / principal` (cents on the dollar). We already capture `fair_value`,
`principal`, and `cost` per holding, so this is computable; `fair_value / cost` (the per-holding
mark) is a fallback when principal isn't tagged. Compare prices across holders **as of aligned
reporting periods** (see caveats).

### The matching problem (the messy part — best-effort, confidence-scored)

Issuer names don't match cleanly across filers ("Acme Corp" vs "Acme Holdings LLC" vs a whole SOI
row crammed into one member — Apollo/First Eagle denormalize). Same issuer can have multiple
tranches (1st lien term loan, revolver, 2nd lien). So matching is tiered, each tier tagged with a
confidence, and only high/medium tiers feed price comparison:

1. **Normalize issuer names** — lowercase, strip legal suffixes (Corp/Inc/LLC/LP/Holdings),
   punctuation; fuzzy-cluster with `rapidfuzz` (already a dependency).
2. **Block by issuer cluster**, then disambiguate the instrument within it using the fields we have:
   - **High confidence:** issuer + maturity + spread all align → same tranche.
   - **Medium:** issuer + spread (or issuer + instrument type) align.
   - **Low:** issuer only → report as an aggregate, not an instrument-level price compare.
3. **Compute price** per matched holding, then per matched instrument report the holder set + each
   mark + **dispersion** (min/max/range/std-dev) and flag outliers (e.g. >N points off the median).

### Caveats (state them in the output — it's discovery, not exact reconciliation)

- **Name conventions vary**; some filers denormalize the issuer — those holdings match weakly or
  not at all. Expect partial coverage; report match rate.
- **Period mismatch:** BDCs have different fiscal quarter-ends, so marks are as-of different dates.
  Align to the nearest common period and **surface the date gap** — a mark difference across a
  2-month gap isn't purely a valuation difference.
- **Instrument granularity / missing principal:** fall back to mark (`fv/cost`) and lower the
  confidence when the tranche can't be pinned down or principal isn't tagged.
- Output confidence-scored matches; the valuable bit is the **interesting dispersions surfaced for
  human review**, not a claim of exhaustive matching.

### Where it sits & how to start

- A **new downstream analysis** consuming the per-filing holdings CSVs in `data/holdings/` across
  ALL BDCs — independent of the core extractor/validation (like the spreadsheet assembler reads the
  JSONs). Likely a new `src/output/` (or `src/analysis/`) script producing a cross-holder marks
  table / workbook tab.
- **Prototypable on the UNLISTED set today** (~130k holding rows / 256 CSVs already on disk) before
  listed BDCs land — proves the matching approach, then simply scales to the full universe. The
  matched-issuer overlap (and thus the value) grows substantially once listed BDCs are added.
- Pair naturally with `holdings_fv_recon` (the existing per-filing diagnostic) — the cross-holder
  view is the universe-wide complement to that per-filing one.
