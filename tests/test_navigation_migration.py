"""Genuine RF-1 upgrade, legacy digests/receipts and new FK/uniqueness constraints."""

import io
import json
import sqlite3
import subprocess
import sys
import zipfile
from dataclasses import asdict
from pathlib import Path

from navigation_support import navigation_data
from test_migrations import assert_original_rows_equal, stored_rows

from trpg_state_machine import SetSceneFlag, StateMachine, TypedCommand, load_adventure_package


def test_actual_rf1_database_upgrade_preserves_all_rows_digest_and_command_replay(
    tmp_path, context
):
    root = Path(__file__).resolve().parents[1]
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "203b704270cda44fe136b6656bec25142f24642e", "src"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    source = tmp_path / "rf1"
    with zipfile.ZipFile(io.BytesIO(archive)) as contents:
        contents.extractall(source)
    data = navigation_data()
    data["manifest"]["schema_version"] = "state-machine-foundation/2"
    del data["navigation"]
    document = tmp_path / "original.json"
    document.write_text(json.dumps(data), "utf-8")
    db = tmp_path / "old.db"
    old = subprocess.run(
        [
            sys.executable,
            str(root / "tests/_navigation_baseline_worker.py"),
            str(source),
            str(db),
            str(document),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    old = json.loads(old.stdout)
    assert old["schema"] == 21
    original = stored_rows(db)
    package = load_adventure_package(document).package
    with StateMachine(db) as sm:
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 35
        assert_original_rows_equal(original, stored_rows(db))
        state = sm.resume_session(old["session_id"], package, context=context)
        receipt = sm.submit_command(
            TypedCommand(
                state.session_id,
                "legacy.flag",
                state.pc_actor_id,
                "set_scene_flag",
                SetSceneFlag(state.current_scene_id, "flag.lamp_checked", True),
                0,
            ),
            package,
            context=context,
        )
        assert json.loads(json.dumps(asdict(receipt))) == old["receipt"]
        assert package.navigation is None and state.package_digest == package.package_digest
        assert_original_rows_equal(original, stored_rows(db))
        assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert sm._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM navigation_activities").fetchone()[0] == 0
