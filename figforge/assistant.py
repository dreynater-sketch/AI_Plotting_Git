"""The Claude side of FigForge's assistant -- for now only a connection check.

The key comes from ANTHROPIC_API_KEY: a Vercel env var when hosted, and the
git-ignored .env locally (loaded below when this runs as a script; the local
editor itself never calls Claude). The chat loop that drives ops.py's tools
comes later.

    python -m figforge.assistant      # one tiny request, prints the reply + cost
"""

import os

import anthropic

MODEL = "claude-opus-5"

# Claude Opus 5 list prices, $ per million tokens (input, output).
PRICE_PER_MTOK = (5.00, 25.00)


def configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def ping():
    """One minimal request, to prove the key and model work from wherever
    this runs. Returns what came back and what it cost; raises
    anthropic.APIError subclasses on failure (bad key, no credit, ...)."""
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=1024,
        output_config={"effort": "low"},
        # If a safety classifier declines, Anthropic retries on its
        # recommended model instead of returning the refusal.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user",
                   "content": "Reply with one short, cheerful sentence confirming "
                              "FigForge can reach you."}],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    u = response.usage
    cost = (u.input_tokens * PRICE_PER_MTOK[0] + u.output_tokens * PRICE_PER_MTOK[1]) / 1e6
    return {
        "ok": response.stop_reason != "refusal",
        "model": response.model,
        "reply": text,
        "stop_reason": response.stop_reason,
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cost_usd": round(cost, 5),
        "request_id": response._request_id,
    }


def _load_dotenv(path=".env"):
    """KEY=value lines from .env into the environment (never printed)."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


if __name__ == "__main__":
    _load_dotenv()
    if not configured():
        raise SystemExit("ANTHROPIC_API_KEY is not set -- add it to .env first.")
    r = ping()
    print(f"{r['model']}: {r['reply']}")
    print(f"{r['input_tokens']} in + {r['output_tokens']} out tokens = ${r['cost_usd']:.5f}"
          f"  (request {r['request_id']})")
