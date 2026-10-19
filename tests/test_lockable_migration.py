"""Actual Schema24 history and transfer receipt preservation; atomic Schema25 upgrade."""

import io
import json
import sqlite3
import subprocess
import sys
import zipfile
from dataclasses import asdict
from pathlib import Path

import pytest
from item_support import item_data, item_loader
from test_migrations import assert_original_rows_equal, stored_rows

from trpg_state_machine import ObjectState, StateMachine, StateMachineError, world_command_from_data
from trpg_state_machine.npc_results import (
    NPCDoorIntent,
    NPCDropIntent,
    NPCEvaluationResult,
    NPCNavigationIntent,
    NPCPickupIntent,
    _result_json,
)
from trpg_state_machine.package import load_adventure_package


@pytest.mark.parametrize(
    "fault",
    [None, "CREATE TABLE door_locks", "CREATE TABLE door_credentials", "PRAGMA user_version = 25"],
)
def test_genuine_schema24_upgrade_is_atomic_and_does_not_grant_locks(
    tmp_path, context, monkeypatch, fault
):
    root = Path(__file__).parents[1]
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "1098bd749eaa2ca114f8d7e36e8058f6bec0089f", "src"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    source = tmp_path / "gc3"
    with zipfile.ZipFile(io.BytesIO(archive)) as contents:
        contents.extractall(source)
    document, db = tmp_path / "old.json", tmp_path / "old.db"
    document.write_text(json.dumps(item_data()), "utf-8")
    old = json.loads(
        subprocess.run(
            [
                sys.executable,
                str(root / "tests/_lock_legacy_worker.py"),
                str(source),
                str(db),
                str(document),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
    )
    assert old["schema"] == 24
    result = NPCEvaluationResult(
        action_intents=(
            NPCDoorIntent("scene.platform", "object.platform_gate", ObjectState.CLOSED, 7),
            NPCPickupIntent("object.dagger_a", "scene.platform", "2,1", 7),
            NPCDropIntent("object.dagger_a", "scene.platform", "1,1", 7),
            NPCNavigationIntent("activity.legacy", "scene.platform", "1,1", 7),
        )
    )
    assert _result_json(result) == old["npc_result_json"]
    original = stored_rows(db)
    if fault:
        connect = sqlite3.connect

        class Failing(sqlite3.Connection):
            def execute(self, sql, *args):
                result = super().execute(sql, *args)
                if fault in sql:
                    raise sqlite3.OperationalError("GC4 migration injected failure")
                return result

        monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, factory=Failing, **kw))
        with pytest.raises(StateMachineError):
            StateMachine(db, dnd_loader=item_loader())
        monkeypatch.undo()
        assert stored_rows(db) == original
        with sqlite3.connect(db) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 24
    p = load_adventure_package(document).package
    with StateMachine(db, dnd_loader=item_loader()) as sm:
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 36
        assert_original_rows_equal(original, stored_rows(db))
        assert sm._conn.execute("SELECT count(*) FROM door_locks").fetchone()[0] == 0
        assert sm._conn.execute("SELECT count(*) FROM door_credentials").fetchone()[0] == 0
        sm.resume_session(old["sid"], p, context=context)
        raw = old["command"]
        del raw["check_world_effects"]
        replay = sm.submit_command(world_command_from_data(raw), p, context=context)
        assert json.loads(json.dumps(asdict(replay))) == old["receipt"]
        assert (
            replay.status == "committed" and list(replay.event_seqs) == old["receipt"]["event_seqs"]
        )
        assert_original_rows_equal(original, stored_rows(db))
        assert sm._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_public_example_uses_common_pump_and_new_process_restore(tmp_path):
    root = Path(__file__).parents[1]
    # The example creates its own fresh SQLite and executes an actual restore subprocess.
    result = subprocess.run(
        [sys.executable, str(root / "examples/lockable_passages.py")],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["restored_in_new_process"]["locked"] is True
    assert report["restored_in_new_process"]["door"] == "closed"
    assert "object.amulet_a" in report["restored_in_new_process"]["owned_instances"]
