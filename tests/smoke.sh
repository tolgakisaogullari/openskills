#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# tests/smoke.sh — end-to-end smoke test for the SumelaOS setup pipeline.
#
# The script layer (setup.sh self-modifies the project: renders templates,
# registers plugins, edits SKILL_REGISTRY.md, wires hooks) is the highest-risk,
# previously-untested code in the framework — CI only parse-checked it. This test
# runs setup against a throwaway COPY of the repo and asserts the contract:
#
#   1. setup --non-interactive succeeds
#   2. AGENTS.md + every selected IDE pointer (incl. .opencode) is generated
#   3. a selected plugin is registered EXACTLY once
#   4. validate-structure + reconcile --check pass on the result
#   5. a SECOND setup run is idempotent (no duplicate plugin entry, still valid)
#
# Dependency-free (bash + coreutils + python3 for reconcile). Run from anywhere:
#   bash tests/smoke.sh
# Exit 0 = all assertions passed, 1 = a failure (printed above).
# -----------------------------------------------------------------------------
set -uo pipefail

# Keep the smoke test hermetic: setup wires the memory plugins, but we don't want
# it to pip-install / start Docker / pull models here. (setup-memory.sh is covered
# by its own checks.)
export SUMELA_SKIP_MEMORY_SETUP=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

WORK="$(mktemp -d)"
WORK_TEAM=""
trap 'rm -rf "$WORK" ${WORK_TEAM:+"$WORK_TEAM"}' EXIT

PASS=0 FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }
# assert_file <path> <label>
assert_file() { if [ -f "$WORK/$1" ]; then ok "$2"; else bad "$2 (missing: $1)"; fi; }
# assert_count <needle> <file> <expected> <label>
assert_count() {
  local n; n="$(grep -cF "$1" "$WORK/$2" 2>/dev/null || echo 0)"
  if [ "$n" = "$3" ]; then ok "$4 (=$3)"; else bad "$4 (expected $3, got $n)"; fi
}

echo "SumelaOS smoke test"
echo "  repo:  $REPO_ROOT"
echo "  work:  $WORK"

# --- Stage a clean copy (no .git, no nested temp/test output) ----------------
# Copy tracked + untracked source but exclude .git so setup's git wiring starts fresh.
stage_copy() {
  # Exclude generated overlay (never in framework source; regenerated per fixture). Without
  # this, a maintainer's prior local `setup.sh` run would leak generated rule files into the
  # fixture and spuriously fail the domain-parity check. (AGENTS.md / RULE_REGISTRY.md are
  # already gitignored, but tar copies the working tree, so exclude them explicitly too.)
  ( cd "$REPO_ROOT" && tar --exclude='./.git' --exclude='./tests/.out' \
      --exclude='./AGENTS.md' --exclude='./.sumela/RULE_REGISTRY.md' \
      --exclude='./.sumela/rules/domains' --exclude='./.github/CODEOWNERS' \
      --exclude='./.sumela/rules/operational_excellence_maintenance.md' \
      --exclude='./.sumela/rules/backend_standards.md' \
      --exclude='./.sumela/rules/frontend_standards.md' \
      --exclude='./.sumela/rules/mobile_standards.md' \
      -cf - . ) | ( cd "$1" && tar -xf - )
  ( cd "$1" && git init -q && git add -A && git -c user.email=smoke@test -c user.name=smoke commit -qm init ) || true
}
stage_copy "$WORK"

run_setup() {
  ( cd "$WORK" && bash scripts/setup.sh --non-interactive \
      --project-name "SmokeProj" \
      --project-purpose "smoke test fixture" \
      --stacks "backend" \
      --plugins "qdrant-session-memory" \
      --ides "claude,cursor,opencode" \
      --governance "solo" ) >"$WORK/setup.log" 2>&1
}

echo ""
echo "Run 1 — fresh setup"
if run_setup; then ok "setup.sh --non-interactive exited 0"; else bad "setup.sh exited non-zero"; sed 's/^/    /' "$WORK/setup.log" | tail -25; fi

assert_file "AGENTS.md"                    "AGENTS.md generated"
assert_file "CLAUDE.md"                     "Claude pointer generated"
assert_file ".cursor/rules/00-agent.md"     "Cursor pointer generated"
assert_file ".opencode/AGENTS.md"           "OpenCode pointer generated"
assert_file ".sumela/RULE_REGISTRY.md"      "RULE_REGISTRY.md generated"
assert_count "<name>qdrant-session-memory</name>" ".sumela/SKILL_REGISTRY.md" 1 "plugin registered once"

# No unrendered placeholders should survive in the generated overlay.
if grep -rqF '{{' "$WORK/AGENTS.md" "$WORK/.opencode/AGENTS.md" 2>/dev/null; then
  bad "unrendered {{placeholder}} left in generated files"
else
  ok "no unrendered placeholders in generated files"
fi

echo ""
echo "Structure + registry validation"
if ( cd "$WORK" && bash scripts/validate-structure.sh --check-placeholders --post-setup ) >"$WORK/validate.log" 2>&1; then
  ok "validate-structure.sh passed (incl. --post-setup: hooks + gitignore + gitattributes landed)"
else
  bad "validate-structure.sh failed"; sed 's/^/    /' "$WORK/validate.log" | tail -20
fi
if command -v python3 >/dev/null 2>&1; then
  if ( cd "$WORK" && python3 scripts/reconcile-registry.py --check ) >"$WORK/reconcile.log" 2>&1; then
    ok "reconcile-registry --check in sync"
  else
    bad "reconcile-registry --check reported drift"; sed 's/^/    /' "$WORK/reconcile.log" | tail -10
  fi
else
  echo "  SKIP  reconcile-registry (python3 unavailable)"
fi

# Regression guard: get_repo_root() must resolve the repo root (not the plugin
# dir) in a vendored adoption layout, else Qdrant ingestion silently no-ops.
if command -v python3 >/dev/null 2>&1; then
  if python3 "$REPO_ROOT/tests/test_get_repo_root.py" >"$WORK/get_repo_root.log" 2>&1; then
    ok "get_repo_root resolves repo root across layouts"
  else
    bad "get_repo_root unit test failed"; sed 's/^/    /' "$WORK/get_repo_root.log" | tail -15
  fi
  # Extra-ingest-dirs config resolution + path validation (env/conf precedence,
  # reject absolute/escape/glob/symlink, skip-missing, dedupe).
  if python3 "$REPO_ROOT/tests/test_extra_ingest_dirs.py" >"$WORK/extra_ingest.log" 2>&1; then
    ok "extra ingest dirs: config resolution + path validation"
  else
    bad "extra ingest dirs unit test failed"; sed 's/^/    /' "$WORK/extra_ingest.log" | tail -20
  fi
  # Embedding input bounds: an over-long prompt aborts the Ollama model runner, which
  # 500s every concurrent request too — silently losing chunks from the index.
  if python3 "$REPO_ROOT/tests/test_embedding_bounds.py" >"$WORK/embed_bounds.log" 2>&1; then
    ok "embedding bounds: byte-based token cap, num_batch, retry"
  else
    bad "embedding bounds unit test failed"; sed 's/^/    /' "$WORK/embed_bounds.log" | tail -20
  fi
else
  echo "  SKIP  get_repo_root + extra-ingest unit tests (python3 unavailable)"
fi

echo ""
echo "Run 2 — idempotency (re-run must not duplicate)"
if run_setup; then ok "second setup.sh run exited 0"; else bad "second setup.sh run exited non-zero"; sed 's/^/    /' "$WORK/setup.log" | tail -25; fi
assert_count "<name>qdrant-session-memory</name>" ".sumela/SKILL_REGISTRY.md" 1 "plugin still registered exactly once after re-run"

echo ""
echo "Run 3 — team mode + domains (domain-scope generation + parity)"
WORK_TEAM="$(mktemp -d)"
stage_copy "$WORK_TEAM"
if ( cd "$WORK_TEAM" && bash scripts/setup.sh --non-interactive \
      --project-name "SmokeTeam" \
      --project-purpose "smoke team fixture" \
      --stacks "backend" \
      --ides "claude" \
      --governance "team" \
      --domains "Card, Payments" ) >"$WORK_TEAM/setup.log" 2>&1; then
  ok "team+domains setup exited 0"
else
  bad "team+domains setup exited non-zero"; sed 's/^/    /' "$WORK_TEAM/setup.log" | tail -25
fi
# Domain rule files are generated, slugified (Card -> card, "Payments" -> payments).
[ -f "$WORK_TEAM/.sumela/rules/domains/card.md" ]     && ok "domain rule card.md generated"     || bad "domain rule card.md missing"
[ -f "$WORK_TEAM/.sumela/rules/domains/payments.md" ] && ok "domain rule payments.md generated" || bad "domain rule payments.md missing"
# Exactly two registered domain rule paths + the scopes section. Count real
# <path> entries (the example comment uses <slug>, which this pattern won't match).
dom_n="$(grep -cE '<path>\.sumela/rules/domains/[^<]+</path>' "$WORK_TEAM/.sumela/RULE_REGISTRY.md" 2>/dev/null || echo 0)"
if [ "$dom_n" = "2" ]; then ok "2 registered domain rule paths (=2)"; else bad "expected 2 domain rule paths, got $dom_n"; fi
if grep -qF '<domain_scopes>' "$WORK_TEAM/.sumela/RULE_REGISTRY.md"; then ok "<domain_scopes> section present"; else bad "<domain_scopes> section missing"; fi
# The {{domain_name}} placeholder must be rendered (no stray braces) in the rule file.
if grep -qF '{{' "$WORK_TEAM/.sumela/rules/domains/card.md" 2>/dev/null; then bad "unrendered placeholder in domain rule file"; else ok "domain rule placeholders rendered"; fi
# Validation + parity must pass on the team project (domain rule <-> registry).
if ( cd "$WORK_TEAM" && bash scripts/validate-structure.sh --check-placeholders --post-setup ) >"$WORK_TEAM/validate.log" 2>&1; then
  ok "team: validate-structure passed (incl. domain parity)"
else
  bad "team: validate-structure failed"; sed 's/^/    /' "$WORK_TEAM/validate.log" | tail -20
fi
if command -v python3 >/dev/null 2>&1; then
  if ( cd "$WORK_TEAM" && python3 scripts/reconcile-registry.py --check ) >"$WORK_TEAM/reconcile.log" 2>&1; then
    ok "team: reconcile --check in sync (domain parity)"
  else
    bad "team: reconcile --check drift"; sed 's/^/    /' "$WORK_TEAM/reconcile.log" | tail -10
  fi
fi

echo ""
echo "-------------------------------------------"
echo "smoke: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
