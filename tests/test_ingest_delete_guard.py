#!/usr/bin/env python3
"""Dependency-free test for the delete-failure guard in the bulk Qdrant ingests.

Guards a field-grade silent hole. Both bulk ingests refresh a file idempotently by
DELETE-by-filter followed by UPSERT. The delete was wrapped in a try/except that only
printed `[warn] delete failed`, then fell through to the upsert — and a delete failure
was recorded in NEITHER failure set, so the run printed "Ingested: <file>", reported
Status: SUCCESS and exited 0.

Point ids are deterministic per (file, chunk_index), so upserting over a failed delete
rewrites indices 0..n-1 but cannot remove a LONGER tail from an earlier version. A file
that shrank from 40 chunks to 12 therefore kept chunks 12-39 of the old content, with a
stale total_chunks in their payload, and retrieval answered confidently from code that
no longer existed — the exact failure the all-or-nothing rule closes on the embed side.

Pinned here: a failed delete SKIPS the upsert, is counted, is named in the report, and
exits 2 ("ran, some entries stale") — never 0, and never 1. The 1 matters: a delete that
fails for every file writes nothing, so an exit derived from "did anything land" gave 1,
which the plugin README maps to "could not run — fix the dependency named in the report".
No dependency is broken in that case; ensure_collection already answered.

Run directly (no pytest, no third-party deps, no live Qdrant/Ollama):

    python3 tests/test_ingest_delete_guard.py

Exits non-zero if any assertion failed.
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".sumela/memory-plugins/qdrant-session-memory/scripts"


def check(name, cond):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}")
        check.failed += 1
check.failed = 0


class _FakeCount:
    def __init__(self, count):
        self.count = count


class FakeQdrant:
    """Records calls. `delete_raises` simulates a delete that fails while the rest of
    the connection stays healthy — the narrow window this guard exists for."""

    def __init__(self, *a, delete_raises=False, fail_first_only=False, **kw):
        self.delete_raises = delete_raises
        self.fail_first_only = fail_first_only
        self.deleted, self.upserted = [], []

    def collection_exists(self, collection_name):
        return True

    def count(self, collection_name, exact=False):
        return _FakeCount(5)          # non-empty: no promotion to a full re-ingest

    def delete(self, collection_name, points_selector):
        self.deleted.append(points_selector)
        if self.fail_first_only:
            # Fail the first TWO attempts — the guard's two tries for the FIRST file —
            # then succeed. That leaves exactly one stale entry alongside healthy ones,
            # the mix that pins the exit code and the SUCCESS gate. Counting attempts
            # rather than matching paths keeps the stub free of Filter internals.
            if len(self.deleted) <= 2:
                raise RuntimeError("simulated delete failure (timeout)")
            return
        if self.delete_raises:
            raise RuntimeError("simulated delete failure (timeout)")

    def upsert(self, collection_name, points):
        self.upserted.append(points)


def install_qdrant_stub(client_box):
    """Seed sys.modules so the ingest's module-level `from qdrant_client import ...`
    resolves without the real package. `client_box` receives the constructed client."""
    qc = types.ModuleType("qdrant_client")
    models = types.ModuleType("qdrant_client.models")

    def _client(*a, **kw):
        client_box.append(_client.instance)
        return _client.instance
    qc.QdrantClient = _client

    class _Any:
        def __init__(self, *a, **kw): pass
    for name in ("PointStruct", "Filter", "FieldCondition", "MatchValue", "VectorParams"):
        setattr(models, name, _Any)
    models.Distance = types.SimpleNamespace(COSINE="Cosine")
    qc.models = models
    sys.modules["qdrant_client"] = qc
    sys.modules["qdrant_client.models"] = models
    return _client


def load_ingest(script_name, mod_name):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS / script_name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_code_ingest(tmp, delete_raises, fail_first_only=False):
    """Load and run ingest-code-to-qdrant.py against a stub Qdrant. Returns
    (exit_code, stdout, fake_client)."""
    box = []
    factory = install_qdrant_stub(box)
    factory.instance = FakeQdrant(delete_raises=delete_raises,
                                  fail_first_only=fail_first_only)

    os.environ["SUMELA_REPO_ROOT"] = str(tmp)
    os.environ["SUMELA_COLLECTION_PREFIX"] = "guardtest"   # never persists a slug file
    os.environ["SRC_DIR"] = "src"

    mod = load_ingest("ingest-code-to-qdrant.py",
                      f"ingest_code_guard_{delete_raises}_{fail_first_only}")
    # Neutralize the two preflights and the network: this test is about the delete
    # branch, and the real ones would reach a live venv / a live Ollama.
    mod.qdrant_client_preflight = lambda: None
    mod.ollama_preflight = lambda url, timeout=5: None
    mod.get_embedding = lambda text, url: [0.1] * mod.EMBED_DIM

    argv, sys.argv = sys.argv, ["ingest-code-to-qdrant.py"]
    buf = io.StringIO()
    code = None
    try:
        with contextlib.redirect_stdout(buf):
            mod.main()
    except SystemExit as e:
        code = e.code
    finally:
        sys.argv = argv
    return code, buf.getvalue(), factory.instance


def run_wiki_ingest(tmp, delete_raises):
    """The structural twin. Fixed in the same change, so it is exercised the same way —
    a fix applied to one copy of a twin and not the other is this repo's known disease."""
    box = []
    factory = install_qdrant_stub(box)
    factory.instance = FakeQdrant(delete_raises=delete_raises)

    os.environ["SUMELA_REPO_ROOT"] = str(tmp)
    os.environ["SUMELA_COLLECTION_PREFIX"] = "guardtest"
    os.environ["WIKI_DIR"] = "wiki"

    mod = load_ingest("ingest-wiki-to-qdrant.py", f"ingest_wiki_guard_{delete_raises}")
    mod.qdrant_client_preflight = lambda: None
    mod.ollama_preflight = lambda url, timeout=5: None
    mod.get_embedding = lambda text, url: [0.1] * 1024

    argv, sys.argv = sys.argv, ["ingest-wiki-to-qdrant.py"]
    buf = io.StringIO()
    code = None
    try:
        with contextlib.redirect_stdout(buf):
            mod.main()
    except SystemExit as e:
        code = e.code
    finally:
        sys.argv = argv
    return code, buf.getvalue(), factory.instance


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / ".sumela").mkdir()                 # makes this dir the repo root
        (tmp / "src").mkdir()
        (tmp / "src" / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
        (tmp / "src" / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")

        # --- control: a healthy delete still ingests and still exits 0 --------------
        code, out, client = run_code_ingest(tmp, delete_raises=False)
        check("healthy run upserts every file", len(client.upserted) == 2)
        check("healthy run exits 0", code == 0)
        check("healthy run reports SUCCESS", "Status: SUCCESS" in out)

        # --- the guard: a failed delete must not be papered over -------------------
        code, out, client = run_code_ingest(tmp, delete_raises=True)
        check("failed delete SKIPS the upsert (no stale tail under new points)",
              client.upserted == [])
        check("failed delete does not report the file as ingested",
              "Ingested: src/a.py" not in out)
        check("failed delete is named in the report",
              "Files left STALE (delete failed, upsert skipped): 2" in out)
        check("failed delete does not report SUCCESS", "Status: SUCCESS" not in out)
        check("failed delete tells the operator to re-run",
              "NOT up to date" in out)
        # 2, not 1. Nothing landed, but the run reached the write stage against a
        # reachable backend — 1 would send the operator chasing a healthy dependency.
        check("failed delete exits 2 (ran, stale) and never 0", code == 2)
        check("failed delete does not claim the upsert FAILED (it was never attempted)",
              "Qdrant upsert: SKIPPED" in out)
        # A lost RESPONSE is not a failed request: the delete may have landed
        # server-side, so it is attempted twice before the entry is abandoned.
        check("delete is retried once before the file is given up",
              len(client.deleted) == 2 * 2)

        # --- MIXED: one file stale, the rest healthy -------------------------------
        # This is the case that pins the tri-state exit and the SUCCESS gate. With every
        # file failing, `qdrant_ok` is already False and the other failure sets are
        # already empty, so both `delete_failed` terms are short-circuited and a mutant
        # that drops either one survives. Here they are the only terms that fire.
        code, out, client = run_code_ingest(tmp, delete_raises=False, fail_first_only=True)
        check("mixed run: the healthy file still lands", len(client.upserted) == 1)
        check("mixed run: exits 2 (ran, some entries stale)", code == 2)
        check("mixed run: is NOT reported as SUCCESS", "Status: SUCCESS" not in out)
        check("mixed run: names exactly one stale file",
              "Files left STALE (delete failed, upsert skipped): 1" in out)
        check("mixed run: upsert line still reads OK (writes did happen)",
              "Qdrant upsert: OK" in out)

        # --- the wiki twin: same guard, exercised the same way ---------------------
        wiki = tmp / "wiki"
        wiki.mkdir()
        for name in ("one", "two"):
            (wiki / f"{name}.md").write_text(
                f"---\ntype: concept\ntags: t\n---\n\n# {name}\n\nbody text for {name}\n",
                encoding="utf-8")

        code, out, client = run_wiki_ingest(tmp, delete_raises=False)
        check("wiki twin: healthy run upserts every page", len(client.upserted) == 2)
        check("wiki twin: healthy run exits 0", code == 0)

        code, out, client = run_wiki_ingest(tmp, delete_raises=True)
        check("wiki twin: failed delete SKIPS the upsert", client.upserted == [])
        check("wiki twin: failed delete is named in the report",
              "Pages left STALE (delete failed, upsert skipped): 2" in out)
        check("wiki twin: failed delete does not report SUCCESS",
              "Status: SUCCESS" not in out)
        check("wiki twin: failed delete exits 2 (ran, stale) and never 0", code == 2)

    sys.modules.pop("qdrant_client", None)
    sys.modules.pop("qdrant_client.models", None)

    if check.failed:
        print(f"\n{check.failed} assertion(s) FAILED")
        sys.exit(1)
    print("\nAll delete-guard assertions passed")


if __name__ == "__main__":
    main()
