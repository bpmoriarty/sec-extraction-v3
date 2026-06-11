"""
holdings_compare.py — Cross-BDC holdings & mark comparison (docs/HOLDINGS_COMPARISON_PLAN.md).

Independent of the extractor. Reads the per-filing schedule-of-investments CSVs in data/holdings/
(one row per holding) and works toward matching the SAME underlying credit across BDCs so we can
compare each holder's mark (fair value as a % of par).

This file currently implements PHASE 1 (consolidate + clean + parse) and a light issuer
normalization + diagnostic, so we can measure parse quality and confirm the named anchors
(Anaplan, Flexera, ...) surface before building issuer clustering / issue matching / mark
comparison (phases 2-5).

Run the diagnostic:
    uv run python src/analysis/holdings_compare.py --diagnose
Write the cleaned consolidated table to data/dataset/:
    uv run python src/analysis/holdings_compare.py --build

Design notes (why the parsing is shaped this way):
  - The raw `issuer` field is the XBRL InvestmentIdentifierAxis member verbatim. Its format varies
    by FILER, not by row: comma-delimited ("Name, Sector, Instrument"), em-dash ("Name - Debt"),
    or a denormalized category PATH with the real issuer buried at the end ("Non-Control...
    Application Software Airship Group"). No single split works; we detect the format per row.
  - We DEGRADE HONESTLY: a row we can't confidently parse gets parse_ok=False and is reported in
    the coverage stats rather than force-matched. The plan's promise is best-effort discovery with
    confidence, not exhaustive reconciliation.
"""

from __future__ import annotations

import argparse
import glob
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOLDINGS_DIR = PROJECT_ROOT / "data" / "holdings"
OUT_DIR = PROJECT_ROOT / "data" / "dataset"

# ---------------------------------------------------------------------------
# Cleaning vocab
# ---------------------------------------------------------------------------

# Category / hierarchy / sector phrases that denormalized filers prepend to the issuer
# ("Investments United States Debt Investments Health Care Equipment & Services ACI Group ...").
# Stripped from the FRONT token-by-token, repeatedly, stopping at the first non-boilerplate token.
# This is a generic vocabulary (affiliation buckets + geographies + GICS sectors/industries + asset
# classes), NOT per-filer special-casing. '&' is normalized to 'and' for matching.
_CATEGORY_PHRASES = [
    # affiliation / control buckets
    "non-controlled/non-affiliated investments", "non-control/non-affiliate investments",
    "non-controlled non-affiliated investments", "non-controlled/non-affiliated",
    "non-control/non-affiliate", "controlled affiliate investments",
    "non-controlled affiliate investments", "controlled investments", "control investments",
    "affiliate investments", "affiliated investments", "affiliate", "affiliated",
    "non-controlled", "non-affiliated",
    # SOI section headers
    "portfolio company debt securities", "portfolio company warrant investments",
    "portfolio company equity investments", "debt securities portfolio", "debt securities",
    "debt investments", "equity investments", "warrant investments",
    "us corporate debt", "u s corporate debt", "corporate debt", "us debt", "u s debt",
    "issuer name", "issuer", "investments", "investment",
    # sector phrases seen as headers on specific denormalized filers
    "drug discovery and development", "drug discovery", "cannabis",
    "transportation services", "utilities services", "business services",
    "consumer products and services", "consumer products and services",
    "consumer products", "consumer services", "special retail", "specialty retail",
    # asset classes / seniority used as a bucket header
    "senior secured first lien debt", "first and second lien debt", "first lien debt",
    "second lien debt", "secured debt", "subordinated debt", "unsecured debt",
    "first lien senior secured", "1st lien senior secured debt", "1st lien last out unitranche",
    "last out unitranche", "1st lien", "2nd lien", "3rd lien", "senior secured",
    "first lien", "second lien", "unitranche", "broadly syndicated", "one stop",
    "preferred equity", "common equity", "common stock", "preferred stock", "common shares",
    "preferred shares", "structured products", "joint ventures", "short term investments",
    # sector / asset-class leads on specific denormalized filers (leading-strip only, so safe)
    "services", "industrial conglomerates", "retail and consumer products",
    "consumer products and services", "diversified",
    # geographies
    "united states", "united kingdom", "australia", "canada", "netherlands", "luxembourg",
    "germany", "france", "ireland", "switzerland", "new zealand", "europe", "north america",
    "cayman islands", "bermuda", "jersey", "spain", "italy", "sweden", "norway", "denmark",
    # GICS sectors
    "energy", "materials", "industrials", "consumer discretionary", "consumer staples",
    "health care", "healthcare", "financials", "information technology", "communication services",
    "utilities", "real estate",
    # GICS industry groups / industries common in BDC SOIs
    "software", "it services", "high tech industries", "technology hardware", "semiconductors",
    "communications equipment", "electronic equipment", "internet",
    "health care equipment and services", "health care equipment", "health care technology",
    "health care providers and services", "health care providers", "pharmaceuticals",
    "biotechnology", "life sciences tools and services", "life sciences tools",
    "commercial services and supplies", "commercial and professional services",
    "commercial services", "professional services", "equity securities", "debt securities",
    "capital goods", "aerospace and defense", "machinery", "building products",
    "construction and engineering", "electrical equipment", "trading companies and distributors",
    "media", "entertainment", "media and entertainment", "interactive media and services",
    "hotels restaurants and leisure", "hotels, restaurants and leisure", "consumer services",
    "diversified consumer services", "specialty retail", "multiline retail", "distributors",
    "food products", "food and staples retailing", "beverages", "household durables",
    "household products", "personal products", "leisure products",
    "textiles apparel and luxury goods", "textiles, apparel and luxury goods",
    "chemicals", "containers and packaging", "metals and mining", "paper and forest products",
    "insurance", "banks", "capital markets", "diversified financial services",
    "consumer finance", "financial services", "thrifts and mortgage finance",
    "road and rail", "transportation", "air freight and logistics", "airlines",
    "energy equipment and services", "oil gas and consumable fuels", "oil, gas and consumable fuels",
    "automobiles", "auto components", "automotive",
]

# Structured-attribute markers used by denormalized filers ("... Issuer Investment Type First Lien
# ... Interest Rate 8.79% Maturity Date 11/2030"). The issuer name sits BEFORE the first marker, so
# we cut the member there — discarding attribute noise we already capture as clean XBRL facts.
_STRUCT_MARKER = re.compile(
    r"\b(investment type|type of investment|interest term|interest rate|reference rate(?: and spread)?|"
    r"maturity\s*/?\s*dissolution|maturity|commitment type|commitment expiration|"
    r"investment date|acquisition date|acquisition\s+\d|\bindustry\b|\bsector\b|asset type|"
    r"facility type|all[- ]?in rate|benchmark|variable index|variable interest rate|"
    r"original purchase|purchase date|initial purchase)\b", re.I)

# A stray subtotal percentage or pure-punctuation token between path levels ("Debt Investments
# 233.2% United States - 220.5% 1st Lien ...") — treated as strippable boilerplate.
_PCT_TOKEN_RE = re.compile(r"^[-–—(]*\d+(\.\d+)?\s*%[)]*$")
_PUNCT_TOKEN_RE = re.compile(r"^[-–—:;,.]+$")

# Legal-entity suffixes — when the token right after the first comma is one of these, the comma is
# INSIDE the legal name ("Belnick, LLC"), so the issuer name extends past it.
_LEGAL_SUFFIXES = {
    "llc", "l.l.c.", "inc", "inc.", "incorporated", "corp", "corp.", "corporation",
    "co", "co.", "company", "lp", "l.p.", "llp", "l.l.p.", "ltd", "ltd.", "limited",
    "plc", "n.v.", "nv", "s.a.", "sa", "gmbh", "ag", "ab", "sas", "s.a.s.", "bv", "b.v.",
    "holdings", "holding", "group", "partners", "lp.",
}

# Instrument / seniority keywords (searched in the full description, lowercased).
_SENIORITY = [
    (r"\bfirst lien\b|\b1st lien\b", "First Lien"),
    (r"\bsecond lien\b|\b2nd lien\b", "Second Lien"),
    (r"\bthird lien\b|\b3rd lien\b", "Third Lien"),
    (r"\bsenior secured\b", "Senior Secured"),
    (r"\bsubordinat", "Subordinated"),
    (r"\bjunior\b|\bmezzanine\b", "Subordinated"),
    (r"\bunsecured\b", "Unsecured"),
    (r"\bsenior\b", "Senior"),
]
_INSTRUMENT = [
    (r"\bdelayed draw\b|\bddtl\b", "Delayed Draw Term Loan"),
    (r"\brevolv|\brevolver\b|\bline of credit\b", "Revolver"),
    (r"\bterm loan\b|\bterm debt\b", "Term Loan"),
    (r"\bnotes?\b", "Notes"),
    (r"\bpreferred\b", "Preferred"),
    (r"\bwarrant", "Warrant"),
    (r"\bcommon\b", "Common Equity"),
    (r"\bequity\b|\bmember(ship)? units?\b|\bllc units?\b|\bshares?\b", "Equity"),
    (r"\bbond", "Notes"),
    (r"\bloan\b", "Loan"),
]

# Subtotal / aggregate rows to drop entirely (not real holdings).
_SUBTOTAL_RE = re.compile(r"\btotal\b|\bsubtotal\b", re.I)
_PCT_ONLY_RE = re.compile(r"^[^a-zA-Z]*\(\s*\d+\.?\d*\s*%\s*\)\s*$")  # e.g. "(2.02%)"
_JUNK_EXACT = {"investment", "investments", "", "nan", "none"}

# Named validation anchors (broadly-held credits found in the recon) — used by --diagnose.
_ANCHORS = ["anaplan", "flexera", "finastra", "avalara", "zendesk", "petvet", "icefall"]


def _fix_mojibake(s: str) -> str:
    """The em-dash separator some filers use comes through as a replacement char (�) or cp1252
    garble. Normalize any of those to ' - ' so the dash-format parser can split on it."""
    for ch in ("�", "", "", "–", "—", "–", "—"):
        s = s.replace(ch, " - ")
    return re.sub(r"\s+", " ", s).strip()


def _norm_tok(t: str) -> str:
    return t.lower().replace("&", "and").strip(" ,.:;-")


# category phrases as token tuples, longest-first (so "health care equipment" beats "health care")
_CATEGORY_TOKENS = sorted(
    ([_norm_tok(t) for t in p.replace("&", "and").split()] for p in _CATEGORY_PHRASES),
    key=len, reverse=True)


def _strip_category_prefix(s: str) -> str:
    """Strip leading category/geo/sector phrases token-by-token, REPEATEDLY — denormalized members
    stack several ('Investments United States Health Care Equipment & Services <issuer>'). Stops at
    the first token that isn't boilerplate, so a real issuer ('ACI Group Holdings, Inc.') survives.
    Operates on the original tokens (preserving case/punctuation) guided by a normalized view."""
    toks = s.split()
    changed = True
    while changed and toks:
        changed = False
        # strip a leading stray percentage / bare number / pure-punctuation token first
        # (denormalized filers embed subtotal percentages, sometimes written "2 2" without a %)
        if toks and (_PCT_TOKEN_RE.match(toks[0]) or _PUNCT_TOKEN_RE.match(toks[0])
                     or re.fullmatch(r"\d+(\.\d+)?", toks[0])):
            toks = toks[1:]
            changed = True
            continue
        norm = [_norm_tok(t) for t in toks]
        for phrase in _CATEGORY_TOKENS:
            n = len(phrase)
            if n <= len(toks) and norm[:n] == phrase:
                toks = toks[n:]
                changed = True
                break
    return " ".join(toks).lstrip(" ,-:").strip()


def parse_issuer(raw: str) -> dict:
    """Parse a raw SOI member string into {issuer_name, instrument_text, parse_ok}.

    Format detection (per row):
      1. comma-delimited  "Name[, Suffix], Sector, Instrument"  -> issuer = leading name parts
      2. em-dash          "Name - Instrument"                   -> issuer = part before dash
      3. fallback         denormalized path / freeform          -> strip category prefix; best guess

    parse_ok=False when the result is empty or still looks like a category bucket (so it can be
    excluded from matching and counted in the coverage report rather than silently mismatched)."""
    if raw is None:
        return {"issuer_name": None, "instrument_text": None, "parse_ok": False}
    s = _fix_mojibake(str(raw))
    low = s.lower().strip()
    if low in _JUNK_EXACT or _PCT_ONLY_RE.match(s) or _SUBTOTAL_RE.search(s):
        return {"issuer_name": None, "instrument_text": None, "parse_ok": False}

    # Cut at the first structured-attribute marker (denormalized filers). Everything from the
    # marker on is attribute noise (type/rate/maturity) we already have as XBRL facts; the issuer
    # is in the head. Keep the tail as instrument_text so seniority/type derivation still sees it.
    struct_tail = None
    m = _STRUCT_MARKER.search(s)
    if m and m.start() > 0:
        struct_tail = s[m.start():]
        s = s[:m.start()].strip(" ,-:")

    s = _strip_category_prefix(s)
    # drop a trailing enumerator ("... Term Loan 2", "... Secured Debt 1") — same instrument, copy n
    s_noenum = re.sub(r"\s+\d{1,2}$", "", s).strip()

    instrument_text = None
    issuer_name = None

    if "," in s_noenum:
        # comma format
        parts = [p.strip() for p in s_noenum.split(",") if p.strip()]
        if parts:
            name_parts = [parts[0]]
            i = 1
            # absorb legal-suffix / parenthetical continuations into the name
            while i < len(parts):
                tok = parts[i].lower().strip().rstrip(".")
                first_word = tok.split()[0] if tok else ""
                if tok in _LEGAL_SUFFIXES or first_word in _LEGAL_SUFFIXES \
                        or parts[i].startswith("(") or "d/b/a" in tok or "f/k/a" in tok \
                        or "fka" in tok or "dba" in tok:
                    # a legal-suffix part may itself carry a dash-delimited instrument tail
                    # ("Inc. - Delayed Draw Loan") — keep only the pre-dash piece in the name.
                    seg, dash, tail = parts[i].partition(" - ")
                    name_parts.append(seg)
                    if dash:
                        instrument_text = tail.strip()
                        i += 1
                        break
                    i += 1
                else:
                    break
            issuer_name = ", ".join(name_parts)
            if instrument_text is None:
                instrument_text = ", ".join(parts[i:]) if i < len(parts) else None
    elif " - " in s_noenum:
        head, _, tail = s_noenum.partition(" - ")
        issuer_name = head.strip()
        instrument_text = tail.strip() or None
    else:
        # freeform / denormalized path — keep as the issuer guess (lossy)
        issuer_name = s_noenum or None
        instrument_text = None

    # the struct tail (type/seniority words) feeds instrument-type/seniority derivation
    if struct_tail:
        instrument_text = (instrument_text + " " + struct_tail) if instrument_text else struct_tail
    ok = bool(issuer_name) and issuer_name.lower() not in _JUNK_EXACT \
        and not _SUBTOTAL_RE.search(issuer_name) and len(issuer_name) > 1
    return {"issuer_name": issuer_name, "instrument_text": instrument_text, "parse_ok": ok}


def derive_seniority(text: str) -> str | None:
    if not text:
        return None
    low = text.lower()
    for pat, label in _SENIORITY:
        if re.search(pat, low):
            return label
    return None


def derive_instrument_type(text: str) -> str | None:
    if not text:
        return None
    low = text.lower()
    for pat, label in _INSTRUMENT:
        if re.search(pat, low):
            return label
    return None


_SUFFIX_TOKENS = re.compile(
    r"\b(llc|l\.l\.c\.|inc|incorporated|corp|corporation|co|company|lp|l\.p\.|llp|ltd|limited|"
    r"plc|nv|n\.v\.|sa|s\.a\.|gmbh|ag|holdings?|group|partners|the)\b", re.I)
_PAREN_RE = re.compile(r"\([^)]*\)")


def normalize_issuer(name: str) -> str | None:
    """Aggressive normalization for clustering/anchor tests: drop parentheticals (d/b/a, fka),
    legal suffixes, punctuation; lowercase; collapse whitespace. 'Belnick, LLC (d/b/a ...)' ->
    'belnick'."""
    if not name:
        return None
    s = _PAREN_RE.sub(" ", str(name).lower())
    s = re.sub(r"[^a-z0-9&\s]", " ", s)
    s = _SUFFIX_TOKENS.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ---------------------------------------------------------------------------
# Phase 2 — issuer clustering (fuzzy)
# ---------------------------------------------------------------------------

# Generic tokens dropped when building an issuer's CORE KEY (the distinctive-token fingerprint used
# for clustering). Three families: (a) sponsor-backed borrower scaffolding ("X Buyer", "Y Bidco",
# "Z Holdings"); (b) SOI boilerplate that survived Phase-1 parsing on denormalized filers
# (controlled/affiliated/lien/secured/debt/...); (c) single-word GICS sector remnants. Dropping
# these means a denormalized "non controlled affiliated software avalara avalara first lien" and a
# clean "avalara" collapse to the SAME core ('avalara') and merge safely — while boilerplate can no
# longer bridge two unrelated issuers (the bug that produced a 14k-name mega-cluster).
_GENERIC_TOKENS = {
    # sponsor-backed scaffolding / legal
    "holdings", "holding", "holdco", "topco", "midco", "bidco", "buyer", "parent", "intermediate",
    "acquisition", "acquisitions", "group", "capital", "partners", "investors", "investor",
    "investments", "investment", "corp", "company", "inc", "llc", "ltd", "plc", "purchaser",
    "services", "service", "solutions", "systems", "technologies", "technology", "brands",
    "usa", "global", "international", "the", "borrower", "finance", "financial", "newco",
    "national", "american", "ventures", "enterprises", "industries", "and",
    # SOI boilerplate that can survive denormalized parsing
    "non", "controlled", "noncontrolled", "affiliated", "affiliate", "nonaffiliated", "control",
    "lien", "1st", "2nd", "3rd", "first", "second", "third", "senior", "secured", "subordinated",
    "unsecured", "debt", "loan", "loans", "notes", "note", "revolver", "revolving", "term",
    "delayed", "draw", "ddtl", "unitranche", "equity", "common", "preferred", "warrant",
    "warrants", "units", "unit", "interest", "interests", "stock", "shares", "share", "credit",
    "facility", "due", "sofr", "libor", "prime", "euribor", "spread", "fund", "fixed", "last",
    "out", "line", "bank", "related", "party", "undrawn", "commitment", "pssl", "pslf", "rate",
    # structured-template remnants (denormalized filers)
    "issuer", "name", "maturity", "original", "purchase", "date", "variable", "index", "benchmark",
    "floor", "initial", "type", "cash", "all", "corporate", "pik", "index", "reference", "loc",
    "securities", "uk", "warrants", "warrant",
    # single-word GICS sector remnants
    "software", "healthcare", "industrials", "materials", "energy", "utilities", "financials",
    "media", "entertainment", "insurance", "banks", "chemicals", "machinery", "transportation",
    "pharmaceuticals", "biotechnology", "automobiles", "automotive", "retail", "distributors",
    "beverages", "internet", "semiconductors", "airlines", "products", "manufacturing",
}


class _UnionFind:
    def __init__(self, items):
        self.p = {x: x for x in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _sig_tokens(norm: str) -> list[str]:
    """Distinctive tokens of a normalized name: length>=2, not generic, not all-digits. Keeping
    2-char tokens matters — short prefixes ('gs'/'pl' in 'gs acquisitionco', 'g1' in 'g1
    therapeutics') are the only thing distinguishing otherwise-identical sponsor names."""
    return [t for t in norm.split()
            if len(t) >= 2 and t not in _GENERIC_TOKENS and not t.isdigit()]


def core_key(norm: str) -> str:
    """The distinctive-token fingerprint of an issuer name: significant tokens, de-duplicated and
    order-independent. 'finastra usa' -> 'finastra'; 'anaplan anaplan' -> 'anaplan'; a denormalized
    'non controlled software avalara avalara first lien' -> 'avalara'. Empty when the name is all
    boilerplate (such rows are left unclustered — honest, not force-matched)."""
    return " ".join(sorted(set(_sig_tokens(norm))))


def cluster_issuers(norms_freq: dict[str, int], threshold: int = 92,
                    max_block: int = 1500) -> dict[str, str]:
    """Cluster normalized issuer names into one canonical name each, in two safe steps:

      1. EXACT CORE — names with the same core_key merge (handles 'finastra'/'finastra usa',
         'anaplan'/'anaplan anaplan', and denormalized variants that collapse to a clean core).
      2. FUZZY CORE — distinct cores that are typo/spacing variants ('lendingpoint'/'lending point',
         'u s renal care'/'us renal care') merge when token_sort_ratio >= threshold. Blocked by a
         shared significant token so it stays tractable; cdist within each block.

    Because clustering keys are DISTINCTIVE tokens only, boilerplate ('controlled', 'lien', 'debt')
    can't bridge unrelated issuers. Canonical = the original norm with the highest row frequency in
    the cluster (ties -> shortest). Returns {issuer_norm -> canonical_norm}. Empty-core norms map to
    themselves (singletons)."""
    uniq = list(norms_freq)
    uf = _UnionFind(uniq)

    core_of = {u: core_key(u) for u in uniq}
    by_core: dict[str, list[str]] = defaultdict(list)
    for u in uniq:
        by_core[core_of[u]].append(u)

    # step 1: exact-core merges (skip the empty core — unclusterable boilerplate-only names)
    core_rep: dict[str, str] = {}   # core_key -> a representative norm
    for c, members in by_core.items():
        if not c:
            continue
        for m in members[1:]:
            uf.union(members[0], m)
        core_rep[c] = members[0]

    # step 2: fuzzy-merge distinct cores, blocked by significant token
    blocks: dict[str, list[str]] = defaultdict(list)
    for c in core_rep:
        for t in set(c.split()):
            blocks[t].append(c)
    seen_pairs: set = set()
    for tok, cores in blocks.items():
        if len(cores) < 2 or len(cores) > max_block:
            continue
        scores = process.cdist(cores, cores, scorer=fuzz.token_sort_ratio, workers=-1)
        for i in range(len(cores)):
            row = scores[i]
            for j in range(i + 1, len(cores)):
                if row[j] >= threshold:
                    uf.union(core_rep[cores[i]], core_rep[cores[j]])

    groups: dict[str, list[str]] = defaultdict(list)
    for u in uniq:
        groups[uf.find(u)].append(u)
    canon: dict[str, str] = {}
    for members in groups.values():
        # prefer the CLEANEST display name: fewest words, then most frequent, then alphabetical.
        # (a denormalized 'common stock 1 0 medeanalytics' loses to the plain 'medeanalytics')
        best = min(members, key=lambda x: (len(x.split()), -norms_freq[x], x))
        for u in members:
            canon[u] = best
    return canon


# ---------------------------------------------------------------------------
# Consolidation
# ---------------------------------------------------------------------------

def load_consolidated() -> pd.DataFrame:
    """Read every per-filing holdings CSV into one DataFrame, parse the issuer field, derive
    seniority / instrument type / price (FV/par). Adds: issuer_name, issuer_norm, instrument_text,
    seniority, instrument_type, price, price_basis, parse_ok."""
    files = sorted(glob.glob(str(HOLDINGS_DIR / "*.csv")))
    if not files:
        raise SystemExit(f"No holdings CSVs in {HOLDINGS_DIR}")
    frames = []
    for f in files:
        try:
            frames.append(pd.read_csv(f, dtype=str))
        except Exception:
            continue
    df = pd.concat(frames, ignore_index=True)

    # numeric coercions
    for c in ("fair_value", "cost", "principal", "spread", "rate", "pik_rate"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    parsed = df["issuer"].apply(parse_issuer).apply(pd.Series)
    df = pd.concat([df, parsed], axis=1)
    df["issuer_norm"] = df["issuer_name"].apply(normalize_issuer)
    # seniority/instrument derived from the full raw member (instrument_text can be empty)
    src = df["issuer"].fillna("") + " " + df["instrument_text"].fillna("")
    df["seniority"] = src.apply(derive_seniority)
    df["instrument_type"] = src.apply(derive_instrument_type)

    # price = FV / principal (cents on the dollar); fallback FV/cost flagged lower-fidelity
    fv, par, cost = df["fair_value"], df["principal"], df["cost"]
    df["price"] = (fv / par).where((par > 0) & fv.notna())
    df["price_basis"] = df["price"].notna().map({True: "par", False: None})
    fallback = df["price"].isna() & (cost > 0) & fv.notna()
    df.loc[fallback, "price"] = (fv / cost)[fallback]
    df.loc[fallback, "price_basis"] = "cost"
    # guard against absurd prices from scale mismatches / bad par
    df.loc[(df["price"] < 0) | (df["price"] > 5), "price"] = pd.NA
    return df


def add_clusters(df: pd.DataFrame, threshold: int = 90) -> pd.DataFrame:
    """Add issuer_cluster (canonical normalized name) via Phase-2 fuzzy clustering. Rows that
    didn't parse keep a null cluster."""
    freq = df.loc[df["parse_ok"], "issuer_norm"].dropna().value_counts().to_dict()
    canon = cluster_issuers(freq, threshold=threshold)
    df["issuer_cluster"] = df["issuer_norm"].map(canon)
    return df


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------

def diagnose() -> None:
    df = load_consolidated()
    n = len(df)
    print(f"consolidated rows: {n:,} from {df['cik'].nunique()} CIKs, "
          f"{df['reporting_date'].nunique()} reporting dates\n")

    ok = df["parse_ok"].sum()
    print("PARSE COVERAGE")
    print(f"  issuer parsed (parse_ok): {ok:,} ({ok/n:.1%})")
    print(f"  seniority derived:        {df['seniority'].notna().sum():,} "
          f"({df['seniority'].notna().mean():.1%})")
    print(f"  instrument_type derived:  {df['instrument_type'].notna().sum():,} "
          f"({df['instrument_type'].notna().mean():.1%})")
    print(f"  price computable:         {df['price'].notna().sum():,} "
          f"({df['price'].notna().mean():.1%})  "
          f"[par={ (df['price_basis']=='par').sum():,}, cost={(df['price_basis']=='cost').sum():,}]")
    print(f"  spread present:           {df['spread'].notna().sum():,} "
          f"({df['spread'].notna().mean():.1%})")
    print(f"  maturity present:         {df['maturity'].notna().sum():,} "
          f"({df['maturity'].notna().mean():.1%})")

    # cross-fund overlap on normalized issuer within a reporting date
    g = (df[df["parse_ok"]]
         .groupby(["issuer_norm", "reporting_date"])["cik"].nunique())
    multi = g[g >= 2]
    print("\nCROSS-FUND OVERLAP (parsed issuers)")
    print(f"  (issuer_norm, date) pairs held by >=2 funds: {len(multi):,}")
    print(f"  ... by >=3 funds: {(g>=3).sum():,}   >=5: {(g>=5).sum():,}   >=10: {(g>=10).sum():,}")

    print("\nNAMED-ANCHOR CHECK (max funds holding, any date)")
    for a in _ANCHORS:
        hit = df[df["issuer_norm"].fillna("").str.contains(a, na=False) & df["parse_ok"]]
        if hit.empty:
            print(f"  {a:12} —  not found")
            continue
        top = hit.groupby("reporting_date")["cik"].nunique().max()
        norms = hit["issuer_norm"].value_counts().head(3).index.tolist()
        print(f"  {a:12} max {top} funds/date   norms={norms}")

    print("\nSAMPLE PARSES (random)")
    samp = df[df["parse_ok"]].sample(min(8, int(ok)), random_state=3)
    for _, r in samp.iterrows():
        print(f"  raw={str(r['issuer'])[:60]!r}")
        print(f"      -> issuer={r['issuer_name']!r}  norm={r['issuer_norm']!r}  "
              f"sen={r['seniority']} type={r['instrument_type']} price={r['price']}")

    bad = df[~df["parse_ok"]]
    print(f"\nUNPARSED / DROPPED: {len(bad):,} rows. Sample raw members:")
    for v in bad["issuer"].dropna().drop_duplicates().head(8):
        print(f"  {str(v)[:80]!r}")


def diagnose_clusters(threshold: int = 90) -> None:
    df = add_clusters(load_consolidated(), threshold=threshold)
    parsed = df[df["parse_ok"]]
    n_norm = parsed["issuer_norm"].nunique()
    n_clust = parsed["issuer_cluster"].nunique()
    print(f"clustering @ WRatio>={threshold}")
    print(f"  distinct issuer_norm: {n_norm:,}  ->  clusters: {n_clust:,} "
          f"(merged {n_norm - n_clust:,})\n")

    # cross-fund overlap AFTER clustering (compare to the pre-cluster numbers)
    g = parsed.groupby(["issuer_cluster", "reporting_date"])["cik"].nunique()
    print("CROSS-FUND OVERLAP (clustered)")
    print(f"  (cluster, date) pairs held by >=2 funds: {(g>=2).sum():,}")
    print(f"  ... >=3: {(g>=3).sum():,}   >=5: {(g>=5).sum():,}   >=10: {(g>=10).sum():,}\n")

    print("NAMED-ANCHOR CHECK (norms folded into each anchor's cluster)")
    for a in _ANCHORS:
        hit = parsed[parsed["issuer_norm"].fillna("").str.contains(a, na=False)]
        if hit.empty:
            print(f"  {a:12} not found"); continue
        # the cluster that most of the anchor's rows map to
        cl = hit["issuer_cluster"].mode().iat[0]
        members = sorted(parsed.loc[parsed["issuer_cluster"] == cl, "issuer_norm"].unique(),
                         key=len)
        funds = parsed.loc[parsed["issuer_cluster"] == cl]\
            .groupby("reporting_date")["cik"].nunique().max()
        print(f"  {a:12} -> '{cl}'  ({len(members)} norms, max {funds} funds)")
        print(f"               folds: {members[:6]}{' ...' if len(members) > 6 else ''}")

    print("\nLARGEST CLUSTERS (by #distinct norms folded — eyeball for over-merge)")
    sizes = parsed.groupby("issuer_cluster")["issuer_norm"].nunique().sort_values(ascending=False)
    for cl, k in sizes.head(12).items():
        sample = sorted(parsed.loc[parsed["issuer_cluster"] == cl, "issuer_norm"].unique(),
                        key=len)[:4]
        print(f"  [{k:3} norms] {cl[:34]:34}  e.g. {sample}")

    print("\nSAMPLE MULTI-NORM MERGES (random clusters with 2-5 norms — eyeball for correctness)")
    multi = sizes[(sizes >= 2) & (sizes <= 5)].index
    import pandas as _pd  # local alias to avoid confusion
    for cl in _pd.Series(list(multi)).sample(min(10, len(multi)), random_state=5):
        members = sorted(parsed.loc[parsed["issuer_cluster"] == cl, "issuer_norm"].unique(), key=len)
        print(f"  {cl[:30]:30} <- {members}")


def build(threshold: int = 90) -> None:
    df = add_clusters(load_consolidated(), threshold=threshold)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "holdings_consolidated.csv"
    df.to_csv(out, index=False, encoding="utf-8")
    print(f"wrote {len(df):,} rows ({df['issuer_cluster'].nunique():,} clusters) -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--diagnose", action="store_true", help="report parse coverage + anchors")
    ap.add_argument("--cluster", action="store_true", help="report Phase-2 issuer clustering")
    ap.add_argument("--build", action="store_true", help="write consolidated cleaned CSV")
    ap.add_argument("--threshold", type=int, default=90, help="WRatio merge threshold (Phase 2)")
    args = ap.parse_args()
    if args.build:
        build(threshold=args.threshold)
    elif args.cluster:
        diagnose_clusters(threshold=args.threshold)
    else:
        diagnose()
