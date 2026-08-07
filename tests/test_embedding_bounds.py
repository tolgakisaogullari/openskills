#!/usr/bin/env python3
"""Dependency-free unit test for the embedding input bounds in lib/memory_ingest.py.

Guards a field failure that was silent by construction. Ollama loads an embedding model
with n_batch = n_ubatch = 2048; an embedding model is non-causal, so llama.cpp cannot
split an over-long prompt across ubatches — it aborts (SIGTRAP), the runner process
dies, and Ollama answers 500 to that request AND every other one in flight. Measured on
qwen3-embedding:0.6b: 1970 tokens → 200, 2104 tokens → 500, deterministic when the
prefix cache is cold.

Nothing in the pipeline noticed: `chunk_text` budgets WORDS (2-7x fewer than tokens
depending on content), the ingest scripts logged a [warn] per failed chunk and reported
SUCCESS, and one repo silently lost 2496 chunks across 617 files that way.

Run directly (no pytest, no third-party deps, no live Ollama):

    python3 tests/test_embedding_bounds.py

Exits non-zero if any assertion failed.
"""
import importlib.util
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / ".sumela/memory-plugins/qdrant-session-memory/scripts/lib/memory_ingest.py"

_spec = importlib.util.spec_from_file_location("memory_ingest_bounds", MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def check(name, cond):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}")
        check.failed += 1
check.failed = 0


class _FakeResponse:
    def __init__(self, payload, error=None):
        self._payload = payload
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        return self._payload


def install_fake_requests(responses):
    """Install a stub `requests` module that pops from `responses` and records calls.

    get_embedding imports requests lazily inside the function, so seeding sys.modules
    is enough — no network, no Ollama, and the test stays hermetic in CI.
    """
    calls = []
    fake = types.ModuleType("requests")
    # get_embedding catches requests.ConnectionError/Timeout by name to classify a
    # backend outage, so the stub must carry them or it diverges from the real module.

    def post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    fake.post = post
    sys.modules["requests"] = fake
    return calls


def main():
    estimate_tokens = _mod.estimate_tokens
    chunk_text = _mod.chunk_text
    get_embedding = _mod.get_embedding
    limit = _mod.EMBED_MAX_TOKENS

    # --- estimate_tokens: must never UNDER-estimate -------------------------------
    # Real (chars, tokens) pairs measured against qwen3-embedding:0.6b. The digit row is
    # the load-bearing one: tokens EXCEED chars there, which is why the bound is chars+2
    # and not any chars/N ratio. A ratio tuned on prose silently under-counts digits,
    # and under-counting is exactly what kills the runner.
    MEASURED = [
        ("Turkish prose",   4503, 1403),
        ("digit-heavy",     3054, 3055),   # 1.00 chars/token — tokens > chars
        ("base64",          4096, 2973),
        ("minified JSON",   8461, 4804),
        ("pure punctuation", 6000, 4001),
    ]
    for label, chars, tokens in MEASURED:
        check(f"{label} ({chars} chars -> {tokens} tokens) is not under-estimated",
              estimate_tokens("x" * chars) >= tokens)
    check("empty text needs no split", estimate_tokens("") <= limit)

    # --- the bound must count BYTES, not codepoints ---------------------------------
    # This is the assertion whose absence hid a live defect: the original bound used
    # len(text), and an ASCII-only test suite could never notice. Qwen uses byte-level
    # BPE, so a token covers at least one BYTE — measured at the old 7372-codepoint
    # limit, Cyrillic reached 14,740 bytes and common CJK 22,110, and the CJK chunk
    # returned HTTP 500 while the estimate still read 7372.
    for label, sample in (("Cyrillic", "д"), ("CJK", "字"),
                          ("emoji", "\U0001F600"), ("CJK Ext-B", "\U00020000")):
        text = sample * 3000
        check(f"{label} is bounded by bytes, not codepoints",
              estimate_tokens(text) >= len(text.encode("utf-8")))
        check(f"{label} chunks all fit the byte budget",
              all(len(c.encode("utf-8")) + 2 <= limit for c in chunk_text(text)))
    mixed = "ascii " + "字" * 5000 + " \U0001F600" * 500
    check("mixed-script text is bounded",
          all(len(c.encode("utf-8")) + 2 <= limit for c in chunk_text(mixed)))
    check("splitting never breaks a codepoint (round-trip is exact)",
          "".join(chunk_text(mixed)) == mixed)
    for c in chunk_text("字" * 5000):
        c.encode("utf-8").decode("utf-8")   # raises if a codepoint was cut in half
    check("every emitted chunk is valid UTF-8", True)

    # --- chunk_text: every emitted chunk is bounded --------------------------------
    prose = " ".join(f"kelime{i}" for i in range(5000))
    chunks = chunk_text(prose)
    check("prose splits into multiple chunks", len(chunks) > 1)
    check("every prose chunk is within the token bound",
          all(estimate_tokens(c) <= limit for c in chunks))

    # The case word-chunking structurally CANNOT catch: no whitespace at all, so the
    # whole file is one "word" and sails past any word budget however small. This is a
    # minified bundle, a base64 blob, or a single-line generated file.
    blob = "A" * (limit * 10)
    blob_chunks = chunk_text(blob)
    check("whitespace-free blob is split despite being one 'word'", len(blob_chunks) > 1)
    check("every blob chunk is within the token bound",
          all(estimate_tokens(c) <= limit for c in blob_chunks))
    check("blob split loses no content", "".join(blob_chunks) == blob)

    # A single enormous word inside otherwise normal text must not escape either.
    mixed = "intro words here " + ("B" * (limit * 4)) + " trailing words"
    check("oversized token inside normal text is still bounded",
          all(estimate_tokens(c) <= limit for c in chunk_text(mixed)))

    check("blank input yields no chunks", chunk_text("   \n\t ") == [])
    check("short input stays a single chunk", chunk_text("kısa bir metin") == ["kısa bir metin"])

    # --- get_embedding: num_batch is actually sent ---------------------------------
    # Raising the ceiling is half the fix; without this assertion a refactor could drop
    # the option and every symptom would return with the suite still green.
    DIM = _mod.EMBED_DIM
    calls = install_fake_requests([_FakeResponse({"embedding": [0.1] * DIM})])
    vec = get_embedding("merhaba", "http://localhost:11434")
    check("embedding vector is returned", vec == [0.1] * DIM)
    check("request carries options.num_batch",
          calls[0]["json"].get("options", {}).get("num_batch") == _mod.EMBED_NUM_BATCH)
    check("num_batch clears the 2048 default that crashes the runner",
          _mod.EMBED_NUM_BATCH > 2048)
    check("max-token budget leaves headroom under num_batch",
          _mod.EMBED_MAX_TOKENS < _mod.EMBED_NUM_BATCH)

    # Concurrency default is measured (12.3 chunk/s at 1 worker, 24.3 at 4, 23.9 at 8),
    # and must stay >= 1: ThreadPoolExecutor(max_workers=0) raises.
    check("embed worker count is a positive integer", _mod.EMBED_MAX_WORKERS >= 1)

    # --- get_embedding: one retry, because neighbours are collateral damage ---------
    # When a runner dies on somebody else's oversized prompt, the concurrent requests
    # also get a 500. Ollama restarts it, so those succeed on a second attempt.
    _mod.EMBED_RETRY_DELAY_SECONDS = 0      # keep the test fast
    calls = install_fake_requests([
        RuntimeError("500 Server Error: Internal Server Error"),
        _FakeResponse({"embedding": [0.3] * _mod.EMBED_DIM}),
    ])
    check("transient failure is retried and succeeds",
          get_embedding("merhaba", "http://localhost:11434") == [0.3] * _mod.EMBED_DIM)
    check("retry issued exactly two requests", len(calls) == 2)

    calls = install_fake_requests([
        RuntimeError("boom 1"),
        RuntimeError("boom 2"),
    ])
    raised = None
    try:
        get_embedding("merhaba", "http://localhost:11434")
    except Exception as e:      # noqa: BLE001 — asserting the type below
        raised = e
    check("exhausted retries re-raise so the caller can record the failure",
          isinstance(raised, RuntimeError))
    check("no silent success after exhausted retries", len(calls) == 2)


    # An empty embedding array is a 200 response Ollama really produces on soft failures.
    calls = install_fake_requests([_FakeResponse({"embedding": []}),
                                   _FakeResponse({"embedding": []})])
    raised = None
    try:
        get_embedding("merhaba", "http://localhost:11434")
    except Exception as e:      # noqa: BLE001
        raised = e
    check("an empty embedding is rejected rather than stored",
          isinstance(raised, ValueError))

    sys.modules.pop("requests", None)


    if check.failed:
        print(f"\n{check.failed} assertion(s) FAILED")
        sys.exit(1)
    print("\nAll embedding-bounds assertions passed")


if __name__ == "__main__":
    main()
