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
Cost: well under $0.01.
"""

import os
import sys

import truststore

# Must run BEFORE the anthropic client is created so httpx picks up the patched SSL context.
truststore.inject_into_ssl()

import anthropic  # noqa: E402  (import after inject_into_ssl on purpose)

# The pilot's primary model — testing the exact model we'll use, not just connectivity.
MODEL = "claude-sonnet-5"


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Set it for this shell and re-run:")
        print('  $env:ANTHROPIC_API_KEY = "sk-ant-..."')
        return 1

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=16,
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "").strip()
    print(f"reply: {text!r}")
    print(f"model: {resp.model}")
    print(f"usage: in={resp.usage.input_tokens} out={resp.usage.output_tokens}")
    print("SMOKE TEST PASSED — API reachable through this network, model accessible.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
