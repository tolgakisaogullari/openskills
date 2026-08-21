#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# tests/test_post_checkout_worktree.sh — regression test for the post-checkout
# fresh-clone vs. linked-worktree discrimination.
#
# `git worktree add` hands post-checkout an ALL-ZERO previous HEAD — the exact
# same signal `git clone` gives — but the two need OPPOSITE handling:
#
#   clone           -> nothing is indexed locally yet; diff HEAD against the empty
#                      tree so the whole tree is ingested (correct, intended).
#   worktree add    -> the Qdrant collections are namespaced PER PROJECT, so they
#                      are ALREADY populated from the main checkout. The same
#                      empty-tree diff re-embeds every tracked file for zero gain
#                      (field report: 17,387 files, Ollama pinned at 68% CPU).
#
# The discriminator is --git-dir != --git-common-dir, and it MUST be compared on
# normalized physical paths: git returns the two in MIXED forms depending on cwd —
# from a subdirectory of the MAIN checkout, --git-dir is absolute while
# --git-common-dir is "../.git", which a raw string compare misreads as a worktree.
#
# Git chdirs to the working-tree root before running a hook, so the hook itself
# never observes that cwd. Cases 4-5 therefore test the two layers separately:
# case 4 pins the hook's behaviour, and case 5 calls _sumela_is_linked_worktree
# DIRECTLY from a subdirectory — the level where the mixed forms actually appear,
# and the level whose contract must not depend on cwd. Dropping the normalization
# leaves cases 1-4 green and fails only case 5 (verified).
#
# The heavy sync helpers are stubbed, so this test never touches Qdrant, Ollama,
# graphify or the network — it asserts the hook's DECISION, not its side effects.
#
# Dependency-free (bash + git). Run from anywhere:  bash tests/test_post_checkout_worktree.sh
# Exit 0 = all assertions passed, 1 = a failure (printed above).
# -----------------------------------------------------------------------------
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="$REPO_ROOT/.sumela/git-hooks/post-checkout"
LIB="$REPO_ROOT/.sumela/git-hooks/_lib.sh"

if [ ! -f "$HOOK" ] || [ ! -f "$LIB" ]; then
  echo "SKIP: hooks not present ($HOOK)"; exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0 FAIL=0
ok()  { echo "  PASS  $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }

# --- a throwaway origin repo with two commits (a real HEAD move to check out) ---
ORIGIN="$WORK/origin"
mkdir -p "$ORIGIN"
git -C "$ORIGIN" init -q -b main
git -C "$ORIGIN" config user.email t@example.com
git -C "$ORIGIN" config user.name  Test
mkdir -p "$ORIGIN/scripts"
echo one > "$ORIGIN/a.txt"; echo helper > "$ORIGIN/scripts/s.sh"
git -C "$ORIGIN" add -A && git -C "$ORIGIN" commit -qm one
echo two > "$ORIGIN/b.txt"
git -C "$ORIGIN" add -A && git -C "$ORIGIN" commit -qm two

# --- hooks dir: the REAL post-checkout over a lib whose syncs are echo stubs ---
HP="$WORK/hooks"
mkdir -p "$HP"
cp "$HOOK" "$HP/post-checkout"
chmod +x "$HP/post-checkout"
cat > "$HP/_lib.sh" <<EOF
. "$LIB"
sumela_collections_migrate(){ echo "migrate"; return 0; }
sumela_memory_sync(){  echo "memory_sync"; }
sumela_wiki_sync(){    echo "wiki_sync"; }
sumela_code_sync(){    echo "code_sync"; }
sumela_graph_sync(){   echo "graph_sync"; }
sumela_update_check(){ echo "update_check"; }
EOF

# assert_ran <log-file> <needle> <expected: yes|no> <label>
assert_ran() {
  if grep -q "^$2\$" "$1" 2>/dev/null; then got=yes; else got=no; fi
  if [ "$got" = "$3" ]; then ok "$4"; else bad "$4 (expected $2=$3, got $got)"; fi
}

run() { git -c core.hooksPath="$HP" "$@"; }

echo "== 1. fresh clone — every sync runs (initial local population) =="
run clone -q "$ORIGIN" "$WORK/clone" > "$WORK/1.log" 2>&1
assert_ran "$WORK/1.log" code_sync   yes "clone re-embeds code_chunks"
assert_ran "$WORK/1.log" memory_sync yes "clone ingests chat_history"
assert_ran "$WORK/1.log" graph_sync  yes "clone builds the code graph"

echo "== 2. git worktree add — Qdrant syncs skipped, graph still built =="
git -C "$WORK/clone" -c core.hooksPath="$HP" worktree add -q --detach "$WORK/wt" HEAD \
  > "$WORK/2.log" 2>&1
assert_ran "$WORK/2.log" code_sync   no  "worktree add does NOT re-embed code_chunks"
assert_ran "$WORK/2.log" wiki_sync   no  "worktree add does NOT re-embed wiki_pages"
assert_ran "$WORK/2.log" memory_sync no  "worktree add does NOT re-ingest chat_history"
assert_ran "$WORK/2.log" migrate     no  "worktree add skips the collection migrate"
assert_ran "$WORK/2.log" graph_sync  no  "worktree add does NOT rebuild the main checkout's code graph"
assert_ran "$WORK/2.log" update_check yes "worktree add still runs the update check"

echo "== 3. real HEAD move INSIDE a linked worktree — normal full path =="
git -C "$WORK/wt" -c core.hooksPath="$HP" checkout -q --detach HEAD~1 > "$WORK/3.log" 2>&1
assert_ran "$WORK/3.log" code_sync yes "in-worktree checkout syncs normally (real prev HEAD)"

echo "== 4. real HEAD move in the MAIN checkout — normal full path =="
git -C "$WORK/clone" -c core.hooksPath="$HP" checkout -q --detach HEAD~1 > "$WORK/4.log" 2>&1
assert_ran "$WORK/4.log" code_sync yes "main-checkout checkout syncs normally"

# put the main checkout back on the branch before the helper-level cases
git -C "$WORK/clone" -c core.hooksPath="$HP" checkout -q main > /dev/null 2>&1

echo "== 5. _sumela_is_linked_worktree directly, incl. cwd = a SUBDIRECTORY =="
# This is where the mixed --git-dir / --git-common-dir forms actually occur. An
# un-normalized compare passes 5a/5c/5d and FAILS 5b — the whole point of this case.
# assert_kind <dir> <expected: linked|main> <label>
assert_kind() {
  local got
  got="$(cd "$1" && . "$LIB" >/dev/null 2>&1
         if _sumela_is_linked_worktree; then echo linked; else echo main; fi)"
  if [ "$got" = "$2" ]; then ok "$3"; else bad "$3 (expected $2, got $got)"; fi
}
assert_kind "$WORK/clone"         main   "5a main checkout root -> main"
assert_kind "$WORK/clone/scripts" main   "5b main checkout SUBDIR -> main (normalization guard)"
assert_kind "$WORK/wt"            linked "5c worktree root -> linked"
assert_kind "$WORK/wt/scripts"    linked "5d worktree SUBDIR -> linked"

echo "== 6. SUMELA_WORKTREE_SYNC=1 overrides the worktree skip =="
SUMELA_WORKTREE_SYNC=1 git -C "$WORK/clone" -c core.hooksPath="$HP" \
  worktree add -q --detach "$WORK/wt2" HEAD > "$WORK/6.log" 2>&1
assert_ran "$WORK/6.log" code_sync  yes "explicit opt-in re-enables the syncs in a worktree"
assert_ran "$WORK/6.log" graph_sync yes "explicit opt-in re-enables the graph rebuild"

echo "== 7. PRODUCTION wiring: relative core.hooksPath in a worktree =="
# The whole design rests on this: setup.sh wires a RELATIVE core.hooksPath, and git
# resolves it against the MAIN working tree — so `git worktree add` runs the MAIN
# checkout's hook, making SUMELA_INSTALL_ROOT (derived from $0) the main checkout, NOT
# the worktree. That is why graphify's output dir is main's and rebuilding it is waste.
# An absolute hooksPath (cases 1-6, needed to inject the stub lib) cannot show this.
mkdir -p "$WORK/clone/.relhooks"
cat > "$WORK/clone/.relhooks/post-checkout" <<'HOOK'
#!/usr/bin/env bash
libdir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
{ echo "hookdir=$libdir"; echo "cwd=$PWD"; } >> "$PROBE_OUT"
HOOK
chmod +x "$WORK/clone/.relhooks/post-checkout"
PROBE_OUT="$WORK/7.log"; export PROBE_OUT; : > "$PROBE_OUT"
git -C "$WORK/clone" -c core.hooksPath=.relhooks worktree add -q --detach "$WORK/wt3" HEAD
main_real="$(cd "$WORK/clone" && pwd -P)"
wt_real="$(cd "$WORK/wt3" && pwd -P)"
if grep -q "^hookdir=$main_real/.relhooks\$" "$PROBE_OUT"; then
  ok "worktree add runs the MAIN checkout's hook (install root = main checkout)"
else
  bad "worktree add runs the MAIN checkout's hook (got: $(grep '^hookdir=' "$PROBE_OUT"))"
fi
if grep -q "^cwd=$wt_real\$" "$PROBE_OUT"; then
  ok "…while cwd is the worktree (so cwd must NOT be used to locate the install)"
else
  bad "cwd is the worktree (got: $(grep '^cwd=' "$PROBE_OUT"))"
fi

echo
echo "  $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
