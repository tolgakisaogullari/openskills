# SumelaOS Git Hooks

Wired via `git config core.hooksPath .sumela/git-hooks` (done by `setup.sh` /
`/initSumela`, once per clone). Two concerns live here:

- **`pre-commit` — enforcement.** Runs the structure validation (the same check as
  CI) before a commit that touches the agent-control surface, so a broken registry
  path or an unfilled `{{placeholder}}` is caught locally. Bypass with
  `git commit --no-verify`.
- **`post-merge` / `post-checkout` / `post-commit` — team memory sync.** Keep each
  developer's **local** Qdrant in sync with the session summaries (and wiki/code)
  committed to git, so a decision a teammate recorded becomes semantically searchable
  right after a `git pull`. `post-commit` is the safety net for **conflicted** merges,
  which skip `post-merge`. (Self-gating: inert without the Qdrant plugin.)

```
teammate's session ──commit summary.md──► git (shared source of truth)
                                            │
                            git pull / merge ▼
                                       post-merge hook
                                            │  (changed summaries only)
                                            ▼
                                  session-ingest.py ──► YOUR local Qdrant
```

The markdown summaries in git are the shared source of truth; your Qdrant is a
derived local cache. These hooks are the cache-invalidation step.

This directory holds two independent concerns: **enforcement** (`pre-commit`) and
**team memory sync** (`post-merge`/`post-checkout`/`post-commit`).

## Hooks

| Hook | Fires on | What it does |
|---|---|---|
| `pre-commit` | `git commit` | Runs `scripts/validate-structure.sh --check-placeholders` when the commit touches the agent-control surface / second-brain. Blocks on failure. Bypass: `git commit --no-verify`; disable: `export SUMELA_DISABLE_PRECOMMIT=1`. Same check as CI. |
| `post-merge` | `git merge`, merge step of `git pull` (clean merges only) | Re-ingest session summaries / wiki / code changed in `ORIG_HEAD..HEAD` into local Qdrant + refresh the code graph. Self-gates: no-op without the Qdrant plugin. |
| `post-checkout` | `git checkout`/`switch`, `git clone` | Same, for `prev..new` (empty-tree on clone). |
| `post-commit` | `git commit` (acts only on **merge commits**) | Safety net for **conflicted** merges: git skips `post-merge` on a conflict and runs commit hooks on the manual resolution commit instead. On a merge commit (≥2 parents) runs the same syncs for `HEAD^1..HEAD`; on any ordinary commit it is a cheap no-op (immediate `exit 0`). |
| `_lib.sh` | (sourced by the memory hooks) | shared memory-sync logic |

`pre-commit` is useful for every project; the memory hooks self-gate, so wiring
`core.hooksPath` is safe even without the Qdrant plugin.

**No double-firing.** A clean merge creates its merge commit via git's merge
machinery, which does **not** run commit hooks — only `post-merge` fires. A
conflicted merge skips `post-merge`; the manual resolution `git commit` fires
`post-commit`, which detects the merge commit and runs the sync. The two paths are
mutually exclusive, so a given merge is synced exactly once.

## Guarantees

- **Incremental** — only summaries Added/Modified in the range are ingested.
- **Non-blocking** — ingestion runs in the background; git returns immediately.
- **Best-effort** — if the Qdrant plugin isn't installed or Qdrant is unreachable,
  the hook skips silently and exits 0. It never fails a git operation.
- **Path-scoped** — fires only when files under the summaries directory change.

Re-ingesting is safe because `session-ingest.py` uses deterministic point IDs
(delete-by-session + upsert), so a summary is never duplicated in Qdrant.

## Installation

Hooks in `.git/hooks/` are not version-controlled, so this directory is wired in
via git's `core.hooksPath`:

```bash
git config core.hooksPath .sumela/git-hooks
```

`scripts/setup.sh` / `setup.ps1` (and `/initSumela`) run this for you. Each
developer runs it once per clone.

## Configuration (environment variables)

| Var | Default | Purpose |
|---|---|---|
| `SUMELA_DISABLE_PRECOMMIT` | (unset) | Set to `1` to disable the pre-commit validation hook for your clone |
| `SUMELA_DISABLE_MEMORY_SYNC` | (unset) | Set to `1` to disable the memory-sync hooks for your clone |
| `SUMELA_DISABLE_UPDATE_CHECK` | (unset) | Set to `1` to stop the pull-time "newer SumelaOS available" check (see below) |
| `SUMELA_UPDATE_CHECK_INTERVAL` | `86400` | Seconds between upstream version probes (default once/day) |
| `SUMELA_SUMMARIES_DIR` | `$WIKI_PATH/session-summaries` | Where session summaries live |
| `WIKI_PATH` | `docs/second-brain/wiki` | Wiki root |
| `QDRANT_HOST` / `QDRANT_PORT` | `localhost` / `6333` | Qdrant endpoint |

Background ingestion logs to `.sumela/.memory-sync.log` (gitignored, per-developer;
auto-truncated past ~512 KB).

## Upstream update check

On `git pull` / branch checkout, `post-merge`/`post-checkout` also run
`sumela_update_check`: it probes the SumelaOS upstream's release tags
(`git ls-remote --tags`, using your existing git auth — no clone) and, if a newer
`vX.Y.Z` exists than your `.sumela/VERSION`, prints a one-line, non-blocking notice
suggesting `bash scripts/update.sh`. The probe runs at most once per
`SUMELA_UPDATE_CHECK_INTERVAL` (default 24h), cached in the gitignored
`.sumela/.update-check`; the notice keeps showing every pull (from cache) until you
update. It is **best-effort**: offline / no git / no upstream tags → silent.

**Never blocks the pull:** the network probe runs in a *detached background* process
(macOS has no portable `timeout`), and the notice is printed synchronously from the
cache — so a freshly-published release shows on the *next* pull, and a slow/unreachable
upstream can't stall git.

**Security:** the upstream URL is read from `.sumela/upstream.conf` (the same single
source `scripts/update.sh` uses; fork-overridable). Because that file is **tracked**, a
malicious branch could repoint it — so the probe (and the updater's clone) run with
`GIT_ALLOW_PROTOCOL=https:ssh:git` to refuse git's `ext::`/`file::` transports (which
can execute commands) and with `GIT_TERMINAL_PROMPT=0` + ssh `BatchMode` so it never
prompts. Still, treat `.sumela/upstream.conf` as security-sensitive like
`.sumela/git-hooks/**` — a `CODEOWNERS` rule (`/.sumela/upstream.conf @your-security-owners`)
is recommended. Privacy: the probe is an anonymous tag listing of a public repo — it
discloses only your IP to the host (as any git operation does) and sends no project
data. Opt out entirely with `export SUMELA_DISABLE_UPDATE_CHECK=1`.

## Known limitations

- **`git pull --rebase`** does not fire `post-merge` (rebased commits are not
  merges, so `post-commit` does not catch them either). Coverage falls back to
  `post-checkout` (when HEAD moves) and the session-bootstrap backstop that
  flags summaries not yet in local memory.
- **`git merge --squash`** produces a single-parent commit, not a merge commit, so
  the `post-commit` merge-commit guard (≥2 parents) does **not** detect it, and a
  squash merge does not fire `post-merge` either. The squashed changes are still in
  git and get picked up by the next clean merge / branch checkout (`post-merge` /
  `post-checkout`) or a manual re-ingest. This is an accepted gap, not a regression.
- **`git commit --amend` on a merge commit** re-fires `post-commit` and re-runs the
  sync for the same `HEAD^1..HEAD` range. Ingestion is idempotent (delete-by-key +
  deterministic upsert), so this is correct — just a redundant background pass; no
  guard is kept (a state file would re-introduce the marker we deliberately retired).
- **Concurrent pulls** can launch two background ingests for the same summary.
  Because `session-ingest.py` deletes-by-`session_id` then upserts deterministic
  IDs from the same source file, the steady state is always correct; only a
  sub-second window where a concurrent query sees partial chunks is possible.
- **Newline characters in a summary filename** are unsupported (the diff is
  newline-delimited). Session summaries use date/topic slugs, so this does not
  occur in practice.

## Security

`core.hooksPath` points git at this **version-controlled** directory, so git
executes `post-merge`/`post-checkout`/`post-commit` (and the `_lib.sh` they source)
on every `merge`/`checkout`/`commit`/`pull`. Consequences to be aware of:

- A `git clone` alone does **not** auto-run these hooks: `core.hooksPath` is local
  config that each developer sets once per clone (via setup). Cloning untrusted
  code does not execute them.
- **But** once wired, pulling or checking out an **untrusted branch** runs the
  *then-current* hook scripts before you review the diff. Treat changes under
  `.sumela/git-hooks/**` as security-sensitive.
- Recommended: protect this directory with a `CODEOWNERS` rule
  (`/.sumela/git-hooks/ @your-security-owners`) so hook changes require explicit
  review, and audit hook diffs in PRs.

If you copy these hooks into another hooks path manually (e.g. when merging with
husky), preserve the executable bit: `git update-index --chmod=+x <path>/pre-commit <path>/post-merge <path>/post-checkout <path>/post-commit`.
