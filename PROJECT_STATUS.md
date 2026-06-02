# SEC Filing Extraction — Project Status

## Project Goal

Extract structured financial data from SEC filings (HTML format) across a broad
set of filers and filing types. The long-term aim is a scalable pipeline that can
handle many filers with minimal per-filer manual work.

---

## Current State

**Phase: Fund universe built. Downloader not yet started.**
**Last Session: 2026-06-02**

### What's Working
- Virtual environment set up (`uv venv` inside `sec-extraction-v3/`)
- `src/fund_universe/build_universe.py` — seeds from 1,895 existing filenames +
  queries EDGAR N-23C3A form type → outputs 324 funds (202 interval, 98 ncsr, 24 BDC)
- `src/fund_universe/enrich_from_mstar.py` — merges Morningstar's 508-fund list
  against universe by CIK (pass 1) then by name (pass 2) → 532 funds total
- `data/fund_universe.csv` — master fund list, 532 funds

### What's Not Done Yet
- `initial_pull.py` and `update_pull.py` — paused intentionally, next priority
- 198 "unknown" category funds have no CIK — excluded from downloader until
  CIKs are sourced
- Sub-category labels (unlisted BDC / unlisted REIT / interval fund / tender
  offer fund) not yet applied — waiting for organized Morningstar data

---

## What We Learned from Prior Work

### Old project (`old - SEC Filing Extraction/`)

This is the most complete prior attempt. Key things that were built and worked well:

**Format classification system** — The single most valuable idea from prior work.
195 filer profiles were grouped into 6 extraction groups based on how their HTML
is structured:

| Group | Name | Count | Approach |
|-------|------|-------|----------|
| 1 | BDC XBRL (10-K/10-Q) | 23 | HTML table + optional XBRL fallback |
| 2 | N-CSR Standard Inline NAV | 130 | Single BS table, NAV at bottom |
| 3 | N-CSR Separate NAV Table | 19 | BS table + separate NAV table |
| 4 | N-CSR Embedded NAV Formula | 13 | Regex to parse NAV from label text |
| 5 | N-CSR No Per-Share NAV | 7 | BS fields only |
| 6 | N-CSR Text-Dense | 3 | Custom text parser (Fundrise) |

The key insight: group by structure, not by filer name. Within a group, only
small per-filer config differences remain (amounts_in_thousands, label variants,
share class count, etc.).

**Working extractor code:**
- `group1_bdc_xbrl_extractor.py` — handles BDC 10-K/10-Q filings
- `group2_ncsr_inline_nav_extractor.py` — handles most N-CSR filings
- Both include: HTML parsing, field extraction with confidence scoring,
  multi-class NAV, thousands scaling, and validation checks (C1–C5)

**Format detector** (`format_detector.py`) — maps filenames to filer names
and format profiles using a prefix lookup table.

**195 format profiles** in `skills/sec-extraction/formats/` — one .md file per
filer documenting the exact structure, quirks, and config for that filer.

**Validation rules** (C1–C5):
- C1: Balance sheet equation (Assets = Liabilities + Net Assets)
- C2: NAV calculation per share class (Net Assets / Shares = NAV)
- C3: Sum of class net assets = total net assets
- C4: Income statement equation (Income - Expenses = Net Investment Income)
- C5: Fair value hierarchy sum (L1 + L2 + L3 = Total)

**What data was extracted:**
- Balance sheet: total assets, total liabilities, net assets, investments at
  fair value, cash, total debt
- NAV per share: per class — net assets, shares outstanding, NAV per share
- Income statement: total investment income, expenses, net investment income,
  plus optional line items
- Fair value hierarchy: Level 1/2/3 totals for investments

**Filename convention used:** `FundName_CIK_FilingType_Date.htm`
(e.g., `Cliffwater_Corporate_Lending_Fund_0001735964_N-CSRS_2022-12-09.htm`)

### Archived pandas project (`archived - SEC Filing Extraction - Pandas/`)

Earlier, simpler attempt focused on Cliffwater filings only. Used pandas for
table extraction. Good as a starting point but too narrow in scope.

---

## What to Bring Forward (Do Not Rewrite from Scratch)

The following are worth adapting into the new codebase:

1. **The 6-group classification framework** — keep as the core design principle
2. **The extraction logic from group1 and group2 extractors** — battle-tested
   against 195 real filers; adapt rather than rewrite
3. **The validation rules (C1–C5)** — already correct and reusable
4. **The format profiles** — 195 .md files document real filer quirks; reference
   when onboarding new filers

---

## What to Do Differently This Time

- Better folder structure and habits from the start
- Clearer scope — define what we're building before writing code
- Incremental — get one group working end-to-end before expanding
- Better output — decide on the target format (CSV, database, etc.) upfront

---

## Decisions Made

- Old folders kept as reference, not deleted
- New project folder: `sec-extraction-v3/`
- Shared filings data stays in the parent `filings/` folder
- edgartools is a Python library (not an MCP) — installed via `uv pip install edgartools`
- CIK is the primary identifier for all funds — name-based EDGAR lookup is too
  unreliable for production use
- Interval funds discovered via N-23C3A EDGAR form query (only interval funds
  file this form)
- 198 no-CIK Morningstar funds added as category "unknown" — excluded from
  downloader until CIKs are obtained
- Sub-category classification (BDC / REIT / interval / tender offer) deferred
  until after the downloader is built — classification matters for extraction,
  not for downloading
- OneDrive requires `--link-mode=copy` for all `uv pip install` commands

---

## Next Steps

1. **Source CIKs** for the 198 "unknown" funds if possible (Morningstar has
   provided all it has; may need manual lookup or another data source)
2. **Build `initial_pull.py`** — download all historical filings for funds with
   CIKs (form types: N-CSR, N-CSRS for interval/ncsr funds; 10-K, 10-Q for BDCs)
3. **Build `update_pull.py`** — periodic check for new filings since last run
4. **Apply sub-category labels** when organized Morningstar data is available
5. **Begin extraction work** — start with one format group end-to-end

---

## Session Log

| Date | What Happened |
|------|---------------|
| 2026-06-02 | Built fund universe pipeline. `build_universe.py` seeds from existing filenames + queries EDGAR N-23C3A → 324 funds. `enrich_from_mstar.py` merges 508-fund Morningstar list by CIK then by name → 532 funds total. 198 funds have no CIK and are marked "unknown". Downloader not started — paused to commit. |
| 2026-06-01 | Project started. Read all old code. Created this folder and status file. |
