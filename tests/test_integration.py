import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "lantern_station.yaml"
WORKER = ROOT / "tests" / "_restart_worker.py"


def run_process(mode, database):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1"}
    completed = subprocess.run(
        [sys.executable, str(WORKER), mode, str(database), str(FIXTURE)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize("shutdown", ["prepare", "crash"])
def test_create_enter_mutate_leave_revisit_save_restart_resume(tmp_path, shutdown):
    database = tmp_path / "restart.sqlite3"
    saved = run_process(shutdown, database)
    sid = saved["creation"]["session_id"]
    # Read actual durable rows independently, before the recovery process runs.
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute(
            "SELECT current_scene_id, world_version, event_seq FROM sessions WHERE session_id = ?",
            (sid,),
        ).fetchone() == ("scene.workshop", 6, 11)
        assert (
            db.execute(
                "SELECT state FROM objects "
                "WHERE session_id = ? AND object_id = 'object.workshop_door'",
                (sid,),
            ).fetchone()[0]
            == "open"
        )
        flags, visits = db.execute(
            "SELECT flags_json, visited_count FROM scene_instances "
            "WHERE session_id = ? AND scene_id = 'scene.workshop'",
            (sid,),
        ).fetchone()
        assert json.loads(flags) == {"flag.workshop_checked": True}
        assert visits == 2
        assert (
            db.execute(
                "SELECT current_scene_id FROM actors "
                "WHERE session_id = ? AND actor_id = 'npc.caretaker'",
                (sid,),
            ).fetchone()[0]
            == "scene.workshop"
        )
        assert db.execute(
            "SELECT current_hp, max_hp FROM characters WHERE session_id = ?", (sid,)
        ).fetchone() == (11, 12)
        assert db.execute(
            "SELECT event_seq FROM events WHERE session_id = ? ORDER BY event_seq", (sid,)
        ).fetchall() == [(i,) for i in range(1, 12)]
    # Fresh interpreter + fresh connection + freshly loaded immutable package.
    resumed = run_process("resume", database)
    assert resumed == saved
    assert resumed["state"]["player"]["pc_template_id"] == "pc_template.visitor"
    assert resumed["state"]["rng"] == saved["state"]["rng"]
    assert resumed["state"]["ruleset_binding"] == saved["state"]["ruleset_binding"]
    assert resumed["state"]["package_digest"] == saved["creation"]["package_digest"]
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM creation_commands").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 11
