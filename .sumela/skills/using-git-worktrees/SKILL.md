---
name: using-git-worktrees
description: "Use when starting any implementation task that requires an isolated workspace — before creating branches or writing any code."
---

<execution_workflow>
Execute these steps strictly in order. DO NOT announce the skill.

1. DIRECTORY RESOLUTION:
   - Check existing directories: `.worktrees` (preferred) or `worktrees`.
   - Check `CLAUDE.md` or `SKILL_REGISTRY.md` for explicit preferences (`grep -i "worktree.*director" CLAUDE.md`).
   - If none found, STOP and ask the user: "No worktree directory found. Prefer 1) `.worktrees/` (local) or 2) `~/.config/superpowers/worktrees/<project>/` (global)?" Wait for answer.

2. GITIGNORE VERIFICATION (SETUP EXCEPTION):
   - If using a local directory (`.worktrees` or `worktrees`), you MUST verify it is ignored: `git check-ignore -q <dir>`.
   - If NOT ignored, add the directory to `.gitignore`, show the exact diff, and ask the user whether to commit or stage this setup change BEFORE creating the worktree.
   - *CRITICAL EXCEPTION:* This setup commit (e.g., `git commit -m "chore: ignore local worktrees directory"`) is ALLOWED only with explicit user approval and bypasses the strict "no-commit" rule used during feature implementation.

3. CREATE WORKTREE & BRANCH:
   - Determine a descriptive branch name based on the task intent using standard prefixes (e.g., `feat/add-auth`, `fix/login-crash`, `refactor/api-routes`, `sec/patch-idor`).
   - Collision check: verify `git branch --list <branch_name>` returns no existing branch and `<path>` does not already exist. If either exists, choose a clearer unique name or ask the user.
   - If the plan or user specifies a base branch/ref, create from it: `git worktree add <path> -b <branch_name> <base-ref>`. Otherwise create from current `HEAD`: `git worktree add <path> -b <branch_name>`.
   - Run: `cd <path>`
   - *CRITICAL CONTEXT WARNING:* Ensure ALL subsequent operations, terminal commands, and subagent dispatches in this session occur strictly within this new `<path>`. In tool-based IDEs, explicitly set every command's working directory to `<path>`; do not rely on a prior `cd` persisting.
   - *MEMORY NOTE:* Creating a worktree does NOT rebuild the memory index or the code graph, by design — both are per-project caches owned by the MAIN checkout, and nothing they derive from has changed (see `.sumela/git-hooks/README.md` → Worktrees). So Tier-1/Tier-2 retrieval still works from inside the worktree, but it describes the main checkout: code you write here is not indexed until it is merged and pulled there. Do not "fix" this by forcing a re-embed — that re-embeds the whole tree for no gain.

4. ENVIRONMENT SETUP:
   - Auto-detect and install dependencies based on project files:
     - .NET: `*.sln` / `*.csproj` -> `dotnet restore` then the project build command.
     - Node.js: inspect lockfiles and package scripts. Prefer `pnpm install --frozen-lockfile` for `pnpm-lock.yaml`, `yarn install --frozen-lockfile` for `yarn.lock`, `npm ci` for `package-lock.json`, otherwise `npm install`.
     - Rust: `Cargo.toml` -> `cargo build`.
     - Python: `requirements.txt` -> `pip install -r requirements.txt`; `pyproject.toml` -> use the project's configured tool (`uv`, `poetry`, or documented command).
     - Go: `go.mod` -> `go mod download`.

5. BASELINE VERIFICATION & HANDOFF (ADAPTIVE GATE):
   - Attempt to run the project's verification commands to ensure the baseline is clean before writing any new code. .NET projects: build and, if applicable, test. JS/TS projects: use the package manager's configured test/build scripts. Other stacks: their respective documented test runner.
   - If tests FAIL: Report the failures to the user and STOP. Ask for permission to proceed or investigate. NEVER start new feature work on a broken baseline.
   - If tests PASS (or if no test suite exists/is configured): Report "Worktree ready at <path> on branch <branch_name>. Baseline verified. Ready to implement."
</execution_workflow>
