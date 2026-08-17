"""
ncsr_anchors.py — free, local cross-check anchors from a filing's inline XBRL (rule X1).

WHAT THIS IS FOR. Session 10 established that these funds do NOT tag their financial
statements in XBRL, which is why the whole N-CSR path is LLM-over-text. But ~10.7% of
the corpus (330 of 3,084 filings) still carries a thin band of inline XBRL for
cover-page and N-2 prospectus concepts. Those tags cost nothing to read and were
written by the FILER, not by us and not by a model — so where one exists it is an
independent check on what the model said. Nothing here calls an API.

WHAT WE ACTUALLY GET (measured on real filings, not assumed):

  dei:DocumentPeriodEndDate      the period end, e.g. "September 30, 2025"
  dei:EntityRegistrantName       the fund name, e.g. "ACAP Strategic Fund"
  dei:EntityCentralIndexKey      the CIK
  us-gaap:NetAssetValuePerShare  NAV per share, per share class, per date
  cef:LongTermDebtPrincipal      borrowings
  cef:OutstandingSecurityHeldShares  shares outstanding, per class, per period
  cef:ManagementFeesPercent / IncentiveFeesPercent / SalesLoadPercent

The first three are the valuable ones and they were not in the original plan. The
single likeliest way this pipeline produces a wrong row is the model reading the
PRIOR-year column of a side-by-side statement; `dei:DocumentPeriodEndDate` catches
exactly that, from the filer's own markup, for free.

TWO THINGS THAT WILL BITE A READER WHO SKIMS THIS:

1. FACTS FOR SEVERAL PERIODS COEXIST. A filing tags the current year AND two or three
   prior years (ACAP carries share counts for FY2023, FY2024 and FY2025 side by side).
   An anchor is therefore useless until you know which period it belongs to — so every
   fact here is resolved through its `contextRef` to a start/end date, and callers must
   filter on the period. Comparing an untagged-period anchor to a model value would
   manufacture false mismatches, which is worse than having no anchor at all.

2. `scale` AND `sign` ARE NOT DECORATION. XBRL's `scale="-2"` means the printed 1.50 is
   really 0.015, and `sign="-"` means negate the printed figure. Reading the displayed
   text and ignoring these gives a number that is wrong by 100x with no outward sign of
   trouble. Both are applied here.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# ── Markup patterns ────────────────────────────────────────────────────────────────
# These filings are SGML-ish HTML, not well-formed XML, so lxml is not reliably able to
# namespace-resolve them; the surrounding modules already parse these documents with
# regex over raw text for the same reason. Case-insensitive because filers are
# inconsistent about element casing.
_CONTEXT_RE = re.compile(
    r"<xbrli:context\b[^>]*\bid\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</xbrli:context\s*>",
    re.DOTALL | re.IGNORECASE,
)
_START_RE = re.compile(r"<xbrli:startDate\s*>\s*([\d]{4}-[\d]{2}-[\d]{2})", re.IGNORECASE)
_END_RE = re.compile(
    r"<xbrli:(?:endDate|instant)\s*>\s*([\d]{4}-[\d]{2}-[\d]{2})", re.IGNORECASE
)
_MEMBER_RE = re.compile(r"<xbrldi:explicitMember\b[^>]*>\s*([^<]+)", re.IGNORECASE)

_FACT_RE = re.compile(
    r"<ix:(nonFraction|nonNumeric)\b([^>]*)>(.*?)</ix:\1\s*>", re.DOTALL | re.IGNORECASE
)
_ATTR_RE = re.compile(r"(\w[\w:.-]*)\s*=\s*[\"']([^\"']*)[\"']")
_TAG_RE = re.compile(r"<[^>]*>")


@dataclass(frozen=True)
class Anchor:
    """One inline-XBRL fact, with its period and dimension resolved."""

    concept: str                 # e.g. "us-gaap:NetAssetValuePerShare"
    value: float | None          # numeric facts only, with scale and sign applied
    text: str | None             # the displayed text (all facts)
    period_start: date | None
    period_end: date | None      # the instant, or the end of the duration
    member: str | None           # raw dimension member, e.g. "XCAPX:ClassAMember"

    @property
    def class_label(self) -> str | None:
        """A readable share-class hint from the dimension member, or None.

        'XCAPX:ClassAMember' -> 'Class A'. This is a HINT for matching against what the
        model read off the page, never an authority: filers name members freely and a
        mismatch here means "could not match", not "the model was wrong".
        """
        if not self.member:
            return None
        raw = self.member.split(":", 1)[-1]
        if raw.endswith("Member"):
            raw = raw[: -len("Member")]
        # Split CamelCase into words: "ClassA" -> "Class A", "Institutional" -> unchanged.
        spaced = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", raw)
        return spaced.strip() or None


def _parse_iso(s: str) -> date | None:
    try:
        y, m, d = (int(p) for p in s.split("-"))
        return date(y, m, d)
    except (ValueError, TypeError):
        return None


def _to_number(text: str, attrs: dict[str, str]) -> float | None:
    """Displayed text -> the real numeric value, applying XBRL `scale` and `sign`."""
    cleaned = text.replace(",", "").replace("$", "").replace("%", "").strip()
    # Some filers print negatives in parentheses even inside a tagged fact.
    negate_parens = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()").strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    scale = attrs.get("scale")
    if scale not in (None, ""):
        try:
            value *= 10 ** int(scale)
        except ValueError:
            pass
    if attrs.get("sign") == "-" or negate_parens:
        value = -value
    return value


def parse_anchors(raw: str) -> "AnchorSet":
    """Read every resolvable inline-XBRL fact out of one filing's raw HTML."""
    contexts: dict[str, tuple[date | None, date | None, str | None]] = {}
    for cid, body in _CONTEXT_RE.findall(raw):
        s = _START_RE.search(body)
        e = _END_RE.search(body)
        m = _MEMBER_RE.search(body)
        contexts[cid] = (
            _parse_iso(s.group(1)) if s else None,
            _parse_iso(e.group(1)) if e else None,
            m.group(1).strip() if m else None,
        )

    anchors: list[Anchor] = []
    for kind, attr_text, inner in _FACT_RE.findall(raw):
        attrs = {k: v for k, v in _ATTR_RE.findall(attr_text)}
        concept = attrs.get("name")
        if not concept:
            continue
        # xs:nil facts are placeholders the filer tagged but left empty — very common in
        # these documents (ACAP has ten nil AnnualDividendPayment tags). They carry no
        # information; treating a nil as 0 would be a fabricated anchor.
        if attrs.get("xs:nil", "").lower() == "true":
            continue
        text = html.unescape(_TAG_RE.sub("", inner))
        text = " ".join(text.split())
        if not text:
            continue
        start, end, member = contexts.get(attrs.get("contextRef", ""), (None, None, None))
        anchors.append(
            Anchor(
                concept=concept,
                value=_to_number(text, attrs) if kind.lower() == "nonfraction" else None,
                text=text,
                period_start=start,
                period_end=end,
                member=member,
            )
        )
    return AnchorSet(anchors)


class AnchorSet:
    """The anchors from one filing, with the lookups the mapper actually needs."""

    def __init__(self, anchors: list[Anchor]) -> None:
        self.anchors = anchors

    def __len__(self) -> int:
        return len(self.anchors)

    def __bool__(self) -> bool:
        return bool(self.anchors)

    def _first_text(self, concept: str) -> str | None:
        for a in self.anchors:
            if a.concept.lower() == concept.lower() and a.text:
                return a.text
        return None

    # ── Identity anchors (the valuable ones) ──────────────────────────────────────

    @property
    def document_period_end(self) -> str | None:
        """The filer's own statement of the period end, e.g. 'September 30, 2025'.

        Left as the printed string rather than parsed to a date: it is compared against
        `period_end_as_printed`, which is also a printed string, and date-parsing every
        filer's format is a source of failure we do not need to take on here.
        """
        return self._first_text("dei:DocumentPeriodEndDate")

    @property
    def registrant_name(self) -> str | None:
        return self._first_text("dei:EntityRegistrantName")

    @property
    def cik(self) -> str | None:
        raw = self._first_text("dei:EntityCentralIndexKey")
        return raw.zfill(10) if raw and raw.isdigit() else raw

    @property
    def document_type(self) -> str | None:
        return self._first_text("dei:DocumentType")

    # ── Numeric anchors, always period-scoped ─────────────────────────────────────

    def numeric(self, concept: str, period_end: date | None = None) -> list[Anchor]:
        """Numeric facts for a concept, optionally restricted to one period end.

        ALWAYS pass `period_end` when comparing against extracted data. Filings tag
        several years at once, so an unfiltered lookup mixes the current year with
        prior years and will report mismatches that are really period confusion.
        """
        out = [
            a for a in self.anchors
            if a.concept.lower() == concept.lower() and a.value is not None
        ]
        if period_end is not None:
            out = [a for a in out if a.period_end == period_end]
        return out

    def nav_by_class(self, period_end: date | None = None) -> dict[str, float]:
        """NAV per share keyed by share-class hint (or 'single' when undimensioned)."""
        out: dict[str, float] = {}
        for a in self.numeric("us-gaap:NetAssetValuePerShare", period_end):
            key = a.class_label or "single"
            out.setdefault(key, a.value)  # type: ignore[arg-type]
        return out

    def concepts(self) -> dict[str, int]:
        """Concept -> count. For diagnostics and for scoping future anchor rules."""
        counts: dict[str, int] = {}
        for a in self.anchors:
            counts[a.concept] = counts.get(a.concept, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def anchors_for_file(path: Path) -> AnchorSet:
    """Convenience wrapper: read a filing off disk and parse its anchors."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ncsr_sections import read_filing_text  # noqa: PLC0415 - shares the encoding logic

    return parse_anchors(read_filing_text(path))


if __name__ == "__main__":
    # Self-test / diagnostic. Free: local files only, no API key, no network.
    #   uv run python src/extraction/ncsr_anchors.py <filing.htm>
    import argparse

    ap = argparse.ArgumentParser(description="Dump a filing's inline-XBRL anchors.")
    ap.add_argument("path", type=Path)
    args = ap.parse_args()

    aset = anchors_for_file(args.path)
    print(f"{args.path.name}\n  anchors: {len(aset)}")
    print(f"  period end (dei): {aset.document_period_end!r}")
    print(f"  registrant      : {aset.registrant_name!r}")
    print(f"  cik / form      : {aset.cik!r} / {aset.document_type!r}")
    print(f"  NAV by class    : {aset.nav_by_class()}")
    print("  concepts:")
    for concept, n in aset.concepts().items():
        print(f"    {n:4d}  {concept}")
