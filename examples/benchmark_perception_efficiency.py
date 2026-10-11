"""Synthetic SQLite comparison with the actual Batch C runtime (no external services).

uv run --locked python examples/benchmark_perception_efficiency.py
Artifacts go under .test-tmp; timings are local samples, never production estimates.
"""

import argparse
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from copy import deepcopy
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "e23cde5"


def measured(conn, operation):
    statements, steps = [], 0

    def progress():
        nonlocal steps
        steps += 1
        return 0

    conn.set_trace_callback(statements.append)
    conn.set_progress_handler(progress, 1)
    start = perf_counter()
    try:
        value = operation()
    finally:
        elapsed = perf_counter() - start
        conn.set_trace_callback(None)
        conn.set_progress_handler(None, 0)
    return value, {
        "sql_statements": len(statements),
        "sqlite_vm_steps": steps,
        "seconds": round(elapsed, 6),
        "perception_cursor_writes": sum(
            "INSERT INTO npc_perception_cursors" in s for s in statements
        ),
        "completion_cursor_writes": sum(
            "INSERT INTO npc_completion_cursors" in s for s in statements
        ),
    }


def worker(source, directory, npcs, history):
    sys.path.insert(0, str(source))
    import yaml

    from trpg_state_machine import (
        CreateSessionRequest,
        PlayerSelection,
        StateMachine,
        TrustedContext,
    )
    from trpg_state_machine.package import _validate, canonical_json
    from trpg_state_machine.perception import JudgmentResult, NPCContext, SignalJudgment

    context = TrustedContext("perf.owner", "perf.player")
    data = yaml.safe_load((ROOT / "tests/fixtures/lantern_station.yaml").read_text("utf-8"))
    template = deepcopy(data["actors"][0])
    data["actors"] = []
    for scene in data["scenes"]:
        scene["actor_ids"] = []
    for i in range(npcs):
        actor = deepcopy(template)
        actor.update(actor_id=f"npc.perf{i}", initial_scene_id="scene.platform")
        data["actors"].append(actor)
        data["scenes"][0]["actor_ids"].append(actor["actor_id"])

    def create(sm, package):
        return sm.create_session(
            CreateSessionRequest(
                "perf.create",
                package.adventure_id,
                package.package_version,
                package.package_digest,
                (PlayerSelection(context.player_id, "pc_template.observer"),),
            ),
            package,
            context=context,
        ).session_id

    package = _validate(data)
    with StateMachine(directory / "fanout.db") as sm:
        sid = create(sm, package)
        npc = NPCContext("perf.worker", sid, "npc.perf0")
        sm.grant_npc_access(sid, npc.observer_id, npc.principal_id, package, context=context)
        with sm._transaction(write=True):
            staged = sm._capture_event(
                sid,
                package,
                "scene.flag_changed",
                {"scene_id": "scene.platform"},
                phase="perf.synthetic",
            )

        def append():
            with sm._transaction(write=True):
                sm._append_events(
                    sid, 0, [staged], source_command_id="perf.synthetic", package=package
                )

        _, commit = measured(sm._conn, append)
        _, judgment = measured(
            sm._conn,
            lambda: sm.project_npc_event(
                sid,
                2,
                package,
                context=npc,
                judgment=lambda request: JudgmentResult(
                    tuple(SignalJudgment(s.signal_id, False) for s in request.signals)
                ),
            ),
        )

    # Separate history fixture: one NPC, H complete rows followed by 40 pending.
    # Seed completion proofs in SQLite, explicitly synthetic (no JEV/LLM timing).
    data["actors"] = data["actors"][:1]
    data["actors"][0]["senses"] = {"visual": True, "auditory": True}
    data["scenes"][0]["actor_ids"] = ["npc.perf0"]
    package = _validate(data)
    with StateMachine(directory / "history.db") as sm:
        sid = create(sm, package)
        npc = NPCContext("perf.worker", sid, "npc.perf0")
        sm.grant_npc_access(sid, npc.observer_id, npc.principal_id, package, context=context)
        with sm._transaction(write=True):
            staged = sm._capture_event(
                sid,
                package,
                "object.state_changed",
                {"scene_id": "scene.platform", "object_id": "object.platform_gate"},
                phase="perf.synthetic",
            )
            sm._append_events(
                sid,
                0,
                [staged] * (history + 40),
                source_command_id="perf.synthetic",
                package=package,
            )
            sm._conn.execute(
                "INSERT INTO perception_results("
                "session_id,event_seq,observer_id,decisions_json,complete) "
                "SELECT session_id,event_seq,npc_id,?,1 FROM npc_perception_queue "
                "WHERE session_id=? AND event_seq<=?",
                (
                    canonical_json(
                        {"schema": "perception-decisions/2", "answers": {"signal.visible": False}}
                    ),
                    sid,
                    history + 2,
                ),
            )
            sm._conn.execute(
                "UPDATE npc_perception_queue SET complete=1 WHERE session_id=? AND event_seq<=?",
                (sid, history + 2),
            )

        def legacy_gap():
            return (
                sm._conn.execute(
                    "SELECT min(event_seq) FROM (SELECT event_seq FROM npc_perception_queue "
                    "WHERE session_id=? AND npc_id=? AND complete=0 UNION ALL "
                    "SELECT b.event_seq FROM perception_event_barriers b WHERE b.session_id=? "
                    "AND NOT EXISTS (SELECT 1 FROM npc_perception_queue q "
                    "WHERE q.session_id=b.session_id "
                    "AND q.event_seq=b.event_seq AND q.npc_id=? AND q.complete=1))",
                    (sid, npc.observer_id, sid, npc.observer_id),
                ).fetchone()[0]
                - 1
            )

        cursor_value, cursor = measured(
            sm._conn,
            lambda: (
                sm._npc_perception_cursor(sid, npc.observer_id)
                if hasattr(sm, "_npc_perception_cursor")
                else legacy_gap()
            ),
        )
        if cursor_value != history + 2:
            raise AssertionError("Synthetic history fixture produced an unexpected prefix")
        cursor["event_seq"] = cursor_value
        modern = hasattr(sm, "get_pending_npc_perception_queue")
        rows, queue = measured(
            sm._conn,
            lambda: (
                sm.get_pending_npc_perception_queue(sid, package, context=npc)
                if modern
                else sm.get_npc_perception_queue(sid, package, context=npc)
            ),
        )
        queue["returned_rows"] = len(rows)
        queue["completed_history_rows"] = history + 1
        sql = "SELECT * FROM npc_perception_queue WHERE session_id=? AND npc_id=? "
        sql += (
            "AND complete=0 AND event_seq>0 ORDER BY event_seq LIMIT 20"
            if modern
            else "ORDER BY event_seq"
        )
        queue["query_plan"] = [
            r[3] for r in sm._conn.execute("EXPLAIN QUERY PLAN " + sql, (sid, npc.observer_id))
        ]
        return {
            "schema": sm._conn.execute("PRAGMA user_version").fetchone()[0],
            "npcs": npcs,
            "event_commit": commit,
            "npc_judgment": judgment,
            "cursor_calculation": cursor,
            "queue_read": queue,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--npcs", type=int, default=200)
    parser.add_argument("--history", type=int, default=5000)
    parser.add_argument("--worker", nargs=2, metavar=("SOURCE", "DIRECTORY"))
    args = parser.parse_args()
    if args.npcs < 1 or args.history < 1:
        parser.error("npcs and history must be positive")
    if args.worker:
        print(
            json.dumps(worker(Path(args.worker[0]), Path(args.worker[1]), args.npcs, args.history))
        )
        return
    output = ROOT / ".test-tmp"
    output.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="perception-benchmark-", dir=output))
    archived = subprocess.run(
        ["git", "archive", "--format=zip", BASELINE, "src"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archived)) as archive:
        archive.extractall(directory / "baseline")
    results = {
        "baseline_commit": BASELINE,
        "sqlite_version": sqlite3.sqlite_version,
        "synthetic": True,
    }
    for name, source in (("before", directory / "baseline/src"), ("after", ROOT / "src")):
        run_dir = directory / name
        run_dir.mkdir()
        run = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                str(source),
                str(run_dir),
                "--npcs",
                str(args.npcs),
                "--history",
                str(args.history),
            ],
            cwd=ROOT,
            env=dict(os.environ, PYTHONUTF8="1"),
            check=True,
            capture_output=True,
            text=True,
        )
        results[name] = json.loads(run.stdout)
    result = json.dumps(results, indent=2)
    (directory / "results.json").write_text(result + "\n", encoding="utf-8")
    print(result)
    print(f"Evidence: {directory / 'results.json'}")


if __name__ == "__main__":
    main()
