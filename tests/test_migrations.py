"""Real historical runtimes, migration locks, crash recovery and projection epochs."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest
import yaml
from perception_support import synthetic_notice
from test_mechanics import mechanical_package as mechanical_package

from trpg_state_machine import (
    ObjectState,
    SetObjectState,
    StateMachine,
    TypedCommand,
    load_adventure_package,
)
from trpg_state_machine.perception import NPCContext

ROOT = Path(__file__).resolve().parents[1]
HISTORY = {
    7: "74368459ec805a341d9b6e9451e218444a174fa6",
    6: "4d84e36ff889c94e107a86d27eb28f274ddc6136",
    1: "182efbe6b6b331fea274b5ae7863a8888e791339",
    2: "86ac1bf816d917c155cadf436097eb6b6f81a6f4",
    3: "1e289a9b57d72d955777ff1d781b1ba90f89112f",
    4: "16291de08b41ef147a2389bdb3d02235fa65cb0e",
    5: "0a3837d8a0ce83a47b2c8ef4bf37046562d712f5",
}


def archived_source(tmp_path, version):
    destination = tmp_path / f"source-{version}"
    archive = subprocess.run(
        [
            "git",
            "archive",
            "--format=zip",
            HISTORY[version],
            "src",
            "tests/fixtures/lantern_station.yaml",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as contents:
        contents.extractall(destination)
    return destination


def historical_database(tmp_path, version, fixture=None):
    source = archived_source(tmp_path, version)
    database = tmp_path / "historical.db"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests/_legacy_database_worker.py"),
            str(source),
            str(database),
            str(fixture or source / "tests/fixtures/lantern_station.yaml"),
            "yes" if fixture else "no",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    created = json.loads(result.stdout)
    assert created["schema"] == version
    return database, created["sessions"]


def stored_rows(database):
    with sqlite3.connect(database) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {
            name: (
                [r[1] for r in conn.execute(f"PRAGMA table_xinfo({name})")],
                set(conn.execute(f"SELECT * FROM {name}").fetchall()),
            )
            for (name,) in tables
        }


def assert_preserved(database, original):
    with sqlite3.connect(database) as conn:
        for table, (columns, rows) in original.items():
            after = set(conn.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall())
            assert rows <= after, table
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 29
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def assert_original_rows_equal(original, current, *, excluded=()):
    """Exact equality of every original column/row across additive schema changes."""
    for table, (columns, rows) in original.items():
        if table in excluded:
            continue
        current_columns, current_rows = current[table]
        indices = [current_columns.index(column) for column in columns]
        assert {tuple(row[i] for i in indices) for row in current_rows} == rows, table


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_actual_old_runtime_upgrade_preserves_authority_and_audits_history(
    tmp_path, package, context, version
):
    database, sessions = historical_database(tmp_path, version)
    original = stored_rows(database)
    with StateMachine(database) as sm:
        for sid in sessions:
            snapshot = sm.resume_session(sid, package, context=context)
            assert snapshot.package_digest == package.package_digest
            assert snapshot.ruleset_binding == package.ruleset_binding
            assert snapshot.character.current_hp == 11
            assert snapshot.rng.position == snapshot.rng.rng_version == 0
            assert sm.perception_cursor(sid, package, context=context) == snapshot.event_seq
            assert (
                sm.project_event(sid, 1, package, context=context, judgment=synthetic_notice)
                == "legacy_unavailable"
            )
        assert sm.resume_session(sessions[0], package, context=context).world_version == 3
        assert sm.resume_session(sessions[1], package, context=context).world_version == 0
        assert (
            sm._conn.execute("SELECT count(*) FROM legacy_perception_boundaries").fetchone()[0] == 2
        )
        # Historical work is retained as unavailable, never completed or false.
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM event_outbox WHERE consumer='perception' AND done=1"
            ).fetchone()[0]
            == 0
        )
        assert sm._conn.execute("SELECT count(*) FROM perception_results").fetchone()[0] == 0
        assert sm._conn.execute("SELECT count(*) FROM event_observations").fetchone()[0] == 0
        assert sm._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert_preserved(database, original)
    audit = stored_rows(database)
    for _ in range(2):
        with StateMachine(database) as sm:
            assert (
                sm.project_event(
                    sessions[0], 1, package, context=context, judgment=synthetic_notice
                )
                == "legacy_unavailable"
            )
        assert stored_rows(database) == audit


def upgrade_with_old_schema5(tmp_path, database):
    source = archived_source(tmp_path, 5)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0,sys.argv[1]); "
            "from trpg_state_machine import StateMachine; "
            "StateMachine(sys.argv[2]).close()",
            str(source / "src"),
            str(database),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )


def test_real_schema4_nonzero_rng_and_mechanical_receipt_survive_upgrade(
    tmp_path, mechanical_package, context
):
    package = mechanical_package
    database, sessions = historical_database(tmp_path, 4, tmp_path / "mechanics.yaml")
    original = stored_rows(database)
    with StateMachine(database) as sm:
        snapshot = sm.resume_session(sessions[0], package, context=context)
        assert snapshot.world_version == 4
        assert snapshot.rng.position == snapshot.rng.rng_version == 1
        assert snapshot.character.current_hp == 12
        assert snapshot.character.resources == {"resource.tokens": 2}
        assert snapshot.character.inventory == ("item.synthetic_token",)
        receipt = sm.get_command_receipt(sessions[0], "old.mechanical", package, context=context)
        assert receipt.status == "committed" and receipt.world_version == 4
    assert_preserved(database, original)


@pytest.mark.parametrize("via_schema5", [False, True])
def test_legacy_boundary_allows_same_session_new_observations_but_holds_new_gaps(
    tmp_path, package_data, context, via_schema5
):
    # Fully authored/validated forward-compatible typed input. This is NOT a
    # claim that the historical YAML loader supported the new senses field.
    for actor in package_data["actors"]:
        actor["senses"] = {"visual": True, "auditory": True}
    fixture = tmp_path / "approved.yaml"
    fixture.write_text(yaml.safe_dump(package_data), "utf-8")
    package = load_adventure_package(fixture).package
    assert package is not None
    database, sessions = historical_database(tmp_path, 4, fixture)
    if via_schema5:
        upgrade_with_old_schema5(tmp_path, database)
    original = stored_rows(database)
    with StateMachine(database) as sm:
        sid, other = sessions
        before = sm.resume_session(sid, package, context=context)
        boundary = before.event_seq
        sm.grant_npc_access(sid, "npc.caretaker", "npc.agent", package, context=context)
        npc = NPCContext("npc.agent", sid, "npc.caretaker")
        first = TypedCommand(
            sid,
            "new.open",
            before.pc_actor_id,
            "set_object_state",
            SetObjectState("scene.platform", "object.platform_gate", ObjectState.OPEN),
            3,
        )
        second = replace(
            first,
            command_id="new.close",
            expected_world_version=4,
            payload=replace(first.payload, state=ObjectState.CLOSED),
        )
        r1 = sm.submit_command(first, package, context=context)
        r2 = sm.submit_command(second, package, context=context)
        assert (
            sm.project_event(
                sid, r2.event_seqs[0], package, context=context, judgment=synthetic_notice
            )
            == "resolved"
        )
        assert sm.read_observations(sid, package, context=npc) == ()
        assert sm.perception_cursor(sid, package, context=context) == boundary
        assert (
            sm.project_event(
                sid, r1.event_seqs[0], package, context=context, judgment=synthetic_notice
            )
            == "resolved"
        )
        observations = sm.read_observations(sid, package, context=npc)
        assert len(observations) == 2
        assert sm.perception_cursor(sid, package, context=context) == r2.event_seqs[0]
        assert sm.perception_cursor(other, package, context=context) == 2
        assert (
            sm.project_event(sid, 1, package, context=context, judgment=synthetic_notice)
            == "legacy_unavailable"
        )
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM perception_results WHERE event_seq<=? AND session_id=?",
                (boundary, sid),
            ).fetchone()[0]
            == 0
        )
    # All original authority/receipt/evidence rows remain unchanged. Projection
    # cursor is intentionally advanced, so omit only that derived table here.
    original.pop("projection_cursors", None)
    # New commands legitimately modify these authoritative rows; equality was
    # checked by the separate historical-runtime test before any new command.
    for table in ("sessions", "objects", "rng_states"):
        original.pop(table, None)
    assert_preserved(database, original)
    with StateMachine(database) as sm:
        assert sm.read_observations(sid, package, context=npc) == observations
        assert (
            sm.project_event(
                sid, r1.event_seqs[0], package, context=context, judgment=synthetic_notice
            )
            == "already_complete"
        )
        assert sm._conn.execute("SELECT count(*) FROM npc_inbox").fetchone()[0] == 2


@pytest.mark.parametrize("version", [0, 1, 4, 5])
def test_concurrent_initialization_and_upgrade(tmp_path, version, monkeypatch):
    if version:
        database, _ = historical_database(tmp_path, 1 if version == 5 else version)
        if version == 5:
            upgrade_with_old_schema5(tmp_path, database)
        original = stored_rows(database)
    else:
        database, original = tmp_path / "empty.db", {}
    barrier = Barrier(4)
    connect = sqlite3.connect
    version_reads = []

    class TracedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "PRAGMA user_version" and not getattr(self, "read_version", False):
                version_reads.append(self.in_transaction)
                self.read_version = True
            return super().execute(sql, *args)

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: connect(*a, factory=TracedConnection, **k)
    )

    def open_runtime(_):
        barrier.wait(timeout=10)
        with StateMachine(database) as sm:
            return tuple(
                sm._conn.execute(f"PRAGMA {p}").fetchone()[0]
                for p in ("user_version", "foreign_keys", "journal_mode")
            )

    with ThreadPoolExecutor(max_workers=4) as workers:
        assert list(workers.map(open_runtime, range(4))) == [(29, 1, "wal")] * 4
    assert len(version_reads) >= 4 and all(version_reads)
    monkeypatch.undo()
    assert_preserved(database, original)


@pytest.mark.parametrize("missing_evidence", [False, True])
def test_schema5_mixed_history_does_not_archive_real_pending_work(
    tmp_path, package_data, context, missing_evidence
):
    for actor in package_data["actors"]:
        actor["senses"] = {"visual": True, "auditory": True}
    fixture = tmp_path / "approved.yaml"
    fixture.write_text(yaml.safe_dump(package_data), "utf-8")
    package = load_adventure_package(fixture).package
    database, sessions = historical_database(tmp_path, 4, fixture)
    upgrade_with_old_schema5(tmp_path, database)
    # Real schema-5 commands and projection commits, before the new migration.
    script = """
import json, sys
sys.path.insert(0, sys.argv[1])
from trpg_state_machine import (
    StateMachine, TrustedContext, TypedCommand, ObjectState, SetObjectState,
    SetSceneFlag, load_adventure_package,
)
package = load_adventure_package(sys.argv[3]).package
context = TrustedContext('authenticated:alice','player.alice')
sid = sys.argv[4]
with StateMachine(sys.argv[2]) as sm:
    before = sm.read_session(sid,package,context=context)
    receipts = []
    for key,kind,payload,version in [
        ('legacy.unavailable','set_scene_flag',SetSceneFlag('scene.platform','flag.lamp_checked',True),3),
        ('genuine.later','set_object_state',SetObjectState('scene.platform','object.platform_gate',ObjectState.OPEN),4)]:
        command = TypedCommand(sid,key,before.pc_actor_id,kind,payload,version)
        receipts.append(sm.submit_command(command,package,context=context))
    sm.grant_npc_access(sid,'npc.caretaker','npc.agent',package,context=context)
    assert sm.project_event(sid,receipts[1].event_seqs[0],package,context=context)=='resolved'
    if sys.argv[5]=='yes':
        sm._conn.execute('DELETE FROM event_evidence WHERE session_id=? AND event_seq=?',
                         (sid,receipts[0].event_seqs[0]))
    print(json.dumps({'boundary':before.event_seq,'first':receipts[0].event_seqs[0],'later':receipts[1].event_seqs[0]}))
"""
    run = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "source-5/src"),
            str(database),
            str(fixture),
            sessions[0],
            "yes" if missing_evidence else "no",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert run.returncode == 0, run.stderr
    saved = json.loads(run.stdout)
    sid = sessions[0]
    npc = NPCContext("npc.agent", sid, "npc.caretaker")
    with StateMachine(database) as sm:
        assert (
            sm._conn.execute(
                "SELECT through_event_seq FROM legacy_perception_boundaries WHERE session_id=?",
                (sid,),
            ).fetchone()[0]
            == saved["boundary"]
        )
        assert sm.perception_cursor(sid, package, context=context) == saved["boundary"]
        assert sm.read_observations(sid, package, context=npc) == ()
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM npc_inbox WHERE session_id=?", (sid,)
            ).fetchone()[0]
            == 1
        )
        status = sm.project_event(
            sid, saved["first"], package, context=context, judgment=synthetic_notice
        )
        assert status == ("unresolved" if missing_evidence else "resolved")
        if missing_evidence:
            assert sm.perception_cursor(sid, package, context=context) == saved["boundary"]
            assert sm.read_observations(sid, package, context=npc) == ()
            assert (
                sm._conn.execute(
                    "SELECT done FROM event_outbox WHERE session_id=? AND event_seq=? "
                    "AND consumer='perception'",
                    (sid, saved["first"]),
                ).fetchone()[0]
                == 0
            )
        else:
            assert sm.perception_cursor(sid, package, context=context) == saved["later"]
            # The old Observation is still visible behind the correct watermark,
            # but has no recoverable game-time lifetime, so is not Recent.
            assert sm.read_observations(sid, package, context=npc) == ()
            retained = sm.get_pending_npc_observation_work(sid, package, context=npc)
            assert len(retained) == 1 and retained[0].retention_status == "legacy_unknown"
    with StateMachine(database) as sm:
        assert sm.read_observations(sid, package, context=npc) == ()
        assert len(sm.get_pending_npc_observation_work(sid, package, context=npc)) == (
            0 if missing_evidence else 1
        )


@pytest.mark.parametrize("version", [0, 1, 3, 5])
def test_process_interruption_rolls_back_complete_migration(tmp_path, version):
    if version:
        database, _ = historical_database(tmp_path, 1 if version == 5 else version)
        if version == 5:
            upgrade_with_old_schema5(tmp_path, database)
        original = stored_rows(database)
    else:
        database, original = tmp_path / "empty.db", {}
    # Kill after real DDL/data changes, including schema-6 history/cursor writes.
    script = """
import os, sqlite3, sys
sys.path.insert(0, sys.argv[1])
original = sqlite3.connect
class CrashConnection(sqlite3.Connection):
    def execute(self, sql, *args):
        result = super().execute(sql, *args)
        if (sql.strip().upper() == 'DROP TABLE RNG_STATES;'
            or 'FROM legacy_perception_boundaries b JOIN sessions s' in sql):
            os._exit(73)
        return result
sqlite3.connect = lambda *a, **k: original(*a, factory=CrashConnection, **k)
from trpg_state_machine import StateMachine
StateMachine(sys.argv[2])
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(ROOT / "src"), str(database)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONUTF8": "1"},
        capture_output=True,
    )
    assert process.returncode == 73, process.stderr
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert stored_rows(database) == original
    with StateMachine(database):
        pass
    assert_preserved(database, original)
