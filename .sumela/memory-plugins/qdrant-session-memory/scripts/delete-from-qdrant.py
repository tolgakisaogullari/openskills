#!/usr/bin/env python3
"""
delete-from-qdrant.py — remove a deleted source file's points from a Qdrant
collection, so a file removed upstream stops surfacing in semantic search.

The ingest scripts only delete-and-reupsert points for files they still SEE on
disk (idempotent per-file). A file deleted upstream is never walked again, so its
old embedding lingers as an orphan. The pull/checkout hooks call this to delete
those orphans by their payload key.

Usage:
    python delete-from-qdrant.py --collection wiki_pages  --key page_path  --value "docs/second-brain/wiki/foo.md"
    python delete-from-qdrant.py --collection code_chunks --key file_path  --value "src/app.ts"

Payload keys (set by the ingest scripts): wiki_pages -> page_path (repo-relative),
code_chunks -> file_path (repo-relative), chat_history -> session_id.

Best-effort: if qdrant_client is missing or Qdrant is unreachable, it prints a
notice and exits 0 — it must never fail its caller (a git hook).

Environment: QDRANT_HOST (default localhost), QDRANT_PORT (default 6333).
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.memory_ingest import resolve_collection_arg

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="Delete points from a Qdrant collection by a payload key match.")
    p.add_argument("--collection", required=True,
                   help="Qdrant collection: a logical base (wiki_pages, code_chunks, chat_history) "
                        "resolves to the per-project physical name; any other name passes through.")
    p.add_argument("--key", required=True, help="Payload field to match (e.g. page_path, file_path, session_id).")
    p.add_argument("--value", required=True, help="Value the payload field must equal for a point to be deleted.")
    p.add_argument("--host", default=os.getenv("QDRANT_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(os.getenv("QDRANT_PORT", "6333")))
    a = p.parse_args()

    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Filter, FieldCondition, MatchValue
    except ImportError:
        print("delete-from-qdrant: qdrant_client not installed — skipping.")
        return 0

    collection = resolve_collection_arg(a.collection)
    try:
        client = QdrantClient(host=a.host, port=a.port, check_compatibility=False)
        client.delete(
            collection_name=collection,
            points_selector=Filter(must=[FieldCondition(key=a.key, match=MatchValue(value=a.value))]),
        )
        print(f"delete-from-qdrant: removed points where {a.key} == '{a.value}' from '{collection}'.")
    except Exception as e:  # collection missing / Qdrant down / etc. — never fail the caller.
        print(f"delete-from-qdrant: skipped ({e}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
