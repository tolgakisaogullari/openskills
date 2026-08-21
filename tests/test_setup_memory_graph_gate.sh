#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# test_setup_memory_graph_gate.sh — the graph-build success gate in setup-memory.sh.
#
# Regression guard for a silent-success hole: the build is reported OK only when
# graphify EXITS 0 **and** graphify-out/graph.json exists. Gating on the artifact
# alone looks harmless — until a failed rebuild runs on top of a stale graph.json
# left by an earlier build, which is the normal case for a per-developer,
# regenerable graphify-out/. The script would then print "Code graph built" and
# "Nothing left to do by hand" over a stale graph, and Tier-2 impact queries would
# answer from it believing it fresh.
#
# The other half of the contract is pinned too: above graphify's viz limit the
# missing graph.html is NOT a failure and NOT a to-do — graph.json is what the
# query path reads.
#
# Self-contained: builds a throwaway project, puts a STUB `graphify` on PATH (the
# real CLI is never needed), and runs the real script. No pytest, no network.
#
#   bash tests/test_setup_memory_graph_gate.sh
# -----------------------------------------------------------------------------
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FAILED=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAILED=$((FAILED + 1)); }
check() { if [ "$2" = "yes" ]; then pass "$1"; else fail "$1"; fi; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- a throwaway project the script will anchor to (nearest .sumela upward) ---
scaffold() {  # $1 = project dir
  local p="$1"
  mkdir -p "$p/scripts" "$p/.sumela/memory-plugins/graphify-code-graph" "$p/graphify-out"
  cp "$REPO_ROOT/scripts/setup-memory.sh" "$p/scripts/"
  # Empty requirements: the script pip-installs plugin deps unconditionally, and
  # this test must not touch the network.
  : > "$p/.sumela/memory-plugins/graphify-code-graph/requirements.txt"
  printf '<available_skills>\n</available_skills>\n' > "$p/.sumela/SKILL_REGISTRY.md"
}

# --- stub graphify: chosen exit code, optionally writes graph.json ---
stub_graphify() {  # $1 = bindir, $2 = exit code, $3 = "write" | "nowrite"
  mkdir -p "$1"
  cat > "$1/graphify" <<EOF
#!/usr/bin/env bash
# Log every invocation so a test can assert on what actually RAN — matching the
# word "cluster-only" in the script's OUTPUT would also match the escape-hatch
# hint it prints, which is text, not a run.
echo "\$@" >> "$1/invocations.log"
if [ "\$1" = "update" ] && [ "$3" = "write" ]; then
  mkdir -p graphify-out && printf '{"nodes": [], "links": []}' > graphify-out/graph.json
fi
[ "$2" -ne 0 ] && echo "fatal: stub graphify failure" >&2
exit $2
EOF
  chmod +x "$1/graphify"
}

run_setup() {  # $1 = project dir, $2 = bindir  → stdout+stderr
  ( cd "$1" && PATH="$2:$PATH" bash scripts/setup-memory.sh --yes \
      --plugins graphify-code-graph 2>&1 )
}

# =============================================================================
echo "setup-memory graph gate"

# 1. THE REGRESSION: failed rebuild on top of a STALE graph.json must NOT be "built".
p="$WORK/stale"; scaffold "$p"
printf '{"nodes": [], "links": []}' > "$p/graphify-out/graph.json"   # last week's build
stub_graphify "$WORK/bin-fail" 3 nowrite
out="$(run_setup "$p" "$WORK/bin-fail")"
case "$out" in *"build failed"*) r=yes ;; *) r=no ;; esac
check "failed build over a stale graph.json is reported as failed" "$r"
case "$out" in *"Code graph built"*) r=no ;; *) r=yes ;; esac
check "failed build over a stale graph.json is NOT reported as built" "$r"
case "$out" in *"need a one-time manual step"*) r=yes ;; *) r=no ;; esac
check "the failure is counted in the manual-step summary" "$r"

# 2. Successful build, no graph.html (over graphify's viz limit): OK, not a to-do.
p="$WORK/ok"; scaffold "$p"
stub_graphify "$WORK/bin-ok" 0 write
out="$(run_setup "$p" "$WORK/bin-ok")"
case "$out" in *"Code graph built"*) r=yes ;; *) r=no ;; esac
check "graph.json without graph.html is a successful build" "$r"
case "$out" in *"Nothing left to do by hand"*) r=yes ;; *) r=no ;; esac
check "a skipped viz leaves no manual step behind" "$r"
grep -q "cluster-only" "$WORK/bin-ok/invocations.log" 2>/dev/null && r=no || r=yes
check "the viz is not force-regenerated (graphify cluster-only never invoked)" "$r"
[ "$(grep -c . "$WORK/bin-ok/invocations.log" 2>/dev/null || echo 0)" = "1" ] && r=yes || r=no
check "graphify is invoked exactly once (no second clustering pass)" "$r"
[ -f "$p/graphify-out/graph.html" ] && r=no || r=yes
check "no graph.html is written" "$r"

# 3. Clean exit that produced nothing is still a failure (exit 0 is not the gate).
p="$WORK/empty"; scaffold "$p"
stub_graphify "$WORK/bin-empty" 0 nowrite
out="$(run_setup "$p" "$WORK/bin-empty")"
case "$out" in *"build failed"*) r=yes ;; *) r=no ;; esac
check "exit 0 with no graph.json is still a failure" "$r"

echo ""
if [ "$FAILED" -ne 0 ]; then
  echo "$FAILED assertion(s) FAILED"
  exit 1
fi
echo "All setup-memory graph-gate assertions passed"
