# Portfolio Overlap Analysis — Research Plan

Scoped 2026-06-12 (session 12). Reuses the cleaned/clustered/matched holdings pipeline
(`src/analysis/holdings_compare.py`); produces a **separate** deliverable. **No code written yet.**

Decisions (Brian, 2026-06-12): dating = **per-date over time** (compute at each common date and show
the trend); output = **charts + tables**. Grain = **fund-level pairs** (the question is about one
fund vs another fund).

---

## 1. Goal

For any two BDC funds, **how much of what they own is the same?** Two grains, both requested:

- **Issuer overlap** — how many *borrowers* both funds lend to.
- **Issue overlap** — how many of the *same specific loans* (tranches) both funds hold.

And explicitly **directional** ("and vice versa"): of Fund A's book, what share is also in Fund B —
and separately, of Fund B's book, what share is also in A. These differ whenever the funds are
different sizes.

Use cases: spotting co-lending syndicates, measuring how differentiated a fund really is, finding
each fund's "nearest neighbor," and seeing whether overlap is rising or falling over time.

---

## 2. Metrics (best practices for set + portfolio overlap)

For a fund pair (A, B) at a given reporting date, at **each** grain (issuer and issue):

| Metric | Formula | Why |
|---|---|---|
| **Common count** | \|A ∩ B\| | Raw shared names/loans. |
| **Directional coverage** | \|A∩B\|/\|A\| and \|A∩B\|/\|B\| | The "vice versa" — asymmetric when sizes differ. |
| **Jaccard** | \|A∩B\| / \|A∪B\| | Size-fair symmetric similarity (0–1); the standard. |
| **Overlap coefficient** | \|A∩B\| / min(\|A\|,\|B\|) | Handles big-vs-small (a small fund fully inside a big one → 1.0). |
| **$-weighted overlap** | Σ min(wₐ, w_b) over common names | Count overlap can mislead — two funds may share names at very different sizes. Weight = fair value / fund's total. |
| **Expected overlap + lift** | E[\|A∩B\|] via hypergeometric on the date's universe; lift = observed / expected | Says overlap is *more than chance* given each fund's size and the pool — not just large because a fund is big. |

Each pair also carries a **same-manager flag** (derived fund→manager map): intra-manager overlap
(e.g. the several Blue Owl funds co-investing) is largely mechanical; the analytically interesting
signal is **cross-manager** overlap (genuine club deals). The flag lets us filter either way.

---

## 3. Method, step by step

**Inputs:** consolidated + clustered + matched holdings (`issuer_cluster`, `issue_id`, `fair_value`,
`cik`, `fund_name`, `reporting_date`).

1. **Build per-fund holdings sets per reporting date.** For each (fund, date): the set of
   `issuer_cluster`s, the set of `issue_id`s, and each name's **weight** = its fair value / the
   fund's total priced fair value that date. (Issue-grain overlap uses issues matched at
   High/Medium confidence so we're comparing genuinely-the-same loans.)
2. **Define the date's universe** = all distinct issuers / issues held by *any* fund that date — the
   pool for the hypergeometric expectation.
3. **For every fund pair present on that date**, compute all metrics in §2 at both grains.
4. **Repeat across all reporting dates** (the "per-date over time" choice), then:
   - **Latest snapshot** — the headline pairwise tables/matrices at the most recent date.
   - **Trend** — for the most-overlapping and most-changed pairs, Jaccard over time (rising/falling
     co-investment).
5. **Per-fund roll-up:** for each fund, # of funds it meaningfully overlaps with (Jaccard ≥ threshold),
   its single nearest-neighbour fund, average overlap, and how concentrated/differentiated it is.

**Note on dates:** pairs are computed *within* a shared reporting date, so different fiscal
quarter-ends simply mean two funds co-appear only on the dates they share — surfaced honestly
(per-date coverage). This is cleaner than forcing mismatched dates together.

---

## 4. Output — `data/dataset/portfolio_overlap.xlsx` (separate workbook)

| Tab | Contents |
|---|---|
| **Overview** | Method, metric definitions, caveats, headline counts. |
| **PairsLatest** | Long-form, one row per fund pair at the latest common date: common issuers/issues, directional A→B & B→A, Jaccard (both grains), overlap coef, $-weighted overlap, expected + lift, same-manager flag. Sortable to find the most-overlapping pairs. |
| **MatrixIssuer** / **MatrixIssue** | Fund × fund Jaccard matrices at the latest date (the heatmaps' data). |
| **PairsByDate** | The same pair metrics across all dates (the panel behind the trend). |
| **Trend** | Jaccard over time for the top pairs (and biggest movers) — co-investment rising/falling. |
| **FundSummary** | Per fund: # overlapping partners, nearest neighbour, avg Jaccard, differentiation score. |
| **Charts** | Heatmap of the issuer-Jaccard matrix (latest date), clustered so co-lending groups sit together; a small-multiples or line chart of the top pairs' Jaccard trend. *(Optional, if useful: a fund co-lending network graph via `networkx` — flagged as an extension, not core.)* |

---

## 5. New scripts & dependencies

- **New module:** `src/analysis/portfolio_overlap.py` — imports `load_consolidated` /
  `add_clusters` / `match_issues` from `holdings_compare.py`. CLI:
  `uv run python src/analysis/portfolio_overlap.py [--threshold 92]`.
- **Dependencies:**
  - `scipy` — hypergeometric expectation (`scipy.stats.hypergeom`); shared with the bias study.
  - `matplotlib` — heatmap + trend charts.
  - `numpy` / `pandas` — already present.
  - *(Optional)* `networkx` — only if we add the co-lending network graph.
  - Install via `uv pip install scipy matplotlib --link-mode=copy` (OneDrive working dir).

---

## 6. Scale & performance

74 funds → up to ~2,700 unordered pairs per date × ~18 dates ≈ 50k pair-date rows at each grain —
trivial for pandas. Matrices are 74×74. Heatmaps rendered for the latest date (and optionally a
couple of historical snapshots), not every date.

---

## 7. Validation / sanity checks

- **Self-overlap = 1.0** (a fund vs itself) as a unit test.
- **Symmetry:** Jaccard(A,B) == Jaccard(B,A); directional A→B and B→A bracket it.
- **Known club deals reappear:** the broadly-held anchors (Anaplan, Icefall, etc.) should drive high
  overlap among their known co-holders.
- **Same-manager pairs rank near the top** of overlap (expected) — a good smoke test; cross-manager
  high-overlap pairs are the real finding.
- **Lift sanity:** intra-manager lift ≫ 1; unrelated funds ≈ 1.

---

## 8. Risks & honest caveats (state in the output)

- Overlap is computed on **matched, priced** holdings; unmatched/equity positions are out of scope,
  so counts are "comparable-universe" overlap, not literally every line item.
- Issue-grain overlap inherits the matcher's confidence — two funds "share a loan" means the matcher
  grouped them at High/Medium confidence.
- Different quarter-ends limit which funds co-appear on a date (coverage, not error).
- Dollar weights use reported fair value; partly-funded facilities are small by construction.

---

## 9. Phasing

1. Per-fund sets + weights per date; pairwise count + directional + Jaccard at the latest date
   (the core table most users want).
2. Overlap coefficient + $-weighted overlap + hypergeometric lift + same-manager flag.
3. Per-date panel + trend + FundSummary.
4. Charts (heatmap + trend) + workbook assembly + validation pass.
