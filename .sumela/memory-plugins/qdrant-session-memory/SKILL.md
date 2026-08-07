---
name: qdrant-session-memory
description: "Use when answering questions about past decisions, prior sessions, or 'why' questions — semantic search over Qdrant session history. Activates Tier-1 routing."
---

# Tier-1: Qdrant Session Memory

## Important: No API Key Required for Queries

All query operations read from the local Qdrant database via `query-qdrant.py`. They are **purely local** — no external API calls, no cost. API keys are only needed for embedding generation via Ollama (which runs locally).

## Proactive Usage (Silent)

The agent should query Qdrant **without waiting for the user to ask** in these scenarios:

### Before Starting a Task
When the user assigns a new task, check if similar work was done before:
```
python .sumela/memory-plugins/qdrant-session-memory/scripts/query-qdrant.py "<task description>" --limit 3
```
- If top score ≥ 0.5: read the matching session summary for context, lessons learned, and decisions made
- If score < 0.5: skip — no relevant past context

### When Encountering an Entity or Concept
When the task involves an entity, service, or concept that might have been discussed before:
```
python .sumela/memory-plugins/qdrant-session-memory/scripts/query-qdrant.py "<entity name> <context>" --limit 3
```
Example: Task mentions "CommentService" → query "CommentService design decisions"

### When Making Architectural Decisions
Before proposing an architecture or design choice, check if similar decisions were made:
```
python .sumela/memory-plugins/qdrant-session-memory/scripts/query-qdrant.py "<decision topic>" --limit 3
```
Example: "caching strategy" → check if caching was discussed before

### When Debugging
Before diving into a bug, check if similar issues were encountered:
```
python .sumela/memory-plugins/qdrant-session-memory/scripts/query-qdrant.py "<bug description or error message>" --limit 3
```

### When a File is Mentioned
When the task involves a specific file that might have history:
```
python .sumela/memory-plugins/qdrant-session-memory/scripts/query-qdrant.py "<filename> changes" --limit 3
```

**RULE:** This is SILENT — do not announce "I'm checking past sessions via Qdrant" unless the user explicitly asks. Just query, read relevant context, and incorporate into your analysis.

## Reactive Usage (User-Triggered)

These triggers require an explicit user query:

- **Trigger:** Query contains "why", "what did we decide", "previously", "last time" (or the equivalent in any language), or references past sprint/decision/ADR.
- **Command:** `python .sumela/memory-plugins/qdrant-session-memory/scripts/query-qdrant.py "<query>" --limit 3`
- **Threshold:** Top score ≥ 0.5 → read matching session summary file
- **Fallback:** If Qdrant unavailable or score < 0.5 → skip, let Tier-3 (_SEARCH_INDEX.md) handle it

### By developer / domain / date (session-summary metadata)
- **Trigger:** "what did developer X do last week", "which sessions touched the Card domain", "what happened between two dates".
- **Filters:** `--developer "<name>"`, `--domain <Domain>`, `--since YYYY-MM-DD`, `--until YYYY-MM-DD` (exact match on developer/domain; date range on `date_int`).
- **Filter-only listing** (all matches, not top-K semantic): pass `"*"` as the query:
  ```
  python .sumela/memory-plugins/qdrant-session-memory/scripts/query-qdrant.py "*" --developer "Ada" --since 2026-06-01
  ```
- **Authoritative fallback:** for exact commit-level attribution, `git log --author=… --since=…`. The summary captures the session narrative; git captures the commits.

## Session Ingestion
- After context-handoff, run:
  ```
  python .sumela/memory-plugins/qdrant-session-memory/scripts/session-ingest.py <path-to-session-summary.md>
  ```
- Requires: Ollama (qwen3-embedding:0.6b), Qdrant on localhost:6333
- If Qdrant unavailable: session summary remains as markdown only (no data loss)

## Code Ingestion (Background)
- Run periodically or after significant code changes:
  ```
  python .sumela/memory-plugins/qdrant-session-memory/scripts/ingest-code-to-qdrant.py
  ```
- Walks `src/` for code files (`.cs`, `.ts`, `.tsx`, `.py`, `.go`, `.rs`, `.java`, `.js`, `.jsx`)
- Excludes build artifacts, dependencies, generated files, and secrets
- Upserts into Qdrant `code_chunks` collection
- Configurable via env vars: `SRC_DIR`, `CODE_PATTERNS`, `CODE_CHUNKS_COLLECTION`

## Wiki Ingestion (Background)
- Run periodically or after wiki updates:
  ```
  python .sumela/memory-plugins/qdrant-session-memory/scripts/ingest-wiki-to-qdrant.py
  ```
- Walks `docs/second-brain/wiki/` for `.md` files (excluding special files like `_INDEX.md`, `_LOG.md`)
- Parses YAML frontmatter for metadata
- Upserts into Qdrant `wiki_pages` collection
- Configurable via env vars: `WIKI_DIR`, `WIKI_PAGES_COLLECTION`

## Scripts Reference

| Script | Purpose | When to Run |
|---|---|---|
| `setup-qdrant.py` | Create Qdrant collections | Once during setup |
| `session-ingest.py` | Ingest session summary | After each context-handoff |
| `query-qdrant.py` | Semantic search over sessions | On-demand (proactive + reactive) |
| `ingest-code-to-qdrant.py` | Ingest source code files | AUTO on pull: re-embeds just the changed files (incremental, background, post-merge/post-checkout `sumela_code_sync`); first build / empty collection promotes to a full walk. Force a full re-embed with `SUMELA_PULL_CODE_REINGEST=1`; pass `--changed-file <list>` for a manual incremental run |
| `ingest-wiki-to-qdrant.py` | Ingest wiki pages | After wiki updates; AUTO on pull when a curated page changed (post-merge/post-checkout `sumela_wiki_sync`) |
| `delete-from-qdrant.py` | Remove a deleted file's points by payload key | Auto on pull for wiki pages / code files removed upstream (orphan pruning) |
| `lib/memory_ingest.py` | Shared helpers (chunk, embed, ID) | Used by other scripts |

## Prerequisites
- Python 3.10+
- `pip install -r .sumela/memory-plugins/qdrant-session-memory/requirements.txt`
- Ollama running with `qwen3-embedding:0.6b` model
- Qdrant running on localhost:6333
