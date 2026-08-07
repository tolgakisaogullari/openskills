# shellcheck shell=bash
# _lib.sh — shared logic for SumelaOS memory-sync git hooks.
#
# Re-ingests CHANGED session summaries into the developer's LOCAL Qdrant so that
# decisions a teammate committed become semantically searchable right after a
# `git pull`. The markdown summaries in git are the shared source of truth; each
# developer's Qdrant is a derived local cache, and these hooks are the
# cache-invalidation step ("source changed in my worktree -> refresh my cache").
#
# Design contract (all four must hold):
#   * incremental    — only summaries that changed in the pulled/checked-out range
#   * non-blocking    — ingestion runs in the background; never delays git
#   * best-effort     — missing plugin / down Qdrant -> skip silently, exit 0,
#                       never fail a git operation (the data stays in git)
#   * path-scoped     — fires only when files under the summaries dir changed
#
# This file also provides three more best-effort, non-blocking pull-time refreshers
# of LOCAL derived caches (so a teammate's pulled work becomes searchable/queryable
# on your machine) — none of which touch the tracked tree:
#   * sumela_graph_sync — graphify code graph (gitignored graph dir)
#   * sumela_wiki_sync  — Qdrant `wiki_pages` (re-ingest changed pages; prune removed)
#   * sumela_code_sync  — Qdrant `code_chunks` (prune removed always; re-embed the
#                         CHANGED files incrementally every pull; first build is full)
# All four prune orphans for files deleted upstream EXCEPT chat_history, where a
# removed summary is intentionally retained (deleting a file does not retract a
# past decision from memory).
#
# Opt out / tune per developer:
#   export SUMELA_DISABLE_MEMORY_SYNC=1   # session summaries -> chat_history (default on)
#   export SUMELA_DISABLE_GRAPH_SYNC=1    # graphify code graph              (default on)
#   export SUMELA_DISABLE_WIKI_SYNC=1     # Qdrant wiki_pages                (default on)
#   export SUMELA_DISABLE_CODE_SYNC=1     # Qdrant code_chunks: off entirely (no prune/embed)
#   export SUMELA_PULL_CODE_REINGEST=1    # Qdrant code_chunks: force a FULL tree re-embed
#   export SUMELA_DISABLE_UPDATE_CHECK=1  # don't probe upstream for a newer SumelaOS release
# Override paths/endpoints: SUMELA_SUMMARIES_DIR, WIKI_PATH, QDRANT_HOST, QDRANT_PORT

# Git's well-known empty-tree object (lets us diff a fresh clone's HEAD against
# "nothing", so every existing summary counts as added on first checkout).
SUMELA_EMPTY_TREE="4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# The SumelaOS install may live in a monorepo SUBDIR, not at the git root. Resolve
# the install root ONCE from this file's own location (it lives at
# <install>/.sumela/git-hooks/_lib.sh), independent of cwd or the git root — so a
# subpackage install syncs its OWN summaries, not paths under the repo root.
if [ -z "${SUMELA_INSTALL_ROOT:-}" ]; then
  _sumela_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
  SUMELA_INSTALL_ROOT="$(cd "$_sumela_lib_dir/../.." 2>/dev/null && pwd)"
fi

# Best-effort: read a single frontmatter scalar (first match) from a summary file.
# Only scans the leading `---`…`---` block so body text can't shadow a key.
# awk (not sed) for portability — BSD/macOS sed mishandles a one-line `{ s///p }`.
_sumela_fm() {  # $1 = file, $2 = key  → prints the value (may be empty)
  [ -f "$1" ] || return 0
  awk -v key="$2" '
    NR==1 && $0 !~ /^---[[:space:]]*$/ { exit }     # no frontmatter block
    NR>1  && $0 ~  /^---[[:space:]]*$/ { exit }     # end of frontmatter
    $0 ~ "^[[:space:]]*" key "[[:space:]]*:" {
      sub("^[[:space:]]*" key "[[:space:]]*:[[:space:]]*", ""); print; exit
    }
  ' "$1" 2>/dev/null
}

# Build one human-readable "who / when / what" line for a changed summary, so the
# pull log tells the user WHOSE work and WHICH tasks just became searchable.
#   who   = git author of the commit that brought this change in (from..to)
#   when  = frontmatter session_date, else the commit's short date
#   what  = filename (session id) + frontmatter session_topics
_sumela_summary_desc() {  # $1=repo $2=from $3=to $4=relpath
  local repo="$1" from="$2" to="$3" f="$4" full="$1/$4"
  local id who when topics
  id="$(basename "$f" .md)"
  who="$(git -C "$repo" log -1 --format='%an' "$from..$to" -- "$f" 2>/dev/null)"
  when="$(_sumela_fm "$full" session_date)"
  [ -z "$when" ] && when="$(git -C "$repo" log -1 --format='%ad' --date=short "$from..$to" -- "$f" 2>/dev/null)"
  topics="$(_sumela_fm "$full" session_topics | tr -d "[]\"'" )"
  local line="  • ${who:-unknown}"
  [ -n "$when" ]   && line="$line  ${when}"
  line="$line  ${id}"
  [ -n "$topics" ] && line="$line  [${topics}]"
  printf '%s\n' "$line"
}

sumela_memory_sync() {
  # $1 = "from" ref, $2 = "to" ref. Ingests summaries changed in from..to.
  local from="$1" to="$2"

  [ -n "${SUMELA_DISABLE_MEMORY_SYNC:-}" ] && return 0

  local repo
  repo="$(git rev-parse --show-toplevel 2>/dev/null)" || return 0
  [ -n "$repo" ] || return 0

  # Install dir (absolute) and its path within the repo (empty when at the root).
  # Derive install_rel via git, NOT a prefix-strip of pwd vs the resolved root — those
  # differ under a symlinked root (e.g. /tmp -> /private/tmp, /var -> /private/var) and
  # would silently degrade the diff pathspec to root-relative, skipping a subdir's summaries.
  local install="${SUMELA_INSTALL_ROOT:-$repo}"
  local install_rel
  install_rel="$(git -C "$install" rev-parse --show-prefix 2>/dev/null)"
  install_rel="${install_rel%/}"

  # Qdrant plugin not installed in this project -> nothing to sync.
  local ingest="$install/.sumela/memory-plugins/qdrant-session-memory/scripts/session-ingest.py"
  [ -f "$ingest" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0

  local summaries_dir="${SUMELA_SUMMARIES_DIR:-${WIKI_PATH:-docs/second-brain/wiki}/session-summaries}"
  [ -d "$install/$summaries_dir" ] || return 0

  # The diff runs at the git root, so the pathspec must be root-relative: prefix the
  # install's in-repo path when SumelaOS lives in a subdir.
  local pathspec="${install_rel:+$install_rel/}$summaries_dir"

  # Which summaries were Added/Modified in this range? (Deletes are ignored —
  # removing a summary file does not retract a past decision from memory.)
  # core.quotePath=false: keep non-ASCII paths raw (accented / Unicode filenames)
  # instead of git's default octal-escaped, double-quoted form — otherwise the
  # file-existence guard below would silently skip them. (Newline-in-filename
  # is still unsupported — unrealistic for committed summary slugs.)
  local changed
  changed="$(git -C "$repo" -c core.quotePath=false diff --name-only --diff-filter=AM "$from" "$to" -- "$pathspec" 2>/dev/null)" || return 0
  [ -n "$changed" ] || return 0

  # Prereq gate: Qdrant must be reachable. If not, skip silently — the summaries
  # are safely in git and the next pull (or a manual re-ingest) will catch up.
  local qhost="${QDRANT_HOST:-localhost}" qport="${QDRANT_PORT:-6333}"
  if ! command -v curl >/dev/null 2>&1 || \
     ! curl -fsS --max-time 2 "http://${qhost}:${qport}/readyz" >/dev/null 2>&1; then
    echo "sumela: memory sync skipped (Qdrant not reachable at ${qhost}:${qport})"
    return 0
  fi

  local count
  count="$(printf '%s\n' "$changed" | grep -c .)"
  local log="$install/.sumela/.memory-sync.log"
  # Keep the per-developer log bounded (truncate past ~512 KB).
  [ -f "$log" ] && [ "$(wc -c <"$log" 2>/dev/null || echo 0)" -gt 524288 ] && : >"$log"

  # Describe WHO and WHICH tasks each arriving summary belongs to (best-effort,
  # cheap for a normal pull). Built once; reused for the inline heads-up and the log.
  local details
  details="$(printf '%s\n' "$changed" | while IFS= read -r f; do
    [ -n "$f" ] || continue
    _sumela_summary_desc "$repo" "$from" "$to" "$f"
  done)"

  # Inline heads-up: header + up to 10 descriptors; the rest (and the ingest
  # results) go to the background log so git is never delayed.
  echo "sumela: ${count} session summary file(s) arrived in this pull — ingesting into local Qdrant in background:"
  printf '%s\n' "$details" | head -10
  [ "$count" -gt 10 ] && echo "  … and $((count - 10)) more — full list + ingest results: .sumela/.memory-sync.log"
  [ "$count" -le 10 ] && echo "  (ingest results: .sumela/.memory-sync.log)"

  # Background subshell so the git operation returns immediately. stdin/stdout/
  # stderr are detached (to a log) so git does not wait on the descriptors.
  (
    cd "$repo" || exit 0
    echo "===== memory-sync: ${count} file(s) @ $(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null) ====="
    echo "Source (who / when / task):"
    printf '%s\n' "$details"
    echo "-----"
    printf '%s\n' "$changed" | while IFS= read -r f; do
      [ -n "$f" ] || continue
      if [ ! -f "$repo/$f" ]; then
        echo "WARN: changed summary not found on disk, skipping: $f"
        continue
      fi
      echo ">>> ingesting: $f"
      # Attribution fallback: if the summary's frontmatter has no `developer`/`session_date`,
      # stamp the commit's git author + date (the work this summary records arrived in this
      # range). Frontmatter, when present, always wins inside session-ingest.py.
      fa="$(git -C "$repo" log -1 --format='%an' "$from..$to" -- "$f" 2>/dev/null)" || fa=""
      fd="$(git -C "$repo" log -1 --format='%ad' --date=short "$from..$to" -- "$f" 2>/dev/null)" || fd=""
      ingest_args=("$repo/$f")
      [ -n "$fa" ] && ingest_args+=(--fallback-developer "$fa")
      [ -n "$fd" ] && ingest_args+=(--fallback-date "$fd")
      python3 "$ingest" "${ingest_args[@]}" || echo "WARN: ingest failed for $f"
    done
    echo "===== memory-sync: done ====="
  ) >>"$log" 2>&1 </dev/null &

  return 0
}

# Refresh the LOCAL code graph (graphify) after a pull/checkout, so dependency &
# impact queries reflect the code that just arrived (teammates' work AND your own
# merged branch). Design contract (mirrors sumela_memory_sync):
#   * incremental  — only when actual CODE changed in the range (a doc/agent-only
#                    pull is skipped; the graph is unaffected by docs/ or .sumela/)
#   * non-blocking — graphify runs in the background; never delays git
#   * best-effort  — no plugin / no `graphify` CLI / no python3 -> skip silently, exit 0
#   * clean tree   — runs `auto-update-memory.py --graph-only`, which writes ONLY the
#                    gitignored graph dir (NO wiki sync, NO _LOG append), so a pull
#                    NEVER dirties the working tree
sumela_graph_sync() {
  # $1 = "from" ref, $2 = "to" ref.
  local from="$1" to="$2"

  [ -n "${SUMELA_DISABLE_GRAPH_SYNC:-}" ] && return 0

  local repo
  repo="$(git rev-parse --show-toplevel 2>/dev/null)" || return 0
  [ -n "$repo" ] || return 0

  # Resolve the install (may be a monorepo subdir) the same way memory-sync does.
  local install="${SUMELA_INSTALL_ROOT:-$repo}"

  # Self-gate: graphify plugin + CLI + python3 + the updater must all be present.
  [ -d "$install/.sumela/memory-plugins/graphify-code-graph" ] || return 0
  command -v graphify >/dev/null 2>&1 || return 0
  command -v python3  >/dev/null 2>&1 || return 0
  local updater="$install/scripts/auto-update-memory.py"
  [ -f "$updater" ] || return 0

  # Install path within the repo (root-relative; "" at the repo root), for scoping.
  local install_rel
  install_rel="$(git -C "$install" rev-parse --show-prefix 2>/dev/null)"
  install_rel="${install_rel%/}"
  local scope="${install_rel:+$install_rel/}"

  # Did real CODE change in this range? Limit to this install's subtree and exclude
  # the agent/wiki layer (docs/, .sumela/) via git pathspec. If nothing else changed,
  # the graph is unchanged — skip the rebuild.
  local changed_code
  changed_code="$(git -C "$repo" diff --name-only "$from" "$to" -- \
      "${install_rel:-.}" ":(exclude)${scope}docs" ":(exclude)${scope}.sumela" 2>/dev/null | head -1)"
  [ -n "$changed_code" ] || return 0

  local log="$install/.sumela/.graph-sync.log"
  [ -f "$log" ] && [ "$(wc -c <"$log" 2>/dev/null || echo 0)" -gt 524288 ] && : >"$log"
  echo "sumela: code changed in this pull — refreshing local code graph (graphify) in background (log: .sumela/.graph-sync.log)"

  # Background + detached so git returns immediately (graphify can be slow on a big repo).
  (
    cd "$install" || exit 0
    echo "===== graph-sync @ $(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null) ====="
    python3 "$updater" --project-root "$install" --graph-only || echo "WARN: graph refresh failed"
    echo "===== graph-sync: done ====="
  ) >>"$log" 2>&1 </dev/null &

  return 0
}

# Is the local Qdrant reachable? (cheap pre-check so we don't background an ingest
# that would just fail). Returns 0 if readyz responds within 2s, else 1.
_sumela_qdrant_up() {
  local qhost="${QDRANT_HOST:-localhost}" qport="${QDRANT_PORT:-6333}"
  command -v curl >/dev/null 2>&1 || return 1
  curl -fsS --max-time 2 "http://${qhost}:${qport}/readyz" >/dev/null 2>&1
}

# One-time migration of pre-namespacing Qdrant collections to this project's
# per-project namespaced names. Teammates get framework updates via a plain `git
# pull` (NOT scripts/update.sh, which pulls upstream SumelaOS), so the pull hook is
# the ONLY automatic surface that can migrate their LOCAL Qdrant — without this they
# would silently rebuild (re-embed) instead of adopting their existing collections.
# Same contract as the sync helpers: best-effort, never fails git, writes ONLY to the
# gitignored .sumela/_migration/. MUST run synchronously BEFORE the sync helpers in
# the hook so an ingest can't pre-create the namespaced collection and pre-empt the
# zero-copy adopt. Steady-state cost is a single marker stat (no python spawn):
#   * marker present + matches (instance,slug) -> instant no-op
#   * Qdrant down                              -> skip (retry next pull), no python
# Opt out: SUMELA_DISABLE_COLLECTION_MIGRATE=1.
#
# RETURN CONTRACT: 0 = safe to run the Qdrant sync helpers; 1 = migration DEFERRED for
# this pull (another migrate holds the lock, or a base is unresolved). The caller MUST
# skip the Qdrant syncs (memory/wiki/code) when this returns 1 — otherwise an ingest
# would create the namespaced collection empty and pre-empt the pending adopt. (Not-
# applicable cases — disabled / no plugin / Qdrant down / already-migrated — return 0;
# the syncs self-guard on Qdrant reachability anyway.)
sumela_collections_migrate() {
  [ -n "${SUMELA_DISABLE_COLLECTION_MIGRATE:-}" ] && return 0

  local repo; repo="$(git rev-parse --show-toplevel 2>/dev/null)" || return 0
  [ -n "$repo" ] || return 0
  local install="${SUMELA_INSTALL_ROOT:-$repo}"

  local mig="$install/.sumela/memory-plugins/qdrant-session-memory/scripts/migrate-collections.py"
  [ -f "$mig" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0

  # Cheap sentinel gate (local file reads only) — skip before any Qdrant probe or
  # python spawn once migration is done for this (instance, slug).
  local qhost="${QDRANT_HOST:-localhost}" qport="${QDRANT_PORT:-6333}"
  # Normalize the port to base-10 so it matches python's int(QDRANT_PORT) in the marker
  # (e.g. "06333" -> 6333). 10# forces decimal (bash would read a leading-zero value as
  # octal). Non-numeric ports are left as-is (python would have failed to write a marker).
  case "$qport" in (""|*[!0-9]*) : ;; (*) qport=$((10#$qport)) ;; esac
  local prefix_file="$install/.sumela/_migration/collection-prefix"
  local marker="$install/.sumela/_migration/.migrated"
  local slug=""
  [ -f "$prefix_file" ] && slug="$(tr -d '[:space:]' < "$prefix_file" 2>/dev/null)"
  if [ -n "$slug" ] && [ -f "$marker" ]; then
    local expected; expected="$(printf '%s:%s\t%s' "$qhost" "$qport" "$slug")"
    grep -qxF "$expected" "$marker" 2>/dev/null && return 0
  fi

  # Not yet migrated for this (instance, slug): only do real work when Qdrant is up.
  _sumela_qdrant_up || return 0

  local log="$install/.sumela/.memory-sync.log"
  [ -f "$log" ] && [ "$(wc -c <"$log" 2>/dev/null || echo 0)" -gt 524288 ] && : >"$log"
  echo "sumela: migrating local Qdrant memory collections to per-project namespacing (one-time; log: .sumela/.memory-sync.log)"
  # Synchronous (NOT backgrounded): must finish before the sync helpers run. It is
  # fast (samples + alias/create) and only does real work once; subsequent pulls hit
  # the marker gate above. Capture the exit code: 75 (EX_DEFERRED) means another migrate
  # is in progress or a base is unresolved -> tell the caller to skip the syncs.
  local rc=0
  { echo "===== collections-migrate @ $(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null) ====="; } >>"$log" 2>&1
  ( cd "$install" && python3 "$mig" ) >>"$log" 2>&1 </dev/null; rc=$?
  { echo "===== collections-migrate: done (rc=$rc) ====="; } >>"$log" 2>&1
  if [ "$rc" -eq 76 ]; then
    # qdrant-client too old/missing in this developer's venv (git pull can't fix the
    # venv). Surface a VISIBLE, actionable one-liner on the pull's stdout — without it
    # the only trace is the log nobody reads, and retrieval silently returns empty.
    # No marker was written, so the next pull AFTER upgrading migrates automatically.
    echo "sumela: ⚠ memory NOT migrated — qdrant-client is too old/missing for this project's scripts (need >=1.12)."
    echo "        Fix once: bash scripts/setup-memory.sh   (auto-migrates on your next pull after that; details: .sumela/.memory-sync.log)"
    return 1   # skip this pull's Qdrant syncs (they'd fail / pre-empt the pending migration)
  fi
  [ "$rc" -eq 75 ] && return 1   # DEFERRED (lock held / partial) -> caller skips Qdrant syncs this pull
  return 0
}

# Delete Qdrant points for files that were DELETED upstream (orphan cleanup). The
# ingest scripts only re-upsert files they still see on disk, so a removed file's
# embedding lingers and keeps surfacing in search — this drops it by payload key.
# Call from inside the background subshell (it shells out to python3 per file).
#   $1=install  $2=collection  $3=key field  $4=mode (path|basename-md)  $5=newline list
_sumela_delete_orphans() {
  local install="$1" collection="$2" keyfield="$3" mode="$4" list="$5"
  local del="$install/.sumela/memory-plugins/qdrant-session-memory/scripts/delete-from-qdrant.py"
  [ -f "$del" ] || return 0
  printf '%s\n' "$list" | while IFS= read -r f; do
    [ -n "$f" ] || continue
    local val="$f"
    [ "$mode" = "basename-md" ] && { val="$(basename "$f")"; val="${val%.md}"; }
    python3 "$del" --collection "$collection" --key "$keyfield" --value "$val" \
      || echo "WARN: orphan delete failed for $f"
  done
}

# Echo the project-configured EXTRA ingest dirs (repo-relative, one per line; empty
# if none). Single source of truth: the Python resolver does ALL config resolution
# and path validation, so the hook/setup/status never re-implement it (and so the
# three callers can never drift from each other). Best-effort: silent + empty if the
# resolver or python3 is absent.
_sumela_extra_ingest_dirs() {  # $1 = install root
  local install="$1"
  local resolver="$install/.sumela/memory-plugins/qdrant-session-memory/scripts/resolve-ingest-dirs.py"
  [ -f "$resolver" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  ( cd "$install" && python3 "$resolver" 2>/dev/null )
}

# Refresh the Qdrant `wiki_pages` collection after a pull, so semantic search over
# curated wiki pages (and any project-configured extra doc dirs) reflects teammates'
# just-pulled updates. The tracked markdown is already current (git), but its LOCAL
# embedding is not — this re-ingests it. Same contract: incremental (only when a
# tracked doc changed — wiki session-summaries and underscore-special/derived files
# are excluded), non-blocking, best-effort, and it writes ONLY to Qdrant (a local
# cache), never the tracked tree.
sumela_wiki_sync() {  # $1 = "from" ref, $2 = "to" ref
  local from="$1" to="$2"
  [ -n "${SUMELA_DISABLE_WIKI_SYNC:-}" ] && return 0

  local repo; repo="$(git rev-parse --show-toplevel 2>/dev/null)" || return 0
  [ -n "$repo" ] || return 0
  local install="${SUMELA_INSTALL_ROOT:-$repo}"
  local ingest="$install/.sumela/memory-plugins/qdrant-session-memory/scripts/ingest-wiki-to-qdrant.py"
  [ -f "$ingest" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0

  local install_rel; install_rel="$(git -C "$install" rev-parse --show-prefix 2>/dev/null)"; install_rel="${install_rel%/}"
  local scope="${install_rel:+$install_rel/}"
  local wikidir="${WIKI_PATH:-docs/second-brain/wiki}"

  # Which CURATED wiki pages changed / were removed? Drop session-summaries
  # (-> chat_history, handled by memory-sync) and the underscore-special/derived
  # files (_LOG/_INDEX/_SEARCH_INDEX/_SCHEMA) the ingest script itself excludes —
  # so e.g. a union-merged _LOG.md alone never triggers a pointless re-ingest.
  local wiki_chg wiki_del
  wiki_chg="$(git -C "$repo" -c core.quotePath=false diff --name-only --diff-filter=AM "$from" "$to" -- "${scope}${wikidir}" 2>/dev/null \
    | grep -vE '/session-summaries/' | grep -vE '/_[^/]*\.md$' | grep -E '\.md$')"
  wiki_del="$(git -C "$repo" -c core.quotePath=false diff --name-only --diff-filter=D "$from" "$to" -- "${scope}${wikidir}" 2>/dev/null \
    | grep -vE '/session-summaries/' | grep -vE '/_[^/]*\.md$' | grep -E '\.md$')"

  # Project-configured EXTRA doc dirs (default none). These are NOT wiki, so the
  # wiki-special filters above do NOT apply — every .md counts. Pathspecs go AFTER
  # the `--`, so a dir name can never be read as a git option.
  local extra_chg="" extra_del=""
  local extra_dirs; extra_dirs="$(_sumela_extra_ingest_dirs "$install")"
  if [ -n "$extra_dirs" ]; then
    local -a eps=()
    while IFS= read -r d; do [ -n "$d" ] && eps+=( "${scope}${d}" ); done <<< "$extra_dirs"
    if [ "${#eps[@]}" -gt 0 ]; then
      extra_chg="$(git -C "$repo" -c core.quotePath=false diff --name-only --diff-filter=AM "$from" "$to" -- "${eps[@]}" 2>/dev/null | grep -E '\.md$')"
      extra_del="$(git -C "$repo" -c core.quotePath=false diff --name-only --diff-filter=D "$from" "$to" -- "${eps[@]}" 2>/dev/null | grep -E '\.md$')"
    fi
  fi

  # Union (sort -u also dedupes a file reachable from both wiki and an extra dir).
  local changed deleted
  changed="$(printf '%s\n%s\n' "$wiki_chg" "$extra_chg" | grep -E '\.md$' | sort -u)"
  deleted="$(printf '%s\n%s\n' "$wiki_del" "$extra_del" | grep -E '\.md$' | sort -u)"
  [ -n "$changed$deleted" ] || return 0

  _sumela_qdrant_up || { echo "sumela: wiki_pages sync skipped (Qdrant not reachable)"; return 0; }

  local log="$install/.sumela/.memory-sync.log"
  local n_chg n_del
  n_chg="$(printf '%s\n' "$changed" | grep -c .)"; n_del="$(printf '%s\n' "$deleted" | grep -c .)"
  echo "sumela: wiki changed in this pull (${n_chg} updated, ${n_del} removed) — syncing Qdrant wiki_pages in background (log: .sumela/.memory-sync.log)"
  (
    cd "$install" || exit 0
    echo "===== wiki-sync @ $(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null) ====="
    # Removed pages: drop their orphaned embeddings (page_path = repo-relative path).
    [ -n "$deleted" ] && { echo "Pruning removed pages:"; _sumela_delete_orphans "$install" wiki_pages page_path path "$deleted"; }
    # Added/modified pages: re-ingest (whole-wiki walk; idempotent per-page upsert).
    [ -n "$changed" ] && { echo ">>> re-ingesting wiki pages"; python3 "$ingest" || echo "WARN: wiki ingest failed"; }
    echo "===== wiki-sync: done ====="
  ) >>"$log" 2>&1 </dev/null &
  return 0
}

# Maintain the Qdrant `code_chunks` collection after a pull, so semantic code
# search reflects teammates' just-pulled changes. Two parts, both cheap and both in
# the background — same contract as the wiki sync (non-blocking, best-effort, writes
# ONLY to the local Qdrant cache, never the tracked tree):
#   * PRUNE   — drop orphaned points for code files removed in this pull.
#   * RE-EMBED — re-embed just the CHANGED files (added/modified) every pull. The
#     ingest script self-creates the collection and, on a first-ever/empty index,
#     promotes the run to a FULL tree build so the whole corpus gets indexed once.
#   SUMELA_PULL_CODE_REINGEST=1 forces a FULL tree re-embed instead of incremental.
#   Disable everything (no prune, no embed): SUMELA_DISABLE_CODE_SYNC=1
sumela_code_sync() {  # $1 = "from" ref, $2 = "to" ref
  local from="$1" to="$2"
  [ -n "${SUMELA_DISABLE_CODE_SYNC:-}" ] && return 0

  local repo; repo="$(git rev-parse --show-toplevel 2>/dev/null)" || return 0
  [ -n "$repo" ] || return 0
  local install="${SUMELA_INSTALL_ROOT:-$repo}"
  local ingest="$install/.sumela/memory-plugins/qdrant-session-memory/scripts/ingest-code-to-qdrant.py"
  [ -f "$ingest" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0

  local install_rel; install_rel="$(git -C "$install" rev-parse --show-prefix 2>/dev/null)"; install_rel="${install_rel%/}"
  local scope="${install_rel:+$install_rel/}"

  # Code added/modified vs removed in this range (exclude docs/ + .sumela/).
  local changed deleted
  changed="$(git -C "$repo" -c core.quotePath=false diff --name-only --diff-filter=AM "$from" "$to" -- \
      "${install_rel:-.}" ":(exclude)${scope}docs" ":(exclude)${scope}.sumela" 2>/dev/null)"
  deleted="$(git -C "$repo" -c core.quotePath=false diff --name-only --diff-filter=D "$from" "$to" -- \
      "${install_rel:-.}" ":(exclude)${scope}docs" ":(exclude)${scope}.sumela" 2>/dev/null)"
  [ -n "$changed$deleted" ] || return 0

  _sumela_qdrant_up || { echo "sumela: code_chunks sync skipped (Qdrant not reachable)"; return 0; }

  local log="$install/.sumela/.memory-sync.log"

  # PRUNE removed code files (cheap) — always, in the background.
  if [ -n "$deleted" ]; then
    echo "sumela: $(printf '%s\n' "$deleted" | grep -c .) code file(s) removed in this pull — pruning Qdrant code_chunks orphans in background"
    ( cd "$install" || exit 0
      echo "===== code-sync(prune) @ $(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null) ====="
      _sumela_delete_orphans "$install" code_chunks file_path path "$deleted"
    ) >>"$log" 2>&1 </dev/null &
  fi

  # RE-EMBED — only when code was added/modified.
  [ -n "$changed" ] || return 0

  # SUMELA_PULL_CODE_REINGEST=1 -> full tree re-embed; otherwise incremental on just
  # the changed files. The changed list is handed to the ingest script via a temp
  # file (robust for any number of paths and detached background stdin).
  local full="${SUMELA_PULL_CODE_REINGEST:-}"
  local changed_list=""
  if [ -z "$full" ]; then
    changed_list="$(mktemp 2>/dev/null)" || changed_list=""
    if [ -n "$changed_list" ]; then
      printf '%s\n' "$changed" >"$changed_list"
    else
      full=1   # no temp file available -> fall back to a full re-embed
    fi
  fi

  if [ -n "$full" ]; then
    echo "sumela: re-embedding the full source tree into Qdrant code_chunks in background (log: .sumela/.memory-sync.log)"
  else
    echo "sumela: re-embedding $(printf '%s\n' "$changed" | grep -c .) changed code file(s) into Qdrant code_chunks in background (incremental; log: .sumela/.memory-sync.log)"
  fi

  ( trap '[ -n "$changed_list" ] && rm -f "$changed_list"' EXIT   # clean up even if cd below fails
    cd "$install" || exit 0
    echo "===== code-sync(ingest) @ $(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null) ====="
    local ok=0
    if [ -n "$full" ]; then
      python3 "$ingest" || ok=1
    else
      python3 "$ingest" --changed-file "$changed_list" || ok=1
    fi
    [ "$ok" -ne 0 ] && echo "WARN: code ingest failed"
    echo "===== code-sync: done ====="
  ) >>"$log" 2>&1 </dev/null &
  return 0
}

# Resolve the canonical SumelaOS upstream repo URL. Single source shared with the
# updater: a tracked `.sumela/upstream.conf` (first non-comment line) lets a fork
# point elsewhere; otherwise the shipped default. Keep this default in sync with
# scripts/update.sh REPO_URL_DEFAULT.
_sumela_upstream_repo() {  # $1 = install root
  local conf="$1/.sumela/upstream.conf" url=""
  [ -f "$conf" ] && url="$(grep -vE '^[[:space:]]*(#|$)' "$conf" 2>/dev/null | head -1 | tr -d '[:space:]')"
  [ -n "$url" ] || url="https://github.com/tolgakisaogullari/SumelaOS.git"
  printf '%s' "$url"
}

# Best-effort "a newer SumelaOS is available" notice. Piggybacks on the pull/checkout
# hooks. Refreshes the upstream's latest release tag at most once per interval
# (default 24h) in a DETACHED background probe (`git ls-remote --tags` — the caller's
# git auth, works for public AND private upstreams, no clone), cached in a gitignored
# file; the non-blocking one-liner is printed SYNCHRONOUSLY from that cache, so a
# newly-tagged release shows on the next pull. Silent on any failure (offline, no git,
# no tags yet); the probe is backgrounded + protocol-restricted so it can never block,
# prompt, or be hijacked. Opt out: SUMELA_DISABLE_UPDATE_CHECK=1.
sumela_update_check() {
  [ -n "${SUMELA_DISABLE_UPDATE_CHECK:-}" ] && return 0
  command -v git >/dev/null 2>&1 || return 0

  local repo install
  repo="$(git rev-parse --show-toplevel 2>/dev/null)" || return 0
  install="${SUMELA_INSTALL_ROOT:-$repo}"
  local verfile="$install/.sumela/VERSION"
  [ -f "$verfile" ] || return 0
  local local_ver; local_ver="$(tr -d '[:space:]' < "$verfile" 2>/dev/null)"
  [ -n "$local_ver" ] || return 0

  local cache="$install/.sumela/.update-check"      # gitignored: "<epoch>\t<remote_ver>"
  local interval="${SUMELA_UPDATE_CHECK_INTERVAL:-86400}"
  local now last=0 remote=""
  now="$(date +%s 2>/dev/null || echo 0)"
  if [ -f "$cache" ]; then
    last="$(awk 'NR==1{print $1}' "$cache" 2>/dev/null)"
    case "$last" in ""|*[!0-9]*) last=0 ;; esac          # tolerate a corrupt/garbled cache
    remote="$(awk 'NR==1{print $2}' "$cache" 2>/dev/null)"
    case "$remote" in *[!0-9.]*) remote="" ;; esac       # only a dotted-numeric version is usable
  fi

  # Refresh at most once per interval, in a DETACHED background probe so a slow or
  # unreachable upstream can NEVER block the pull (there is no portable `timeout` —
  # macOS ships neither timeout nor gtimeout). The notice below is printed
  # synchronously from whatever the cache already holds, so a freshly-published
  # release surfaces on the NEXT pull. The probe is hardened:
  #   * GIT_ALLOW_PROTOCOL — refuse ext::/file:: etc. so a poisoned upstream.conf
  #     (a tracked file, pullable from an untrusted branch) can't turn this auto-run
  #     probe into code execution via git's ext:: transport.
  #   * GIT_TERMINAL_PROMPT=0 + ssh BatchMode — never prompt for credentials.
  if [ "$((now - last))" -ge "$interval" ]; then
    local url; url="$(_sumela_upstream_repo "$install")"
    # Claim the interval slot now (sync) so concurrent/next pulls don't re-spawn.
    printf '%s\t%s\n' "$now" "$remote" > "$cache" 2>/dev/null
    ( fresh="$(env GIT_TERMINAL_PROMPT=0 GIT_ALLOW_PROTOCOL=https:ssh:git \
          GIT_SSH_COMMAND='ssh -oBatchMode=yes -oConnectTimeout=5' \
          GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=5 \
          git ls-remote --tags --refs "$url" 2>/dev/null \
        | sed -n 's#.*refs/tags/v\([0-9][0-9.]*\)$#\1#p' | sort -V | tail -1)"
      [ -n "$fresh" ] && printf '%s\t%s\n' "$now" "$fresh" > "$cache" 2>/dev/null
    ) >/dev/null 2>&1 </dev/null &
  fi

  # Notice (from cache) only if the upstream version is STRICTLY newer than local.
  [ -n "$remote" ] || return 0
  [ "$remote" = "$local_ver" ] && return 0
  [ "$(printf '%s\n%s\n' "$local_ver" "$remote" | sort -V | tail -1)" = "$remote" ] || return 0
  printf 'sumela: ⬆ SumelaOS %s is available (you are on %s). Update: bash scripts/update.sh  (silence: export SUMELA_DISABLE_UPDATE_CHECK=1)\n' "$remote" "$local_ver"
  return 0
}
