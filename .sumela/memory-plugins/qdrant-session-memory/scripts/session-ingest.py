#!/usr/bin/env python3
"""
session-ingest.py — Session Memory Ingestion Pipeline (v2.3-agnostic)

Usage:
    python session-ingest.py <session_summary_md_path>
    python session-ingest.py <path> --collection chat_history --ollama-url http://localhost:11434

What it does:
    1. Reads the session summary markdown.
    2. Extracts structured metadata: session_date, topics, decisions, affected_files.
    3. Chunks the body (512 words, 50 overlap), token-bounded — see lib.memory_ingest.
    4. Generates embeddings via Ollama (qwen3-embedding:0.6b).
    5. Deletes any prior points for this session, then upserts the fresh chunks
       into Qdrant 'chat_history' with enriched payload (idempotent re-ingest).
    6. Prints a structured status report for the agent to relay to the user.

Payload schema per chunk:
    text          : str    (the chunk body)
    session_id    : str    (filename without .md)
    date          : str    (ISO date, from frontmatter or today)
    date_int      : int     (YYYYMMDD form of `date` for range filtering; 0 if unparseable)
    developer     : str    (who did the work — frontmatter `developer`, else --fallback-developer, else "unknown")
    developer_email: str   (frontmatter `developer_email`, else "")
    domains       : list   (frontmatter `domains` — the session's domain context)
    spec_artifact : str    (frontmatter `spec_artifact` path, else "")
    plan_artifact : str    (frontmatter `plan_artifact` path, else "")
    topics        : list   (from frontmatter session_topics)
    decisions     : list   (extracted from a "## Decisions" / "## Decisions Made" block)
    affected_files: list   (any file paths mentioned in the chunk that match common code patterns)
    chunk_index   : int
    total_chunks  : int

These extra fields make `chat_history` queryable by developer / domain / date range
(see query-qdrant.py --developer/--domain/--since/--until). Summaries written before this
feature lack them and won't match a filter until re-ingested; the post-merge hook only
re-ingests summaries Added/Modified in a pulled range, so pre-existing ones backfill only
when re-committed (or via a one-time manual re-ingest of wiki/session-summaries/).

Environment:
    OLLAMA_URL       — Ollama base URL (default: http://localhost:11434)
    QDRANT_HOST      — Qdrant host (default: localhost)
    QDRANT_PORT      — Qdrant port (default: 6333)
    QDRANT_COLLECTION — Collection name (default: chat_history)
"""
import sys
import os
import re
import uuid
import hashlib
import argparse
from datetime import datetime
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.memory_ingest import (
    resolve_collection_arg, project_slug, qdrant_client_preflight,
    # chunk_text/get_embedding live in the lib so the embedding-input bounds they carry
    # apply to EVERY path. Private copies here and in query-qdrant.py were the reason a
    # fix to the lib alone would have left the summary and query paths still crashing.
    chunk_text, get_embedding,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def print_report(summary_lines: list):
    print("\n" + "=" * 60)
    print("SESSION INGEST REPORT")
    print("=" * 60)
    for line in summary_lines:
        print(line)
    print("=" * 60 + "\n")


def report_success(session_id: str, chunk_count: int, qdrant_ok: bool, decisions_n: int, files_n: int,
                   developer: str = "unknown", domains: list = None):
    print_report([
        f"Status: {'SUCCESS' if qdrant_ok else 'PARTIAL'}",
        f"Session ID: {session_id}",
        f"Developer: {developer}",
        f"Domains: {', '.join(domains) if domains else '(none)'}",
        f"Chunks created: {chunk_count}",
        f"Decisions extracted: {decisions_n}",
        f"Affected files mentioned: {files_n}",
        f"Qdrant upsert: {'OK' if qdrant_ok else 'FAILED (fallback to markdown)'}",
        "Action for agent: Relay this summary to the user.",
    ])


def report_failure(stage: str, reason: str):
    print_report([
        "Status: FAILED",
        f"Stage: {stage}",
        f"Reason: {reason}",
        "Action for agent: Inform the user of the failure.",
    ])

try:
    import requests
except ImportError:
    report_failure("Dependency", "requests not installed. Run: pip install requests")
    sys.exit(1)

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue
except ImportError:
    report_failure("Dependency", "qdrant-client not installed. Run: pip install qdrant-client")
    sys.exit(1)

# Common code-file path patterns (Windows + POSIX)
FILE_PATTERN = re.compile(
    r"\b(?:src|tests|docs|scripts|nginx)[\\/][\w\-\.\\/]+\.(?:cs|ts|tsx|js|jsx|py|md|json|yml|yaml|sql|csproj)\b",
    re.IGNORECASE,
)

# Decision section heading variants
DECISION_HEADERS = (
    re.compile(r"^##\s+Decisions?\s+Made\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^##\s+Decisions?\s*$", re.IGNORECASE | re.MULTILINE),
)


def deterministic_id(key: str, chunk_index: int) -> str:
    """Process-independent point ID derived from (key, chunk_index).

    Re-ingesting the same summary upserts (overwrites) the same points instead
    of creating duplicates — this is what makes the post-merge ingest hook safe
    to run on every pull. `key` is the session_id (summary filename without
    extension), so summary filenames MUST be unique across the project.
    """
    hex_str = hashlib.sha256(f"{key}_{chunk_index}".encode("utf-8")).hexdigest()[:32]
    return str(uuid.UUID(hex=hex_str))


def extract_frontmatter(content: str) -> dict:
    fm = {}
    if not content.startswith("---"):
        return fm
    parts = content.split("---", 2)
    if len(parts) < 3:
        return fm
    for line in parts[1].strip().splitlines():
        # Skip full-line comments; tolerate a trailing " # comment" on a value (agents
        # sometimes copy the annotated template verbatim). " #" (space-hash) avoids
        # clipping a '#' that is part of a value (e.g. "c#-migration").
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.split(" #", 1)[0].strip()
            fm[k.strip()] = v
    return fm


def extract_decisions(content: str) -> List[str]:
    """Find a Decisions section and pull out bullet points."""
    for pattern in DECISION_HEADERS:
        m = pattern.search(content)
        if not m:
            continue
        rest = content[m.end():]
        next_h = re.search(r"^##\s+", rest, re.MULTILINE)
        block = rest[: next_h.start()] if next_h else rest
        bullets = re.findall(r"^\s*[-*]\s+(.+)$|^\s*\d+\.\s+(.+)$", block, re.MULTILINE)
        flat = [a or b for a, b in bullets if (a or b)]
        return [d.strip() for d in flat if d.strip()]
    return []


def extract_affected_files(text: str) -> List[str]:
    matches = set(m.group(0).replace("\\", "/") for m in FILE_PATTERN.finditer(text))
    return sorted(matches)


def main():
    parser = argparse.ArgumentParser(
        description="Ingest a session summary markdown into Qdrant."
    )
    parser.add_argument("summary_path", help="Path to session summary markdown file")
    parser.add_argument("--collection", default="chat_history",
                        help="Qdrant collection: a logical base (chat_history) resolves to the "
                             "per-project physical name; QDRANT_COLLECTION env overrides.")
    parser.add_argument("--ollama-url", default=os.getenv("OLLAMA_URL", "http://localhost:11434"),
                        help="Ollama base URL (default: http://localhost:11434 or OLLAMA_URL env)")
    parser.add_argument("--host", default=os.getenv("QDRANT_HOST", "localhost"),
                        help="Qdrant host (default: localhost or QDRANT_HOST env)")
    parser.add_argument("--port", type=int, default=int(os.getenv("QDRANT_PORT", "6333")),
                        help="Qdrant port (default: 6333 or QDRANT_PORT env)")
    parser.add_argument("--fallback-developer", default="",
                        help="Used as `developer` when the summary frontmatter has none "
                             "(the post-merge hook passes the commit's git author here).")
    parser.add_argument("--fallback-date", default="",
                        help="Used as `session_date` when frontmatter has none (ISO YYYY-MM-DD).")
    args = parser.parse_args()

    # Preflight: qdrant-client>=1.12 required (a git pull bumps requirements, not the venv).
    # Report one actionable line instead of a traceback before any embedding work.
    preflight = qdrant_client_preflight()
    if preflight:
        report_failure("qdrant-client", preflight)
        sys.exit(1)

    # Resolve the logical base to the per-project physical collection, and the slug
    # used to namespace point IDs + stamp payloads (so two projects on one shared
    # Qdrant never collide). See lib.memory_ingest.
    collection = resolve_collection_arg(args.collection)
    slug = project_slug()

    summary_path = args.summary_path
    if not os.path.exists(summary_path):
        report_failure("File I/O", f"File not found: {summary_path}")
        sys.exit(1)

    with open(summary_path, "r", encoding="utf-8") as f:
        text = f.read()

    fm = extract_frontmatter(text)
    session_date = fm.get("session_date") or args.fallback_date or datetime.now().strftime("%Y-%m-%d")

    def _list_field(key):
        """Parse a frontmatter value that may be a YAML inline list or comma string."""
        raw = fm.get(key, "")
        return [t.strip() for t in raw.strip("[]").replace("'", "").replace('"', "").split(",") if t.strip()]

    topics = _list_field("session_topics")
    domains = _list_field("domains")
    developer = (fm.get("developer") or args.fallback_developer or "unknown").strip()
    developer_email = fm.get("developer_email", "").strip()
    spec_artifact = fm.get("spec_artifact", "").strip()
    plan_artifact = fm.get("plan_artifact", "").strip()
    # date_int (YYYYMMDD) for Qdrant Range filtering; 0 if the date isn't a clean ISO date.
    try:
        date_int = int(datetime.strptime(session_date.strip(), "%Y-%m-%d").strftime("%Y%m%d"))
    except (ValueError, AttributeError):
        date_int = 0
    # splitext (not .replace) so filenames containing ".md" mid-name aren't mangled.
    session_id = os.path.splitext(os.path.basename(summary_path))[0]

    decisions = extract_decisions(text)
    affected_files = extract_affected_files(text)

    chunks = chunk_text(text)
    if not chunks:
        # Bail BEFORE the delete below: an empty summary would otherwise wipe the prior
        # session's points and upsert nothing in their place.
        print("[session-ingest] Summary body is empty — nothing to ingest, index unchanged.")
        report_success(session_id, 0, False, len(decisions), len(affected_files))
        sys.exit(0)
    print(f"[session-ingest] Chunked into {len(chunks)} chunks.")
    print(f"[session-ingest] Decisions extracted: {len(decisions)}")
    print(f"[session-ingest] Affected files: {len(affected_files)}")

    qdrant_ok = False
    try:
        client = QdrantClient(host=args.host, port=args.port, check_compatibility=False)
        points = []
        for i, chunk in enumerate(chunks):
            embedding = get_embedding(chunk, args.ollama_url)
            chunk_files = extract_affected_files(chunk) or affected_files
            points.append(
                PointStruct(
                    id=deterministic_id(f"{slug}::{session_id}", i),
                    vector=embedding,
                    payload={
                        "text": chunk,
                        "session_id": session_id,
                        "project_slug": slug,
                        "date": session_date,
                        "date_int": date_int,
                        "developer": developer,
                        "developer_email": developer_email,
                        "domains": domains,
                        "spec_artifact": spec_artifact,
                        "plan_artifact": plan_artifact,
                        "topics": topics,
                        "decisions": decisions,
                        "affected_files": chunk_files,
                        "chunk_index": i,
                        "total_chunks": len(chunks),
                    },
                )
            )
        # Delete any prior points for this session BEFORE upserting fresh ones.
        # Deterministic IDs make re-ingest overwrite indices 0..N-1, but if an
        # edited summary now yields FEWER chunks, the old higher-index points
        # would be orphaned. Delete-by-session_id then upsert guarantees the
        # collection exactly mirrors the current summary — which is what the
        # post-merge ingest hook relies on. Embeddings are computed above, so by
        # the time we delete we are committed to a successful re-insert.
        client.delete(
            collection_name=collection,
            points_selector=Filter(
                must=[FieldCondition(key="session_id", match=MatchValue(value=session_id))]
            ),
        )
        client.upsert(collection_name=collection, points=points)
        qdrant_ok = True
        print(f"[session-ingest] Replaced session '{session_id}' with {len(points)} chunks in Qdrant '{collection}'.")
    except Exception as e:
        print(f"[session-ingest] WARNING: Qdrant upsert failed: {e}")
        print("[session-ingest] Fallback: markdown summary already exists; will retry on next run.")

    report_success(session_id, len(chunks), qdrant_ok, len(decisions), len(affected_files),
                   developer=developer, domains=domains)
    sys.exit(0)


if __name__ == "__main__":
    main()
