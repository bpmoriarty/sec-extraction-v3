# Credit Migration & Fund Attribution — Plan

**Status:** planned, not built (2026-08-18). Standalone deliverable; nothing mixes into
`holdings_marks_comparison.xlsx`.

**Origin:** a seven-question review list from a colleague (his numbering has two items
labelled 5, so they are 5a and 5b here — nothing is dropped).

**Window:** **2024-12-31 → 2026-06-30**, fixed and identical for every fund. Rationale in §3.

**Cost:** zero API spend. All inputs already exist on disk. No new extraction.

---

## 1. Goal

Three questions, in plain terms:

1. **Which troubled credits matter most?** Weight each issuer's price change by how much of it
   the BDC universe actually holds, so a 40-point fall on a $2bn credit outranks the same fall
   on a $20m one.
2. **How much of each fund's book went bad?** What share of assets crossed from healthy to
   impaired over the window, in dollars and in issuer counts.
3. **What did that cost the fund?** How much of the fund's valuation change came from the
   deteriorating slice, benchmarked against the asset-weighted universe and against the fund's
   own reported total return.

---

## 2. Mapping the seven questions onto three engines

His list looks like seven builds. It is three, which is why this is tractable.

| Engine | His items | Grain | What it produces |
|---|---|---|---|
| **A — Credit migration** | 2, 3, 4 | fund × price bucket | Assets and issuer counts crossing thresholds |
| **B — Issuer importance** | 1, 5a | issuer, then universe | Exposure-weighted price change; breadth |
| **C — Fund attribution** | 5b, 6 | fund vs universe | Valuation drag, and its share of total return |

Note the internal structure he may not have spotted: **#3 is a level** (what sits below 90 now),
**#2 is a transition** (what fell through 90), and **#4 is #2 counted in issuers instead of
dollars**. One engine, three cuts. A migration matrix (§5, MigrationMatrix) produces all three
simultaneously and makes the threshold debate moot.

Likewise **#1 and #5a are the same computation at two grains** — per issuer, then aggregated
across the issuers meeting the criteria.

---

## 3. Window: why 2024-12-31 (MEASURED)

A later start was tested against candidate start dates, all ending 2026-06-30:

| Start | Funds at both ends | Asset coverage | Issuers at both ends | (fund, issuer) pairs |
|---|---|---|---|---|
| 2023-12-31 | 58 | 97.4% | 2,783 | 3,774 |
| 2024-06-30 | 59 | 97.5% | 3,288 | 4,831 |
| **2024-12-31** | **60** | **98.1%** | **4,033** | **6,381** |
| 2025-06-30 | 67 | 98.4% | 4,979 | 8,072 |

Moving the start from Dec 2023 to Dec 2024 grows the issuer universe **45%** and the
fund-issuer pairs **69%**. The gain is not fund count (58 → 60; asset coverage was already
97.4%) — it is **loan survivorship**. A loan held in Dec 2023 has a good chance of being repaid
or sold before Jun 2026, and a transition measure needs the loan observable at *both* ends.

His actual metric also improves: issuers crossing **95 → <90** rise from **298 to 321**
(95 → <85 is unchanged at 204).

**What the later start gives up: 5% of the move.** Holding the sample constant at the 2,680
issuers present on all three dates — the only fair comparison:

- FV-weighted change Dec 2023 → Jun 2026: **−3.18 pts**
- of which Dec 2023 → Dec 2024: **−0.16 pts (5%)**
- of which Dec 2024 → Jun 2026: **−3.02 pts (95%)**

The deterioration is almost entirely a 2025–H1 2026 event. Twelve extra months of history costs
45% of the loan universe and buys 5% of the signal.

It also aligns with four of the six as-of dates already on `LoanHistory` (2024-12, 2025-06,
2025-12, 2026-06), so the two files reconcile.

**The one real cost, handled not hidden.** A Dec-2024 start cannot see credits that fell
*before* Dec 2024 and stayed down; they read as "flat and low", and his #2 wording ("priced
above 95 at the start") excludes them by construction. Those are the already-impaired credits
and arguably the most interesting ones. The migration matrix surfaces them natively in its
low-start rows, and that block must be **reported, not filtered** — "already troubled at the
start" is a category, not an omission.

---

## 4. The four things that had to be resolved first (all MEASURED)

### 4.1 Contaminated dollar numerators — RESOLVED, and cheaply

Summing each fund's holdings against its XBRL-tagged `investments_at_fair_value` does not
reconcile: median **1.391** at 2024-12-31, 90th percentile **6.15**, with 33 of 71 funds over
1.5×. Filers tag industry-level AGGREGATE rows on the same `InvestmentIdentifierAxis`. Phase 6
dodged this by using the tagged total only as a *denominator*; these questions need clean dollar
*numerators*, so it cannot be dodged.

Three distinct causes, diagnosed on the worst offenders:

| Fund | Ratio | Cause |
|---|---|---|
| Oxford Square Capital | 32.1× | Top-level totals (`Investments in Securities and Cash Equivalents`, `Cash Equivalents`), no par |
| Horizon Technology Finance | 5.0× | Unparsed raw XBRL member names (`PortfolioInvestmentAssetsMember`, `NonaffiliateDebtInvestmentsMember`) |
| Bain Capital Specialty Finance | 1.79× | Denormalized category paths that look like real holdings and carry matching principal |

Candidate fixes, measured by how many funds become usable at BOTH endpoints and what share of
end-date BDC assets they represent:

| Filter | Funds usable | Asset coverage |
|---|---|---|
| Baseline (priced debt, `parse_ok`) | 30 | 51.5% |
| + drop member-name / total-phrase rows | 31 | 51.7% |
| **+ require a par (principal) amount** | **43** | **88.9%** |
| + both filters | 43 | 88.9% |

**The sophisticated fix is worthless (+0.2pp). The trivial one carries the whole load.**
Requiring a par amount takes asset coverage from 51.5% to **88.9%**, because aggregate rows are
fair-value-only sector totals that almost never carry par, while real debt holdings do. It is
also conceptually mandatory: **percent of par is undefined without par.**

Two consequences to state in the output:

- It drops priced debt whose price came from the FV/cost fallback (`price_basis == "cost"`),
  which is mostly partially-funded revolvers and delayed-draws. Those sit near par by
  construction, so excluding them slightly understates the healthy bucket. Disclose it.
- The residual ~11% of assets sit in funds that still over-count (the Bain-type denormalized
  hierarchy). Those are **flagged and excluded, with the exclusion list published** on the
  Coverage tab. Per-filer hierarchy inference is explicitly out of scope — it is the expensive
  problem and it buys 11%.

**Self-validating by design:** the bucket shares are computed as
`Σ(priced debt FV in bucket) / tagged portfolio total`. Across all buckets they must sum to
≤ ~1.0. A fund summing above 1.05 has double-counted rows and is flagged out automatically —
the test is the same object as the metric, so contamination cannot pass silently.

### 4.2 Only some funds can support a start→end comparison

At 2024-12-31, 60 of the funds present at 2026-06-30 also have priced holdings (98.1% of
end-date assets); after the par filter, **43 funds are usable at both ends, 88.9% of assets**.
Eleven funds exist at the end but not the start — seven first appear at 2025-06-30, a launch
wave.

**A fund launched in 2025 shows zero migration and will rank as the best manager in the book.**
Every fund-level tab therefore carries `usable?` and `constant_sample?` flags, and the unusable
funds are sorted into their own block rather than interleaved. This is the same failure mode as
the Phase 6 composition guard: a sample change masquerading as a result.

### 4.3 "The fund's price return" does not exist in this data — something better does

We hold marks on loans, not fund returns. A weighted mark change is **not a return**: it omits
interest income (the dominant component of BDC total return), realised gains and losses,
leverage, and fees. A fund with −2% weighted mark change can post +8% NAV total return because
coupon dominates. Publishing mark change as "price return" would invite a serious misreading.

`financial_highlights.total_return` **is** extracted — 46 of 72 June-2026 filings (64%). So #6
is answered as **valuation drag vs actual reported total return**, side by side, with the mark
change explicitly labelled a drag and never a return. That is a stronger answer than the proxy
the question implies.

### 4.4 Weighting by ending fair value is circular

A marked-down loan carries a smaller ending weight and so understates its own contribution.
**All weights are fixed at the start of the window** (start-date fair value), with **par as a
robustness check** since par is mark-invariant.

---

## 5. Thresholds: parameters, not a choice (MEASURED)

His "(90 or 95?)" and "(below 90 or 85?)" hedges are correct instincts. The mark distribution at
2026-06-30 explains why:

| Bucket | Share of priced rows |
|---|---|
| 98–100 | **54.7%** |
| 95–98 | 12.1% |
| 90–95 | 6.5% |
| 85–90 | 3.4% |
| ≤85 | 10.0% |
| >100 | 13.1% |

**80.1% of assets sit at ≥95 and only 13.4% below 90.** So "above 95 at start, below 90 at end"
is a meaningful, non-trivial slice. But with over half the mass at 98–100, the answer is highly
sensitive to whether "healthy" means 95 or 98.

Therefore: thresholds are module constants, and the Coverage tab carries a **sensitivity grid**
(95→90, 95→85, 98→90, 90→85) so the headline is never one arbitrary pair. Marks above 110 (2.9%)
are treated as suspect — accrued interest landing in fair value — and bucketed separately rather
than as premium.

---

## 6. Deliverable format

`data/dataset/credit_migration.xlsx`, built by a new `src/analysis/credit_migration.py`, reading
`holdings_consolidated.csv` / `holdings_matched.csv` and `data/extracted/*.json`. Follows the
`--from-cache` pattern so re-runs take seconds.

| Tab | Answers | Contents |
|---|---|---|
| **Overview** | — | Window, thresholds, the four §4 caveats, and the reconciliation result stated up front |
| **MigrationMatrix** | 2, 3, 4 | Start bucket × end bucket, in $ and issuer count; universe-wide and per fund. Includes the diagonal (what stayed) and the already-impaired low-start rows |
| **FundMigration** | 2, 3, 4 | Per fund: % of assets per bucket at both ends, % migrated, issuers migrated, the exited slice, `usable?`, `constant_sample?`, priced-debt coverage |
| **IssuerImpact** | 1, 5a | Per issuer: start-weighted price change, BDC-visible debt at both ends ($ and par), share of the BDC universe, holder count — sorted by importance × deterioration |
| **FundAttribution** | 5b, 6 | Per fund: whole-portfolio weighted mark change, the deteriorating slice's contribution, vs the asset-weighted universe mean, **and vs reported `total_return`** |
| **Concentration** | *unasked* | Issuers driving 80% of each fund's drag |
| **ManagerRollup** | *unasked* | Every cut above at parent-manager grain |
| **Coverage** | — | Reconciliation per fund-date, funds excluded and why, threshold sensitivity grid, what the caps dropped |

---

## 7. The unasked questions worth including

- **A migration matrix answers #2, #3 and #4 at once.** Start bucket × end bucket, the same
  object as a rating transition matrix. He asked for three slices; the matrix gives every slice
  plus the diagonal, which is the baseline his numbers must be judged against.
- **What happened to the loans that left?** A fund that *sold* its deteriorating credits shows
  zero migration — arguably correct (they got out) but currently invisible and easily mistaken
  for good underwriting. Needs a third category beside held-and-migrated: **exited**.
  Repaid-at-par and sold-at-a-loss are partly separable from position and par changes.
- **Selection or marking?** #6 says a fund took a valuation hit; it does not say whether that is
  because it *holds* worse credits or *marks* them more conservatively. `marking_bias.xlsx`
  already isolates the marking half. Joining them turns "this fund looks bad" into "unlucky" vs
  "honest".
- **Is the drag concentrated?** How many issuers account for 80% of each fund's decline. One bad
  name is a different risk story from thirty, and both produce the same aggregate number.
- **Manager rollup.** He asks per fund; several managers run five or more vehicles, and
  family-level concentration is usually the real finding. `fund_manager_map.csv` exists.
- **Non-accrual cross-check.** `non_accrual_fair_value` is extracted. A fund whose marks fell
  while non-accruals did not is marking ahead of the credit event — or behind it. A free
  validation of the whole exercise.

---

## 8. Caveats to state in the output

- **"Importance to the market" is really importance to the BDCs we track.** A loan's true market
  size includes CLOs, insurance accounts, and private funds we cannot see; our universe is 92
  funds with holdings. #1 measures **BDC-visible breadth**, and must be labelled as such.
- **Issuer grain, not tranche.** MEASURED in Phase 7: of 1,059 (issuer, seniority, spread)
  tranches, 55% survive only one as-of date, because repricings and tagging drift move the
  spread. Several tranches of one borrower therefore collapse into a median mark and a summed
  exposure.
- **Weighted mark change is not a return** (§4.3).
- **Non-calendar fiscal quarter-ends.** Funds whose quarters do not land on 12-31/6-30 have no
  observation exactly at the window endpoints.
- **~11% of assets excluded** as unreconcilable (§4.1), listed by name on Coverage.
- Everything remains **best-effort matching with confidence, not exact reconciliation.**

---

## 9. Sequencing — three checkpoints, killable at the first

The par filter is cheap, but the reconciliation gate is still the thing that decides whether
dollar answers are safe. Build in this order so it can be abandoned early:

1. **Filter + reconciliation gate.** Deliverable: the Coverage tab alone — how many fund-dates
   reconcile within ±10%, which funds are excluded, what asset share survives. Expected 43 funds
   / 88.9% of assets. **If that number comes in materially lower on a fresh run, stop:** items
   2/3/5a/6 are not safely answerable in dollars, and the fallback is issuer *counts* only,
   which needs no reconciliation at all.
2. **Engines A and B** — MigrationMatrix, FundMigration, IssuerImpact.
3. **Engine C** — FundAttribution against reported total return, plus Concentration and
   ManagerRollup.

---

## 10. Open decisions

- **Report all 92 funds with flags, or only the 43 usable ones?** Recommendation: show all,
  with unusable funds in a separate block, so nobody accidentally ranks a 2025 launch as the
  cleanest book.
- **#6 headline:** drag vs reported `total_return` (real, 64% coverage) or pure weighted mark
  change (full coverage, not a return)? Recommendation: both columns side by side, mark change
  explicitly labelled a valuation drag.
- **Par or fair value as the #1 weight?** Recommendation: start-date fair value as the headline,
  par as the published robustness check.
