#!/usr/bin/env python3
"""
ingest-code-to-qdrant.py — Source Code Ingestion Pipeline (v1.2)

Usage:
    # FULL ingest — walk the whole source tree (first build / manual refresh):
    python .sumela/memory-plugins/qdrant-session-memory/scripts/ingest-code-to-qdrant.py

    # INCREMENTAL ingest — re-embed only the files listed in FILE (one repo-relative
    # path per line). Used by the pull-time hook so code search stays fresh cheaply:
    python ...ingest-code-to-qdrant.py --changed-file /tmp/changed.txt

What it does:
    1. Selects code files (.cs, .ts, .tsx, .py, .go, .rs, .java, .js, .jsx):
         * FULL        — walks src/ recursively.
         * INCREMENTAL — takes only the paths in --changed-file that are under src/,
                         match the code patterns, pass the skip filters, and exist.
    2. Excludes build artifacts, dependencies, generated files, and secrets.
    3. Ensures the 'code_chunks' collection exists (creates it if missing). On an
       EMPTY/just-created collection an incremental run is promoted to a FULL walk,
       so the first pull after setup builds the whole corpus.
    4. Reads file contents and chunks if > 512 tokens.
    5. Generates embeddings via Ollama (qwen3-embedding:0.6b) in parallel.
    6. Deletes existing points for the file (idempotency) and upserts new chunks
       into Qdrant 'code_chunks' collection with structured payload.
    7. Prints a structured status report for the agent to relay to the user.

Payload schema per point:
    text          : str    (the chunk body)
    file_path     : str    (relative path from repo root)
    file_type     : str    (extension without dot)
    chunk_index   : int
    total_chunks  : int

Environment:
    OLLAMA_HOST defaults to http://localhost:11434
    QDRANT_HOST defaults to localhost:6333
    QDRANT_PORT defaults to 6333
    CODE_CHUNKS_COLLECTION defaults to code_chunks
    SRC_DIR defaults to src (relative to repo root)
    CODE_PATTERNS defaults to *.cs,*.ts,*.tsx,*.py,*.go,*.rs,*.java,*.js,*.jsx
"""
import sys, os, fnmatch, argparse
from pathlib import Path
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.memory_ingest import (
    get_repo_root, chunk_text, get_embedding, deterministic_id, print_report,
    resolve_collection_arg, project_slug, qdrant_client_preflight,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def report_success(files_ingested: int, chunk_count: int, qdrant_ok: bool, mode: str,
                   files_skipped: int = 0):
    lines = [
        f"Status: {'SUCCESS' if qdrant_ok and not files_skipped else 'PARTIAL'}",
        f"Mode: {mode}",
        f"Files ingested: {files_ingested}",
        f"Total chunks: {chunk_count}",
        f"Qdrant upsert: {'OK' if qdrant_ok else 'FAILED'}",
    ]
    if files_skipped:
        lines.append(f"Files SKIPPED (embedding failed, left unchanged): {files_skipped}")
        lines.append("Action: re-run this ingest; these files are NOT up to date in the index.")
    print_report("CODE INGEST REPORT", lines)


def report_failure(stage: str, reason: str):
    print_report("CODE INGEST REPORT", [
        "Status: FAILED",
        f"Stage: {stage}",
        f"Reason: {reason}",
    ])


try:
    import requests
except ImportError:
    report_failure("Dependency", "requests not installed. Run: pip install requests")
    sys.exit(1)

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        PointStruct, Filter, FieldCondition, MatchValue, Distance, VectorParams,
    )
except ImportError:
    report_failure("Dependency", "qdrant-client not installed. Run: pip install qdrant-client")
    sys.exit(1)

OLLAMA_URL = os.getenv("OLLAMA_HOST", "http://localhost:11434")
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
# Per-project physical collection (honors CODE_CHUNKS_COLLECTION override inside the
# resolver). PROJECT_SLUG namespaces both the payload and the point IDs so two
# projects sharing one Qdrant instance never overwrite each other's points.
COLLECTION_NAME = resolve_collection_arg("code_chunks")
PROJECT_SLUG = project_slug()
CHUNK_SIZE = 512
OVERLAP = 50
MAX_WORKERS = 4

# MUST stay in sync with setup-qdrant.py (same collection, same vector geometry) —
# a mismatch makes upserts fail with a dimension error.
EMBED_DIM = 1024              # Qwen3-Embedding-0.6B
DISTANCE = Distance.COSINE

REPO_ROOT = get_repo_root()
SRC_DIR = REPO_ROOT / os.getenv("SRC_DIR", "src")
CODE_PATTERNS = tuple(
    p.strip()
    for p in os.getenv(
        "CODE_PATTERNS",
        "*.cs,*.ts,*.tsx,*.py,*.go,*.rs,*.java,*.js,*.jsx",
    ).split(",")
)

EXCLUDED_DIRS = {
    "bin",
    "obj",
    "node_modules",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    "site-packages",
    ".git",
    ".idea",
    ".vscode",
}

EXCLUDED_PATTERNS = (
    "*.generated.*",
    "*.designer.*",
    "*.min.js",
    "*.min.css",
)

SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "appsettings*.json",
    "secrets.json",
    "*.key",
    "*.pem",
    "*.pfx",
    "*.p12",
    "*.mobileprovision",
)


def should_skip_file(file_path: Path) -> bool:
    """Return True if the file should not be ingested."""
    name = file_path.name
    parts = set(file_path.parts)

    if parts & EXCLUDED_DIRS:
        return True

    for pat in EXCLUDED_PATTERNS:
        if fnmatch.fnmatch(name, pat):
            return True

    for pat in SECRET_PATTERNS:
        if fnmatch.fnmatch(name, pat):
            return True

    return False


def _matches_code_pattern(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in CODE_PATTERNS)


def ensure_collection(client: QdrantClient) -> bool:
    """Ensure the code_chunks collection exists; create it (matching setup-qdrant.py)
    if missing. Returns True when a FULL ingest is warranted — i.e. the collection
    was just created or is empty — so a first-ever or wiped index gets fully built
    even when only a few files changed. Raises on a Qdrant communication failure
    (the caller treats that as a clean skip)."""
    if not client.collection_exists(COLLECTION_NAME):
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=EMBED_DIM, distance=DISTANCE),
        )
        print(f"[info] created collection '{COLLECTION_NAME}' ({EMBED_DIM}-dim, {DISTANCE})")
        return True
    try:
        return client.count(collection_name=COLLECTION_NAME, exact=False).count == 0
    except Exception:
        # Collection exists but count failed — don't guess a full re-embed; let the
        # incremental list drive the work.
        return False


def full_walk() -> List[Path]:
    code_files: List[Path] = []
    for pattern in CODE_PATTERNS:
        code_files.extend(SRC_DIR.rglob(pattern))
    return sorted(set(code_files))


def incremental_select(changed_file: Path) -> List[Path]:
    """Resolve the --changed-file list down to ingestable code files: repo-relative,
    under SRC_DIR, matching a code pattern, not skipped, and present on disk."""
    src_resolved = SRC_DIR.resolve()
    out = set()
    try:
        lines = changed_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        report_failure("Input", f"could not read --changed-file {changed_file}: {e}")
        sys.exit(1)
    for raw in lines:
        rel = raw.strip()
        if not rel:
            continue
        # Paths come from `git diff --name-only` (repo-relative). Reject anything
        # that isn't a plain repo-relative path.
        if rel.startswith("/") or rel.startswith("..") or "/../" in rel:
            print(f"[warn] ignoring non-repo-relative path: {rel}")
            continue
        if not _matches_code_pattern(Path(rel).name):
            continue
        p = (REPO_ROOT / rel).resolve()
        if src_resolved != p and src_resolved not in p.parents:
            continue  # not under src/
        if should_skip_file(p):
            continue
        if not p.is_file():
            continue  # deleted/renamed away — orphan prune handles removals
        out.add(p)
    return sorted(out)


def main():
    parser = argparse.ArgumentParser(description="Ingest source code into Qdrant code_chunks.")
    parser.add_argument(
        "--changed-file",
        default=None,
        help="Path to a file of newline-separated repo-relative paths to ingest "
             "incrementally. Omit for a full src/ walk.",
    )
    args = parser.parse_args()

    # Preflight: qdrant-client>=1.12 is required (a git pull bumps requirements, not the
    # venv). Report one actionable line instead of a traceback on an old/missing client.
    preflight = qdrant_client_preflight()
    if preflight:
        report_failure("qdrant-client", preflight)
        sys.exit(1)

    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, check_compatibility=False)

    # Ensure the target collection exists before we touch it. On a fresh/empty
    # collection, promote an incremental request to a full build.
    try:
        empty = ensure_collection(client)
    except Exception as e:
        report_failure("Collection", f"Qdrant unavailable: {e}")
        sys.exit(1)

    incremental = args.changed_file is not None
    if incremental and empty:
        print(f"[info] '{COLLECTION_NAME}' is empty — promoting incremental run to a full build")
        incremental = False

    mode = "incremental" if incremental else "full"

    if incremental:
        code_files = incremental_select(Path(args.changed_file))
    else:
        if not SRC_DIR.exists():
            # Nothing to ingest (no source tree) — not an error.
            report_success(0, 0, True, mode)
            sys.exit(0)
        code_files = full_walk()

    # Collect all chunks first for parallel embedding
    all_jobs = []  # (rel_path, file_type, chunk_index, chunk_text, total_chunks)
    for code_path in code_files:
        if should_skip_file(code_path):
            continue

        rel_path = code_path.relative_to(REPO_ROOT).as_posix()
        file_type = code_path.suffix.lstrip(".")

        try:
            with open(code_path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            print(f"[warn] could not read {rel_path}: {e}")
            continue

        if not content.strip():
            continue

        chunks = chunk_text(content, CHUNK_SIZE, OVERLAP)
        if not chunks:
            continue

        for i, chunk in enumerate(chunks):
            all_jobs.append((rel_path, file_type, i, chunk, len(chunks)))

    if not all_jobs:
        report_success(0, 0, True, mode)
        sys.exit(0)

    # Parallel embedding generation
    print(f"[info] Embedding {len(all_jobs)} chunks ({mode}) via Ollama (workers={MAX_WORKERS})...")
    embedding_map = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_key = {}
        for rel_path, file_type, i, chunk, total in all_jobs:
            key = (rel_path, i)
            future = executor.submit(get_embedding, chunk, OLLAMA_URL)
            future_to_key[future] = key

        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                embedding_map[key] = future.result()
            except Exception as e:
                embedding_map[key] = e

    # Group by rel_path for idempotent delete + upsert. A file is ALL-OR-NOTHING:
    # see the skip loop below.
    files = {}
    failed_files = {}
    for rel_path, file_type, i, chunk, total in all_jobs:
        key = (rel_path, i)
        emb = embedding_map.get(key)
        if isinstance(emb, Exception):
            failed_files.setdefault(rel_path, []).append((i, emb))
            continue
        files.setdefault(rel_path, []).append({
            "file_type": file_type,
            "chunk_index": i,
            "chunk": chunk,
            "total_chunks": total,
            "embedding": emb,
        })

    # Any failed chunk disqualifies its whole file. Writing the survivors would mean
    # deleting a complete index entry and replacing it with a partial one — and
    # reporting success. A silently incomplete entry is worse than a stale one:
    # retrieval answers confidently from a file it only half knows. Leave the file's
    # existing points untouched instead, and say so.
    for rel_path, failures in failed_files.items():
        files.pop(rel_path, None)
        first_index, first_error = failures[0]
        print(f"[warn] {rel_path}: {len(failures)} chunk(s) failed to embed "
              f"(first: chunk {first_index}: {first_error}) — file left unchanged in the index")

    total_chunks = 0
    files_ingested = 0

    for rel_path, chunks_data in files.items():
        # Delete ALL existing points for this file (by file_path) BEFORE upserting, so a
        # file that shrank does not leave stale higher-index chunks behind. Safe only
        # because every chunk of this file embedded successfully — files with any
        # failure were removed above and never reach this delete.
        try:
            client.delete(
                collection_name=COLLECTION_NAME,
                points_selector=Filter(
                    must=[FieldCondition(key="file_path", match=MatchValue(value=rel_path))]
                ),
            )
        except Exception as e:
            print(f"[warn] delete failed for {rel_path}: {e}")

        points = []
        for cd in chunks_data:
            point_id = deterministic_id(f"{PROJECT_SLUG}::{rel_path}", cd["chunk_index"])
            points.append(
                PointStruct(
                    id=point_id,
                    vector=cd["embedding"],
                    payload={
                        "text": cd["chunk"],
                        "file_path": rel_path,
                        "file_type": cd["file_type"],
                        "project_slug": PROJECT_SLUG,
                        "chunk_index": cd["chunk_index"],
                        "total_chunks": cd["total_chunks"],
                    },
                )
            )

        try:
            client.upsert(collection_name=COLLECTION_NAME, points=points)
            total_chunks += len(points)
            files_ingested += 1
            print(f"  Ingested: {rel_path} ({len(points)} chunks)")
        except Exception as e:
            print(f"[warn] upsert failed for {rel_path}: {e}")

    qdrant_ok = total_chunks > 0
    report_success(files_ingested, total_chunks, qdrant_ok, mode, len(failed_files))
    # A run that skipped files did NOT fully refresh the index — exit non-zero so a
    # caller (hook, CI, operator) sees it rather than reading "SUCCESS" and moving on.
    sys.exit(0 if qdrant_ok and not failed_files else 1)


if __name__ == "__main__":
    main()
