#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# setup-memory.sh — bring the optional memory stack up with the FEWEST manual
# steps: auto-run the cheap/safe parts (pip deps), and confirm-and-run the
# invasive ones (start Qdrant via Docker, pull the Ollama model, install the
# graphify CLI). Nothing is left as "go read the docs and do it yourself".
#
# It is idempotent and best-effort: anything already present is reused, and a
# missing prerequisite that cannot be auto-installed prints the EXACT command.
#
# Usage:
#   bash scripts/setup-memory.sh                       # interactive (default Yes on Enter)
#   bash scripts/setup-memory.sh --plugins qdrant-session-memory,graphify-code-graph
#   bash scripts/setup-memory.sh --yes                 # auto-confirm every action (agent/CI)
#   bash scripts/setup-memory.sh --non-interactive     # never prompt; skip invasive, print commands
#
# Env: QDRANT_HOST/PORT, OLLAMA_URL, SUMELA_EMBED_MODEL (default qwen3-embedding:0.6b)
# -----------------------------------------------------------------------------
set -uo pipefail

ASSUME_YES=false
NON_INTERACTIVE=false
PLUGINS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)          ASSUME_YES=true; shift ;;
    --non-interactive) NON_INTERACTIVE=true; shift ;;
    --plugins)         PLUGINS="$2"; shift 2 ;;
    *) echo "setup-memory: unknown option: $1" >&2; exit 2 ;;
  esac
done

# --- anchor to the nearest .sumela/ (works from any subdir) ---
ROOT="$(pwd)"
while [ "$ROOT" != "/" ] && [ ! -d "$ROOT/.sumela" ]; do ROOT="$(dirname "$ROOT")"; done
[ -d "$ROOT/.sumela" ] || { echo "setup-memory: no .sumela/ found from $(pwd) upward."; exit 1; }
cd "$ROOT"

PLUGDIR=".sumela/memory-plugins"
QHOST="${QDRANT_HOST:-localhost}"; QPORT="${QDRANT_PORT:-6333}"
EMBED_MODEL="${SUMELA_EMBED_MODEL:-qwen3-embedding:0.6b}"

if command -v tput >/dev/null 2>&1 && [ -t 1 ]; then
  B=$(tput bold); G=$(tput setaf 2); Y=$(tput setaf 3); C=$(tput setaf 6); R=$(tput sgr0)
else B="" G="" Y="" C="" R=""; fi
ok(){ echo "  ${G}ok${R}    $1"; }
todo(){ echo "  ${Y}todo${R}  $1"; }
info(){ echo "  ${C}info${R}  $1"; }
section(){ echo ""; echo "${B}$1${R}"; }
MANUAL=0; manual(){ todo "$1"; MANUAL=$((MANUAL+1)); }

# confirm <prompt> — default YES on Enter. --yes: always yes. --non-interactive or
# no TTY: NO (caller will print the exact command instead).
confirm() {
  $ASSUME_YES && return 0
  $NON_INTERACTIVE && return 1
  [ -t 1 ] && [ -r /dev/tty ] || return 1
  printf '  %s [Y/n] ' "$1" >/dev/tty
  local a=""; read -r a </dev/tty 2>/dev/null || a=""
  case "$a" in n|N|no|NO) return 1 ;; *) return 0 ;; esac   # default Yes on Enter
}

# Register a memory plugin in SKILL_REGISTRY.md if its files are present but it
# isn't registered yet — this is what makes `--plugins X` an "enable it (now or
# later)" command, not just a runtime step.
register_plugin() {  # $1 = plugin name
  local p="$1" reg=".sumela/SKILL_REGISTRY.md"
  [ -f "$reg" ] || return 0
  grep -q "<name>$p</name>" "$reg" 2>/dev/null && return 0   # already registered
  if SUMELA_REG_PLUGIN="$p" python3 - "$reg" <<'PY'
import os, sys
p = os.environ["SUMELA_REG_PLUGIN"]; path = sys.argv[1]
entry = ('\n<skill activation="lazy">\n<name>%s</name>\n'
         '<description>Memory plugin — see `.sumela/memory-plugins/%s/SKILL.md` for routing and prerequisites.</description>\n'
         '<path>.sumela/memory-plugins/%s/SKILL.md</path>\n</skill>\n') % (p, p, p)
s = open(path, encoding="utf-8").read()
tag = "</available_skills>"
if tag not in s:
    sys.exit(1)
open(path, "w", encoding="utf-8").write(s.replace(tag, entry + "\n" + tag, 1))
PY
  then ok "Registered $p in SKILL_REGISTRY.md"; else todo "register $p in SKILL_REGISTRY.md by hand"; fi
}

qdrant_up(){ command -v curl >/dev/null 2>&1 && curl -fsS --max-time 2 "http://${QHOST}:${QPORT}/readyz" >/dev/null 2>&1; }

# Which plugins? explicit --plugins, else whatever is present on disk.
want(){ case ",$PLUGINS," in *",$1,"*) return 0 ;; esac; [ -z "$PLUGINS" ] && [ -d "$PLUGDIR/$1" ]; }

echo "${B}SumelaOS memory setup${R}  ($ROOT)"
command -v python3 >/dev/null 2>&1 || { echo "python3 not found — the memory stack needs Python 3.10+."; exit 1; }
PIP="python3 -m pip"

# --- Enable explicitly-requested plugins (register now; the "add later" path) ---
# bootstrap copies all plugin files, so "add a plugin later" = run this with
# --plugins <name>: it registers the plugin (if present) and sets up its runtime.
if [ -n "$PLUGINS" ]; then
  section "Enable plugins"
  _saved_ifs="$IFS"; IFS=','
  for p in $PLUGINS; do
    p="$(echo "$p" | tr -d ' ')"; [ -n "$p" ] || continue
    if [ -d "$PLUGDIR/$p" ]; then register_plugin "$p"
    else manual "Plugin '$p' is not present under $PLUGDIR/ — fetch it first (re-run scripts/bootstrap.sh, which copies all plugins, or ask your agent to add it), then re-run this."; fi
  done
  IFS="$_saved_ifs"
fi

# =============================================================================
# Qdrant session memory
# =============================================================================
if want qdrant-session-memory && [ -d "$PLUGDIR/qdrant-session-memory" ]; then
  section "Qdrant session memory"

  # 1) Python deps — cheap + project-local -> AUTO (no prompt).
  req="$PLUGDIR/qdrant-session-memory/requirements.txt"
  if [ -f "$req" ]; then
    if $PIP install -q -r "$req" >/dev/null 2>&1; then ok "Python deps installed ($req)"
    else todo "pip install failed — run: $PIP install -r $req"; fi
  fi

  # 2) Ollama (embeddings). Installing Ollama itself is platform-specific -> guide.
  #    Pulling the model once Ollama exists is cheap -> confirm-and-run.
  if command -v ollama >/dev/null 2>&1; then
    if ollama list 2>/dev/null | grep -q "${EMBED_MODEL%%:*}"; then
      ok "Ollama model present ($EMBED_MODEL)"
    elif confirm "Pull the embedding model '$EMBED_MODEL' now (~hundreds of MB)?"; then
      if ollama pull "$EMBED_MODEL"; then ok "Pulled $EMBED_MODEL"; else todo "ollama pull failed — run: ollama pull $EMBED_MODEL"; fi
    else
      manual "Pull the embedding model: ollama pull $EMBED_MODEL"
    fi
  else
    manual "Install Ollama (https://ollama.com/download), then: ollama pull $EMBED_MODEL"
  fi

  # 3) Qdrant server. Reuse if running; else start via Docker (confirm); else guide.
  if qdrant_up; then
    ok "Qdrant reachable at ${QHOST}:${QPORT}"
  elif command -v docker >/dev/null 2>&1; then
    if confirm "Start a local Qdrant via Docker (qdrant/qdrant on :${QPORT})?"; then
      if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx sumela-qdrant; then
        docker start sumela-qdrant >/dev/null 2>&1
      else
        docker run -d --name sumela-qdrant -p "${QPORT}:6333" \
          -v sumela-qdrant-storage:/qdrant/storage qdrant/qdrant >/dev/null 2>&1
      fi
      # Wait for readiness (up to ~20s).
      n=0; while [ $n -lt 20 ] && ! qdrant_up; do n=$((n+1)); command -v sleep >/dev/null 2>&1 && sleep 1; done
      if qdrant_up; then ok "Qdrant started (container: sumela-qdrant)"; else todo "Qdrant container started but not ready yet — re-run once it is up"; fi
    else
      manual "Start Qdrant: docker run -d --name sumela-qdrant -p ${QPORT}:6333 -v sumela-qdrant-storage:/qdrant/storage qdrant/qdrant"
    fi
  else
    manual "Start Qdrant (install Docker, then): docker run -d -p ${QPORT}:6333 -v sumela-qdrant-storage:/qdrant/storage qdrant/qdrant"
  fi

  # 4) Create collections + seed the index, once Qdrant is reachable.
  if qdrant_up; then
    s="$PLUGDIR/qdrant-session-memory/scripts"
    # Migrate FIRST (idempotent, best-effort): if this machine has pre-namespacing
    # bare collections from an earlier install, adopt (alias) or rebuild them into
    # this project's namespaced names BEFORE setup-qdrant would create empty ones —
    # otherwise setup-qdrant pre-creates the namespaced collection and adoption of
    # the legacy data is lost. On a fresh machine this is a cheap no-op.
    [ -f "$s/migrate-collections.py" ] && { python3 "$s/migrate-collections.py" 2>&1 | sed 's/^/  /' || true; }
    [ -f "$s/setup-qdrant.py" ] && { python3 "$s/setup-qdrant.py" >/dev/null 2>&1 && ok "Qdrant collections ready" || todo "setup-qdrant.py failed — run: python3 $s/setup-qdrant.py"; }
    # Seed wiki_pages (cheap). code_chunks is left empty here; the first pull that
    # touches code builds it automatically (empty collection -> full walk), then
    # every later pull re-embeds just the changed files incrementally.
    # Seed if the wiki dir exists OR the project configured extra ingest dirs (the
    # ingest script self-discovers both via the same resolver).
    extra_dirs=""
    [ -f "$s/resolve-ingest-dirs.py" ] && extra_dirs="$(python3 "$s/resolve-ingest-dirs.py" 2>/dev/null)"
    if [ -f "$s/ingest-wiki-to-qdrant.py" ] && { [ -d "docs/second-brain/wiki" ] || [ -n "$extra_dirs" ]; }; then
      # Three-valued exit: 0 seeded, 2 seeded with stale pages, 1 could not run.
      # Collapsing 2 into the failure branch used to claim seeding was "skipped" when
      # nearly everything had landed — and "will sync on pull" is false for the pages
      # that actually failed, since the wiki sync only fires when a .md changes.
      wiki_log="$(python3 "$s/ingest-wiki-to-qdrant.py" 2>&1)"; wiki_rc=$?
      case "$wiki_rc" in
        0) ok "Seeded wiki_pages" ;;
        2) info "Seeded wiki_pages, but some pages were skipped — re-run to finish: python3 $s/ingest-wiki-to-qdrant.py"
           printf '%s\n' "$wiki_log" | grep -E '^\[warn\]' | head -5 ;;
        *) info "wiki_pages seeding skipped (will sync on pull)" ;;
      esac
    fi
    info "code_chunks left empty — built automatically on the next pull that touches code (or run now: python3 $s/ingest-code-to-qdrant.py)"
  fi
fi

# =============================================================================
# Graphify code graph
# =============================================================================
if want graphify-code-graph && [ -d "$PLUGDIR/graphify-code-graph" ]; then
  section "Graphify code graph"
  req="$PLUGDIR/graphify-code-graph/requirements.txt"
  [ -f "$req" ] && { $PIP install -q -r "$req" >/dev/null 2>&1 && ok "Python deps installed ($req)" || info "pip deps skipped"; }

  if command -v graphify >/dev/null 2>&1; then
    ok "graphify CLI present"
  elif command -v uv >/dev/null 2>&1 && confirm "Install the graphify CLI via 'uv tool install graphifyy'?"; then
    uv tool install graphifyy >/dev/null 2>&1 && ok "graphify installed (uv)" || todo "install failed — run: uv tool install graphifyy"
  elif command -v pipx >/dev/null 2>&1 && confirm "Install the graphify CLI via 'pipx install graphifyy'?"; then
    pipx install graphifyy >/dev/null 2>&1 && ok "graphify installed (pipx)" || todo "install failed — run: pipx install graphifyy"
  else
    manual "Install the graphify CLI: uv tool install graphifyy   (or: pipx install graphifyy)"
  fi

  if command -v graphify >/dev/null 2>&1; then
    if confirm "Build the code graph now (graphify update . — AST-only, no API key)?"; then
      # AST-only build: the `update <path>` subcommand is graphify's no-LLM code-only
      # path and works as the FIRST build too (no pre-existing graph.json needed —
      # verified on graphify 0.8.35). Bare `graphify .` attempts SEMANTIC extraction
      # of doc/paper/image files and hard-fails without an LLM API key, which this
      # plugin deliberately does not require (README: AST-only by design).
      # Output is intentionally NOT suppressed: graphify prints its own viz/limit
      # warnings (e.g. "graph has N nodes > 5000 limit — skipped graph.html") and we
      # must surface them. Success is gated on the ARTIFACT THE QUERY PATH READS —
      # graph.json — not on exit 0 and NOT on graph.html: Tier-2 (`query-graph.py`)
      # reads graph.json, the wiki insights page is generated from GRAPH_REPORT.md,
      # and nothing reads the viz. Above graphify's ~5000-node limit the viz is
      # skipped by design; forcing it re-ran clustering and wrote a several-hundred-MB
      # HTML nobody can open, so we report the skip and move on.
      graphify update .; build_rc=$?
      # BOTH terms matter: rc catches a failed rebuild, the artifact catches a "clean"
      # exit that produced nothing. Gating on the artifact ALONE would report success
      # whenever a stale graph.json from an earlier run survives a failed rebuild —
      # and on a large repo graph.json is the only gate left, so that is the default
      # case, not an edge case.
      if [ "$build_rc" -eq 0 ] && [ -f graphify-out/graph.json ]; then
        if [ -f graphify-out/graph.html ]; then
          ok "Code graph built (graphify-out/, incl. interactive graph.html)"
        else
          ok "Code graph built (graphify-out/graph.json — Tier-2 queries ready)"
          info "interactive graph.html not built (graphify skips the viz above ~5000 nodes) — nothing in the query path needs it. Want it anyway: GRAPHIFY_VIZ_NODE_LIMIT=100000000 graphify cluster-only .   (SUMELA_GRAPHIFY_VIZ=1 covers the pull-time sync only, not this script)"
        fi
      else
        manual "build failed (rc=$build_rc; graphify-out/graph.json $([ -f graphify-out/graph.json ] && echo "is stale — left as-is" || echo "missing")) — run: graphify update ."
      fi
    else
      manual "Build the code graph: graphify update ."
    fi
  fi
fi

# =============================================================================
section "Summary"
if [ "$MANUAL" -eq 0 ]; then
  echo "${G}${B}Memory stack ready.${R} Nothing left to do by hand."
else
  echo "${Y}${B}$MANUAL item(s) need a one-time manual step${R} — each is listed above with the exact command. Re-run 'bash scripts/setup-memory.sh' afterwards to finish wiring."
fi
exit 0
