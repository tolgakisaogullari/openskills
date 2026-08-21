#!/usr/bin/env python3
"""
auto-update-memory.py — Automatic memory stack maintenance (v2.4-agnostic)

Usage:
    python auto-update-memory.py
    python auto-update-memory.py --project-root /path/to/repo --graph-dir graphify-out --wiki-path docs/second-brain/wiki

What it does:
    1. Rebuilds the code graph (`graphify update .` — AST-only, no LLM key needed).
    2. Syncs Graphify insights to Obsidian wiki (with noise filter).
    3. Verifies Qdrant is reachable and appends a status line to _LOG.md.
    4. Prints a structured status report for the agent to relay to the user.

This script is safe to run after every code-commit or at session end.

Environment:
    GRAPHIFY_OUT_DIR — graph output directory (default: graphify-out)
    WIKI_PATH        — wiki directory path (default: docs/second-brain/wiki)
    PROJECT_ROOT     — project root directory (default: auto-detected from script location)
    QDRANT_HOST      — Qdrant host (default: localhost)
    QDRANT_PORT      — Qdrant port (default: 6333)
"""
# Defer annotation evaluation so `X | None` hints don't require Python 3.10+ at
# runtime — the pull/checkout git hook calls this with whatever `python3` a teammate
# has, and crashing at def-time would silently fail the background graph refresh.
from __future__ import annotations

import subprocess
import sys
import os
import json
import argparse
import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def print_report(summary_lines: list):
    print("\n" + "=" * 60)
    print("MEMORY UPDATE REPORT")
    print("=" * 60)
    for line in summary_lines:
        print(line)
    print("=" * 60 + "\n")


def report_success(graphify_ok: bool, sync_ok: bool, qdrant_ok: bool, log_warning: str | None = None,
                   graph_note: str | None = None):
    lines = [
        "Status: COMPLETE",
        f"1. Graphify code graph: {'OK' if graphify_ok else 'FAIL'}",
    ]
    if graph_note:
        lines.append(f"   note: {graph_note}")
    lines += [
        f"2. Wiki sync (graphify -> Obsidian): {'OK' if sync_ok else 'FAIL'}",
        f"3. Qdrant health check: {'OK' if qdrant_ok else 'FAIL'}",
    ]
    if log_warning:
        lines.append(f"4. _LOG.md size: {log_warning}")
    lines.append("Action for agent: Relay this summary to the user.")
    print_report(lines)


def report_failure(stage: str, reason: str):
    print_report([
        "Status: FAILED",
        f"Stage: {stage}",
        f"Reason: {reason}",
        "Action for agent: Inform the user of the failure.",
    ])


def get_script_dir() -> Path:
    """Return the directory containing this script."""
    return Path(__file__).resolve().parent


def _node_count(graph_json: Path) -> int:
    """Best-effort node count from graphify's graph.json (0 if unreadable)."""
    try:
        with open(graph_json) as f:
            d = json.load(f)
        n = d.get("nodes")
        if n is None:
            n = d.get("graph", {}).get("nodes", [])
        return len(n) if isinstance(n, list) else int(n)
    except Exception:
        return 0


def _echo_viz_warnings(*streams: str) -> None:
    """Best-effort surface graphify's own graph.html/viz warnings even though we
    capture its output — they explain a silently-skipped graph.html. Anchored on the
    viz/graph.html concept so unrelated lines (e.g. 'Rate limit: none (AST only)') are
    NOT echoed as if they were warnings. The synthesized viz_note in run_graphify is the
    guaranteed signal; this is the nice-to-have echo of graphify's exact wording."""
    for s in streams:
        for line in (s or "").splitlines():
            low = line.lower()
            if ("graph.html" in low or "visualiz" in low or "too large" in low
                    or "graphify_viz_node_limit" in low
                    or (("skip" in low or "viz" in low) and ("graph" in low or "html" in low))):
                print("      graphify: " + line.strip())


# graphify's own default interactive-viz cap. Only used to word the "why is graph.html
# missing" note correctly — the limit itself lives in graphify, we never override it.
VIZ_NODE_LIMIT = 5000


def run_graphify(project_root: Path, graph_dir: str = "graphify-out"):
    """Rebuild the code graph. Returns (ok, viz_note).

    `ok` is True when the query-critical graph.json is present — graph.html is a
    human-only interactive viz, so a skipped viz is reported as a NOTE, not a build
    failure and not a to-do (mirrors setup-memory.{sh,ps1}, which gate success on
    graph.json for the same reason, and never report a silent success).
    """
    out_dir = project_root / graph_dir
    # AST-only rebuild: the `update <path>` SUBCOMMAND is graphify's no-LLM code-only
    # path, and it works both incrementally AND as the very first build (verified on
    # graphify 0.8.35 with no pre-existing graph.json). The bare `graphify .` and
    # `graphify . --update` FLAG forms attempt SEMANTIC extraction of doc/paper/image
    # files and hard-fail without an LLM API key — wrong for this hook, which must
    # stay key-free (plugin README: AST-only by design). We do NOT hard-code --force:
    # graphify's fewer-nodes guard protects against a broken parse wiping the graph;
    # after a deleting refactor, set GRAPHIFY_FORCE=1 in the env (inherited by this
    # subprocess) to override it.
    cmd = ["graphify", "update", "."]
    print(f"[1/4] Running {' '.join(cmd)} ...")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(project_root))
    if result.returncode != 0:
        print("ERROR: graphify failed:", result.stderr)
        return False, None
    _echo_viz_warnings(result.stdout, result.stderr)
    # graphify skips the interactive graph.html above its ~5000-node viz limit. HONOR
    # that skip: nothing in the retrieval path reads graph.html — Tier-2 is
    # `query-graph.py`, which reads graph.json, and the wiki insights page is generated
    # from GRAPH_REPORT.md — and no browser opens a viz that large anyway. Overriding
    # the limit cost a SECOND full clustering pass plus a huge HTML write on EVERY
    # pull: measured on a 193k-node repo, ~226 MB of unopenable HTML with Louvain run
    # twice, one core pinned for the duration. Opt back in with SUMELA_GRAPHIFY_VIZ=1.
    html = out_dir / "graph.html"
    forced_attempted = False
    # Only an affirmative value opts in. "false"/"no"/"off" are the words a user reaches
    # for to turn something OFF; reading them as truthy would restore the exact
    # multi-minute stall this change removes.
    want_viz = os.getenv("SUMELA_GRAPHIFY_VIZ", "").strip().lower() not in (
        "", "0", "false", "no", "off")
    if want_viz and not html.exists() and (out_dir / "graph.json").exists():
        nodes = _node_count(out_dir / "graph.json")
        env = {**os.environ, "GRAPHIFY_VIZ_NODE_LIMIT": str(nodes + 1000)}
        forced_attempted = True
        print(f"      SUMELA_GRAPHIFY_VIZ set — regenerating graph.html "
              f"(viz limit raised to {nodes + 1000})…")
        cl = subprocess.run(["graphify", "cluster-only", "."], cwd=str(project_root),
                            capture_output=True, text=True, env=env)
        if cl.returncode != 0:
            # Don't bury a cluster-only failure (e.g. the subcommand missing in this
            # graphify version) — the keyword echo would filter the error out otherwise.
            err = (cl.stderr or cl.stdout or "").strip().splitlines()
            print(f"WARN  graphify cluster-only failed (rc={cl.returncode}): "
                  f"{err[0] if err else 'no output'}")
        else:
            _echo_viz_warnings(cl.stdout, cl.stderr)
    if not (out_dir / "graph.json").exists():
        # No graph data at all — nothing for the query pipeline to read.
        print("ERROR: graphify produced no graph.json")
        return False, None
    viz_note = None
    if not html.exists():
        nodes = _node_count(out_dir / "graph.json")
        # Say only what is established. Above the viz limit a missing html is the
        # EXPECTED outcome; below it, something else went wrong and pointing the user at
        # the node-limit knob would send them down the wrong path.
        if forced_attempted:
            viz_note = ("SUMELA_GRAPHIFY_VIZ was set but the graph.html regeneration failed "
                        "(see the WARN above) — Tier-2 queries are unaffected, they read graph.json.")
        elif nodes > VIZ_NODE_LIMIT:
            viz_note = (f"interactive graph.html skipped ({nodes} nodes, above graphify's "
                        f"~{VIZ_NODE_LIMIT}-node viz limit) — expected, and nothing in the query "
                        f"path needs it (Tier-2 reads graph.json). Want it anyway: "
                        f"SUMELA_GRAPHIFY_VIZ=1 for this pull-time sync, or "
                        f"GRAPHIFY_VIZ_NODE_LIMIT={nodes + 1000} graphify cluster-only .")
        else:
            viz_note = (f"graph.json is built ({nodes} nodes) but graph.html is missing — below "
                        f"graphify's ~{VIZ_NODE_LIMIT}-node viz limit that is unexpected; check "
                        f"graphify's output above. Tier-2 queries are unaffected (they read graph.json).")
        print("INFO  " + viz_note)
    print("OK    graphify graph updated.")
    return True, viz_note


def check_qdrant(host: str, port: int) -> bool:
    print("[3/4] Checking Qdrant health...")
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(host=host, port=int(port), check_compatibility=False)
        collections = client.get_collections().collections
        print(f"OK    Qdrant reachable. Collections: {len(collections)}")
        return True
    except ImportError:
        print("WARN  qdrant-client not installed, skipping Qdrant health check.")
        return False
    except Exception as e:
        print(f"WARN  Qdrant unreachable: {e}")
        return False


LOG_ROTATE_THRESHOLD = 800  # lines


def check_log_size(wiki_path: Path) -> str | None:
    """Return a warning string if _LOG.md exceeds threshold; else None."""
    log_path = wiki_path / "_LOG.md"
    if not log_path.exists():
        return None
    with open(log_path, "r", encoding="utf-8") as f:
        n = sum(1 for _ in f)
    if n >= LOG_ROTATE_THRESHOLD:
        return (
            f"{n} lines (threshold {LOG_ROTATE_THRESHOLD}). "
            f"Consider archiving entries older than 6 months to wiki/archive/_LOG-YYYY.md "
            f"and adding a `migration` log entry. Manual review required — auto-rotation disabled."
        )
    return None


def log_status(wiki_path: Path, graphify_ok: bool, sync_ok: bool, qdrant_ok: bool):
    print("[4/4] Logging status...")
    log_path = wiki_path / "_LOG.md"
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    line = (
        f"\n## [{date_str}] lint | Auto memory update: "
        f"graphify={'OK' if graphify_ok else 'FAIL'}, "
        f"wiki_sync={'OK' if sync_ok else 'FAIL'}, "
        f"qdrant={'OK' if qdrant_ok else 'FAIL'}\n"
    )
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)
    print("OK    Logged.")


def sync_graphify_wiki(project_root: Path, graph_dir: str, wiki_path: str) -> bool:
    print("[2/4] Syncing Graphify insights to Obsidian wiki...")
    sync_script = project_root / ".sumela/memory-plugins/graphify-code-graph/scripts/sync-graphify-to-obsidian.py"
    if not sync_script.exists():
        print("WARN  sync-graphify-to-obsidian.py not found, skipping wiki sync.")
        return False
    result = subprocess.run(
        [sys.executable, str(sync_script), "--graph-dir", graph_dir, "--wiki-path", wiki_path],
        capture_output=True, text=True, cwd=str(project_root)
    )
    if result.returncode != 0:
        print("WARN  Graphify wiki sync failed:", result.stderr)
        return False
    print("OK    Graphify wiki synced.")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Automatic memory stack maintenance."
    )
    parser.add_argument("--project-root", default=os.getenv("PROJECT_ROOT"),
                        help="Project root directory (default: auto-detected)")
    parser.add_argument("--graph-dir", default=os.getenv("GRAPHIFY_OUT_DIR", "graphify-out"),
                        help="Graph output directory (default: graphify-out or GRAPHIFY_OUT_DIR env)")
    parser.add_argument("--wiki-path", default=os.getenv("WIKI_PATH", "docs/second-brain/wiki"),
                        help="Wiki directory path (default: docs/second-brain/wiki or WIKI_PATH env)")
    parser.add_argument("--qdrant-host", default=os.getenv("QDRANT_HOST", "localhost"),
                        help="Qdrant host (default: localhost or QDRANT_HOST env)")
    parser.add_argument("--qdrant-port", type=int, default=int(os.getenv("QDRANT_PORT", "6333")),
                        help="Qdrant port (default: 6333 or QDRANT_PORT env)")
    parser.add_argument("--graph-only", action="store_true",
                        help="Only rebuild the code graph (graphify update . — AST-only). Skips the wiki sync "
                             "and the _LOG.md append, so it is safe to run from a git pull/checkout "
                             "hook: it writes ONLY to the gitignored graph dir, never the tracked "
                             "working tree.")
    args = parser.parse_args()

    script_dir = get_script_dir()

    if args.project_root:
        project_root = Path(args.project_root)
    else:
        # Default: the repo root is one level up from this scripts/ directory.
        project_root = script_dir.parent

    if not project_root.exists():
        report_failure("Config", f"Project root not found: {project_root}")
        sys.exit(1)

    # Graph-only mode (used by the pull/checkout git hook): rebuild ONLY the code
    # graph. Skip wiki sync and _LOG.md append — both write tracked files, which a
    # git hook must never do (a pull would otherwise leave the tree dirty). The
    # graph dir is gitignored, so this stays clean.
    if args.graph_only:
        g_ok, g_viz = run_graphify(project_root, args.graph_dir)
        lines = [
            "Status: COMPLETE (graph-only)",
            f"1. Graphify code graph: {'OK' if g_ok else 'FAIL'}",
        ]
        if g_viz:
            lines.append(f"   note: {g_viz}")
        lines.append("(wiki sync + _LOG append skipped — graph-only mode)")
        print_report(lines)
        sys.exit(0 if g_ok else 1)

    wiki_path = project_root / args.wiki_path

    g_ok, g_viz = run_graphify(project_root, args.graph_dir)
    s_ok = sync_graphify_wiki(project_root, args.graph_dir, args.wiki_path)
    q_ok = check_qdrant(args.qdrant_host, args.qdrant_port)

    log_status(wiki_path, g_ok, s_ok, q_ok)
    log_warning = check_log_size(wiki_path)
    report_success(g_ok, s_ok, q_ok, log_warning, g_viz)

    if not g_ok or not s_ok or not q_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
