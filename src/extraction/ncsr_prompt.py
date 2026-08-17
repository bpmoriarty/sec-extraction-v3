"""
ncsr_prompt.py — build the one prompt we send per statements block.

THE POINT OF THIS MODULE: the field dictionary the model reads is GENERATED from the
`Field(description=...)` text in `schema/ncsr_raw.py`, not typed out again here. A
hand-written prompt and a schema drift apart the first time someone adds a field, and
the drift is invisible — the model just never fills the new field in. Generating one
from the other makes that class of bug impossible.

PROMPT LAYOUT AND WHY IT MATTERS FOR COST. The request is assembled in two pieces:

    system   = extraction rules + field dictionary   <- IDENTICAL for all ~3,000 filings
    messages = this filing's serialized statements   <- different every time

Prompt caching is a prefix match, so putting the stable half first and marking it with
`cache_control` means we pay to process the rules once and read them back at ~10% cost
on every later filing. Putting anything filing-specific (a filename, a date, the fund
name) into the system half would invalidate that cache for every subsequent request —
so nothing filing-specific goes there. That is a real constraint on this file, not a
style preference.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src" / "schema"))

from ncsr_raw import NCSRRawExtraction, RawShareClass  # noqa: E402

# The behavioural rules. Deliberately short and plain: current Claude models follow a
# system prompt closely, so piling on emphasis makes every rule read as equally urgent
# and none of them land. The one place emphasis IS used is the current-period rule in
# the schema's own `period_end_as_printed` description, because that is the single
# highest-frequency failure mode for side-by-side fund statements.
SYSTEM_RULES = """\
You read the financial statements of US closed-end interval and tender-offer funds \
(SEC forms N-CSR and N-CSRS) and return them as structured data.

The text you are given has been extracted from the filing's HTML. Tables appear \
between [TABLE] and [/TABLE] markers with cells separated by ` | `. Some filings have \
no HTML tables at all and appear as aligned text; read those the same way.

How to read the statements:

- Extract the MOST RECENT period only. These statements almost always print the \
current period beside one or more prior periods, and the prior-year column is the \
easiest thing in the document to take by mistake. Identify the current column first, \
then read every figure from it, and report that column's period-end date back to us.
- Report every figure EXACTLY AS PRINTED. Do not multiply by a scale, do not convert \
units, do not compute a subtotal the filing does not print, and do not reconcile \
figures that disagree. Tell us the scale in `amounts_scale` and `shares_scale` and we \
will do the arithmetic.
- Keep the sign as printed. A figure in parentheses is negative.
- A field that does not appear in this filing is `null`. Leave it null rather than \
deriving it from other lines or carrying it over from a prior period. A null we can \
see is useful; a plausible number we cannot trace is not.
- Percentages are percent numbers: 1.85 means 1.85%.
- Per-share figures are always in dollars per share and are never scaled.

If something is ambiguous — two funds side by side, a line you cannot classify, \
statements that look truncated — extract what you are confident about and describe the \
ambiguity in `extraction_notes`.\
"""


def _field_lines(model: type, indent: str = "") -> list[str]:
    """Render one pydantic model's fields as `- name (type): description` lines.

    Nested models are expanded inline underneath their parent field so the model sees
    the whole contract in one place rather than a cross-reference it has to resolve.
    """
    lines: list[str] = []
    for name, field in model.model_fields.items():
        desc = " ".join((field.description or "").split())
        ann = field.annotation
        # Human-readable type label. `str(ann)` gives things like
        # "typing.Optional[float]"; these three cases cover everything in this schema.
        text = str(ann)
        if "RawShareClass" in text:
            label = "list of share-class objects"
        elif "list[str]" in text:
            label = "list of strings"
        elif "float" in text:
            label = "number or null"
        elif "int" in text:
            label = "integer or null"
        elif "Literal" in text:
            # Show the allowed values rather than the word "Literal".
            opts = text.split("[", 1)[1].rstrip("]").replace("'", "")
            label = f"one of: {opts}"
        else:
            label = "string or null"
        lines.append(f"{indent}- {name} ({label}): {desc}")
        if "RawShareClass" in text:
            lines.extend(_field_lines(RawShareClass, indent + "    "))
    return lines


def field_dictionary() -> str:
    """The generated field-by-field contract. Single source of truth: ncsr_raw.py."""
    return "Fields to extract:\n\n" + "\n".join(_field_lines(NCSRRawExtraction))


def build_system() -> str:
    """The stable half of the prompt — identical for every filing, hence cacheable."""
    return f"{SYSTEM_RULES}\n\n{field_dictionary()}"


def build_user(block_text: str) -> str:
    """The volatile half — just this filing's statements.

    Note what is NOT here: no filename, no CIK, no fund name from the universe, no
    period from the filename. Supplying those would let the model echo our own guess
    back to us, which would defeat the identity cross-check the mapper performs on
    `fund_name_as_printed` and `period_end_as_printed`. The model must read them off
    the page.
    """
    return (
        "Extract the financial statements below.\n\n"
        "<statements>\n"
        f"{block_text}\n"
        "</statements>"
    )


if __name__ == "__main__":
    # Self-test: no API key, no network. Prints the prompt and its rough token cost so
    # a change to the schema's descriptions shows up as a visible cost change.
    system = build_system()
    print(system)
    print("\n" + "=" * 70)
    print(f"system chars: {len(system):,}  (~{len(system)//4:,} tokens, cached after "
          f"the first request)")
    missing = [n for n, f in NCSRRawExtraction.model_fields.items() if not f.description]
    print(f"fields with no description: {missing or 'none'}")
