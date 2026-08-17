# N-CSR LLM Extraction — Plan and Runbook

**Status:** M0–M3 built. M4 (gold sample) is next and gates all spend.
**Written:** 2026-08-16 (session 20).

> **Why this file exists.** The approved plan lived only at
> `C:\Users\bmoriar\.claude\plans\i-d-like-to-move-witty-bachman.md` (session 17). That
> file is **gone** — the plans directory now holds an unrelated plan for a different
> project. The M1–M6 detail survived only as prose inside `PROJECT_STATUS.md`. This
> document is the reconstruction, reconciled against what the code now actually does,
> and it is the living runbook from here on. It was scheduled for M6; losing the
> original moved it up.

---

## 1. Why this path at all

Session 10 probed ten interval/tender-offer funds across every registration vintage and
established that **their financial statements are not XBRL-tagged**:
`xbrl.statements.balance_sheet()` returns `None`. Closed-end and interval funds were
never swept into the financial-statement inline-XBRL mandates — only the N-2 prospectus
cover data is structured. So unlike the BDC path (which reads XBRL facts directly),
these funds require reading the document.

The architecture is therefore: **deterministic local preprocessing → one structured
Claude call → deterministic local mapping → the existing, unchanged validation and
spreadsheet spine.**

```
  .htm on disk
      │  ncsr_sections.extract_sections()          M1 — free, local, no API
      ▼
  serialized statements block  (p50 ~2,346 tokens, 99.6% smaller than the raw HTML)
      │  ncsr_prompt.build_system() + build_user()  M3
      ▼
  ONE Claude call, structured output                M3 — ncsr_llm
      ▼
  NCSRRawExtraction  (flat, values AS PRINTED + a scale)
      │  ncsr_map.map_raw_to_extraction()           M3 — scale, nest, Fact-wrap, score
      │      ▲
      │      └── ncsr_anchors: the filer's own inline XBRL (free cross-check, rule X1)
      ▼
  FilingExtraction
      │  validation.rules.validate()                UNCHANGED, shared with the BDC path
      │  bdc_xbrl.compute_derived()                 UNCHANGED, shared with the BDC path
      ▼
  data/extracted/*.json → build_spreadsheet.py → semiliquid_bdc_dataset.xlsx
```

The load-bearing property is that **only the middle is new**. Everything from
`FilingExtraction` onward is the code that already produced the BDC dataset. That was
verified end-to-end this session with a hand-built record: `validate()` ran C1, C1b, C2,
C5 and A1 against LLM-sourced data with no modification, and correctly failed C3 on a
deliberately inconsistent fixture.

---

## 2. Milestones

| | What | Cost | State |
|---|---|---|---|
| **M0** | Environment: `pyproject.toml`, `uv.lock`, `.python-version`, `anthropic` pinned | $0 | done (`4c41d78`) |
| **M1** | `ncsr_sections.py` + `ncsr_inventory.py` — locate and serialize the statements | $0 | done, **gate 98.8%** |
| **M2** | `ncsr_series.py` — multi-series slicer over 428 multi-block filings | $0 | not started |
| **M3** | Schema, prompt, XBRL anchors, mapper, API module | ~$0 | **done this session** |
| **M4** | ~25-filing hand-verified gold sample | ~$5–10 | **next; gates all spend** |
| **M5** | Batch backfill of the corpus | ~$96 | blocked on M4 |
| **M6** | Ship into the workbook | $0 | blocked on M5 |

**M2 is deliberately not a prerequisite for M5.** It covers 428 of ~3,006 filings and
about $15 of the deadline-sensitive cost; the single-block bulk (~$41 of exposure
against the 2026-08-31 Sonnet intro-pricing deadline) does not depend on it.

---

## 3. What M3 built

| File | Job |
|---|---|
| `src/schema/ncsr_raw.py` | `NCSRRawExtraction` — 64 flat fields the model fills in |
| `src/extraction/ncsr_prompt.py` | System rules + a field dictionary **generated from** the schema's own descriptions |
| `src/extraction/ncsr_anchors.py` | Inline-XBRL facts from the filing itself (rule X1) |
| `src/extraction/ncsr_map.py` | Scale → nest → `Fact(source=LLM)` → confidence, plus the identity cross-check |
| `src/extraction/ncsr_llm.py` | The request, interactive and batched, plus a free cost estimate |

Every one of these runs and self-tests **without an API key**:

```powershell
uv run python src/schema/ncsr_raw.py                    # schema + JSON-schema subset check
uv run python src/extraction/ncsr_prompt.py             # renders the full prompt
uv run python src/extraction/ncsr_anchors.py <file.htm> # dumps a filing's XBRL anchors
uv run python src/extraction/ncsr_map.py                # scaling + identity + negative control
uv run python src/extraction/ncsr_llm.py <file.htm> --dry-run   # request shape, no call
```

### Design decisions, and what each one is defending against

**Flat intermediate, not the nested schema.** Nesting costs output tokens, invites the
model to file a value under the wrong sub-object, and asks it to invent provenance it
cannot know. The mapper does the nesting in testable Python.

**Values as printed plus a separate scale.** The model never multiplies. Fund statements
are routinely "in thousands", and a silently dropped 1000× is indistinguishable from a
fund 1000× smaller — the exact class of error that produced the Prospect holdings
mis-scale. Dollars and share counts carry **separate** scales, because filings commonly
print one in thousands and the other whole.

**Identity is echoed back, not supplied.** The prompt never tells the model the fund
name, CIK, or period — those are precisely what the cross-check compares. The likeliest
failure of this whole pipeline is not a garbled number; it is a perfectly-formed
extraction of the **prior-year column** of a side-by-side statement, which is invisible
in the numbers themselves.

**Every field is required; absence is spelled `null`.** A field with a default can be
silently omitted, and then "did not look" is indistinguishable from "looked and found
nothing". Costs a few hundred output tokens per filing.

**One request shape for pilot and batch.** The Batches API cannot use
`client.messages.parse()`, so `ncsr_llm.build_params()` uses `output_config.format` with
the generated JSON schema and validates the returned JSON itself. A pilot that exercised
a different request shape than the backfill would not be a pilot.

### The XBRL anchors turned out better than the plan assumed

10.7% of the corpus (330 of 3,084 filings) carries inline XBRL — three times the
smoke-test rate. The plan assumed it would supply `cef:` prospectus values. Measured on
real filings, it supplies more:

| Concept | Use |
|---|---|
| `dei:DocumentPeriodEndDate` | **The period end, from the filer.** Directly catches the wrong-column failure |
| `dei:EntityRegistrantName` | Fund-name cross-check |
| `dei:EntityCentralIndexKey` | CIK cross-check against the filename |
| `us-gaap:NetAssetValuePerShare` | NAV per share, per class, per date |
| `cef:LongTermDebtPrincipal` | Borrowings |
| `cef:OutstandingSecurityHeldShares` | Shares outstanding, per class |

Contexts are defined **inline in the `.htm`**, so nothing has to be fetched. Two traps
the parser handles and a reader should know about: (1) a filing tags **several years at
once** — ACAP carries share counts for FY2023, FY2024 and FY2025 — so every anchor is
resolved to a period and lookups must be period-scoped, or you manufacture false
mismatches; (2) `scale="-2"` means the printed 1.50 is really 0.015 and `sign="-"` means
negate, so reading the displayed text alone gives a number wrong by 100× with no outward
sign of trouble.

Verified live: ACAP Strategic Fund → period end "September 30, 2025", CIK 0001467631
(matches the filename), NAV Class A $29.22 / Class W $22.19.

### Confidence model

| Level | When |
|---|---|
| **0.97** `CONF_ANCHORED` | An independent XBRL fact from the filer agrees within 1% |
| **0.80** `CONF_BASE` | Read by the model; nothing contradicts it |
| **0.60** `CONF_ORPHANED` | Value came from a statement the model itself reported absent |
| **0.40** `CONF_CONTESTED` | An XBRL fact disagrees, **or** the filing's identity is contested |

Deliberately coarse — a finer scale would imply a calibration nobody has measured. M4 is
what turns these into evidence-backed numbers. Per the project's standing flag-and-keep
policy, a contested value is **kept and flagged**, never dropped.

### Stated assumption to verify in M4

**Ratios are stored as decimal fractions (0.0185), not percent numbers (1.85).** The
prompt asks the model for percent numbers because that is how filings print them and
asking for a conversion invites a silent 100× error; the mapper divides by 100 on the
way in. The reason is consistency with the BDC side, where
`us-gaap:InvestmentCompanyExpenseRatio` is tagged as a fraction and
`build_spreadsheet.DEC_FMT` renders it "as-tagged". **If the gold sample shows the BDC
column is not consistently a fraction, `ncsr_map._PERCENT_TO_FRACTION` is the only thing
that has to change.**

---

## 4. M4 — the gold sample (next, and it gates everything)

~25 filings, every field hand-verified against the source document. Gate: **≥98% on
identity-anchored fields**, C1/C2 first-pass **≥85%**. This is the most expensive step
in *Brian's* time and the cheapest in compute — which is the point.

Stratification, extended with the three document layouts M1 discovered the hard way
(each was a silent-failure class before it was found):

- single-fund N-CSR **and** N-CSRS
- one Stone Ridge slice, one AIP slice (multi-series)
- one p90-size filing (~4,979 tokens)
- filings both with and without `cef:` anchors
- both vehicle types (Interval Fund, Tender Offer Fund)
- **one legacy EDGAR ASCII-table filing** (Pioneer ILS or Winton)
- **one table-less dot-leader filing** (Principal Real Asset)
- **one `balance_sheet_after_start_anchor` filing**

Also measure in M4, because they are the open cost questions:

1. **Actual output tokens per filing.** The $96 estimate assumed ~5,000 and this has
   never been measured. It is the dominant term.
2. **`effort` sweep (`low` vs `medium`).** Thinking tokens bill as output. Sonnet 5 runs
   adaptive thinking by default, which the original estimate did not account for.
   `ncsr_llm.DEFAULT_EFFORT` is a parameter specifically so this is a sweep, not a guess.
3. **Prompt-cache hit rate.** The ~2,900-token system prompt should be written once and
   read at ~10% thereafter; confirm via `usage.cache_read_input_tokens`.

---

## 5. M5 — the backfill

- ~3,006 distinct located periods, Message Batches API (50% off), resumable 300-filing chunks.
- **Write one JSON per filing as results arrive** (`collect_batch` does this) — batch
  results return in arbitrary order and are keyed only by `custom_id`, and a crash
  partway through must not lose completed work.
- Escalate identity-check failures to `claude-opus-5`.
- **Deadline: Sonnet 5 introductory pricing ($2/$10 per Mtok) ends 2026-08-31.**
  Slipping costs roughly $41 on the single-block bulk and ~$15 on the multi-series tail.

Outstanding decisions before spending:

- **698 located filings have `vehicle_type = unknown`.** They are outside the gate's
  in-scope set (Interval Fund 1,336 + Tender Offer Fund 980 = 2,316 located in scope).
  Extracting them adds roughly $22. Decide before M5.
- **Personal vs enterprise Anthropic account** for the spend.
- `ANTHROPIC_API_KEY` still unset; `src/extraction/api_smoke_test.py` still unrun.

---

## 6. Still open from M1 (free, no API key)

1. **Balance sheet printed *after* the statements run** (ACAP-style) — 52 single-block
   filings still flagged; needs a forward search analogous to `highlights_after_notes`.
2. **62 filings flagged `no_anchors`** — undiagnosed, mostly large documents.
3. **~7 table-less stragglers** — BlueBay Destra, Pender, Destra Multi, Aspiriant.
4. CNR's one residual 40,585-char restatement block.
5. Singleton misses — deliberately out of scope under the ≥3-filings rule.

---

## 7. Rejected alternatives (recorded so they are not re-litigated)

- **`pandas.read_html` → code mapping.** This is the filer-profile approach the project
  already superseded; it does not survive layout variety.
- **`sec-parser`.** Held in reserve if the M1 anchor hit-rate had fallen below 95%. It
  did not — M1 is at 98.8%.
- **edgartools MCP / AI integration.** Serializes *parsed XBRL*; it cannot read untagged
  HTML, which is the entire problem here.
- **Widening `_REFERENCE_CUES` look-behind** (session 19). Measured and rejected: the
  distributions overlap outright (both p75 = 73), so no threshold separates a footnote
  reference from a real heading.
- **Holdings.** Out of scope — N-PORT XML is a separate workstream.
