# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Core framework version is tracked in `.sumela/VERSION` (consumed by `scripts/update.sh`).
Releases are also tagged `vX.Y.Z` (matching `.sumela/VERSION`) so the pull-time update
check can detect a newer upstream via `git ls-remote --tags`.

## [Unreleased]

### Added

- **`SUMELA_GRAPHIFY_VIZ` (v0.14.0)** — opt back in to graphify's interactive `graph.html` on the
  pull-time graph refresh when the graph is above graphify's ~5000-node viz limit. Unset by
  default (see the `Fixed` entry below for why forcing it was removed). Only an affirmative value
  turns it on: `0`, `false`, `no` and `off` all read as off, so a stale export cannot silently
  restore the cost. Documented in `.sumela/git-hooks/README.md` and the graphify plugin README.

- **Self-activating bootstrap on `@`-attach — reliable opt-in adoption without auto-loading
  (v0.12.0).** Field finding from a multi-IDE project where not every developer uses SumelaOS:
  attaching `.sumela/sumela-prompt.md` to a turn via `@`-reference did NOT run
  `<session_bootstrap>` — the agent treated the file as passive reference material and answered
  the literal task (e.g. "git pull") *unless* the user also said "follow the flow". Root cause:
  the bootstrap trigger was framed as session-state ("at the first user turn of every session"),
  which an attached document cannot reason about, so a concurrent literal task always won. A new
  `<activation>` block at the very top of `sumela-prompt.md` (before `<role>`) makes the file
  self-activating — its presence in context IS the activation signal, framed as a read-time
  imperative to execute now (not docs to read/remember) — and the `<session_bootstrap>` trigger
  now also fires "the moment this file enters your context (e.g. attached via `@`-reference)".
  Stays **opt-in**: no `CLAUDE.md`/`SessionStart` forcing, so developers who don't use the
  framework are untouched; activation happens only when one deliberately attaches the file.
  **Idempotent** — run once per session; if registries + eager skills are already in context it
  skips straight to the request, so a pinned/re-attached file does not re-run STEP 0–5 or
  re-emit STEP 2's one-per-session notifications. **Defers to `<authority_hierarchy>`** (an
  explicit user instruction may waive bootstrap; task triviality/urgency is NOT a waiver), keeps
  STEP 0 a non-blocking offer, and keeps bootstrap **silent** (tool calls visible, no narration/
  manifest). Design validated by two independent distinct-lens reviews (consistency +
  adversarial) before merge; verified end-to-end in a real project: attach-alone → full
  bootstrap; plain follow-up → no spurious re-bootstrap; re-attach in same session → idempotent
  skip; FAMILY A (`chat_history`) + FAMILY B (Graphify) retrieval routing intact.

- **Lifecycle-anchored memory retrieval — gates fire on workflow moments, not just
  question-semantics (v0.10.0).** Until now retrieval was driven purely by the question-
  semantic FAMILY A/B/C routing, so memory was consulted only when the agent recognized an
  information gap in what it was *asked* — never anchored to where it *was* in the workflow.
  A new `<workflow_retrieval_gates>` block in `sumela-prompt.md` adds three lifecycle anchors
  (additive to FAMILY A/B/C; all SOFT / best-effort, no enforcement hook): GATE 1 task-intake
  fires on `brainstorming` entry → consult Tier-3 `_SEARCH_INDEX` + Tier-1b `wiki_pages`
  before raw recon; GATE 2 impact-before-contract-change runs `query-graph.py <symbol>
  --impact` before changing a pre-existing symbol's signature (covers all six code-writing
  skills via the eager block plus a mirrored trigger in
  `subagent-driven-development/implementer-prompt.md`); GATE 3 find-code-by-behavior promotes
  `code_chunks` semantic search to a first-class route, ahead of a blind grep, when the symbol
  name is unknown. FAMILY A (`chat_history`) is demoted from MANDATORY to **best-effort**
  (skip when known-empty, note once per session) — routing re-aimed at where the data actually
  lives (`code_chunks`, `wiki_pages`); FAMILY B/C stay MANDATORY, with mirrors aligned in
  `using-second-brain` and `using-superpowers`. A script-side per-session dedup cache is added
  to `query-graph.py` and `query-qdrant.py` (gitignored `.sumela/.retrieval-cache.json`) so
  repeated identical queries within a session are skipped. **Language-agnostic by design:** no
  per-language parsing in core — contract-change judgment lives in the agent/graphify, and the
  gates stay deliberately soft prose with no enforcement hook (documented follow-up if soft
  prose underfires: a narrow git rename/delete-only mechanical leg).

- **Checkpoint vs Flow review mode — per-task review is now the user's choice (v0.9.0).**
  Field feedback from the first real project run: not every user wants a reviewer
  dispatch + approval stop after EVERY task; some prefer one comprehensive review at the
  end. `subagent-driven-development` (new Step 0) and `executing-plans` (Step 2) now ask
  ONCE at execution start: **Checkpoint mode** (default/recommended — per-task Stage-1
  spec + Stage-2 quality/security reviews and a per-task approval gate) or **Flow mode**
  (tasks run back-to-back with one-line progress notes; ONE comprehensive
  `requesting-code-review` covers everything at the end). Guardrails: Flow mode is valid
  ONLY via explicit user opt-in (never self-selected; no answer = Checkpoint), the final
  comprehensive review is mandatory in BOTH modes, plan `## Checkpoint:` blocks still run
  their verifications, and Flow mode still stops on verification failures, ambiguity, or
  auth/security-boundary tasks (those also get Stage-1/Stage-2 even in Flow mode). The
  `identity_and_behavior` per-task-gate rule, `requesting-code-review` cadence notes, and
  the `RULE_REGISTRY.md.template` rule description are updated to the same contract
  (already-installed projects keep their generated RULE_REGISTRY.md — the registry
  description drift there is cosmetic; the rule file itself is replaced by update.sh).

- **Phase-transition rule sync — STEP 4 is no longer bootstrap-only (v0.9.0).** Field
  feedback: rules loaded at session bootstrap (often `[Phase: <none-yet>]`) were never
  re-evaluated when `writing-plans` or an execution skill activated a new phase, so
  phase/stack/domain-conditional rules (e.g. `architecture_patterns` at planning) could
  silently stay unloaded. The canonical contract now lives in `sumela-prompt.md` STEP 4
  (re-run on EVERY phase/stack/domain change), `using-superpowers` STEP 4 enforces it on
  every skill load, and the four core phase skills (`brainstorming`, `writing-plans`,
  `executing-plans`, `subagent-driven-development`) carry their own PHASE RULE SYNC
  backstop step referencing `.sumela/RULE_REGISTRY.md` explicitly.

- **Information-gap routing FAMILY C — project-reference / how-to questions (v0.8.0).** The
  eager `<information_gap_routing>` block (loaded every session) only had FAMILY A
  (past-decision / "why" → Qdrant `chat_history`) and FAMILY B (call-graph / impact →
  Graphify); a present-tense PROJECT-REFERENCE / how-to question ("how do we scaffold a
  service here", "which port does X use", "is there a runbook for Y") matched neither, so
  no tier fired before the skill loaded — the agent risked answering from training or a
  blind grep instead of the project's curated docs. New FAMILY C routes these to Tier-3
  (`_SEARCH_INDEX.md` keyword, mandatory) escalating to Tier-1b (Qdrant `wiki_pages`
  semantic), with the Self-Check now auditing it too. Criteria are tight to avoid
  false positives (fires ONLY for project-specific reference/how-to; excludes general
  programming questions, in-progress edits, plain conversation, and anything already in
  A/B). Also closes two adjacent gaps: Tier-1c (`code_chunks` semantic) is now an eager
  escalation under FAMILY B when the graph is too narrow; and cold-start guards skip an
  empty/unreachable `chat_history` (FAMILY A) or unindexed docs (FAMILY C) with a one-line
  note instead of running empty queries. Trigger definitions stay canonical in the eager
  layer; `using-second-brain`'s decision tree is updated as operational detail only (no
  drift). Eager-layer growth: 18 lines added / 7 replaced (net +11).

### Fixed

- **The pull-time graph sync no longer forces an unreadable `graph.html` (v0.14.0).** Field
  finding from a consuming repo: every `git pull` that touched code pinned a core for minutes.
  The cause was not the graph rebuild — `graphify update`'s AST extraction is already parallel
  (`ProcessPoolExecutor`, workers = `cpu_count`) — but what SumelaOS did *after* it. graphify
  skips the interactive `graph.html` above its ~5000-node viz limit; both
  `auto-update-memory.py` and `setup-memory.{sh,ps1}` treated that skip as an incomplete build,
  raised `GRAPHIFY_VIZ_NODE_LIMIT` above the real node count, and re-ran `graphify cluster-only .`
  to force it. That is a **second full Louvain pass plus a huge HTML write on every pull**:
  measured on a 193k-node / 305k-edge repo, **~226 MB** of HTML that no browser can open, with
  clustering — single-threaded by nature, no flag parallelizes it — run twice.

  Nothing in the retrieval path reads that file. Tier-2 is `query-graph.py`, which reads
  `graph.json`; the `graphify-insights` wiki page is generated from `GRAPH_REPORT.md`. The viz
  was pure cost. The forcing is removed in all three places and **success now gates on
  `graph.json`** — the artifact the query path actually consumes — with the skipped viz reported
  as a NOTE (`INFO`, not `WARN`/`todo`: on a large repo it is the expected outcome, not a chore).
  Small repos are unaffected: under the limit graphify still writes the viz natively and that
  path still reports "incl. interactive graph.html".

  The capability is kept as an opt-in rather than deleted: `SUMELA_GRAPHIFY_VIZ=1` restores the
  forced regeneration for the pull-time sync, and the exact manual command
  (`GRAPHIFY_VIZ_NODE_LIMIT=<nodes+1000> graphify cluster-only .`) is printed in the note. Only a
  non-empty value other than `0` enables it, so a stale `SUMELA_GRAPHIFY_VIZ=0` in a shell profile
  cannot silently re-enable the cost.

  **The success gate is `graph.json` AND a zero exit code, not the artifact alone.** The review
  panel caught a first cut of this change that dropped the `build_rc`/`$built` term from
  `setup-memory.{sh,ps1}`: a failed `graphify update` running over a *stale* `graph.json` from an
  earlier build would then have printed "Code graph built" and "Nothing left to do by hand", and
  on a large repo — where `graph.html` never exists — that is the default path, not an edge case.
  Both terms are now required, matching `auto-update-memory.py`, which never dropped its rc check.

  The skipped-viz note only says what it can establish: above the limit it names the limit, below
  it it says the missing file is *unexpected* rather than prescribing the raise-the-limit remedy,
  and after a failed opt-in regeneration it says so instead of suggesting the flag the user
  already set.

  Covered by two suites, both wired into `tests/smoke.sh` and therefore CI:
  `tests/test_graph_viz_not_forced.py` (23 assertions; 5 fail against the old code — no
  `cluster-only`, no `GRAPHIFY_VIZ_NODE_LIMIT` in the child env, exactly one subprocess call, the
  note points at the opt-in, `=0` stays off) and `tests/test_setup_memory_graph_gate.sh`
  (9 assertions against a stubbed `graphify` on `PATH`, no real CLI needed; 4 fail against the old
  code). Between them they pin the gates that must NOT change: a missing `graph.html` is still a
  success, a missing `graph.json` is still a failure even when graphify exits 0, and a non-zero
  exit is still a failure even when a stale `graph.json` is on disk.

- **`git worktree add` no longer re-embeds the entire code base (v0.13.0).** Field finding from a
  consuming repo: creating a worktree queued **17,387 files** for re-embedding — the whole
  tree — pinning `ollama serve` at 68% CPU with no progress 28 minutes after the last log
  line, and every upsert failing with a 500 from `/api/embeddings`. Root cause: `git
  worktree add` hands `post-checkout` an **all-zero previous HEAD**, byte-for-byte the same
  signal `git clone` gives, and the hook mapped both to the empty-tree diff so every tracked
  file counted as "added". Correct for a clone; wrong for a worktree, where nothing the
  derived caches are built from has changed.

  `post-checkout` now discriminates via a new `_sumela_is_linked_worktree` (`--git-dir` vs
  `--git-common-dir`, compared on **normalized physical paths** — git returns them in mixed
  forms depending on cwd, `/abs/.git` vs `../.git` from a subdirectory, and a raw string
  compare misreads the main checkout as a worktree). On worktree creation **every** sync is
  skipped — the three Qdrant syncs, the collection migrate, and `graph_sync` — leaving only
  the rate-limited update check.

  `graph_sync` is skipped for the same reason as the rest, which took measuring to
  establish: `setup.sh` wires a **relative** `core.hooksPath`, git resolves it against the
  **main** working tree, so `git worktree add` runs the *main checkout's* hook and
  `SUMELA_INSTALL_ROOT` is the main checkout even though cwd is the worktree. Every derived
  cache — Qdrant collections and `graphify-out/` alike — belongs to the main checkout, whose
  tree a new worktree does not touch. An earlier revision of this fix kept `graph_sync` on
  the mistaken premise that the graph was per-worktree; it would have forced a full graphify
  rebuild of the main checkout on every `git worktree add`.

  Clone behaviour, in-worktree checkouts, and main-checkout checkouts are unchanged. Opt back
  in with `SUMELA_WORKTREE_SYNC=1` — its own variable, not a reuse of the permanently
  `export`ed `SUMELA_PULL_CODE_REINGEST`, which would have disabled the guard for anyone who
  set it once. The skip notice is gated on Qdrant actually being reachable (the plugin
  directory is tracked in git, so its presence proves nothing).

  Covered by `tests/test_post_checkout_worktree.sh` (7 cases / 19 assertions, wired into CI):
  5 assertions fail against the old hook; case 5b calls `_sumela_is_linked_worktree` directly
  from a subdirectory — the only level where the mixed path forms occur — and fails if the
  normalization is dropped; case 7 pins the relative-`core.hooksPath` install-root fact the
  whole design rests on, which the absolute path used by the other cases cannot show.

  Two pre-existing gaps found while doing this and documented in
  `.sumela/git-hooks/README.md` rather than papered over here: `chat_history` has no
  initial-population path outside a hooks-wired clone (every other hook path is
  range-incremental, and `setup-memory.sh` seeds only `wiki_pages`), and a checkout inside a
  worktree indexes the **main** checkout's content because the ingest resolves the
  worktree's changed paths under main's tree. The worktree empty-tree diff masked the first
  by accident — the same accident being fixed here.

- **`session-ingest.py` reported success after a failed ingest (v0.13.0).** The companion to
  the embedding fix above, found by reviewing this release: the two bulk ingests got the new
  three-valued exit contract, but the summary path kept `sys.exit(0)` on every failure — it
  printed `WARNING: Qdrant upsert failed` and still told its caller it had succeeded. That
  caller is the pull hook, so the observable result was the silent "memory did not update"
  the field report described. It now exits `1` when an embed or the upsert fails, matching
  the `PARTIAL` status it was already printing. Deliberately **two**-valued, not three: one
  summary is atomic (every embedding is computed before the old points are deleted), so there
  is no partial state for a `2` to describe. `_lib.sh` already invoked it as
  `python3 "$ingest" ... || echo "WARN: ingest failed"` inside a detached background
  subshell, so a non-zero exit is what the caller always expected and git is still never
  failed. It also gained the `ollama_preflight` its two siblings got, so a stopped backend is
  reported once instead of being rediscovered chunk by chunk while sleeping through the retry
  budget. The plugin README's "Graceful Degradation" section documented the old
  exit-0 behaviour and is corrected.

- **Python bytecode caches were not ignored in consuming repos (v0.13.0).** This repo's own
  `.gitignore` carries the generic `__pycache__/` rule, but `scripts/lib/sumela-gitignore.list`
  — the single source setup/update seed into a consuming project's `.gitignore` — did not, so
  running any memory script left untracked `__pycache__/` directories under `.sumela/` in every
  non-Python project (a .NET or Node consumer has no reason to ignore Python artifacts). Added
  as `.sumela/**/__pycache__/`, scoped to `.sumela/` on purpose: this is SumelaOS's own runtime
  artifact, not a rule about the consumer's source tree.

- **`.sumela/.heal-last` was committed as a tracked file (v0.13.0).** The withdrawal of the
  self-healing index correctly deleted the four `.gitignore` / `sumela-gitignore.list` entries
  for its runtime markers, but the same commit also committed one of those markers — a bare
  unix timestamp — leaving a stray tracked file that no code writes any more and that every
  consuming repo would inherit. Removed; the ignore-rule deletions stay, since nothing
  references those paths.

- **Embedding: an over-long chunk killed the Ollama model runner and silently punched
  holes in the index (v0.13.0).** Field finding from a consuming repo: one ingest run
  lost **2496 chunks across 617 files** while reporting `SUCCESS`. Ollama loads an
  embedding model with `n_batch = n_ubatch = 2048`, and an embedding model is
  **non-causal** — bidirectional attention needs the whole sequence in ONE ubatch, so
  llama.cpp cannot split an over-long prompt: it aborts (`SIGTRAP`), the runner process
  dies, and Ollama answers `500` to that request *and to every other request in flight*
  (`MAX_WORKERS = 4`, so one bad chunk took three innocent neighbours with it). Bisected
  and reproduced deterministically on `qwen3-embedding:0.6b`: 1970 tokens → `200`, 2104
  tokens → `500`, cache-cold. Nothing caught it because `chunk_text` budgets **words**
  while the limit is in **tokens** — the two differ by 2-7x, and a whitespace-free file
  (minified bundle, base64 blob, single-line generated file) collapses to ONE "word" and
  sails past any word budget. Four independent defects, all fixed:
  - `get_embedding` now sends `options.num_batch` (default 8192, `SUMELA_EMBED_NUM_BATCH`)
    — verified A/B, same input `500` → `200`.
  - `chunk_text` now hard-bounds every emitted chunk, splitting on a **UTF-8 byte**
    budget when words cannot. The bound is `bytes + 2`, provable because byte-level BPE
    (what Qwen uses) never emits a token covering less than one byte. A character-based
    bound was tried first and **shipped broken through two commits**: it under-counts
    every multi-byte script by up to 4x, and a review reproduced live 500s on ordinary
    CJK — which is the worst case in practice because CJK has no word spaces, so a
    paragraph collapses to one "word" and always takes the split path. The ASCII-only
    regression test could not see it. The token budget is **derived** from `num_batch`
    (90%) instead of configured beside it, so lowering one cannot silently invalidate
    the other, and the split accumulates whole characters so a codepoint is never cut.
  - `get_embedding` retries once: a neighbour killed as collateral damage succeeds on the
    second attempt after Ollama restarts the runner.
  - **Ingest is now all-or-nothing per file/page.** Previously a failed chunk was skipped
    and the survivors were upserted *after deleting every existing point for that file* —
    silently replacing a complete index entry with a partial one, and reporting `SUCCESS`.
    A silently incomplete entry is worse than a stale one: retrieval answers confidently
    from a file it only half knows. Files with any failed chunk are now left untouched,
    counted in the report, and the run exits non-zero.

  Also consolidates three private copies of `get_embedding` (and two of `chunk_text`) into
  `lib/memory_ingest.py`. `session-ingest.py` and `query-qdrant.py` each shadowed the
  shared helpers, so fixing the lib alone would have left the session-summary path **and
  the query path** — which embeds caller-supplied text — still crashing. Covered by a new
  dependency-free `tests/test_embedding_bounds.py` (wired into `tests/smoke.sh`, so CI
  runs it) that pins the real measurements, the whitespace-free case, `num_batch` presence
  and the retry contract.


- **graphify plugin: rebuilds hard-failed without an LLM key on repos with docs (v0.9.1).**
  Field report from a consuming repo (graphify CLI 0.8.35, 308 non-code doc/image files):
  `auto-update-memory.py` invoked `graphify .` / `graphify . --update`, which in current
  graphify attempts SEMANTIC extraction of doc/paper/image files and hard-fails without
  an LLM API key — so the pull/checkout auto-rebuild silently died (`graphify=FAIL`),
  contradicting the plugin's "AST-only by design, no API key" contract. Verified
  empirically and switched every invocation to the no-LLM `update <path>` subcommand,
  which also works as the FIRST build (confirmed with no pre-existing graph.json):
  `auto-update-memory.py` (`run_graphify` + docstring + `--graph-only` help),
  `setup-memory.sh` / `setup-memory.ps1` first-build blocks and fallback hints, the
  plugin README/SKILL.md (incl. the prerequisites line), `ADOPTION_GUIDE.md`,
  `sync-graphify-to-obsidian.py`'s error hint, and `context-handoff`. `--force` is
  deliberately NOT hard-coded (graphify's fewer-nodes guard protects against a broken
  parse wiping the graph); `GRAPHIFY_FORCE=1` in the env passes through to the
  subprocess for deleting refactors. (Adversarial review also killed a hint suggesting
  `update . --no-cluster` over an existing graph — verified destructive: it strips all
  community assignments from graph.json.)

- **graphify plugin: `query-graph.py` crashed on Python < 3.10 (v0.9.1).** The
  `dict | None` / `str | None` annotations were evaluated at class-definition time
  (`TypeError: unsupported operand type(s) for |`). Added
  `from __future__ import annotations` (matching `auto-update-memory.py`).

- **Agent could start coding without `brainstorming` — eager DEVELOPMENT GATE added (v0.9.0).**
  Field feedback: given a detailed task description, the agent skipped `brainstorming` and
  wrote code directly. Root cause: the no-code-before-approved-design HARD-GATE lived only
  INSIDE the lazy `brainstorming` skill — if the dispatcher never matched/loaded it, no
  eager HARD gate existed (`identity_and_behavior`'s "Plan-Driven Output" was only a soft
  nudge); and a detailed request reads as "design already chosen → skip". Fix at three
  layers: (1) `sumela-prompt.md` `<skill_resolution>` gains an eager DEVELOPMENT GATE —
  implementation code requires an active implementation skill working from an approved plan,
  `systematic-debugging` for bug fixes, or explicit user consent for a trivial direct edit;
  anything else is a dispatch failure that re-runs `using-superpowers` STEP 4; (2)
  `using-superpowers` intent anchors + forbidden rationalizations now state that a highly
  detailed request is brainstorming INPUT (it speeds the loop), never an approved spec;
  (3) the `brainstorming` routing `description` (frontmatter + SKILL_REGISTRY, in parity)
  says the same, so the dispatcher match itself can no longer be rationalized away.

- **`secure-coding-standard` was not loaded during planning (v0.9.0).** Field feedback:
  a plan was written without the standard in context. `writing-plans` (new Step 0) and
  `brainstorming` (new Step 0) now MANDATE reading
  `.sumela/skills/secure-coding-standard/SKILL.md` before any spec/plan output — for
  EVERY plan, not just "security-flavored" ones. The skill's routing `description`
  (frontmatter + `SKILL_REGISTRY.md`, kept in parity) now advertises the plan-time
  entry point, and `sumela-prompt.md` `<skill_resolution>` SECURITY MANDATE is aligned
  (unconditional for code work; the sensitive-surface list no longer reads as the only
  trigger). `executing-plans` Step 2 likewise loads it unconditionally.

- **Specs/plans invisible in git and the IDE Changes view (v0.9.0).** Root cause found
  in a real install: the standard VisualStudio/.NET `.gitignore` ships a generic
  `artifacts/` build-output pattern that matches ANY directory named `artifacts` — so
  `docs/second-brain/artifacts/{specs,plans}/` files never appeared in `git status` or
  the Source Control Changes panel. Three-layer fix: (1)
  `scripts/lib/sumela-gitignore.list` (single source for setup/update, both shells)
  gains `!docs/second-brain/artifacts/` + `!docs/second-brain/artifacts/**` re-include
  negations, appended at the end of `.gitignore` where they win; (2) `init-sumela`
  Step 3.6b now reads that same `.list` (per-line guards) instead of its own drifted
  inline copy — also picking up the previously missing `.sumela/.update-check`; (3)
  `brainstorming`/`writing-plans` gain a mandatory post-save VISIBILITY CHECK: decide on
  `git check-ignore -q` EXIT CODE only (exit 1 = visible = success; `-v` output alone is
  misleading because it also prints the `!…` negation as a "match"), remediate by
  re-appending the negations at the END of the install-root `.gitignore`, and STOP with
  a warning if a parent dir (e.g. `docs/`) is itself ignored. Artifacts must also be
  written with the IDE's file-write tool so the IDE change tracker registers them.

### Security

- **Hardened the pull-time update check (v0.7.1).** Post-review fixes to the v0.7.0
  `sumela_update_check`: (1) the upstream tag probe now runs in a **detached background**
  process and prints the notice synchronously from cache — macOS ships no `timeout`, and
  the previous inline `git ls-remote` could stall a `git pull` on an unreachable/slow
  upstream (a fresh release now surfaces on the *next* pull instead); (2) the probe (and
  the updater's clone) run with `GIT_ALLOW_PROTOCOL=https:ssh:git` so a poisoned (tracked)
  `.sumela/upstream.conf` can't execute code via git's `ext::`/`file::` transport, plus
  `GIT_TERMINAL_PROMPT=0` + ssh `BatchMode` so it never prompts for credentials;
  (3) a corrupt/garbled cache version field is ignored instead of surfacing in the notice.
  README documents the trust boundary (CODEOWNERS-protect `.sumela/upstream.conf`).

### Added

- **Pull-time "newer SumelaOS available" notice.** Adopters now find out automatically
  when the framework has a newer release instead of having to remember to run the
  updater. `post-merge`/`post-checkout` run a new best-effort `sumela_update_check`
  (`.sumela/git-hooks/_lib.sh`): it lists the upstream's release tags via
  `git ls-remote --tags` (the developer's own git auth — works for public AND private
  upstreams, no clone) and, if a newer `vX.Y.Z` than `.sumela/VERSION` exists, prints a
  one-line non-blocking notice pointing at `bash scripts/update.sh`. The probe is
  rate-limited to once per `SUMELA_UPDATE_CHECK_INTERVAL` (default 24h) via the
  gitignored `.sumela/.update-check` cache; the notice keeps showing every pull (from
  cache) until you update. Best-effort: offline / no git / no upstream tags → silent,
  never blocks a git op. Opt out with `SUMELA_DISABLE_UPDATE_CHECK=1`. The upstream URL
  lives in a new tracked `.sumela/upstream.conf` (single source, fork-overridable) that
  `scripts/update.{sh,ps1}` now also read. Releases are tagged `vX.Y.Z` from now on so
  the check has something to compare against.

- **`post-commit` hook — memory sync survives conflicted merges.** A conflicted
  `git merge`/`git pull` does NOT run `post-merge` (git stops at the conflict and runs
  the COMMIT hooks on the manual resolution commit instead), so until now a conflicted
  pull never refreshed the local Qdrant `code_chunks`/`chat_history`/`wiki_pages` or the
  code graph — a teammate's conflicting changes silently never reached your semantic
  index. The new `post-commit` closes that gap: it runs the same four sync functions as
  `post-merge` (for `HEAD^1..HEAD`) but ONLY when HEAD is a merge commit (≥2 parents),
  and is an immediate `exit 0` no-op on every ordinary commit. No double-firing — a clean
  merge creates its commit without running commit hooks (only `post-merge` fires); a
  conflicted merge skips `post-merge` and only `post-commit` fires — verified empirically
  on git 2.x. Known gaps documented (not conflated): `git merge --squash` (single-parent,
  not detectable by parent count) and `git pull --rebase` (not a merge) remain covered by
  the next clean merge/checkout + session bootstrap; `git commit --amend` on a merge commit
  harmlessly re-runs the idempotent sync. Wired through `setup.{sh,ps1}` (direct + monorepo
  dispatcher), `update.sh`, the CI shell-lint loop, and `.sumela/git-hooks/README.md`.

- **Extra documentation ingest paths** — adopting projects can index authoritative docs that
  live OUTSIDE `docs/second-brain/wiki/` (architecture docs, ADR dirs, API references) into the
  same Qdrant `wiki_pages` Tier-1 index, on the same pull-time/background/gated terms as the
  wiki — without copying them into the wiki (no drift). Mechanism in the framework, policy in
  the consumer: the framework ships an EMPTY default and NO project-specific paths. Configure
  per project via `.sumela/ingest.conf` (tracked, one repo-relative dir per line; copy the
  shipped `.sumela/ingest.conf.example`) or the `EXTRA_INGEST_DIRS` env var (comma/colon-
  separated; wins when set). A single resolver (`lib/memory_ingest.get_extra_ingest_dirs`,
  exposed to bash/PowerShell via `resolve-ingest-dirs.py`) does all resolution + validation so
  the three callers never drift: paths must be repo-relative and resolve to a real dir strictly
  inside the repo (absolute / `~` / `..` / globs / drive-letters / leading-dash / symlink-escape
  all rejected; missing skipped with a warning, never fails a pull). Docs only — `.md` files,
  symlink-safe walk (no following dir symlinks; per-file repo-containment re-check), deduped by
  resolved path; the code corpus is untouched. A per-run file cap (`EXTRA_INGEST_MAX_FILES`,
  default 5000) plus `.git`/`node_modules`/`vendor` skips bound the background re-embed.
  `sumela_wiki_sync` expands its change scope + orphan-prune to the extra dirs; `setup-memory`
  seeds them on first bring-up; `status.{sh,ps1}` report them. Documented in `ADOPTION_GUIDE.md`
  (incl. the trust note: a tracked path is a team-wide decision — CODEOWNERS-protect in team
  mode). Covered by `tests/test_extra_ingest_dirs.py`.

- **Rich, queryable session memory** — session summaries now carry structured metadata so
  memory answers "which developer did what, in which domain, when". A canonical
  `session-summary` page type + template lands in `_SCHEMA.md` (frontmatter: `developer`,
  `developer_email`, `domains`, `spec_artifact`, `plan_artifact`, `session_date`,
  `session_topics`; required detailed sections). `session-ingest.py` ingests these into the
  `chat_history` payload (incl. `date_int` for range filters); `query-qdrant.py` gains
  `--developer` / `--domain` / `--since` / `--until` filters plus a filter-only listing mode
  (`"*"` query → all matches via scroll, not top-K). The post-merge hook passes the commit's
  git author/date as `--fallback-developer`/`--fallback-date` so even un-stamped summaries are
  attributed. `sumela-prompt.md` routes who/when/domain questions to these filters (git log as
  the authoritative commit-level fallback).
- **Session summary written at task completion, not only on handoff** — the
  `<session_summary_protocol>` is now canonical in `using-second-brain` (single source);
  `context-handoff` references it, and `finishing-a-development-branch` Step 7 also writes +
  ingests a session summary. Previously a clean task finish with no context-pressure handoff
  left no conversational `chat_history` record (only curated-wiki ADR/commit-log updates).

- **Business-domain rule scope** — a third rule axis, orthogonal to stack scope.
  In team mode, setup / `/initSumela` asks the project's domain taxonomy (e.g. Card,
  Payments); each domain gets a tracked `RULE_REGISTRY.md` `<domain_scopes>` entry +
  a `.sumela/rules/domains/<slug>.md` rule file (generated from a new
  `domain_standards.md.empty` template). Which domain(s) a developer works in is
  per-developer and untracked (`.sumela/local.md` `domains:`); `sumela-prompt.md`
  STEP 4 loads the union of matching domain rules (a developer can be `backend` +
  `Card` at once), warning-and-skipping any domain not in the taxonomy. The Context
  Manifest gains a `[Domain: …]` header. `reconcile-registry.py` and
  `validate-structure.sh` now enforce domain rule↔registry parity (and `--stats`
  reports `domain_rules`).
- **`/onboardSumela` teammate onboarding** — a new skill (the single source of truth)
  for a developer who pulls an already-installed repo. It wires git hooks, sets the
  per-developer interaction language + domains in `.sumela/local.md`, and offers the
  optional memory runtime — without re-running install or touching team-wide config
  (use `/initSumela` only for first-time install). A non-nagging `STEP 0` onboarding
  gate in `sumela-prompt.md` detects a fresh clone (hooks unwired AND no `local.md`)
  and offers it once, deferring to the skill rather than duplicating its steps.
- **Parallel code-review panel** — `requesting-code-review` now dispatches three
  lane reviewers concurrently instead of one generalist: Correctness & Security
  (incl. auth/credential token lifecycle + security-boundary tests), Design &
  Contracts (conventions, architecture, API/contract stability, backward-compat),
  and Integration & Operations (cross-module impact via graphify, performance,
  data/persistence, observability/rollback, test coverage). The orchestrator then
  synthesizes the lanes — dedupes overlapping findings, surfaces `CONFLICT`s, and
  applies an AND-gate (any lane's Critical blocks commit/merge). New lane templates:
  `reviewer-correctness-security.md`, `reviewer-design-contracts.md`,
  `reviewer-integration-ops.md`; the legacy single-reviewer `code-reviewer.md` is
  retained as an IDE/degraded fallback. SDD's final review (Step 5) inherits the
  panel automatically; its per-task Stage-1/Stage-2 reviews are unchanged.
- **`reconcile-registry.py --stats`** — prints the canonical skill counts
  (`skill_workflows`, `loadable_skills`, `plugin_skills`) as the single source of
  truth for any number quoted in docs.
- **README skill-count drift guard** — `validate-structure.sh` compares the README
  `<!-- sumela:skill-count -->` marker against `--stats` and fails on mismatch, so
  the documented count can never silently diverge when a skill is added/removed
  (silent no-op in user projects, where README isn't present).
- **"How SumelaOS Extends Superpowers" README section** — an evidence-based,
  honest side-by-side vs [obra/superpowers](https://github.com/obra/superpowers)
  (21 vs 14 skill workflows, the review panel, rules/memory/governance layers — and
  where Superpowers is still broader on harness count).

### Changed

- **Context Manifest triggers narrowed** — the manifest now prints only on explicit
  user request (`/context`) and immediately before high-stakes actions (commit,
  code-review dispatch, finishing a branch, shipping, `/evolve`). It no longer
  prints at session start or on every phase transition, removing the "first output
  must be the manifest" mandate. Cuts recurring output tokens and per-turn latency
  while keeping the GAP-visibility checkpoint where it matters; spec trimmed in
  `sumela-prompt.md`. Dependent references (using-superpowers, RULE_REGISTRY
  template, AGENTS.md template, init-sumela, README) updated to match.

- **Qdrant `code_chunks` now refreshes incrementally on every pull, automatically.**
  `sumela_code_sync` previously gated the heavy whole-tree re-embed behind a 14-day
  staleness check that PROMPTED `[y/N]` on an interactive pull (default No) or printed
  a notice on a non-interactive one — so semantic code search routinely drifted up to
  two weeks stale on an active repo. It now re-embeds just the files that changed in the
  pull (`ingest-code-to-qdrant.py --changed-file <list>`), in the background, with no
  prompt — the same cheap/non-blocking terms as `sumela_wiki_sync`. The ingest script
  self-creates the `code_chunks` collection if missing and promotes an incremental run
  to a FULL walk when the collection is empty, so the first pull after setup builds the
  whole corpus (and `setup-memory` no longer leaves it for a manual/prompted build).
  `SUMELA_PULL_CODE_REINGEST=1` is repurposed to force a full tree re-embed on a pull;
  `SUMELA_DISABLE_CODE_SYNC=1` still turns the whole thing off. The freshness decision
  is now ground-truth (the script checks the collection's real `points_count`) rather
  than the `.code-chunks-synced` marker — which fixes a false-negative where an index
  built outside the hook (manual run / first setup) left the marker unwritten, so the
  hook claimed code_chunks "has never been built" on every pull even with a full
  collection (and the command it suggested, the standalone ingest, never wrote the
  marker, so the warning was unsilenceable). The marker is retired: nothing reads or
  writes it now (per-ingest timestamps live in `.sumela/.memory-sync.log`); its
  `.gitignore` entry is kept only so leftover files from older installs aren't
  committed. Docstring, `qdrant-session-memory/SKILL.md`, and `setup-memory.{sh,ps1}`
  updated to match.

### Removed

- **`SUMELA_CODE_REINGEST_DAYS`** — the code_chunks staleness threshold is gone now that
  re-embedding is incremental-on-every-pull. Anyone who set it can drop it (it is silently
  ignored); use `SUMELA_PULL_CODE_REINGEST=1` to force a full re-embed instead.

### Fixed

- **`.gitignore` runtime-artifact entries now reach UPGRADED installs, not just fresh ones
  (v0.7.2).** `.gitignore` is OVERLAY (the updater never overwrites it), so a managed
  pattern that a release added to setup's seed block — e.g. `.sumela/.update-check` in
  0.7.1 — landed only on fresh installs; projects upgrading via `update.{sh,ps1}` never
  got it, leaving the hook's cache file UNTRACKED (status noise / accidental-commit risk).
  Root cause was structural: the managed list lived only inside `setup.{sh,ps1}`, and even
  setup's own "backfill newer entries" guard was a hand-maintained subset that had drifted
  (it listed `.sumela/_migration/` + `AGENTS.md.bak*` but not the newer cache files). Fix:
  the managed patterns now live in ONE language-agnostic source,
  `scripts/lib/sumela-gitignore.list`, read by `setup.sh`/`setup.ps1` (seed + full backfill)
  AND by a new idempotent reconcile step in `update.sh`/`update.ps1` (adds only patterns
  absent anywhere in `.gitignore`, never duplicates an existing entry, never touches the
  user's own lines; diff + consent, auto on `--yes`, shown-only on `--dry-run`; creates
  `.gitignore` if absent; monorepo-subdir aware). Adding a future runtime-artifact pattern
  is now a one-line edit to the shared list that reaches fresh and upgraded installs alike.

- **Qdrant ingestion no longer silently no-ops in adopted projects.** The Qdrant plugin's
  `get_repo_root()` (`memory-plugins/qdrant-session-memory/scripts/lib/memory_ingest.py`)
  hard-coded "three levels up" from the module, which resolved to the *plugin* directory
  (`.sumela/memory-plugins/qdrant-session-memory`) rather than the repository root. In every
  project that vendored SumelaOS, `WIKI_DIR`/`SRC_DIR` then pointed under the plugin dir
  (so wiki/code ingest found nothing) and `relative_to(REPO_ROOT)` raised on real doc paths —
  the `wiki_pages`/`code_chunks` collections stayed permanently empty while `setup-memory`
  still reported them "ready" and `query-qdrant` returned 0 results forever, so Tier-1
  semantic memory was non-functional with no error on the happy path. `get_repo_root()` now
  walks up to the first ancestor containing `.git` or a top-level `.sumela/` (matching what
  the PowerShell scripts already do), honors a `SUMELA_REPO_ROOT` override, and keeps the old
  three-levels-up only as a last-resort fallback — correct in both the framework's own repo
  and vendored adoptions. Covered by `tests/test_get_repo_root.py` (run in `tests/smoke.sh`).

- **Graphify build no longer reports false success or silently skips `graph.html`.**
  `setup-memory.{sh,ps1}`, `auto-update-memory.py`, and a downstream sync error message used
  `graphify update .` — the incremental path — for the initial build, and the setup scripts
  suppressed all output (`*> $null` / `>/dev/null 2>&1`) and declared "Code graph built" on
  exit 0 alone. On any repo over graphify's ~5000-node viz limit, the interactive `graph.html`
  was skipped with a warning the user never saw, yet setup still reported success. Now:
  - **All paths** use the canonical `graphify .` for the first build (`graphify . --update` only
    for incremental re-runs — `--update` is a flag, not the legacy `update` subcommand).
  - **`setup-memory.{sh,ps1}` (interactive setup):** output is no longer suppressed, so graphify's
    own viz/limit warnings reach the user; the "Code graph built" success is gated on `graph.html`
    actually existing, not exit 0; when graphify skips the viz on a large graph, setup reads the
    real node count, raises `GRAPHIFY_VIZ_NODE_LIMIT` above it, and regenerates the viz via
    `graphify cluster-only .` — falling back to a flagged to-do with the exact command if it still
    can't, never a fake "built".
  - **`auto-update-memory.py` (the git-hook / background path):** same canonical command + viz
    auto-raise/regenerate; graphify's own viz/`graph.html` warnings are best-effort echoed from
    captured output (and a `cluster-only` failure is reported with its exit code), while the
    structured report ALWAYS carries an explicit `note:` line distinguishing "graph updated" (the
    query-critical `graph.json` is fresh) from "interactive `graph.html` skipped" + the exact
    remediation. A skipped viz is reported, not silently treated as full success, and not treated as
    a hard failure (which would falsely fail the background hook on every large-repo commit).
  - The graphify plugin README now documents the AST-only nature (semantic extraction needs a
    generative backend SumelaOS does not wire up), that Tier-2 queries work off `graph.json` even
    when the viz is skipped, and why `graphify-out/` is gitignored.

## [0.4.0] - 2026-06-02

### Added — less manual work, monorepo support

- **Auto-reconcile `SKILL_REGISTRY.md`** — `scripts/reconcile-registry.py` registers
  on-disk skills missing from the registry (verbatim description, `activation="lazy"`)
  and reports orphans; run by `update.sh` with consent.
- **Health report** — `scripts/status.sh` / `status.ps1`: one read-only command for
  version, governance, structure, registry/mirror/shared-rule drift, improvement-queue
  count, git-hook wiring, secret scanner, and memory plugins. Every issue lists its fix.
- **Secret governance** — if `gitleaks` is installed, the pre-commit hook scans staged
  changes and blocks on a finding (silent if absent; `SUMELA_DISABLE_SECRET_SCAN=1` to
  disable; tool/version errors never block). Setup seeds a `.gitignore` secret baseline.
- **Monorepo support** — hooks self-anchor to their install dir (subdir installs now
  validate their own subtree instead of silently no-op'ing). Multiple installs in one
  repo auto-promote to a root dispatcher (`.sumela-hooks/` + `_dispatch.sh`) that runs
  every install's hooks. Org-shared rules live once in `.sumela-shared/rules/`;
  `scripts/sync-shared-rules.py` distributes + registers them (universal) into each
  install (run by setup / update; drift surfaced by `status.sh`).
- **OpenCode IDE support** — `.opencode/AGENTS.md.template` pointer; wired into
  `setup.sh` / `setup.ps1` (template list, IDE map, multiselect, `--ides`) and the
  README Supported-IDEs table, completing the IDE matrix the README already advertised.
- **One-step memory runtime** — `scripts/setup-memory.sh` / `.ps1`: opting into a
  memory plugin no longer leaves the developer to install Qdrant/Ollama/graphify by
  hand. It auto-installs the cheap/safe deps (pip) and CONFIRMS-and-runs the invasive
  steps (start Qdrant via Docker, pull the Ollama embedding model, install the graphify
  CLI), reuses anything already present, then creates the Qdrant collections + builds
  the initial graph. Idempotent; non-interactive mode prints every exact command
  instead of running it; `/initSumela` and `setup.sh`/`.ps1` invoke it after copying
  the plugins (skip with `SUMELA_SKIP_MEMORY_SETUP=1`, e.g. in CI / the smoke test).
  It is also the **add-a-plugin-later** path: since bootstrap copies all plugin
  files, `setup-memory.sh --plugins <name>` registers a not-yet-enabled plugin in
  `SKILL_REGISTRY.md` and brings its runtime up — no re-init needed. All script
  output is English (framework artifacts stay language-neutral); the `/initSumela`
  consent prompt is rendered in the developer's configured interaction language.
- **End-to-end smoke test** — `tests/smoke.sh` runs `setup.sh` against a throwaway
  copy and asserts the contract (AGENTS.md + every selected IDE pointer generated, a
  plugin registered exactly once, no unrendered placeholders, structure + reconcile
  pass) and that a second run is idempotent. Wired into the opt-in CI workflow; this
  is the first FUNCTIONAL test of the self-modifying setup pipeline (was parse-only).
- **Signal taxonomy expanded** (`self-improvement-curator`) — two new capture types:
  `resolution` (agent-originated: a bug/problem the agent fixes itself → capture the
  GENERALIZED class-level lesson, never the one-off instance) and `preference` (a
  proactive standing user instruction, distinct from a reactive `correction`). Wired
  in lockstep across the prompt, registries, schema, and queue README.
- **Pull-time code-graph refresh** — `post-merge`/`post-checkout` now also run
  `sumela_graph_sync`: when a pull/checkout brings CODE changes, it refreshes the
  local graphify graph in the background via `auto-update-memory.py --graph-only`
  (a new graph-only mode that writes ONLY the gitignored graph dir — no wiki sync,
  no `_LOG.md` append, so a pull never dirties the tree). Self-gating, non-blocking,
  opt-out with `SUMELA_DISABLE_GRAPH_SYNC=1`.
- **Pull-time Qdrant content refresh** — the pull hooks also keep the Qdrant semantic
  collections (whose embeddings would otherwise lag the git-current tracked files) in
  sync: `sumela_wiki_sync` re-ingests `wiki_pages` when a CURATED page changed
  (session-summaries + underscore-special files like `_LOG.md` excluded, so a
  union-merged log alone never triggers it; default on, opt-out
  `SUMELA_DISABLE_WIKI_SYNC=1`). `sumela_code_sync` keeps `code_chunks` honest: it
  PRUNES orphans for removed code files cheaply on every pull, but the heavy whole-tree
  re-embed is no longer all-or-nothing — when code_chunks is STALE
  (> `SUMELA_CODE_REINGEST_DAYS`, default 14) it PROMPTS for approval on an interactive
  pull (default No, 30s timeout) or prints a non-blocking notice otherwise;
  `SUMELA_PULL_CODE_REINGEST=1` forces it, `SUMELA_DISABLE_CODE_SYNC=1` turns it off.
- **Qdrant orphan pruning** — new `delete-from-qdrant.py` (delete points by a payload
  key). `wiki_sync`/`code_sync` call it for pages/files DELETED upstream, so a removed
  wiki page or source file stops surfacing in semantic search. `chat_history` is
  deliberately exempt — a removed session summary does not retract a past decision.
  All syncs remain background, best-effort, Qdrant-reachability-gated, and write only
  the Qdrant cache — never the tracked tree.
- **Richer memory-sync log** — the pull-time summary ingest now reports WHO (git
  author) and WHICH tasks (filename + `session_topics`) each arriving summary
  belongs to, inline (up to 10) and in `.sumela/.graph-sync.log`/`.memory-sync.log`.

### Fixed

- **Existing-project install completeness** — the README agent prompt now delegates
  the copy to the maintained `bootstrap.sh`/`.ps1` (no more hand-list drift; bootstrap
  now also copies `docs/second-brain/template/` and the new `.opencode/`), and
  `/initSumela` gained Step 3.6b (`.gitattributes` union-merge + `.gitignore` secret
  baseline) so both install paths reach full parity with `setup.sh`.
- **English-only framework artifacts (global-ready)** — removed hardcoded Turkish from
  the agent-control surface: the prompt's routing/trigger examples, skill trigger
  phrases + user-facing example prompts (`self-improvement-curator`, `using-second-brain`,
  `context-handoff`, `idea-explore`, `finishing-a-development-branch`), the plugin
  trigger lines, the `_SCHEMA.md` template (fully translated, section numbers + enums
  preserved), and the two ingest scripts' examples. Triggers are now English + "the
  equivalent in any language"; instructions that said "respond in Turkish" now say
  "in the configured interaction language". Setup-script output is English; the agent
  still renders all user-facing text in the developer's chosen language.

- **`status.sh` / `status.ps1` hook detection** now recognizes all three wiring forms
  the monorepo work introduced (root, subdir `<rel>/.sumela/git-hooks`, and the
  `.sumela-hooks` dispatcher — verifying this install is registered) instead of only
  the root form; the fix it suggests is rerunning setup (correct for every topology).
- **`setup.ps1` plugin registration is now idempotent** — guards on an existing
  `<name>` before appending, matching `setup.sh`; re-running with a plugin selected no
  longer duplicates its `<skill>` block in `SKILL_REGISTRY.md`.
- **Bootstrap scripts hardened** — `bootstrap.sh` uses `set -euo pipefail`, surfaces
  clone failures, fails loudly on the essential payload, and cleans up via `trap`;
  `bootstrap.ps1` reaches parity (copies all IDE templates + `.gitkeep` dirs, real
  error/next-step output) instead of being a minimal stub.
- **`.sumela/VERSION` bumped to 0.4.0** so `update.sh`'s version gate and `status.sh`
  reflect the post-0.3.0 core (it had stayed at 0.3.0 while features landed).

## [0.3.0] - 2026-06-01

### Added — team enablement

- **Shared session memory** — `session-ingest.py` is idempotent (deterministic point
  IDs, delete-by-session); `post-merge`/`post-checkout` git hooks re-ingest teammates'
  committed session summaries into each developer's local Qdrant on pull.
- **Team-safe wiki** — `_LOG.md` uses git `union` merge; the self-improvement queue is
  a directory (`_improvement-queue/`, one `IMP-YYYYMMDD-<short>.md` per signal, no
  shared counter); `active-project-context.md` per-developer "Active Work" convention.
- **`/evolve` governance** — `governance: solo|team` (AGENTS.md §8). In team mode,
  rule/skill/schema changes route through a PR (`proposed` status + reconcile) and a
  `.github/CODEOWNERS` block guards the agent-control surface.
- **Enforcement** — `validate-structure.sh` runs via a `.sumela/git-hooks/pre-commit`
  hook (scoped, bypassable) and an opt-in GitHub Actions workflow (`setup.sh --ci`).
- **Per-developer config** — gitignored `.sumela/local.md` overrides only
  `interaction_language`; code naming/documentation stay team-wide.
- **Upgrade path** — `.sumela/VERSION` + `scripts/update.sh` / `update.ps1` refresh the
  framework CORE (prompt, skills, scripts, hooks, universal rules, schema, templates)
  without touching the project OVERLAY (AGENTS.md, stack rules, wiki, registries,
  governance/CI choices), with per-file diff + consent.
- **IDE mirror sync** — `scripts/sync-mirrors.sh` regenerates verbatim IDE mirrors
  from `sumela-prompt.md`; drift is checked by pre-commit + CI.

### Changed

- `core.hooksPath` is wired for every git repo (memory hooks self-gate); the CI
  workflow is opt-in (not auto-imposed).
- Reconciled the language protocol: project code follows the configured
  naming/documentation languages; only framework artifacts + commit messages stay English.

## [0.2.0] - 2026-05-22

### Added
- `init-sumela` skill: `/initSumela` command for automatic brownfield adoption
  - Auto-detects tech stack from manifest files (csproj, package.json, go.mod, Cargo.toml, etc.)
  - Identifies architecture pattern from directory structure (Clean Arch, MVC, microservices, monorepo, etc.)
  - Detects code conventions from existing source files (naming, error handling, validation, testing)
  - Generates AGENTS.md, RULE_REGISTRY.md, rules, wiki pages, and IDE pointers in one pass
  - Interactive confirmation before writing any files
- **Proactive Qdrant session context (STEP 3c)**: Agent silently queries Qdrant before starting non-trivial tasks
  - Checks past sessions for relevant decisions, lessons, and context
  - 5 scenarios: task start, entity encounter, architecture decision, debugging, file history
  - Mirrors Graphify's proactive impact analysis pattern

### Fixed
- Subagent-driven-development: added independent review option (4th choice after task completion)
- Graphify plugin: corrected installation to `uv tool install graphifyy` (PyPI, not npm)
- Graphify plugin: added "No API Key Required for Queries" clarification
- **Proactive impact analysis (STEP 3b)**: Agent now queries graphify before code changes to detect affected dependents

## [0.1.0] - 2026-05-22

### Added
- 27 universal agent skills (brainstorming, planning, TDD, debugging, code review, shipping, etc.)
- 7 universal rules + stack-specific rule templates (empty + best-practice variants)
- Second-brain wiki template with Karpathy LLM Wiki pattern (raw_sources → artifacts → wiki)
- Memory plugins: Qdrant session memory (Tier-1), Graphify code graph (Tier-2)
- Setup scripts: setup.sh (Bash), setup.ps1 (PowerShell) with interactive and non-interactive modes
- Validation script: validate-structure.sh
- IDE pointer templates: Claude Code, Cursor, Cline, Kilo Code, Trae
- ADOPTION_GUIDE with greenfield, brownfield, and team onboarding modes
- Self-improvement loop: `/evolve` command for capturing corrections and friction signals
- Context Manifest protocol for session-start transparency
- Phase-to-rule matrix for automatic rule loading based on active phase and stack scope
