"""
ncsr_llm.py — the one Claude call per statements block, interactive and batched.

TWO PATHS, ONE REQUEST SHAPE. M4 runs filings one at a time so we can look at them;
M5 runs the corpus through the Message Batches API at half price. Those are different
transports, and it would be easy to let them drift into different requests — which
would make the pilot a test of something we never ship. So `build_params()` builds the
request body once and both paths use it verbatim.

That constraint is also why this module does NOT use `client.messages.parse()`, the
usual pydantic convenience. `.parse()` exists only on the interactive endpoint; the
Batches API takes raw `MessageCreateParamsNonStreaming`. Using `output_config.format`
with the schema generated from `NCSRRawExtraction` — and validating the returned JSON
ourselves — keeps one shape for both, and the validation step is identical either way.

COST SHAPE, MEASURED NOT ASSUMED. M1 measured serialized blocks at p50 ≈ 2,346 tokens,
which makes this workload OUTPUT-dominated: the ~3,000 input tokens per filing cost far
less than the JSON record the model writes back. Two consequences the code reflects:

  * The system prompt (rules + field dictionary, ~2,900 tokens) is IDENTICAL for every
    filing and is marked `cache_control`, so it is written once and read back at ~10%
    thereafter. Nothing filing-specific may ever go in `system` or that breaks.
  * `effort` is the main cost lever, because thinking tokens are billed as output. It
    is a parameter here rather than a constant precisely so M4 can sweep it against the
    gold sample instead of us guessing. Sonnet 5 runs adaptive thinking BY DEFAULT when
    `thinking` is omitted — the original ~$96 corpus estimate assumed no thinking, so
    this is exactly the number the pilot has to measure.

ON `MAX_TOKENS`: the project's standing default is 4096 for document-processing calls,
to bound runaway output. This module uses 8192 instead, deliberately. The output here
is not open-ended prose — it is a fixed 64-field JSON record whose size is bounded by
the schema, so a higher ceiling costs nothing unless it is actually used. What a too-low
ceiling DOES cost is real: truncation mid-JSON turns a good extraction into a hard
parse failure. Truncation is detected and raised, never silently accepted.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import truststore

# Must run BEFORE the anthropic client is constructed so httpx picks up the patched SSL
# context. The corporate network does SSL inspection; this makes Python trust the
# Windows certificate store. Same pattern as api_smoke_test.py and the EDGAR path.
truststore.inject_into_ssl()

import anthropic  # noqa: E402  (import after inject_into_ssl on purpose)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _p in ("schema", "extraction", "validation"):
    sys.path.insert(0, str(PROJECT_ROOT / "src" / _p))

from ncsr_prompt import build_system, build_user  # noqa: E402
from ncsr_raw import NCSRRawExtraction  # noqa: E402

# Primary model for the corpus run. Chosen in the session-17 plan: Sonnet 5 batch, with
# Opus escalation on identity-check failures. Sonnet 5 introductory pricing
# ($2/$10 per Mtok, halved again by the batch discount) ends 2026-08-31.
MODEL = "claude-sonnet-5"
# Escalation tier. The plan named Opus 4.8; Claude Opus 5 has since shipped at the same
# $5/$25 per Mtok, so the escalation tier is a free upgrade.
ESCALATION_MODEL = "claude-opus-5"

MAX_TOKENS = 8192          # see the module docstring — bounded by the schema, not prose
DEFAULT_EFFORT = "low"     # sweep this in M4; it is the dominant cost lever

# Published list prices, $ per million tokens. Recorded here so `--estimate` explains
# its own arithmetic rather than printing an unsourced number.
PRICES = {
    "claude-sonnet-5": {"in": 2.00, "out": 10.00, "note": "intro pricing, ends 2026-08-31"},
    "claude-opus-5": {"in": 5.00, "out": 25.00, "note": "standard"},
}
BATCH_DISCOUNT = 0.50
CACHE_READ_MULTIPLIER = 0.10


@dataclass
class LLMResult:
    """One model response plus everything needed to audit and price it."""

    raw: NCSRRawExtraction | None
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    error: str | None = None
    request_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.raw is not None and self.error is None


def output_schema() -> dict:
    """The JSON schema sent to the API, generated from NCSRRawExtraction.

    Generated rather than written by hand for the same reason the prompt is: a schema
    and a model that are typed out separately drift, and the drift is invisible.
    """
    return NCSRRawExtraction.model_json_schema()


def build_params(block_text: str, *, model: str = MODEL,
                 effort: str = DEFAULT_EFFORT) -> dict:
    """The request body. Used verbatim by BOTH the interactive and batch paths."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        # The stable half of the prompt, cached. Anything filing-specific placed here
        # would invalidate the cache for every subsequent request in the corpus.
        "system": [{
            "type": "text",
            "text": build_system(),
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": [{"role": "user", "content": build_user(block_text)}],
        "output_config": {
            "format": {"type": "json_schema", "schema": output_schema()},
            "effort": effort,
        },
        # Set explicitly rather than left to the model default: on Sonnet 5, omitting
        # `thinking` runs adaptive thinking, and thinking tokens bill as output on the
        # side of the ledger that dominates this workload's cost. Making it explicit
        # means a future reader can see the choice instead of inheriting it.
        "thinking": {"type": "adaptive"},
    }


def _parse_response(resp, model: str) -> LLMResult:
    """Turn an SDK response into an LLMResult, failing loudly on truncation."""
    u = resp.usage
    result = LLMResult(
        raw=None, model=model, stop_reason=resp.stop_reason,
        input_tokens=u.input_tokens, output_tokens=u.output_tokens,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        request_id=getattr(resp, "_request_id", None),
    )
    # Check the stop reason BEFORE touching content. A refusal has empty content and a
    # truncation has content that is valid-looking but incomplete — both would produce a
    # confusing downstream error if we went straight to parsing.
    if resp.stop_reason == "refusal":
        result.error = "refusal"
        return result
    if resp.stop_reason == "max_tokens":
        result.error = f"truncated at max_tokens={MAX_TOKENS}"
        return result
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        result.error = "no text block in response"
        return result
    try:
        result.raw = NCSRRawExtraction.model_validate_json(text)
    except Exception as ex:  # schema violation — keep the reason, do not crash the run
        result.error = f"schema validation failed: {ex!r}"
    return result


def extract_one(block_text: str, *, client: anthropic.Anthropic | None = None,
                model: str = MODEL, effort: str = DEFAULT_EFFORT) -> LLMResult:
    """Interactive path: one filing, one call. Used by the M4 pilot."""
    client = client or anthropic.Anthropic()
    params = build_params(block_text, model=model, effort=effort)
    try:
        resp = client.messages.create(**params)
    except anthropic.APIStatusError as ex:
        return LLMResult(None, model, None, 0, 0, 0, 0,
                         error=f"{type(ex).__name__}: {ex.status_code} {ex.message}")
    except anthropic.APIConnectionError as ex:
        return LLMResult(None, model, None, 0, 0, 0, 0,
                         error=f"APIConnectionError: {ex!r}")
    return _parse_response(resp, model)


# ── Batch path (M5) ───────────────────────────────────────────────────────────────

def batch_request(custom_id: str, block_text: str, *, model: str = MODEL,
                  effort: str = DEFAULT_EFFORT):
    """One entry for a Message Batches submission.

    `custom_id` is how a result is matched back to its filing — batch results come back
    in ARBITRARY ORDER, so position is meaningless and the id is the only link. Use
    `{cik}_{form}_{filing_date}` plus a block index for multi-series filings.
    """
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    if len(custom_id) > 64:
        raise ValueError(f"custom_id must be <= 64 chars, got {len(custom_id)}: {custom_id}")
    return Request(
        custom_id=custom_id,
        params=MessageCreateParamsNonStreaming(
            **build_params(block_text, model=model, effort=effort)
        ),
    )


def collect_batch(batch_id: str, out_dir: Path, *,
                  client: anthropic.Anthropic | None = None,
                  model: str = MODEL) -> dict[str, int]:
    """Stream a finished batch's results to disk, ONE FILE PER FILING.

    Per-filing writes rather than one aggregate at the end: a crash or an interruption
    partway through must not lose the results already retrieved. The file is named after
    `custom_id`, so re-running skips what is already on disk.
    """
    client = client or anthropic.Anthropic()
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {"succeeded": 0, "errored": 0, "expired": 0, "canceled": 0, "unparsed": 0}

    for entry in client.messages.batches.results(batch_id):
        kind = entry.result.type
        path = out_dir / f"{entry.custom_id}.json"
        if kind != "succeeded":
            stats[kind] = stats.get(kind, 0) + 1
            path.with_suffix(".error.json").write_text(
                json.dumps({"custom_id": entry.custom_id, "type": kind}, indent=2),
                encoding="utf-8")
            continue
        res = _parse_response(entry.result.message, model)
        if not res.ok:
            stats["unparsed"] += 1
            path.with_suffix(".error.json").write_text(
                json.dumps({"custom_id": entry.custom_id, "error": res.error}, indent=2),
                encoding="utf-8")
            continue
        stats["succeeded"] += 1
        path.write_text(json.dumps({
            "custom_id": entry.custom_id,
            "model": res.model,
            "usage": {"input": res.input_tokens, "output": res.output_tokens,
                      "cache_read": res.cache_read_tokens,
                      "cache_write": res.cache_write_tokens},
            "raw": res.raw.model_dump(),  # type: ignore[union-attr]
        }, indent=2), encoding="utf-8")
    return stats


def wait_for_batch(batch_id: str, *, client: anthropic.Anthropic | None = None,
                   poll_seconds: int = 60) -> str:
    """Block until a batch ends. Most finish inside an hour; the hard limit is 24."""
    client = client or anthropic.Anthropic()
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            return batch.processing_status
        counts = batch.request_counts
        print(f"  {batch.processing_status}: processing={counts.processing} "
              f"succeeded={counts.succeeded} errored={counts.errored}", flush=True)
        time.sleep(poll_seconds)


# ── Cost estimate (free — token counting is not billed) ───────────────────────────

def estimate(block_texts: list[str], *, model: str = MODEL,
             assumed_output_tokens: int = 2000,
             client: anthropic.Anthropic | None = None) -> dict:
    """Price a run BEFORE spending anything.

    Input tokens are counted exactly via the token-counting endpoint (free). Output
    tokens cannot be known in advance — hence `assumed_output_tokens`, which is the
    single biggest uncertainty in the whole cost model and the number the M4 pilot
    exists to replace with a measurement. The estimate assumes the cached system prompt
    is written once and read thereafter, which is what the request shape actually does.
    """
    client = client or anthropic.Anthropic()
    system = build_system()
    system_tokens = client.messages.count_tokens(
        model=model,
        system=system,
        messages=[{"role": "user", "content": "x"}],
    ).input_tokens

    per_filing_input = []
    for text in block_texts:
        n = client.messages.count_tokens(
            model=model, messages=[{"role": "user", "content": build_user(text)}]
        ).input_tokens
        per_filing_input.append(n)

    n = len(block_texts)
    price = PRICES[model]
    # First request writes the cached prefix; the rest read it.
    cached_in = system_tokens + (n - 1) * system_tokens * CACHE_READ_MULTIPLIER
    fresh_in = sum(per_filing_input)
    out = n * assumed_output_tokens

    cost_in = (cached_in + fresh_in) / 1e6 * price["in"] * BATCH_DISCOUNT
    cost_out = out / 1e6 * price["out"] * BATCH_DISCOUNT
    return {
        "filings": n,
        "model": model,
        "pricing_note": price["note"],
        "system_tokens": system_tokens,
        "median_block_tokens": sorted(per_filing_input)[n // 2] if n else 0,
        "total_input_tokens": int(cached_in + fresh_in),
        "assumed_output_tokens_each": assumed_output_tokens,
        "cost_input_usd": round(cost_in, 2),
        "cost_output_usd": round(cost_out, 2),
        "cost_total_usd": round(cost_in + cost_out, 2),
    }


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Extract one N-CSR statements block, or price a run.")
    ap.add_argument("path", type=Path, nargs="?", help="a filing .htm to extract")
    ap.add_argument("--effort", default=DEFAULT_EFFORT,
                    choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--estimate", action="store_true",
                    help="count tokens and price the run without extracting")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the request and print its shape; makes no API call")
    args = ap.parse_args(argv)

    if not args.path:
        ap.error("a filing path is required")

    sys.path.insert(0, str(PROJECT_ROOT / "src" / "extraction"))
    from ncsr_sections import extract_sections

    section = extract_sections(args.path)
    if not section.located:
        print(f"M1 did not locate statements in {args.path.name}; flags={section.flags}")
        return 1
    print(f"{args.path.name}: block {len(section.text):,} chars "
          f"(~{section.est_tokens:,} tokens), kinds={section.block_kinds}")

    if args.dry_run:
        params = build_params(section.text, model=args.model, effort=args.effort)
        print(f"  model      : {params['model']}")
        print(f"  max_tokens : {params['max_tokens']}")
        print(f"  effort     : {params['output_config']['effort']}")
        print(f"  thinking   : {params['thinking']}")
        print(f"  system     : {len(params['system'][0]['text']):,} chars, cached")
        print(f"  schema     : {len(json.dumps(params['output_config']['format']['schema'])):,} chars")
        # Plain ASCII in printed output: the Windows console codepage mangles
        # non-ASCII punctuation, and this project has burned time before on mojibake
        # that turned out to be a console artefact rather than a data problem.
        print("  (dry run - no API call made)")
        return 0

    if not _has_credentials():
        print("No API credentials found. Set ANTHROPIC_API_KEY for this shell:")
        print('  $env:ANTHROPIC_API_KEY = "sk-ant-..."')
        return 1

    if args.estimate:
        for k, v in estimate([section.text], model=args.model).items():
            print(f"  {k:28s} {v}")
        return 0

    result = extract_one(section.text, model=args.model, effort=args.effort)
    print(f"  stop_reason={result.stop_reason} in={result.input_tokens} "
          f"out={result.output_tokens} cache_read={result.cache_read_tokens}")
    if not result.ok:
        print(f"  FAILED: {result.error}")
        return 1
    raw = result.raw
    assert raw is not None
    print(f"  fund      : {raw.fund_name_as_printed!r}")
    print(f"  period end: {raw.period_end_as_printed!r} ({raw.period_months} months)")
    print(f"  scale     : amounts={raw.amounts_scale} shares={raw.shares_scale}")
    print(f"  statements: {raw.statements_present}")
    print(f"  net assets: {raw.total_net_assets}")
    print(f"  classes   : {[c.class_label for c in raw.share_classes]}")
    if raw.extraction_notes:
        print(f"  notes     : {raw.extraction_notes}")
    return 0


def _has_credentials() -> bool:
    """True if the SDK will find a credential. Checks the env vars only — an `ant auth`
    profile also works but is not detectable without constructing a client."""
    import os

    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


if __name__ == "__main__":
    sys.exit(_main())
