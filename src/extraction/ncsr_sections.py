"""
ncsr_sections.py — Stage 1 of the N-CSR LLM extraction path: find the financial
statements inside an N-CSR/N-CSRS HTML filing and turn them into compact text.

WHY THIS EXISTS
---------------
Interval funds and tender-offer funds file N-CSR/N-CSRS reports whose financial
statements are NOT XBRL-tagged, so there is no machine-readable source the way there
is for the BDC 10-Ks. Something has to read the HTML. The division of labour is
deliberate:

  * CODE (this module) handles STRUCTURE — where the statements are, what the tables
    look like. Structure is mechanical, so it should be deterministic, free, and
    debuggable.
  * The LLM (ncsr_llm.py, later) handles SEMANTICS — which row label means
    "total net assets". That is what varies across ~130 managers, and it is what no
    amount of per-filer Python ever pinned down (the old "filer profiles" attempt).

This module never calls an API and costs nothing to run.

THE PROBLEM IT SOLVES
---------------------
A median N-CSR is ~0.75 MB of HTML (~170K tokens if you fed the whole thing to a
model); the largest in this corpus is 150 MB. But the financial statements are a
thin slice of it. In a typical filing the Schedule of Investments — the holdings
list, which we do NOT want here, since holdings come from N-PORT — occupies roughly
4%–50% of the document, and the statements we DO want run from about 50% to 59%:

    Statement of Assets and Liabilities    <- block starts here
    Statement of Operations
    Statement of Changes in Net Assets
    Statement of Cash Flows                (absent for many unlevered funds)
    Financial Highlights
    Notes to Financial Statements          <- block ends here

Slicing that block out is what makes one LLM call per filing affordable.

THREE HAZARDS, AND HOW EACH IS HANDLED
--------------------------------------
1. A TABLE OF CONTENTS near the top lists every statement title in the same
   canonical order, so "first match wins" lands on the TOC instead of the statements.
   -> Handled by requiring real GAPS between consecutive titles. TOC entries sit a
      few hundred characters apart; real statements are tens of thousands apart.

2. CROSS-REFERENCES in the notes ("...included in the Statement of Assets and
   Liabilities...") and PAGE FOOTERS ("See Notes to Financial Statements") look
   identical to a heading once the tags are stripped.
   -> Handled two ways: by scoring the ORDERED SEQUENCE (a real block shows Assets
      and Liabilities -> Operations -> Changes in Net Assets in that order, with
      gaps; scattered prose references rarely complete the sequence), and by a
      look-behind that rejects titles preceded by "see" / "accompanying" / "refer to".

3. TITLES SPLIT ACROSS TAGS, e.g. "<b>Statement of </b><b>Assets</b>", and
   non-breaking spaces (&nbsp;) sitting between the words.
   -> Handled by the offset-preserving blanking trick below.

THE OFFSET-PRESERVING TRICK (the one idea worth understanding here)
-------------------------------------------------------------------
To search text without losing track of where it sits in the original HTML, we
replace every tag and entity with an EQUAL NUMBER OF SPACES rather than deleting it.
The result (called "flat" text below) is exactly the same length as the raw HTML, so
a character position in the flat text is also a valid position in the raw HTML.

That buys two things at once:
  * we search on clean text but slice on real HTML (so the slice still parses); and
  * tag-split titles become matchable, because the blanked-out tags turn into
    whitespace that a `\\s+` pattern absorbs.

Run a diagnostic on one filing:
    uv run python src/extraction/ncsr_sections.py <path-to-filing.htm>
    uv run python src/extraction/ncsr_sections.py <path-to-filing.htm> --dump block.txt
"""

from __future__ import annotations

import argparse
import bisect
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from lxml import html as lxml_html

# --------------------------------------------------------------------------------------
# Filename parsing
# --------------------------------------------------------------------------------------
# Files are named  {FundName}_{CIK}_{FormType}_{YYYY-MM-DD}.htm  but BOTH the fund name
# and the form type can themselves contain underscores:
#   AIP_Alternative_Lending_Fund_A_0001709447_N-CSR_2022-12-08.htm   <- "_A" is the FUND
#   1WS_Credit_Income_Fund_0001748680_N-CSR_A_2022-01-19.htm         <- "_A" is an AMENDMENT
# So we anchor on the two unambiguous landmarks — the 10-digit CIK and the trailing
# ISO date — instead of counting underscores.
FILENAME_RE = re.compile(
    r"^(?P<fund>.+)_(?P<cik>\d{10})_(?P<form>.+)_(?P<date>\d{4}-\d{2}-\d{2})$"
)

# Which form types carry fund financial statements. N-23C3A filings are repurchase-offer
# notices with no financials at all (3,084 of them in the corpus) — deliberately excluded.
NCSR_FORMS = {"N-CSR", "N-CSRS"}


@dataclass(frozen=True)
class FilingName:
    """The four facts encoded in a filing's filename."""

    fund_name: str
    cik: str
    form: str  # normalised base form, e.g. "N-CSR" (amendment suffix stripped)
    filing_date: str
    is_amendment: bool
    path: Path

    @property
    def stem_key(self) -> str:
        """Dedupe key: an amendment supersedes the original for the same period."""
        return f"{self.cik}_{self.form}_{self.filing_date}"


def parse_filing_name(path: Path) -> FilingName | None:
    """Pull fund/CIK/form/date out of a filename, or None if it doesn't fit the pattern."""
    m = FILENAME_RE.match(path.stem)
    if not m:
        return None
    form = m.group("form")
    # Trailing "_A" (or "_A1", "_A2") on the FORM means an amendment: "N-CSR_A" -> "N-CSR".
    is_amendment = bool(re.search(r"_A\d*$", form))
    base_form = re.sub(r"_A\d*$", "", form)
    return FilingName(
        fund_name=m.group("fund").replace("_", " ").strip(),
        cik=m.group("cik"),
        form=base_form,
        filing_date=m.group("date"),
        is_amendment=is_amendment,
        path=path,
    )


def iter_ncsr_filings(filings_dir: Path) -> list[FilingName]:
    """Every N-CSR/N-CSRS (incl. amendments) in the corpus, sorted for stable runs."""
    out: list[FilingName] = []
    for p in sorted(filings_dir.glob("*.htm")):
        fn = parse_filing_name(p)
        if fn and fn.form in NCSR_FORMS:
            out.append(fn)
    return out


# --------------------------------------------------------------------------------------
# Offset-preserving flatten
# --------------------------------------------------------------------------------------
# Order matters: comments can contain '>' so they must go before the generic tag rule,
# and <script>/<style> bodies can contain '<' that would confuse it.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]*>", re.DOTALL)
_ENTITY_RE = re.compile(r"&(?:#\d{1,6}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,10});")


def _blank(m: re.Match[str]) -> str:
    """Replace a match with the same number of spaces, so character offsets don't move."""
    return " " * (m.end() - m.start())


def flatten_preserving_offsets(raw: str) -> str:
    """Strip markup to plain text WITHOUT changing any character's position.

    The returned string has the same length as `raw`; index i in one is index i in the
    other. See the module docstring for why that matters.
    """
    out = _COMMENT_RE.sub(_blank, raw)
    out = _SCRIPT_RE.sub(_blank, out)
    out = _TAG_RE.sub(_blank, out)
    out = _ENTITY_RE.sub(_blank, out)
    return out


# Files above this size are flattened in chunks rather than all at once, so we never hold
# a second full-size copy of a 150 MB document in memory.
LARGE_FILE_CHARS = 20_000_000
CHUNK_CHARS = 8_000_000
# Chunks overlap so a title straddling a boundary is still found intact in one of them.
CHUNK_OVERLAP = 8192


# --------------------------------------------------------------------------------------
# Statement anchors
# --------------------------------------------------------------------------------------
class Kind:
    """The statement titles we look for. Plain string constants keep the CSV readable."""

    ASSETS_LIAB = "assets_liabilities"
    OPERATIONS = "operations"
    CHANGES = "changes_in_net_assets"
    CASH_FLOWS = "cash_flows"
    HIGHLIGHTS = "financial_highlights"
    NOTES = "notes"
    SOI = "schedule_of_investments"


# The canonical order the statements appear in, split by whether we insist on finding it.
#
# REQUIRED is the fingerprint of a real statements block: "Statement of Operations"
# followed by "Statement(s) of Changes in Net Assets". Their presence IN ORDER, with a
# plausible gap between them, is what separates the statements from a contents-page row
# or a prose cross-reference.
#
# Why not anchor on the balance sheet, which is the statement we most want? Because its
# title is the LEAST standardised of the three — filers write "Statement of Assets and
# Liabilities", "...Assets, Liabilities and Net Assets", "Statement of Net Assets",
# "Statement of Financial Condition" — and anchoring on it means one unrecognised variant
# silently loses the entire filing. "Operations" and "Changes in Net Assets" are close to
# universal, so we anchor on those and search BACKWARD for the balance sheet, flagging the
# filing when it isn't found. That is the flag-and-keep principle the BDC pipeline uses:
# degrade to a partial, marked result rather than dropping data on the floor.
REQUIRED_SEQUENCE: tuple[str, ...] = (
    Kind.OPERATIONS,
    Kind.CHANGES,
)
# OPTIONAL extends the block forward when present. Many unlevered funds file no cash-flow
# statement at all, and a few print Financial Highlights after the notes.
OPTIONAL_SEQUENCE: tuple[str, ...] = (
    Kind.CASH_FLOWS,
    Kind.HIGHLIGHTS,
)
SEQUENCE: tuple[str, ...] = REQUIRED_SEQUENCE + OPTIONAL_SEQUENCE


def _title_pattern(*alternatives: str) -> re.Pattern[str]:
    """Compile title alternatives, letting any run of whitespace stand in for a space.

    Written with single spaces for readability; every space becomes `\\s+` so the
    pattern still matches after tags between words have been blanked into whitespace.
    """
    body = "|".join(alt.replace(" ", r"\s+") for alt in alternatives)
    return re.compile(f"(?:{body})", re.IGNORECASE)


# Owner nouns in statement titles come in every combination of plural and possessive, with
# either an ASCII or a curly apostrophe depending on the filer's word processor:
# "Shareholders Capital", "Shareholder's Equity", "Members' Capital". A trailing character
# class covers them all without enumerating the variants.
_OWNERS = r"(?:(?:share|stock)holder[s'’]*|member[s'’]*|partner[s'’]*)"
# The balance-sheet-equity noun. Corporations say "equity", LLCs and partnerships "capital";
# funds-of-hedge-funds (a large slice of the tender-offer universe) are usually LLCs.
_EQUITY = r"(?:equity|capital)"

ANCHOR_PATTERNS: dict[str, re.Pattern[str]] = {
    Kind.ASSETS_LIAB: _title_pattern(
        # The comma form covers "Statement of Assets, Liabilities and Net Assets"; the
        # optional "and" covers the ordinary "Statement of Assets and Liabilities". Both
        # match as a PREFIX, so any trailing words ("and Members' Capital") are fine.
        r"(?:consolidated )?statements? of assets(?:,)? (?:and )?liabilities",
        r"(?:consolidated )?statements? of assets (?:&|and/or) liabilities",
        # A minority of funds title the balance sheet this way. "of net assets" must
        # follow "of" directly, so "Statement of CHANGES in Net Assets" cannot match.
        r"(?:consolidated )?statements? of net assets",
        r"(?:consolidated )?statements? of financial condition",
        r"(?:consolidated )?statements? of assets and net assets",
    ),
    Kind.OPERATIONS: _title_pattern(r"(?:consolidated )?statements? of operations"),
    Kind.CHANGES: _title_pattern(
        r"(?:consolidated )?statements? of changes in net assets",
        rf"(?:consolidated )?statements? of changes in {_OWNERS} {_EQUITY}",
        # A few filers drop the "changes in" and title it by the owner noun alone.
        rf"(?:consolidated )?statements? of {_OWNERS} {_EQUITY}",
    ),
    Kind.CASH_FLOWS: _title_pattern(r"(?:consolidated )?statements? of cash flows"),
    Kind.HIGHLIGHTS: _title_pattern(r"(?:consolidated )?financial highlights"),
    Kind.NOTES: _title_pattern(
        r"notes to (?:the )?(?:consolidated )?financial statements",
        r"notes to (?:the )?financial statements",
    ),
    Kind.SOI: _title_pattern(
        r"(?:consolidated )?schedules? of investments",
        r"(?:consolidated )?portfolios? of investments",
        r"(?:consolidated )?schedules? of portfolio investments",
    ),
}

# Phrases that mark a title as a cross-reference or page footer rather than a heading,
# e.g. "See Notes to Financial Statements" printed under every statement page.
_REFERENCE_CUES = (
    "see ",
    "seethe",
    "accompany",
    "refer to",
    "integral part",
    "part of these",
    "part of the",
    "included in",
    "described in",
    "presented in",
    "reported in",
    "shown in",
    "disclosed in",
    "note to",
    "notes to",
)
_LOOKBEHIND_CHARS = 48


@dataclass(frozen=True)
class Anchor:
    """One statement-title hit, positioned in the raw HTML."""

    kind: str
    start: int
    end: int
    is_reference: bool  # True => looks like a cross-reference/footer, not a heading


def _looks_like_reference(flat: str, start: int) -> bool:
    """True if the text just before a title suggests prose, not a heading."""
    window = flat[max(0, start - _LOOKBEHIND_CHARS) : start]
    # Collapse the blanked-out tags so "see<b> notes" reads as "see notes".
    collapsed = " ".join(window.split()).lower()
    if not collapsed:
        return False
    return any(cue in collapsed for cue in _REFERENCE_CUES)


def _scan_chunk(flat: str, offset: int) -> list[Anchor]:
    found: list[Anchor] = []
    for kind, pattern in ANCHOR_PATTERNS.items():
        for m in pattern.finditer(flat):
            found.append(
                Anchor(
                    kind=kind,
                    start=offset + m.start(),
                    end=offset + m.end(),
                    is_reference=_looks_like_reference(flat, m.start()),
                )
            )
    return found


def find_anchors(raw: str) -> list[Anchor]:
    """All statement-title hits in a filing, in document order.

    Large files are scanned in overlapping chunks so memory stays bounded; positions are
    de-duplicated because the overlap means a title can be seen twice.
    """
    if len(raw) <= LARGE_FILE_CHARS:
        anchors = _scan_chunk(flatten_preserving_offsets(raw), 0)
    else:
        anchors = []
        seen: set[tuple[str, int]] = set()
        pos = 0
        while pos < len(raw):
            chunk = raw[pos : pos + CHUNK_CHARS + CHUNK_OVERLAP]
            for a in _scan_chunk(flatten_preserving_offsets(chunk), pos):
                key = (a.kind, a.start)
                if key not in seen:
                    seen.add(key)
                    anchors.append(a)
            pos += CHUNK_CHARS
    anchors.sort(key=lambda a: (a.start, a.kind))
    return anchors


# --------------------------------------------------------------------------------------
# Block selection
# --------------------------------------------------------------------------------------
# Minimum raw-HTML distance between two consecutive statement titles for the pair to be
# believable. Because tags are blanked rather than removed, these distances are raw-markup
# distances: one real statement table is easily 10-30 KB of markup, whereas table-of-
# contents rows sit a few hundred characters apart. That gap is the TOC discriminator.
#
# Note this is checked against the very NEXT occurrence of the next title, and failing it
# REJECTS the candidate outright. Searching forward for some later occurrence that happens
# to satisfy the gap would defeat the purpose — a table-of-contents entry would simply pair
# itself with the real statement hundreds of KB below and look perfectly valid.
MIN_ANCHOR_GAP = 1_500
# Upper bound on the distance to the next statement. Beyond this the statement is treated
# as absent rather than as part of this block.
#
# Deliberately generous, because an absolute character count does not scale across a corpus
# spanning 5 KB to 150 MB: a tight cap silently drops big single-fund filings whose
# statements are simply far apart in markup terms. The work of deciding "is the next title
# part of THIS block" is done structurally by soi_between() instead — a holdings schedule in
# between is the real signal that we have left this block — and this cap is only a backstop
# against runaway blocks.
MAX_ANCHOR_GAP = 400_000
# Optional statements get a tighter bound than required ones. A cash-flow statement or
# Financial Highlights belonging to this run sits right after its siblings; one that is far
# away is the "highlights printed after the notes" layout, which is picked up as its own
# span further down rather than by stretching this block across everything in between.
MAX_OPTIONAL_ANCHOR_GAP = 120_000
# Some filers print Financial Highlights AFTER the notes rather than with the statements.
# Rather than lose it (it carries the expense ratio, total return and per-share operating
# performance), we pick it up as a separate span this far past the end of the main block.
HIGHLIGHTS_LOOKAHEAD = 400_000
HIGHLIGHTS_WINDOW = 60_000
# No span may contain a Schedule of Investments heading: the statements run is contiguous
# and the holdings list sits outside it. This is enforced by TRIMMING spans at the next
# holdings heading, never by discarding a block.
#
# Note this is a rule about one SPAN, not about the filing as a whole. A minority of filers
# print the balance sheet BEFORE the Schedule of Investments (Alpha Core Strategies: A&L on
# page 3, holdings on page 5, Operations on page 6), which is why the balance sheet can
# become a span of its own rather than being glued to the statements run.
#
# How much to take when the balance sheet is captured as its own span:
BALANCE_SHEET_WINDOW = 60_000
# Used when no "Notes to Financial Statements" heading follows the last statement.
DEFAULT_TAIL_CHARS = 80_000
# A notes heading further than this past the last statement is not this block's end marker
# (multi-series filings interleave supplemental sections), so the tail default is used.
MAX_NOTES_DISTANCE = 150_000
# Hard ceiling on one block, so a pathological document can't produce a giant slice.
MAX_BLOCK_CHARS = 1_500_000
# A little context before the first title, which sometimes carries "(in thousands)".
LEAD_IN_CHARS = 200


@dataclass
class Block:
    """A located financial-statements region, as offsets into the raw HTML.

    `spans` holds every slice belonging to this block. Usually that is one contiguous
    region, but when a filer prints Financial Highlights after the notes it becomes two:
    the statements, plus the highlights picked up separately. `start`/`end` describe the
    main span, which is what the diagnostics report.
    """

    start: int
    end: int
    kinds: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    spans: list[tuple[int, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.spans:
            self.spans = [(self.start, self.end)]

    @property
    def chars(self) -> int:
        return sum(e - s for s, e in self.spans)


def _next_heading_gap(anchors: list[Anchor], target: Anchor) -> int:
    """Distance from `target` to the next heading of a DIFFERENT kind.

    Used to tell a real statement from a contents-page row. On a contents page every title
    is a few hundred characters from the next; a real statement is followed by its own
    table, so the next title is tens of thousands of characters away.
    """
    for a in anchors:
        if a.start >= target.end and a.kind != target.kind and not a.is_reference:
            return a.start - target.end
    return 1 << 30  # nothing follows: treat as far away


def select_blocks(anchors: list[Anchor], doc_len: int) -> list[Block]:
    """Choose the financial-statement region(s) from a filing's anchors.

    Returns one block per series: single-fund filings yield exactly one, while
    multi-series trusts (Stone Ridge, AIP) bundle many funds and yield several. The
    per-series attribution itself is M2's job (ncsr_series.py); here we only need to
    find and count the regions.
    """
    by_kind: dict[str, list[Anchor]] = {k: [] for k in ANCHOR_PATTERNS}
    for a in anchors:
        by_kind[a.kind].append(a)

    def first_after(kind: str, pos: int, *, headings_only: bool = False) -> Anchor | None:
        for a in by_kind[kind]:
            if a.start >= pos and not (headings_only and a.is_reference):
                return a
        return None

    # Non-reference Schedule of Investments headings, sorted, for the crossing test below.
    soi_starts = sorted(a.start for a in by_kind[Kind.SOI] if not a.is_reference)

    def soi_between(lo: int, hi: int) -> int:
        """How many holdings-schedule headings sit between two positions.

        This is the structural backbone of block selection. The statements form one
        contiguous run; the Schedule of Investments sits outside it. So any candidate
        extension that would jump over a holdings schedule is reaching into a different
        part of the document (a later series, or an investee fund's statements) and must
        not be joined to this block.
        """
        return bisect.bisect_left(soi_starts, hi) - bisect.bisect_right(soi_starts, lo)

    def last_before(kind: str, pos: int, floor: int) -> Anchor | None:
        """Nearest heading of `kind` before `pos` but not earlier than `floor`."""
        best: Anchor | None = None
        for a in by_kind[kind]:
            if a.is_reference or a.start >= pos or a.start < floor:
                continue
            best = a  # anchors are sorted, so the last survivor is the nearest
        return best

    blocks: list[Block] = []
    consumed_to = -1

    for ops in by_kind[Kind.OPERATIONS]:
        # Skip titles inside a block we already claimed (page headers, "(continued)"
        # repeats, and in-block cross-references all land here).
        if ops.start < consumed_to or ops.is_reference:
            continue

        # Walk the canonical sequence forward from this candidate.
        kinds_found = [Kind.OPERATIONS]
        cursor = ops
        rejected = False
        for kind in SEQUENCE[1:]:
            nxt = first_after(kind, cursor.end)
            if nxt is None:
                if kind in REQUIRED_SEQUENCE:
                    rejected = True
                    break
                continue
            gap = nxt.start - cursor.end
            if gap < MIN_ANCHOR_GAP:
                # Titles this close together are contents-page rows, not statements.
                rejected = True
                break
            required = kind in REQUIRED_SEQUENCE
            limit = MAX_ANCHOR_GAP if required else MAX_OPTIONAL_ANCHOR_GAP
            if gap > limit or soi_between(cursor.end, nxt.start):
                if required:
                    rejected = True
                    break
                continue  # this optional statement simply isn't part of this block
            kinds_found.append(kind)
            cursor = nxt
        if rejected:
            continue

        flags: list[str] = []

        # Extend backward to the balance sheet, which precedes the income statement.
        bs = last_before(
            Kind.ASSETS_LIAB, ops.start, max(consumed_to, ops.start - MAX_ANCHOR_GAP)
        )
        start = max(0, ops.start - LEAD_IN_CHARS)
        bs_span: tuple[int, int] | None = None
        if bs is None:
            # No recognised balance-sheet title. Keep the block (income statement, changes
            # and highlights are still worth extracting) but flag it loudly: C1, the
            # balance-sheet identity check, will have nothing to verify.
            flags.append("preprocess:no_balance_sheet_title")
        elif not soi_between(bs.start, ops.start):
            # Ordinary case: balance sheet sits directly before the income statement. No
            # contents-page check is needed here — sitting immediately before an already
            # validated Operations heading is itself the proof that this title is real.
            kinds_found.insert(0, Kind.ASSETS_LIAB)
            start = max(0, bs.start - LEAD_IN_CHARS)
        elif _next_heading_gap(anchors, bs) < MIN_ANCHOR_GAP:
            # Far from the statements AND immediately followed by another title: this is a
            # contents-page row, not the statement. Fall back to no balance sheet.
            flags.append("preprocess:no_balance_sheet_title")
        else:
            # This filer printed the balance sheet before the holdings schedule. Take it as
            # a separate span so the holdings list in between is skipped, not swallowed.
            next_soi_idx = bisect.bisect_right(soi_starts, bs.start)
            bs_end = min(bs.start + BALANCE_SHEET_WINDOW, doc_len)
            if next_soi_idx < len(soi_starts):
                bs_end = min(bs_end, soi_starts[next_soi_idx])
            bs_span = (max(0, bs.start - LEAD_IN_CHARS), bs_end)
            kinds_found.insert(0, Kind.ASSETS_LIAB)
            flags.append("preprocess:balance_sheet_before_holdings")

        # End at the real "Notes to Financial Statements" heading. Skipping reference-like
        # hits is essential: "See Notes to Financial Statements" is printed as a footer
        # under every statement page and would otherwise truncate the block immediately.
        notes = first_after(Kind.NOTES, cursor.end, headings_only=True)
        if (
            notes is not None
            and (notes.start - cursor.end) <= MAX_NOTES_DISTANCE
            and not soi_between(cursor.end, notes.start)
        ):
            end = notes.start
        else:
            # Multi-series filings interleave supplemental sections, so a far-off notes
            # heading belongs to a later series, not to this block.
            end = min(cursor.end + DEFAULT_TAIL_CHARS, doc_len)
            flags.append("preprocess:no_notes_marker")
        end = min(end, start + MAX_BLOCK_CHARS, doc_len)

        # Trim, rather than discard, at the next holdings schedule. The statements run
        # cannot continue past one, so if the end marker (or the no-notes tail default)
        # overshot into a Schedule of Investments, the right answer is a shorter block —
        # NOT throwing away a perfectly good statements run, which is what an outright
        # rejection here would do.
        next_soi_idx = bisect.bisect_right(soi_starts, start)
        if next_soi_idx < len(soi_starts):
            end = min(end, soi_starts[next_soi_idx])

        # Whatever survives must still contain the income statement we anchored on.
        if end <= ops.end:
            continue

        spans = [(start, end)]
        if bs_span is not None:
            spans.insert(0, bs_span)

        # Recover Financial Highlights when the filer printed it after the notes. Skipping
        # over the notes is fine here (they are prose, and we know where the table is);
        # skipping over a holdings schedule is not, for the reason given in soi_between.
        if Kind.HIGHLIGHTS not in kinds_found:
            hl = first_after(Kind.HIGHLIGHTS, end, headings_only=True)
            if (
                hl is not None
                and (hl.start - end) <= HIGHLIGHTS_LOOKAHEAD
                and not soi_between(end, hl.start)
            ):
                hl_end = min(hl.start + HIGHLIGHTS_WINDOW, doc_len)
                next_soi = bisect.bisect_right(soi_starts, hl.start)
                if next_soi < len(soi_starts):
                    hl_end = min(hl_end, soi_starts[next_soi])
                spans.append((max(0, hl.start - LEAD_IN_CHARS), hl_end))
                kinds_found.append(Kind.HIGHLIGHTS)
                flags.append("preprocess:highlights_after_notes")
                consumed_to = hl_end

        blocks.append(
            Block(start=start, end=end, kinds=kinds_found, flags=flags, spans=spans)
        )
        consumed_to = max(consumed_to, end)

    return blocks


# --------------------------------------------------------------------------------------
# Table serialization
# --------------------------------------------------------------------------------------
# <sup> holds footnote markers ("(a)", "1") that glue themselves onto numbers and corrupt
# them; the others carry no financial content.
_DROP_TAGS = {"sup", "script", "style", "noscript"}
# Cells holding only a symbol: SEC filers routinely put "$" in its own column, and split
# negative numbers so that "(" and ")" occupy separate cells from the digits.
_PREFIX_SYMBOLS = {"$", "(", "$("}
_SUFFIX_SYMBOLS = {")", "%", ")%", "%)"}
_MAX_NARRATIVE_CHARS = 600
# Above this share of replacement characters, treat the document as cp1252 rather than
# UTF-8-with-damage. See read_filing_text.
UTF8_ERROR_TOLERANCE = 1e-4

# Invisible characters that survive tag-stripping and waste tokens (or split a number in
# two). Zero-width space is especially common in EDGAR HTML generated from PDFs.
_INVISIBLE = str.maketrans({c: None for c in "​‌‍⁠﻿­"})

# Artefacts of EDGAR's PDF-to-HTML conversion. They carry no financial meaning, appear
# thousands of times, and would otherwise be billed as input tokens on every call.
_ARTEFACT_RE = re.compile(
    r"""(?xi)
    \bEnd\s+Page\s+\d+\s+Begin\s+Page\s+\d+\b
  | \bField:\s*/?\s*Page\b
  | \bSequence:\s*\d+\b
  | \bTABLE\s+OF\s+CONTENTS\b
  | \bAnchor\b
    """
)


def _collapse_ws(s: str) -> str:
    """Normalise one run of extracted text: invisible characters, artefacts, whitespace."""
    s = s.translate(_INVISIBLE).replace("\xa0", " ")
    s = _ARTEFACT_RE.sub(" ", s)
    return " ".join(s.split())


def _element_text(el: object) -> str:
    """Text of an element and its descendants, skipping footnote-marker subtrees."""
    parts: list[str] = []

    def visit(node) -> None:  # noqa: ANN001 - lxml element
        if node.tag in _DROP_TAGS:
            if node.tail:
                parts.append(node.tail)
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            visit(child)
        if node.tail:
            parts.append(node.tail)

    if el.text:  # type: ignore[attr-defined]
        parts.append(el.text)  # type: ignore[attr-defined]
    for child in el:  # type: ignore[attr-defined]
        visit(child)
    return _collapse_ws("".join(parts))


def _merge_symbol_cells(cells: list[str]) -> list[str]:
    """Fold symbol-only cells into their neighbours so numbers survive intact.

    "$" | "1,234" -> "$1,234"      and      "(" | "237,536" | ")" -> "(237,536)"

    Parenthetical negatives and thousands separators are kept verbatim: the LLM is told
    that "(x)" means negative, and preserving the printed form is what lets the
    extraction quote a `raw_text` snippet that a human can find in the filing.
    """
    out: list[str] = []
    pending_prefix = ""
    for cell in cells:
        if cell in _PREFIX_SYMBOLS:
            pending_prefix += cell
            continue
        if cell in _SUFFIX_SYMBOLS:
            for i in range(len(out) - 1, -1, -1):
                if out[i]:
                    out[i] += cell
                    break
            continue
        out.append(pending_prefix + cell if cell else cell)
        if cell:
            pending_prefix = ""
    if pending_prefix:
        out.append(pending_prefix)
    return out


def _tidy_row(cells: list[str]) -> list[str]:
    """Drop padding columns while keeping enough structure to align year columns.

    Financial Highlights tables carry one column per fiscal year, so the LLM has to be
    able to tell which number belongs to which year. We therefore collapse runs of empty
    spacer cells to a single empty cell rather than deleting them — header and data rows
    get the same treatment, so their columns still line up.
    """
    while cells and not cells[0]:
        cells.pop(0)
    while cells and not cells[-1]:
        cells.pop()
    out: list[str] = []
    for c in cells:
        if not c and out and not out[-1]:
            continue
        out.append(c)
    return out


def _serialize_table(table: object) -> str:
    rows: list[str] = []
    for tr in table.iter("tr"):  # type: ignore[attr-defined]
        cells = [_element_text(td) for td in tr.xpath("./td|./th")]
        if not cells:
            continue
        cells = _tidy_row(_merge_symbol_cells(cells))
        if not any(cells):
            continue
        line = " | ".join(cells)
        # Rules and borders serialize to runs of punctuation; they carry no information.
        if not re.search(r"[0-9A-Za-z]", line):
            continue
        if rows and rows[-1] == line:  # repeated page header inside one table
            continue
        rows.append(line)
    if not rows:
        return ""
    return "[TABLE]\n" + "\n".join(rows) + "\n[/TABLE]"


def serialize_region(raw_slice: str) -> str:
    """Turn a slice of filing HTML into pipe-delimited tables plus short narrative.

    Narrative between tables is kept (trimmed) because it carries two things the numbers
    are meaningless without: the amounts scale ("(in thousands)") and the period headers
    ("Six Months Ended June 30, 2025").
    """
    # The slice starts a little before the statement title (LEAD_IN_CHARS) and so usually
    # begins in the middle of a tag. Dropping everything up to the first '>' keeps that
    # half-tag's attributes ('YLE="text-align: left"...') out of the serialized text.
    head = raw_slice[:LEAD_IN_CHARS * 2]
    if "<" in head:
        cut = head.find("<")
        if ">" in head[:cut]:
            raw_slice = raw_slice[head.index(">") + 1 :]

    try:
        # A mid-document slice is not well-formed HTML; lxml's parser is deliberately
        # forgiving, and `create_parent` gives the fragments a root to hang from.
        root = lxml_html.fragment_fromstring(raw_slice, create_parent="div")
    except Exception:
        return ""

    parts: list[str] = []
    narrative: list[str] = []

    def flush_narrative() -> None:
        if not narrative:
            return
        text = _collapse_ws(" ".join(narrative))
        narrative.clear()
        if text:
            parts.append(text[:_MAX_NARRATIVE_CHARS])

    def visit(node) -> None:  # noqa: ANN001 - lxml element
        if node.tag == "table":
            flush_narrative()
            serialized = _serialize_table(node)
            if serialized:
                parts.append(serialized)
            if node.tail:
                narrative.append(node.tail)
            return
        if node.tag in _DROP_TAGS:
            if node.tail:
                narrative.append(node.tail)
            return
        if node.text:
            narrative.append(node.text)
        for child in node:
            visit(child)
        if node.tail:
            narrative.append(node.tail)

    visit(root)
    flush_narrative()
    return "\n".join(p for p in parts if p.strip())


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------
# When no statements block can be located we fall back to whole-document text, capped so
# one filing can never blow up a prompt. The filing is flagged for review either way.
FALLBACK_MAX_CHARS = 600_000
# Rough character-per-token ratio for English prose and numeric tables. Only used for
# sizing and cost estimates, never for anything load-bearing.
CHARS_PER_TOKEN = 4


@dataclass
class SectionResult:
    """Everything stage 1 knows about one filing."""

    filing: FilingName
    raw_chars: int
    anchor_kinds: list[str]  # kinds present anywhere (headings only)
    blocks: list[Block]
    text: str
    flags: list[str] = field(default_factory=list)
    has_inline_xbrl: bool = False

    @property
    def est_tokens(self) -> int:
        return len(self.text) // CHARS_PER_TOKEN

    @property
    def block_kinds(self) -> list[str]:
        """Statement kinds inside the chosen block(s) — the meaningful hit-rate measure."""
        seen: list[str] = []
        for b in self.blocks:
            for k in b.kinds:
                if k not in seen:
                    seen.append(k)
        return seen

    @property
    def located(self) -> bool:
        return bool(self.blocks)


def read_filing_text(path: Path) -> str:
    """Read a filing, coping with the several encodings SEC filers emit.

    The subtlety worth knowing: a naive "try UTF-8, else cp1252" mishandles a UTF-8 file
    that contains a handful of corrupt bytes. cp1252 can decode almost any byte, so the
    fallback SUCCEEDS and silently rewrites every multi-byte character in the document —
    an em-dash (which means "nil" in a fund statement) becomes three junk characters.

    So when strict UTF-8 fails we measure HOW BADLY it fails. A genuine cp1252 document
    yields one replacement character per non-ASCII byte; a UTF-8 document with a few bad
    bytes yields a handful in a megabyte.
    """
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    lossy = data.decode("utf-8", errors="replace")
    bad_ratio = lossy.count("�") / max(1, len(lossy))
    if bad_ratio < UTF8_ERROR_TOLERANCE:
        return lossy
    return data.decode("cp1252", errors="replace")


def extract_sections(
    path: Path,
    *,
    serialize: bool = True,
    fallback: bool = True,
    notes_chars: int = 0,
) -> SectionResult:
    """Locate and serialize the financial statements in one N-CSR/N-CSRS filing.

    Args:
        serialize: set False for a fast inventory pass that only needs anchor positions.
        fallback: when no block is found, serialize capped whole-document text instead.
        notes_chars: extend each block this far into the notes. The fair-value hierarchy
            and fee notes live there; M1 does not need them, M3 may.
    """
    filing = parse_filing_name(path)
    if filing is None:
        raise ValueError(f"filename does not match the expected pattern: {path.name}")

    raw = read_filing_text(path)
    anchors = find_anchors(raw)
    blocks = select_blocks(anchors, len(raw))

    if notes_chars:
        for b in blocks:
            b.end = min(b.end + notes_chars, len(raw))

    flags: list[str] = []
    heading_kinds = sorted({a.kind for a in anchors if not a.is_reference})

    for b in blocks:
        for fl in b.flags:
            if fl not in flags:
                flags.append(fl)

    text = ""
    if blocks:
        if serialize:
            text = "\n".join(
                serialize_region(raw[s:e]) for b in blocks for s, e in b.spans
            )
            if not text.strip():
                flags.append("preprocess:empty_serialization")
        if len(blocks) > 1:
            # Deliberately "multi_block", not "multi_series": several statement blocks can
            # mean a multi-series trust OR a master fund's statements attached as
            # supplemental information. Telling those apart is M2's job (ncsr_series.py).
            flags.append(f"preprocess:multi_block_{len(blocks)}")
    else:
        flags.append("preprocess:no_anchors")
        if serialize and fallback:
            flat = flatten_preserving_offsets(raw[:FALLBACK_MAX_CHARS])
            text = _collapse_ws(flat)
            flags.append("preprocess:fallback_wholedoc")
            if len(raw) > FALLBACK_MAX_CHARS:
                flags.append("preprocess:truncated")

    return SectionResult(
        filing=filing,
        raw_chars=len(raw),
        anchor_kinds=heading_kinds,
        blocks=blocks,
        text=text,
        flags=flags,
        # Inline XBRL is occasionally present for a few cover-page/CEF concepts. Those
        # tags become independent anchors to cross-check LLM output against (rule X1).
        has_inline_xbrl=("ix:nonFraction" in raw or "cef:" in raw),
    )


# --------------------------------------------------------------------------------------
# CLI diagnostic
# --------------------------------------------------------------------------------------
def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Diagnose section location on one filing.")
    ap.add_argument("path", type=Path)
    ap.add_argument("--dump", type=Path, help="write the serialized block to this file")
    ap.add_argument("--anchors", action="store_true", help="list every anchor hit")
    ap.add_argument("--head", type=int, default=40, help="preview N lines (default 40)")
    args = ap.parse_args(argv)

    res = extract_sections(args.path)
    f = res.filing
    print(f"{f.fund_name}  |  CIK {f.cik}  |  {f.form}  |  {f.filing_date}")
    print(f"raw chars      : {res.raw_chars:,}")
    print(f"inline XBRL    : {res.has_inline_xbrl}")
    print(f"heading kinds  : {', '.join(res.anchor_kinds) or '(none)'}")
    print(f"blocks         : {len(res.blocks)}")
    for i, b in enumerate(res.blocks, 1):
        pct = 100.0 * b.start / max(1, res.raw_chars)
        spans = "  ".join(f"{s:,}-{e:,}" for s, e in b.spans)
        print(
            f"  block {i}: {b.chars:,} chars, starts at {pct:.1f}%  spans: {spans}"
            f"  kinds: {', '.join(b.kinds)}"
        )
    print(f"serialized     : {len(res.text):,} chars  (~{res.est_tokens:,} tokens)")
    print(f"flags          : {', '.join(res.flags) or '(none)'}")

    if args.anchors:
        print("\n--- anchors ---")
        raw = read_filing_text(args.path)
        for a in find_anchors(raw):
            pct = 100.0 * a.start / max(1, len(raw))
            tag = "ref " if a.is_reference else "HEAD"
            print(f"  {pct:6.2f}%  {tag}  {a.kind}")

    if args.dump:
        args.dump.parent.mkdir(parents=True, exist_ok=True)
        args.dump.write_text(res.text, encoding="utf-8")
        print(f"\nwrote {args.dump}")
    elif res.text:
        print("\n--- preview ---")
        for line in res.text.splitlines()[: args.head]:
            print(line[:200])
    return 0


if __name__ == "__main__":
    sys.exit(_main())
