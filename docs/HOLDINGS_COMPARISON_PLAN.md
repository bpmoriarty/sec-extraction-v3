# Cross-BDC Holdings & Mark Comparison — Plan

Scoped 2026-06-11 (session 12). Grounded in a reconnaissance of the 375k-row consolidated
holdings dataset. **No analysis code written yet** — this is the plan of record. (Supersedes the
sketch in `LISTED_BDC_PLAN.md` §9.)

---

## 1. Goal

Across the full BDC universe (listed + unlisted), match the **same underlying credit** held by
multiple BDCs and **compare each holder's mark** (fair value as a % of par) at a given reporting
date. **Two grains, both first-class deliverables:** (a) **issuer-level** — how do BDCs collectively
mark their exposure to Company X (broader coverage, easier match); (b) **issue-level** — how do they
mark *this specific tranche* (sharper, cleaner apples-to-apples, harder match). We report both. BDC marks are manager estimates on illiquid Level-3 loans with no observable market price, so
the same loan held by several BDCs can be marked differently. That **dispersion is the signal** —
aggressive vs. conservative valuation, an early credit-deterioration warning when one holder marks
down before others, and outlier detection.

---

## 2. Feasibility evidence (reconnaissance, 2026-06-11)

Consolidated all 793 holdings CSVs → 375,181 rows / 74 funds. Findings that shape the plan:

| Signal | Value | Implication |
|---|---|---|
| (issuer, period) pairs held by ≥2 funds | **23,251** | Large matchable universe; the premise holds |
| Distribution by #funds | 2:15.4k, 3-12: declining tail, 13+:70 | Matches the club-deal intuition (2–12 holders) |
| Broadly-held names | Icefall/Anaplan/Avalara/Zendesk/PetVet/Flexera/Finastra (13–15 funds) | Real broadly-syndicated credits — validation anchors |
| `spread` coverage | **58.8%** | Primary issue-matching key (contractual, stable) |
| `rate` (all-in) coverage | 48.3% | Weaker key — floating coupons reset; within-period only |
| `fair_value` / `principal` both present | **60.1%** | Price (FV/par) calculable for ~225k rows |
| `cost` coverage | 79.7% | FV/cost fallback when principal missing |
| issuer field with comma ("Name, …, Instrument") | 81.5% | Parse name = text before first comma (after junk-strip) |
| maturity date | **NOW CAPTURED (session 12)** — ~54% of funds tag it, richly (63–100% within-fund) | Strong issue key where present; bimodal (AB 100%, Apollo 97%, Blackstone 0%) |
| reference rate (SOFR/LIBOR/Prime) | **NOW CAPTURED** — ~42% of funds | Disambiguates the spread basis (S+550 ≠ Prime+550) |

A follow-up 24-fund coverage check corrected an initial 7-fund undercount: maturity is **bimodal** —
a fund tags it richly or not at all — and ~half tag it. So it's a worthwhile issue key for the
covered half, captured via the extractor enhancement (§10). Reference-rate type and acquisition date
captured the same way. **Verdict: strongly feasible at the issuer level; issue-level disambiguation
is the work, now better-equipped with maturity + reference rate.**

---

## 3. The core challenge — issuer (easy) vs issue (hard)

(Confirmed by Brian's colleagues' prior attempts.)

- **Issuer = the borrower** (e.g. "Anaplan"). Matchable by fuzzy name normalization.
- **Issue = the specific instrument/tranche** (e.g. "Anaplan 1st-lien term loan, S+550, due 2029").
  An issuer can have many tranches (1st lien, 2nd lien, revolver, delayed-draw, preferred, equity).
  This is where matching is hard and where prior attempts struggled.

**Matching strategy — combine structural + economic + corroborating signals:**

1. **Issuer cluster** (normalize names, fuzzy-match) — the block within which issues are compared.
2. **Instrument type / seniority** — parsed from the description (First Lien / Second Lien / Senior
   Secured / Subordinated / Unsecured / Revolver / Delayed Draw / Term Loan / Preferred / Common /
   Equity / Warrant). Splits an issuer's holdings into tranche families.
3. **Spread over base rate** — the strongest stable economic key (contractual; survives rate resets).
4. **Maturity date** — a strong corroborator when present (see §10; currently missing).
5. **Reference rate + floor** — secondary corroborators.
6. **All-in coupon** — useful only WITHIN a reporting period (resets across dates), as a tiebreaker.
7. **Co-occurrence** — a (issuer, seniority, spread) combo appearing in 2–12 funds in the same
   period is strong evidence of one real club deal (matches the observed distribution). Lone
   appearances or >~15 holders warrant scrutiny.

**Confidence tiers (only High/Medium feed the headline mark comparison):**
- **High** — issuer + seniority + spread align, AND (maturity agrees where both present OR
  co-occurrence sits in the 2–12 club-deal band).
- **Medium** — issuer + seniority + spread align, but maturity missing and single/low co-occurrence.
- **Low** — issuer + seniority only (tranche ambiguous) → reported as aggregate, not a price compare.

---

## 4. The comparison unit

**Price = fair_value / principal** (cents on the dollar) — comparable across holders regardless of
position size. Calculable for ~60% of rows. **Fallback:** fair_value / cost (a mark-vs-cost proxy)
when principal is missing — flagged as lower-fidelity (cost ≠ par). Equity/preferred holdings (no
spread, often no principal) are out of scope for the price comparison (kept for issuer-level context
only).

---

## 5. Pipeline (phases / deliverable increments)

Each phase is a checkpoint; prototype on a clean subset before scaling.

1. **Consolidate + clean** — one normalized holdings table from all CSVs. Parse the issuer field
   into {issuer_name, instrument_text}; derive seniority/type; compute price (FV/par). Strip category/
   subtotal junk rows ("Non-controlled/Non-Affiliated Investments…", "Portfolio Company Debt
   Securities-…"). Dedup exact-duplicate rows within a fund-period.
2. **Issuer normalization + clustering** — strip legal suffixes / aliases (d/b/a, FKA); fuzzy-cluster
   with rapidfuzz; assign an issuer-cluster id. Validate against the 23k overlap + named anchors.
3. **Issue matching** — within each cluster, group into instruments by (seniority, spread, [maturity,
   floor, ref-rate]); assign a confidence tier + co-occurrence count.
4. **Mark comparison** — per (matched issue, period): collect holders + prices, compute median,
   dispersion (range, stdev), and flag outliers (e.g. >N points off the median). Align periods by
   reporting date; flag when holders' dates differ by > ~45 days.
5. **Output + validation** — a cross-holder marks workbook/tab; validate via named anchors, match
   rate, confidence distribution, and a manual-review sample.

---

## 6. Contamination handling (lighter than for §9 metrics)

The 36 listed BDCs (and Kennedy Lewis etc.) have subtotal/duplicate rows that blocked the §9 sum-
based metrics. For THIS analysis the impact is smaller, because we compare per-holding **prices**
(ratios), not sums: a duplicate row just repeats the same price (dedup handles it), and subtotal/
category rows lack a clean (issuer, spread) so they fall out of the matching keys. We still: (a) strip
known category-row prefixes, (b) dedup within fund-period, and (c) carry the per-filing reconciliation
ratio as a confidence input (heavily-contaminated filings → lower confidence).

---

## 7. Caveats (state them in the output)

- **Period alignment** — BDCs have different fiscal quarter-ends; align to the nearest common date
  and surface the day-gap. A mark difference across a 2-month gap isn't purely valuation.
- **Floating-coupon reset** — match on spread, not all-in coupon, across dates.
- **Trading** — a loan can move between portfolios over time; for a point-in-time compare that's
  fine (whoever holds it that quarter). Time-series tracking of one issue is a later extension.
- **Name conventions vary** — some filers denormalize; expect partial coverage. Report match rate.
- **Slices differ** — each BDC holds a different principal of the same loan; price normalizes this.
- It is **best-effort discovery with confidence scores**, not exact reconciliation. The payoff is the
  surfaced dispersions for human review, not a claim of exhaustive matching.

---

## 8. Architecture

A new **analysis module** (`src/analysis/holdings_compare.py`), independent of the extractor —
reads the holdings CSVs (`data/holdings/`) + the JSONs (period dates, balance-sheet investments for
the contamination ratio). Uses `rapidfuzz` (already installed) for name clustering. Output: a
workbook/tab under `data/dataset/`. Pairs naturally with the existing `holdings_fv_recon` diagnostic
(the per-filing complement to this universe-wide view).

---

## 9. Validation approach

- **Named-anchor test** — confirm the matcher groups the known broadly-held credits (Icefall,
  Anaplan, Avalara, Zendesk, PetVet, Flexera, Finastra) into single issues across their holders.
- **Match-rate + confidence distribution** — report % of debt-like holdings matched, by tier.
- **Manual-review sample** — eyeball a stratified sample of High/Medium/Low matches.
- **Dispersion sanity** — most matched marks should cluster tightly (a few points); investigate
  wide spreads (the analytical payoff) and implausible ones (matching error).

---

## 10. Prerequisite — capture maturity + reference rate — DONE (session 12)

Maturity date is a strong issue-matching key but wasn't extracted originally (it wasn't needed for
the §9 summary metrics). A 24-fund coverage check showed it's tagged by ~54% of funds, richly
(bimodal). **Added to the holdings extractor** (`bdc_xbrl.py` `HOLDING_STR_CONCEPTS` + string-fact
handling in `holdings()`; new CSV columns `maturity`, `reference_rate`, `acquisition_date`):
- `us-gaap:InvestmentMaturityDate` → `maturity` (ISO date)
- `us-gaap:InvestmentVariableInterestRateTypeExtensibleEnumeration` → `reference_rate` (normalized to
  SOFR/LIBOR/PRIME/EURIBOR/BASE)
- `us-gaap:InvestmentAcquisitionDate` → `acquisition_date` (ISO date)

These are STRING-valued facts (numeric_value is null) read from the fact `value`; the extractor now
keeps them through the reporting-date instant filter. Verified: AB 100%/100%, Apollo 97%/92%,
Blackstone 0% (doesn't tag — null, no error). **Requires a full clean re-run to populate the CSVs**
before the matcher is built. Other uncaptured holding concepts (per-holding realized/unrealized
gain-loss, per-filer commitment/income concepts) are sparse/niche — not captured now.

---

## 11. Sequencing & risks

- **Prototype first** on a clean, well-tagged subset (funds with high spread+principal coverage and
  clean issuer fields) to prove the matching + measure match rate before scaling to all 74 funds.
- **Risk: false issue-matches** (same issuer, wrong tranche) → mitigated by requiring spread+seniority
  agreement and the confidence tiers; Low-tier never drives a price compare.
- **Risk: name-cluster errors** (over- or under-merging) → fuzzy-match threshold tuning + the
  named-anchor validation + a manual review of borderline clusters.
- **Risk: thin coverage for some filers** (Apollo/First Eagle barely tag rate; denormalized issuers)
  → those holdings match at lower tiers or drop out; report coverage honestly, don't force matches.
