"""
api_smoke_test.py — M0 verification: one trivial Claude API call through corporate SSL.

The corporate network does SSL inspection, which breaks Python's default certificate
verification (same issue we hit with EDGAR in session 3). The fix here is `truststore`:
it makes Python's ssl module trust the Windows certificate store, which contains the
corporate root CA. This is the anthropic-SDK analog of edgartools'
`configure_http(use_system_certs=True)` — and it's harmless on home networks.

Run (after setting your API key for this shell):
    PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."
                 uv run python src/extraction/api_smoke_test.py

Expected output: the model's one-word reply, the model id, and token usage.
Cost: about $0.0002 — two hundredths of a cent.

WHY THIS SCRIPT SETS `thinking` AND CHECKS `stop_reason` (session 20). As first written
it omitted `thinking` and set `max_tokens=16`, then printed "SMOKE TEST PASSED"
unconditionally. On Sonnet 5, omitting `thinking` runs ADAPTIVE THINKING, and
`max_tokens` is a hard cap on thinking PLUS response text — so if the model spent any of
those 16 tokens thinking, the reply came back empty with `stop_reason="max_tokens"` and
the script still declared success. That is the project's most expensive recurring failure
shape (a confident-looking pass hiding a failure: the contents-page blocks that "looked
located", the 290 silently truncated filings), so it is closed here rather than left as a
one-in-N surprise on the very first paid call.
"""

import os
import sys

import truststore

# Must run BEFORE the anthropic client is created so httpx picks up the patched SSL context.
truststore.inject_into_ssl()

import anthropic  # noqa: E402  (import after inject_into_ssl on purpose)

# The pilot's primary model — testing the exact model we'll use, not just connectivity.
MODEL = "claude-sonnet-5"
# Headroom over the ~1-token answer. Small enough to stay free, large enough that the
# result cannot be an artefact of the ceiling.
MAX_TOKENS = 64
# Sonnet 5 intro pricing, $ per million tokens (ends 2026-08-31).
PRICE_IN, PRICE_OUT = 2.00, 10.00


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Set it for this shell and re-run:")
        print('  $env:ANTHROPIC_API_KEY = "sk-ant-..."')
        return 1

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        # Explicit, not omitted: on Sonnet 5 an omitted `thinking` runs adaptive, and a
        # connectivity check has nothing to think about. Disabling it also makes the
        # token counts below a clean reading of the request itself.
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "").strip()
    u = resp.usage
    cost = u.input_tokens / 1e6 * PRICE_IN + u.output_tokens / 1e6 * PRICE_OUT

    print(f"reply : {text!r}")
    print(f"model : {resp.model}")
    print(f"stop  : {resp.stop_reason}")
    print(f"usage : in={u.input_tokens} out={u.output_tokens}  (${cost:.6f})")

    # Verify BEFORE claiming success. Each of these would previously have printed PASSED.
    if resp.stop_reason == "refusal":
        print("SMOKE TEST FAILED - the request was refused; check the prompt.")
        return 1
    if resp.stop_reason == "max_tokens":
        print(f"SMOKE TEST FAILED - truncated at max_tokens={MAX_TOKENS}; raise it.")
        return 1
    if not text:
        print("SMOKE TEST FAILED - reached the model but got no text back.")
        return 1
    if "ok" not in text.lower():
        print(f"SMOKE TEST FAILED - unexpected reply {text!r}; expected 'ok'.")
        return 1

    print("SMOKE TEST PASSED - API reachable through this network, model accessible, "
          "reply verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
