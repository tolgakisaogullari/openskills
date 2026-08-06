# Qdrant Session Memory Plugin

Tier-1 memory: semantic search over session summaries stored in Qdrant vector database.

## Prerequisites

- **Python 3.10+**
- **Ollama** running with `qwen3-embedding:0.6b` model
- **Qdrant** running on `localhost:6333` (default)

## Setup

```bash
# Install Python dependencies
pip install -r .sumela/memory-plugins/qdrant-session-memory/requirements.txt

# Bootstrap Qdrant collections
python .sumela/memory-plugins/qdrant-session-memory/scripts/setup-qdrant.py
```

## Configuration

All scripts accept CLI arguments and environment variables. CLI args take precedence.

| Setting | CLI Arg | Env Var | Default |
|---|---|---|---|
| Qdrant host | `--host` | `QDRANT_HOST` | `localhost` |
| Qdrant port | `--port` | `QDRANT_PORT` | `6333` |
| Ollama URL | `--ollama-url` | `OLLAMA_URL` | `http://localhost:11434` |
| Collection (logical base) | `--collection` | `QDRANT_COLLECTION` | `chat_history` |
| Collection prefix (override) | — | `SUMELA_COLLECTION_PREFIX` | derived per-install |
| Embedding batch size sent to Ollama | — | `SUMELA_EMBED_NUM_BATCH` | `8192` |
| Max tokens per embedded chunk | — | `SUMELA_EMBED_MAX_TOKENS` | 90% of `SUMELA_EMBED_NUM_BATCH` |
| Seconds to wait before retrying a failed embed | — | `SUMELA_EMBED_RETRY_DELAY` | `2` |
| Concurrent embed requests during a bulk ingest | — | `SUMELA_EMBED_MAX_WORKERS` | `4` |
| Failed heal attempts before an entry is retired | — | `SUMELA_HEAL_MAX_ATTEMPTS` | `3` |

`SUMELA_EMBED_NUM_BATCH` is the ceiling Ollama loads the model with; an embedding model is
non-causal, so a prompt that does not fit in ONE batch aborts the model runner and 500s
every concurrent request. `SUMELA_EMBED_MAX_TOKENS` is derived from it rather than set
independently, so lowering the batch cannot silently invalidate the chunk budget. Lower
`SUMELA_EMBED_MAX_WORKERS` on a memory-constrained host (4 is measured: throughput
saturates there and 8 buys nothing).

## Per-project collection isolation

One Qdrant instance is shared across every SumelaOS project on a machine. To keep
projects from intermingling (and to stop a prune in one project deleting a same-path
point in another), the three logical collections are **namespaced per project**:
the physical names are `{slug}__chat_history`, `{slug}__wiki_pages`,
`{slug}__code_chunks`, where `slug` is a stable per-install id (basename + a hash of
the install path), persisted once in `.sumela/_migration/collection-prefix`.

You keep using the **logical base names** everywhere (`--collection code_chunks`,
the agent's retrieval tiers, the git hooks) — every script resolves a base to the
project's physical collection automatically. Set `SUMELA_COLLECTION_PREFIX` to pin a
custom slug, or a per-base env var (`QDRANT_COLLECTION` / `WIKI_PAGES_COLLECTION` /
`CODE_CHUNKS_COLLECTION`) to pin an explicit full collection name.

**Existing installs** are migrated automatically by `scripts/update.sh` /
`scripts/setup-memory.sh` (and idempotently re-checked) via `migrate-collections.py`,
which either *adopts* legacy bare collections (zero-copy alias, when the data is
confidently this project's) or *rebuilds* fresh namespaced ones — never adopting
another project's data. Run it directly to inspect or force a choice:

```bash
python .sumela/memory-plugins/qdrant-session-memory/scripts/migrate-collections.py --dry-run
python .../migrate-collections.py --adopt     # force zero-copy adoption
python .../migrate-collections.py --rebuild   # force fresh rebuild
python .../migrate-collections.py --gc        # drop orphaned legacy bare collections
```

### Examples

```bash
# Use custom Qdrant instance
python .sumela/memory-plugins/qdrant-session-memory/scripts/query-qdrant.py "what did we decide" --host my-qdrant --port 6333

# Use environment variables
export QDRANT_HOST=192.168.1.100
export OLLAMA_URL=http://gpu-server:11434
python .sumela/memory-plugins/qdrant-session-memory/scripts/session-ingest.py session-summary.md
```

## Session metadata — queryable by developer / domain / date

`session-ingest.py` reads the session-summary frontmatter (see `_SCHEMA.md` Session Summary
Page Template) into the `chat_history` payload, so sessions are filterable, not just
semantically searchable. Fields: `developer`, `developer_email`, `domains` (list),
`spec_artifact`, `plan_artifact`, `date`/`date_int` (YYYYMMDD), `session_topics`.

`query-qdrant.py` adds matching filters:

```bash
# Filtered semantic search (combine a query with filters)
python .../query-qdrant.py "card limit bug" --developer "Ada Lovelace" --domain Card

# Filter-ONLY listing — everything a developer did in a date range (pass "*" as the query)
python .../query-qdrant.py "*" --developer "Ada Lovelace" --since 2026-06-01 --until 2026-06-07

# All Card-domain sessions since a date
python .../query-qdrant.py "*" --domain Card --since 2026-06-01
```

`--developer`/`--domain` are exact (case-sensitive) matches on the stored value; `--since`/`--until`
range on `date_int`. Summaries stamp `domains` in the canonical `<domain_scopes>` casing (e.g.
`Card`), so query with that casing (`--domain Card`). Notes: (1) summaries written before this
feature lack these fields and won't match a filter. New + edited summaries carry them going
forward; a pre-existing summary backfills ONLY if it's re-committed (the post-merge hook
re-ingests just the Added/Modified summaries in a pulled range, not the whole tree). To fully
backfill history once, manually re-ingest: `for f in docs/second-brain/wiki/session-summaries/*.md; do python .../session-ingest.py "$f"; done`. (2) when frontmatter omits
`developer`/`session_date`, the hook passes the commit's git author/date via
`--fallback-developer`/`--fallback-date`; (3) for exact commit-level attribution, `git log` is
the authoritative source — this captures the session narrative.

## Scripts

| Script | Purpose |
|---|---|
| `setup-qdrant.py` | Create Qdrant collections (idempotent) |
| `session-ingest.py` | Ingest a session summary markdown into Qdrant (developer/domain/date/spec/plan metadata) |
| `query-qdrant.py` | Semantic search + developer/domain/date filters (and filter-only listing) over session history |
| `ingest-code-to-qdrant.py` | Ingest source code into `code_chunks`. `--changed-file <list>` for an incremental run, `--heal` to repair entries the index is missing or only partially holds (the git hook passes this automatically) |
| `ingest-wiki-to-qdrant.py` | Ingest wiki pages into `wiki_pages` collection |
| `lib/memory_ingest.py` | Shared helpers (chunking, embedding, deterministic IDs) |

## Graceful Degradation

If Qdrant is unavailable:
- `session-ingest.py` prints a warning and exits 0 (markdown summary is preserved)
- `query-qdrant.py` prints a failure report and exits 1 (agent falls back to Tier-3)
- `ingest-code-to-qdrant.py` and `ingest-wiki-to-qdrant.py` print failure reports and exit 1

Exit codes for the two ingest scripts are three-valued — do NOT read non-zero as
"the backend is down":

| Code | Meaning | What to do |
|---|---|---|
| `0` | Index fully refreshed | nothing |
| `2` | Ran, but some entries are stale — an embed or upsert failed and those files were left untouched rather than half-written | re-run; the report names them |
| `1` | Could not run (Qdrant or Ollama unreachable, client missing) | fix the dependency named in the report |

A file is ingested **all-or-nothing**: if any of its chunks fails, its existing points are
left alone. An incomplete entry is worse than a stale one — retrieval would answer
confidently from a file it only half knows. `--heal` finds and repairs those entries, and
the git hook runs it on a schedule (see `.sumela/git-hooks/README.md`).
