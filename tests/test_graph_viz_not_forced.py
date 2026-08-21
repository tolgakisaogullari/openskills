#!/usr/bin/env python3
"""Dependency-free unit test for auto-update-memory.py run_graphify() viz handling.

Regression guard for the forced-visualization bug: when the node count exceeded
graphify's ~5000-node viz limit, the pull-time sync raised GRAPHIFY_VIZ_NODE_LIMIT
above the real count and re-ran `graphify cluster-only .` — a SECOND full clustering
pass plus a several-hundred-MB graph.html write on EVERY pull, for a file nothing in
the retrieval path reads (Tier-2 reads graph.json; the wiki insights page comes from
GRAPH_REPORT.md). Measured on a 193k-node repo: ~226 MB of HTML, Louvain twice, one
core pinned throughout.

Contract pinned here:
  * default            → `graphify update .` ONLY; no cluster-only, no forced viz
  * missing graph.html → still ok=True (success gates on graph.json) + a note
  * SUMELA_GRAPHIFY_VIZ=1 → the opt-in escape hatch still forces the viz
  * missing graph.json → ok=False (the query-critical artifact IS the gate)

Run directly:

    python3 tests/test_graph_viz_not_forced.py

Exits non-zero on the first failed assertion. No pytest / third-party deps.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / "scripts/auto-update-memory.py"


def _load_module():
    """Import auto-update-memory.py under a test-only module name (its main() is
    guarded by __name__ == '__main__', so importing runs no side effects)."""
    spec = importlib.util.spec_from_file_location("auto_update_memory_under_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeRun:
    """Records every subprocess.run call and returns exit 0 with empty output.

    `on_call` lets a case simulate graphify's side effects (e.g. cluster-only
    actually writing graph.html) so the assertions test behaviour, not just calls.
    """

    def __init__(self, on_call=None, rc=0):
        self.calls = []
        self.envs = []
        self.on_call = on_call
        self.rc = rc

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        self.envs.append(kwargs.get("env"))
        if self.on_call:
            self.on_call(list(cmd), kwargs)
        return subprocess.CompletedProcess(
            cmd, self.rc, stdout="", stderr="" if self.rc == 0 else "fatal: stub failure")

    def ran(self, *tokens):
        """True if any recorded call contains all of `tokens`."""
        return any(all(t in c for t in tokens) for c in self.calls)


def _project(root: Path, nodes: int = 193146, with_html: bool = False) -> Path:
    """A project dir whose graphify-out/ holds a graph.json of `nodes` nodes."""
    out = root / "graphify-out"
    out.mkdir(parents=True)
    (out / "graph.json").write_text(
        json.dumps({"nodes": [{"id": f"n{i}"} for i in range(nodes)], "links": []}),
        encoding="utf-8",
    )
    if with_html:
        (out / "graph.html").write_text("<html></html>", encoding="utf-8")
    return root


def check(name, cond):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}")
        check.failed += 1
check.failed = 0


def main():
    os.environ.pop("SUMELA_GRAPHIFY_VIZ", None)
    mod = _load_module()
    real_run = mod.subprocess.run

    # 1. DEFAULT: graph.json present, graph.html absent (over the viz limit).
    #    Exactly one graphify call — the update. No cluster-only, no viz forcing.
    with tempfile.TemporaryDirectory() as d:
        root = _project(Path(d).resolve(), nodes=7000)   # above graphify's viz limit
        fake = FakeRun()
        mod.subprocess.run = fake
        try:
            ok, note = mod.run_graphify(root)
        finally:
            mod.subprocess.run = real_run
        check("default: `graphify update .` is run", fake.ran("graphify", "update"))
        check("default: cluster-only is NOT run (no second Louvain pass)",
              not fake.ran("cluster-only"))
        check("default: GRAPHIFY_VIZ_NODE_LIMIT is never injected",
              all((e or {}).get("GRAPHIFY_VIZ_NODE_LIMIT") is None for e in fake.envs))
        check("default: exactly one subprocess call", len(fake.calls) == 1)
        check("missing graph.html is still a success (graph.json is the gate)", ok is True)
        check("skipped viz is reported as a note", bool(note) and "graph.html" in note)
        check("note points at the opt-in, not at a failure",
              bool(note) and "SUMELA_GRAPHIFY_VIZ" in note)
        check("over-limit note names the limit as the cause",
              bool(note) and "viz limit" in note)

    # 2. graph.html already present → nothing to force, no note.
    with tempfile.TemporaryDirectory() as d:
        root = _project(Path(d).resolve(), nodes=10, with_html=True)
        fake = FakeRun()
        mod.subprocess.run = fake
        try:
            ok, note = mod.run_graphify(root)
        finally:
            mod.subprocess.run = real_run
        check("existing graph.html: ok with no note", ok is True and note is None)
        check("existing graph.html: cluster-only still not run", not fake.ran("cluster-only"))

    # 3. OPT-IN: SUMELA_GRAPHIFY_VIZ=1 restores the forced regeneration, with the
    #    raised node limit in the child env.
    with tempfile.TemporaryDirectory() as d:
        root = _project(Path(d).resolve(), nodes=7000)
        html = root / "graphify-out" / "graph.html"

        def write_html(cmd, kwargs):
            if "cluster-only" in cmd:
                html.write_text("<html></html>", encoding="utf-8")

        fake = FakeRun(on_call=write_html)
        os.environ["SUMELA_GRAPHIFY_VIZ"] = "1"
        mod.subprocess.run = fake
        try:
            ok, note = mod.run_graphify(root)
        finally:
            mod.subprocess.run = real_run
            os.environ.pop("SUMELA_GRAPHIFY_VIZ", None)
        check("opt-in: cluster-only IS run", fake.ran("cluster-only"))
        check("opt-in: viz limit raised above the real node count",
              any(int((e or {}).get("GRAPHIFY_VIZ_NODE_LIMIT", 0)) > 7000 for e in fake.envs))
        check("opt-in: html produced → ok, no note", ok is True and note is None)

    # 3b. Opt-in explicitly disabled with "0" behaves like the default.
    with tempfile.TemporaryDirectory() as d:
        root = _project(Path(d).resolve(), nodes=7000)
        fake = FakeRun()
        os.environ["SUMELA_GRAPHIFY_VIZ"] = "0"
        mod.subprocess.run = fake
        try:
            mod.run_graphify(root)
        finally:
            mod.subprocess.run = real_run
            os.environ.pop("SUMELA_GRAPHIFY_VIZ", None)
        check("SUMELA_GRAPHIFY_VIZ=0 is off, not truthy", not fake.ran("cluster-only"))

    # 4. No graph.json at all → build failure, even though graphify exited 0.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        (root / "graphify-out").mkdir()
        fake = FakeRun()
        mod.subprocess.run = fake
        try:
            ok, note = mod.run_graphify(root)
        finally:
            mod.subprocess.run = real_run
        check("missing graph.json fails the build (exit 0 is not the gate)", ok is False)

    # 5. A missing graph.html BELOW the viz limit is not the expected skip — the note
    #    must not blame the limit or hand out the raise-the-limit remedy, which would
    #    send the user down the wrong path (older graphify, partial write, full disk).
    with tempfile.TemporaryDirectory() as d:
        root = _project(Path(d).resolve(), nodes=120)
        fake = FakeRun()
        mod.subprocess.run = fake
        try:
            ok, note = mod.run_graphify(root)
        finally:
            mod.subprocess.run = real_run
        check("below-limit missing html is flagged as unexpected, not as the normal skip",
              ok is True and bool(note) and "unexpected" in note)
        check("below-limit note does not prescribe raising the viz limit",
              bool(note) and "GRAPHIFY_VIZ_NODE_LIMIT" not in note)

    # 6. A non-zero graphify exit is a failure even when a STALE graph.json survives
    #    from an earlier build — the artifact alone must never stand in for the rc.
    with tempfile.TemporaryDirectory() as d:
        root = _project(Path(d).resolve(), nodes=7000)   # last build's artifact
        fake = FakeRun(rc=3)
        mod.subprocess.run = fake
        try:
            ok, note = mod.run_graphify(root)
        finally:
            mod.subprocess.run = real_run
        check("non-zero graphify exit fails even with a stale graph.json", ok is False)

    # 7. Opt-in set but the regeneration itself fails → the note must not tell the user
    #    to set the flag they already set.
    with tempfile.TemporaryDirectory() as d:
        root = _project(Path(d).resolve(), nodes=7000)
        calls = {"n": 0}

        def fail_cluster(cmd, kwargs):
            if "cluster-only" in cmd:
                calls["n"] += 1

        class ClusterFails(FakeRun):
            def __call__(self, cmd, *a, **kw):
                self.calls.append(list(cmd))
                self.envs.append(kw.get("env"))
                if self.on_call:
                    self.on_call(list(cmd), kw)
                rc = 4 if "cluster-only" in cmd else 0
                return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="boom")

        fake = ClusterFails(on_call=fail_cluster)
        os.environ["SUMELA_GRAPHIFY_VIZ"] = "1"
        mod.subprocess.run = fake
        try:
            ok, note = mod.run_graphify(root)
        finally:
            mod.subprocess.run = real_run
            os.environ.pop("SUMELA_GRAPHIFY_VIZ", None)
        check("failed opt-in regeneration is reported as such", ok is True and bool(note)
              and "regeneration failed" in note)
        check("failed opt-in regeneration does not re-suggest the flag already set",
              bool(note) and "SUMELA_GRAPHIFY_VIZ was set" in note)

    # 8. "false" / "no" / "off" are OFF — the words a user reaches for to disable a knob
    #    must not re-enable the expensive path.
    for word in ("false", "No", "OFF"):
        with tempfile.TemporaryDirectory() as d:
            root = _project(Path(d).resolve(), nodes=7000)
            fake = FakeRun()
            os.environ["SUMELA_GRAPHIFY_VIZ"] = word
            mod.subprocess.run = fake
            try:
                mod.run_graphify(root)
            finally:
                mod.subprocess.run = real_run
                os.environ.pop("SUMELA_GRAPHIFY_VIZ", None)
            check(f"SUMELA_GRAPHIFY_VIZ={word} is off", not fake.ran("cluster-only"))

    if check.failed:
        print(f"\n{check.failed} assertion(s) FAILED")
        sys.exit(1)
    print("\nAll graph-viz assertions passed")


if __name__ == "__main__":
    main()
