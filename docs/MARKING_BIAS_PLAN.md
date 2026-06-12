# Manager Marking-Bias Analysis — Research Plan

Scoped 2026-06-12 (session 12). Reuses the cleaned/clustered/matched holdings pipeline
(`src/analysis/holdings_compare.py`); produces a **separate** deliverable. **No code written yet.**

Decisions (Brian, 2026-06-12): grain = **parent manager only**; rigor = **descriptive + robust CIs
AND a formal fixed-effects model**; output = **charts + tables**.

---

## 1. Goal

Do some asset managers **systematically value the same loans higher (richer) or lower (cheaper)
than their peers**? For every loan held by several managers at the same date, we already know each
holder's mark and the group consensus. Rolling each manager's deviations up across all its
overlapping loans tells us whether a manager leans optimistic, conservative, or in-line — and
whether that's a real, statistically-supported tendency or small-sample noise.

This is **descriptive**, not a judgment of who is "correct."

---

## 2. The comparison unit (why it's fair)

The unit of observation is a **deviation**: how far one manager's mark on a specific loan sits from
the consensus of the *other* managers holding that *same loan at that same reporting date*.

- Comparing **within the same issue** (same borrower, same tranche, same date) automatically
  controls for loan quality, seniority, and timing. A manager that happens to hold riskier paper
  therefore does **not** look "cheap" — it is only ever measured against co-holders of the identical
  instrument.
- This is the standard way to measure relative valuation tendency and removes the biggest
  confounder (portfolio composition) by construction.

---

## 3. Method, step by step

**Inputs:** `match_issues()` output → `fund_marks()` (one mark per fund per issue, lots already
collapsed). All marks in points of par.

1. **Map fund → parent manager.** Derive the manager from each fund name (e.g. "Blue Owl Capital
   Corp", "Blue Owl Technology Finance Corp." → *Blue Owl*; the several Blackstone, Golub, Oaktree,
   Bain, Crescent vehicles → one manager each). Built as a curated lookup (≈40 managers over the ~74
   funds) because the manager rollup is now the *only* grain — accuracy matters. **The fund→manager
   map is emitted as its own tab for Brian to eyeball before trusting any ranking.**

2. **Collapse to one mark per (manager, issue).** A manager with several funds in the same loan
   contributes a single mark (median of its funds), so a manager with 6 vehicles can't dominate the
   consensus or the sample. Same-manager funds almost always co-mark, so this also removes
   pseudo-replication.

3. **Leave-one-out consensus.** For each (issue, manager), the benchmark is the **median of the
   *other* managers'** marks on that issue — never a consensus that includes the manager being
   scored (critical on 2–3 holder loans, where self-inclusion mechanically shrinks the deviation).
   `dev = manager_mark − leave_one_out_median` (points).

4. **Keep only well-populated issues.** Require **≥3 managers** on a loan (so the leave-one-out
   median is over ≥2 others). Record n.

5. **Aggregate per manager across all its issues:**
   - **Median deviation** (robust headline) and **mean deviation**.
   - **% rich / in-line / cheap** (e.g. dev > +1 / within ±1 / < −1 pt).
   - **n comparable issues** and **n distinct dates** (coverage).
   - **Ex-distressed cut:** the same median deviation excluding loans the group marks < 90, so we
     can see whether a manager's tendency is broad or driven by a few stressed names.

6. **Robust uncertainty (the "descriptive + CIs" layer):**
   - **Bootstrap confidence interval** on the median/mean deviation, **resampling by issue** (not by
     row) so the interval respects that one loan = one cluster of correlated marks.
   - **Non-parametric sign / Wilcoxon signed-rank test** that a manager's deviations are centered at
     zero (does it lean rich/cheap beyond chance?).
   - **Minimum-sample floor:** managers with < ~20 comparable issues are shown but flagged
     "low-confidence — too few overlaps," never ranked as if reliable.

7. **Formal model (the "fixed-effects" layer):**
   - Fit `mark_{m,j} = α_manager_m + γ_issue_j + ε` on the (manager, issue) table. The **issue fixed
     effect γ_j** absorbs each loan's consensus level; the **manager coefficient α_m** is that
     manager's average rich/cheapness *after* controlling for which loans it holds — the cleanest
     single-number bias estimate.
   - Implemented as **issue-demeaning + manager dummies in statsmodels OLS with cluster-robust
     standard errors clustered on issue** (corrects for the fact that marks on the same loan are
     correlated). Report each manager's coefficient, SE, and 95% CI.
   - **Multiple-comparisons control:** Benjamini–Hochberg FDR across managers, since we test many at
     once.

8. **Time trend:** per manager, the median deviation **by reporting date**, to see whether a manager
   is drifting richer or more conservative over time (parallels the overlap study's time view).

---

## 4. Confounders & how each is handled

| Confounder | Handling |
|---|---|
| Portfolio composition (riskier book) | Within-issue deviation — compared only to co-holders of the *same* instrument. |
| A multi-vehicle manager over-counting | Collapse to one mark per (manager, issue) before anything else. |
| Self-comparison on thin loans | Leave-one-out consensus; require ≥3 managers. |
| A few distressed names driving the result | Separate ex-distressed median; bootstrap by issue; winsorize extreme deviations. |
| Small samples masquerading as bias | ≥20-issue floor, bootstrap CIs, sign test, FDR control. |
| Correlated observations (same loan) | Bootstrap resamples by issue; regression SEs clustered on issue. |
| Stale / lagged marks | Same-reporting-date matching (the issue key already pins the date). |
| Different fiscal quarter-ends | Managers only co-appear on shared dates — a coverage limit, surfaced in the n columns. |

---

## 5. Output — `data/dataset/marking_bias.xlsx` (separate workbook)

| Tab | Contents |
|---|---|
| **Overview** | Plain-language method, the rich/cheap definition, caveats, headline counts. |
| **ManagerBias** | One row per manager: n issues, n managers-compared-against, median dev, mean dev, bootstrap 95% CI, sign-test p, FE coefficient + CI + FDR-adjusted p, % rich/in-line/cheap, ex-distressed median dev, confidence flag, rank. |
| **ManagerBiasByDate** | Manager × reporting-date median deviation (the drift view). |
| **FundManagerMap** | The fund→manager mapping, for verification. |
| **IssueSample** | A stratified sample of issues showing each manager's mark vs the consensus, for spot-checking. |
| **Charts** | Box/violin plot of each manager's deviation distribution (sorted by median); a caterpillar plot of FE coefficients with 95% CIs (the headline visual). |

---

## 6. New scripts & dependencies

- **New module:** `src/analysis/marking_bias.py` — imports `load_consolidated` / `add_clusters` /
  `match_issues` / `fund_marks` from `holdings_compare.py` (reuse, no duplication). CLI:
  `uv run python src/analysis/marking_bias.py [--threshold 92]`.
- **Dependencies to add** (via `uv pip install … --link-mode=copy` for the OneDrive working dir):
  - `statsmodels` — the fixed-effects regression + cluster-robust SEs (pulls in `scipy`).
  - `scipy` — bootstrap, sign/Wilcoxon test, used directly too.
  - `matplotlib` — the embedded charts.
  - All three are mainstream, pure-analysis libraries; no system deps.

---

## 7. Validation / sanity checks

- **Sign sanity:** a manager known to mark conservatively should land cheap; spot-check a few
  managers' largest deviations against the raw filings.
- **Symmetry:** across all managers the deviations must roughly center at zero (they're measured vs
  peers) — a large non-zero grand mean would signal a bug.
- **Two methods agree:** the FE coefficient and the bootstrap median deviation should rank managers
  similarly; large disagreements get investigated.
- **Stability:** the ranking shouldn't hinge on one date or one big distressed name (ex-distressed +
  by-date tabs show this).

---

## 8. Risks & honest caveats (to state in the output)

- It measures **relative** valuation tendency among **overlapping** loans, not absolute correctness
  and not a manager's whole book.
- Managers with thin cross-manager overlap get wide CIs — reported, not hidden.
- Marks are quarterly manager estimates; small, legitimate timing/information differences exist. The
  analysis flags *systematic* lean, not any single mark.
- Coverage is limited to loans matched at High/Medium confidence on shared dates.

---

## 9. Phasing

1. Manager map + (manager, issue) collapse + leave-one-out deviations → eyeball the map.
2. Descriptive per-manager table + bootstrap CIs + sign test (the headline most users want).
3. Fixed-effects model + FDR + charts.
4. By-date drift + issue sample + workbook assembly + validation pass.
