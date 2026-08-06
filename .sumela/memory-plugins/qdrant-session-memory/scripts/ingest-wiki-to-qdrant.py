#!/usr/bin/env python3
"""
ingest-wiki-to-qdrant.py — Wiki Page Ingestion Pipeline (v1.1)

Usage:
    python .sumela/memory-plugins/qdrant-session-memory/scripts/ingest-wiki-to-qdrant.py

What it does:
    1. Walks docs/second-brain/wiki/ — plus any project-configured EXTRA doc dirs
       (EXTRA_INGEST_DIRS env / .sumela/ingest.conf, default none) — for .md files
       (excluding special files), symlink-safe and deduped by resolved path.
    2. Parses YAML frontmatter (type, tags, date_updated).
    3. Chunks body via naive word-level split (512 tokens, 50 overlap).
    4. Generates embeddings via Ollama (qwen3-embedding:0.6b) in parallel.
    5. Deletes existing points for the page (idempotency) and upserts new chunks
       into Qdrant 'wiki_pages' collection with structured payload.
    6. Prints a structured status report for the agent to relay to the user.

Payload schema per point:
    text          : str    (the chunk body)
    page_path     : str    (relative path from wiki root)
    page_title    : str    (filename without .md)
    page_type     : str    (from frontmatter 'type')
    tags          : list   (from frontmatter 'tags')
    date_updated  : str    (from frontmatter 'date_updated' or today)
    chunk_index   : int
    total_chunks  : int

Environment:
    OLLAMA_HOST defaults to http://localhost:11434
    QDRANT_HOST defaults to localhost:6333
    QDRANT_PORT defaults to 6333
    WIKI_PAGES_COLLECTION defaults to wiki_pages
    WIKI_DIR defaults to docs/second-brain/wiki (relative to repo root)
"""
from __future__ import annotations

import sys, os
from datetime import datetime
from pathlib import Path
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.memory_ingest import (
    get_repo_root, get_extra_ingest_dirs, chunk_text, get_embedding,
    deterministic_id, print_report, resolve_collection_arg, project_slug,
    qdrant_client_preflight,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def report_success(pages_ingested: int, chunk_count: int, qdrant_ok: bool,
                   pages_skipped: int = 0):
    lines = [
        f"Status: {'SUCCESS' if qdrant_ok and not pages_skipped else 'PARTIAL'}",
        f"Pages ingested: {pages_ingested}",
        f"Total chunks: {chunk_count}",
        f"Qdrant upsert: {'OK' if qdrant_ok else 'FAILED'}",
    ]
    if pages_skipped:
        lines.append(f"Pages SKIPPED (embedding failed, left unchanged): {pages_skipped}")
        lines.append("Action: re-run this ingest; these pages are NOT up to date in the index.")
    print_report("WIKI INGEST REPORT", lines)


def report_failure(stage: str, reason: str):
    print_report("WIKI INGEST REPORT", [
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
    from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue
except ImportError:
    report_failure("Dependency", "qdrant-client not installed. Run: pip install qdrant-client")
    sys.exit(1)

OLLAMA_URL = os.getenv("OLLAMA_HOST", "http://localhost:11434")
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
# Per-project physical collection (honors WIKI_PAGES_COLLECTION override inside the
# resolver). PROJECT_SLUG namespaces the payload and point IDs (see ingest-code).
COLLECTION_NAME = resolve_collection_arg("wiki_pages")
PROJECT_SLUG = project_slug()
CHUNK_SIZE = 512
OVERLAP = 50
MAX_WORKERS = 4

REPO_ROOT = get_repo_root()
WIKI_DIR = REPO_ROOT / os.getenv("WIKI_DIR", "docs/second-brain/wiki")
# Project-owned EXTRA documentation dirs (default: none). Validated repo-relative
# dirs inside the repo; their .md docs land in the SAME wiki_pages collection,
# keyed by repo-relative page_path so they never collide with wiki pages.
EXTRA_DIRS = get_extra_ingest_dirs(REPO_ROOT)
# Defence-in-depth: cap total docs per run so a misconfigured / hostile extra dir
# (e.g. a huge tree) can't pin the machine re-embedding through Ollama in the
# background. Override with EXTRA_INGEST_MAX_FILES.
MAX_DOC_FILES = int(os.getenv("EXTRA_INGEST_MAX_FILES", "5000"))
# Dirs never descended into during ANY doc walk (huge / non-doc trees).
ALWAYS_SKIP_DIRS = {".git", "node_modules", "vendor", ".venv", "__pycache__"}
EXCLUDED_FILES = {
    "_INDEX.md",
    "_SEARCH_INDEX.md",
    "_LOG.md",
    "_SCHEMA.md",
}
# Whole subdirectories to skip during wiki ingestion. The improvement queue is
# agent self-learning (one IMP-*.md per signal), not project knowledge — do not
# ingest it into the wiki_pages collection. (Wiki-specific: NOT applied to extra
# dirs, which are walked with only the universal ALWAYS_SKIP_DIRS guard.)
EXCLUDED_DIRS = {
    "_improvement-queue",
}


def _walk_md(root: Path, excluded_files: set, excluded_dirs: set):
    """Yield .md Paths under root WITHOUT following directory symlinks and skipping
    any file whose real path escapes REPO_ROOT. This is the security backstop for
    extra dirs: even if `root` itself validated clean, a symlink committed INSIDE it
    must not let the walk read files outside the repo."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune excluded + symlinked subdirs in place (os.walk respects this).
        dirnames[:] = [
            d for d in dirnames
            if d not in excluded_dirs and d not in ALWAYS_SKIP_DIRS
            and not os.path.islink(os.path.join(dirpath, d))
        ]
        for fn in filenames:
            if not fn.endswith(".md") or fn in excluded_files:
                continue
            fp = Path(dirpath) / fn
            if fp.is_symlink():
                continue
            rp = fp.resolve()
            if rp != REPO_ROOT and REPO_ROOT not in rp.parents:
                continue
            yield fp


def collect_doc_files():
    """Build an ordered, de-duplicated list of (md_path, page_path) across the wiki
    dir and every configured extra dir. Dedupe is by RESOLVED absolute path so a file
    reachable from two roots (e.g. an extra dir nesting the wiki) is ingested once."""
    sources = []
    if WIKI_DIR.exists():
        sources.append((WIKI_DIR, EXCLUDED_FILES, EXCLUDED_DIRS))
    for d in EXTRA_DIRS:
        # An extra dir that equals or contains the wiki is almost always a misconfig;
        # dedupe handles correctness, but warn so the operator notices.
        if d == WIKI_DIR or d in WIKI_DIR.parents:
            print(f"[warn] extra ingest dir {d} overlaps the wiki dir — pages deduped, "
                  f"but consider narrowing the config", file=sys.stderr)
        sources.append((d, set(), set()))  # extra dirs: no wiki-specific excludes
    jobs = []
    seen = set()
    for root, ef, ed in sources:
        for fp in sorted(_walk_md(root, ef, ed)):
            rp = fp.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            jobs.append((fp, fp.relative_to(REPO_ROOT).as_posix()))
            if len(jobs) >= MAX_DOC_FILES:
                print(f"[warn] doc-file cap reached ({MAX_DOC_FILES}); remaining files "
                      f"skipped. Narrow EXTRA_INGEST_DIRS or raise EXTRA_INGEST_MAX_FILES.",
                      file=sys.stderr)
                return jobs
    return jobs


def extract_frontmatter(content: str) -> tuple[dict, str]:
    fm = {}
    body = content
    if not content.startswith("---"):
        return fm, body
    parts = content.split("---", 2)
    if len(parts) < 3:
        return fm, body
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, parts[2]


def main():
    # Preflight: qdrant-client>=1.12 required (git pull bumps requirements, not the venv).
    preflight = qdrant_client_preflight()
    if preflight:
        report_failure("qdrant-client", preflight)
        sys.exit(1)

    if not WIKI_DIR.exists() and not EXTRA_DIRS:
        report_failure("Input", f"No docs to ingest: wiki dir not found ({WIKI_DIR}) "
                                f"and no extra ingest dirs configured")
        sys.exit(1)

    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, check_compatibility=False)

    doc_files = collect_doc_files()  # ordered, deduped (md_path, page_path) across wiki + extra dirs
    total_chunks = 0
    pages_ingested = 0

    # Collect all chunks first for parallel embedding
    all_jobs = []  # (page_path, page_title, fm, chunk_index, chunk_text, total_chunks)
    for md_path, page_path in doc_files:
        page_title = md_path.stem

        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()

        fm, body = extract_frontmatter(content)
        chunks = chunk_text(body, CHUNK_SIZE, OVERLAP)
        if not chunks:
            continue

        for i, chunk in enumerate(chunks):
            all_jobs.append((page_path, page_title, fm, i, chunk, len(chunks)))

    if not all_jobs:
        report_success(0, 0, True)
        sys.exit(0)

    # Parallel embedding generation
    print(f"[info] Embedding {len(all_jobs)} chunks via Ollama (workers={MAX_WORKERS})...")
    embedding_map = {}  # key -> (embedding, Exception)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_key = {}
        for page_path, page_title, fm, i, chunk, total in all_jobs:
            key = (page_path, i)
            future = executor.submit(get_embedding, chunk, OLLAMA_URL)
            future_to_key[future] = key

        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                embedding_map[key] = future.result()
            except Exception as e:
                embedding_map[key] = e

    # Group by page_path for idempotent delete + upsert. A page is ALL-OR-NOTHING:
    # see the skip loop below.
    pages = {}
    failed_pages = {}
    for page_path, page_title, fm, i, chunk, total in all_jobs:
        key = (page_path, i)
        emb = embedding_map.get(key)
        if isinstance(emb, Exception):
            failed_pages.setdefault(page_path, []).append((i, emb))
            continue
        pages.setdefault(page_path, []).append({
            "page_title": page_title,
            "fm": fm,
            "chunk_index": i,
            "chunk": chunk,
            "total_chunks": total,
            "embedding": emb,
        })

    # Any failed chunk disqualifies its whole page — writing the survivors would replace
    # a complete index entry with a partial one and report success. Full reasoning lives
    # in the code-ingest twin.
    for page_path, failures in failed_pages.items():
        pages.pop(page_path, None)
        first_index, first_error = failures[0]
        print(f"[warn] {page_path}: {len(failures)} chunk(s) failed to embed "
              f"(first: chunk {first_index}: {first_error}) — page left unchanged in the index")

    for page_path, chunks_data in pages.items():
        # Delete existing points for this page. Safe only because every chunk of this
        # page embedded successfully — pages with any failure were removed above.
        try:
            client.delete(
                collection_name=COLLECTION_NAME,
                points_selector=Filter(
                    must=[FieldCondition(key="page_path", match=MatchValue(value=page_path))]
                ),
            )
        except Exception as e:
            print(f"[warn] delete failed for {page_path}: {e}")

        points = []
        for cd in chunks_data:
            point_id = deterministic_id(f"{PROJECT_SLUG}::{page_path}", cd["chunk_index"])
            fm = cd["fm"]
            page_type = fm.get("type", "concept")
            tags_raw = fm.get("tags", "")
            tags = [t.strip().strip("'\"[]") for t in tags_raw.split(",") if t.strip()] if tags_raw else []
            date_updated = fm.get("date_updated", datetime.now().strftime("%Y-%m-%d"))
            points.append(
                PointStruct(
                    id=point_id,
                    vector=cd["embedding"],
                    payload={
                        "text": cd["chunk"],
                        "page_path": page_path,
                        "page_title": cd["page_title"],
                        "page_type": page_type,
                        "tags": tags,
                        "date_updated": date_updated,
                        "project_slug": PROJECT_SLUG,
                        "chunk_index": cd["chunk_index"],
                        "total_chunks": cd["total_chunks"],
                    },
                )
            )

        try:
            client.upsert(collection_name=COLLECTION_NAME, points=points)
            total_chunks += len(points)
            pages_ingested += 1
            print(f"  Ingested: {page_path} ({len(points)} chunks)")
        except Exception as e:
            print(f"[warn] upsert failed for {page_path}: {e}")

    qdrant_ok = total_chunks > 0
    report_success(pages_ingested, total_chunks, qdrant_ok, len(failed_pages))
    # Non-zero when pages were skipped: the index was not fully refreshed and the
    # caller must not read "SUCCESS" and move on.
    sys.exit(0 if qdrant_ok and not failed_pages else 1)


if __name__ == "__main__":
    main()
