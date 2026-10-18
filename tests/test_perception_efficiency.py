"""SQLite pending scans, on-demand prefixes, transaction snapshots and Schema 20."""

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
from dnd_support import create
from perception_support import synthetic_notice
from test_event_local_evidence import N1, N2, A, command
from test_migrations import assert_original_rows_equal, stored_rows
from test_npc_perception_queue import change, negative, prepared
from test_npc_perception_queue import queue_data as queue_data

from trpg_state_machine import ObjectState, SetSceneFlag, StateMachine, StateMachineError
from trpg_state_machine.package import _validate
from trpg_state_machine.perception import NPCContext
from trpg_state_machine.perception_queue import (
    _NPC_PERCEPTION_CURSOR_SQL,
    _PENDING_PERCEPTION_SQL,
)

ROOT = Path(__file__).resolve().parents[1]


def unrelated(sm, sid, package, count):
    """Synthetic volume fixture, using the real event/evidence/outbox commit path."""
    with sm._transaction(write=True):
        staged = sm._capture_event(
            sid, package, "scene.flag_changed", {"scene_id": A}, phase="test.unrelated"
        )
        version = sm._conn.execute(
            "SELECT world_version FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()[0]
        sm._append_events(
            sid, version, [staged] * count, source_command_id="test.volume", package=package
        )


def test_many_unrelated_events_no_cursor_writes_or_npc_sweep(tmp_path, queue_data, context):
    sm, _, sid, package, (one, two) = prepared(tmp_path, queue_data, context)
    with sm:
        first, _, _ = change(sm, sid, package, context, ObjectState.OPEN, "gap")
        statements = []
        sm._conn.set_trace_callback(statements.append)
        unrelated(sm, sid, package, 2000)
        sm._conn.set_trace_callback(None)
        assert not any(
            "npc_completion_cursors" in s or "npc_perception_cursors" in s for s in statements
        )
        assert not any("FROM actors" in s for s in statements)
        assert sm.npc_perception_cursor(sid, package, context=one) == first - 1
        assert (
            sm.project_npc_event(sid, first, package, context=one, judgment=negative) == "resolved"
        )
        assert sm.npc_perception_cursor(sid, package, context=one) == first + 2000
        assert sm.npc_completed_cursor(sid, package, context=one) == first + 2000
        assert sm.npc_perception_cursor(sid, package, context=two) == first - 1
        assert sm.npc_completed_cursor(sid, package, context=two) == first - 1


def test_pending_pagination_retry_order_and_history_compatibility(tmp_path, queue_data, context):
    sm, _, sid, package, (one, two) = prepared(tmp_path, queue_data, context)
    with sm:
        seqs = [
            change(
                sm,
                sid,
                package,
                context,
                ObjectState.OPEN if i % 2 == 0 else ObjectState.CLOSED,
                str(i),
            )[0]
            for i in range(47)
        ]
        assert (
            sm.project_npc_event(sid, seqs[1], package, context=one, judgment=negative)
            == "resolved"
        )
        assert sm.project_npc_event(sid, seqs[0], package, context=one) == "unresolved"
        expected = [s for s in seqs if s != seqs[1]]
        first = sm.get_pending_npc_perception_queue(sid, package, context=one)
        assert len(first) == 20 and all(not r.complete for r in first)
        assert first == sm.get_pending_npc_perception_queue(sid, package, context=one)
        pages, after = [], None
        while page := sm.get_pending_npc_perception_queue(
            sid, package, context=one, limit=7, after_event_seq=after
        ):
            pages.extend(r.event_seq for r in page)
            after = page[-1].event_seq
        assert pages == expected
        assert [
            r.event_seq
            for r in sm.get_pending_npc_perception_queue(sid, package, context=two, limit=1000)
        ] == seqs
        # A scan position is not an ACK; polling again includes the unresolved first event.
        assert (
            sm.get_pending_npc_perception_queue(sid, package, context=one, limit=1)[0].event_seq
            == seqs[0]
        )
        assert sm.npc_perception_cursor(sid, package, context=one) == seqs[0] - 1
        assert (
            sm.get_pending_npc_perception_queue(
                sid, package, context=one, after_event_seq=2**63 - 1
            )
            == ()
        )
        history = sm.get_npc_perception_queue(sid, package, context=one)
        assert len(history) == 48 and any(r.complete for r in history)


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5, "20", None])
def test_pending_limit_validation(tmp_path, queue_data, context, limit):
    sm, db, sid, package, (one, _) = prepared(tmp_path, queue_data, context)
    with sm:
        before = stored_rows(db)
        with pytest.raises(StateMachineError, match="limit") as error:
            sm.get_pending_npc_perception_queue(sid, package, context=one, limit=limit)
        assert error.value.code == "invalid_request" and stored_rows(db) == before


@pytest.mark.parametrize("after", [-1, True, 1.5, "1", 2**63])
def test_pending_after_validation(tmp_path, queue_data, context, after):
    sm, _, sid, package, (one, _) = prepared(tmp_path, queue_data, context)
    with sm, pytest.raises(StateMachineError) as error:
        sm.get_pending_npc_perception_queue(sid, package, context=one, after_event_seq=after)
    assert error.value.code == "invalid_request"


def test_pending_authorization_session_scope_and_index(tmp_path, queue_data, context):
    sm, _, sid, package, (one, _) = prepared(tmp_path, queue_data, context)
    with sm:
        seq, _, _ = change(sm, sid, package, context, ObjectState.OPEN, "pending")
        other = create(sm, package, context, "other")
        bound = replace(one, session_id=other)
        sm.grant_npc_access(other, N1, one.principal_id, package, context=context)
        assert [
            r.event_seq for r in sm.get_pending_npc_perception_queue(other, package, context=bound)
        ] == [2]
        for target, ctx in ((sid, bound), (other, one), (sid, replace(one, principal_id="spoof"))):
            with pytest.raises(StateMachineError) as error:
                sm.get_pending_npc_perception_queue(target, package, context=ctx)
            assert error.value.code == "unauthorized"
        assert [
            r.event_seq for r in sm.get_pending_npc_perception_queue(sid, package, context=one)
        ] == [seq]
        for sql, args in (
            (_PENDING_PERCEPTION_SQL, (sid, N1, 0, 20)),
            (_NPC_PERCEPTION_CURSOR_SQL, (N1, N1, sid)),
        ):
            plan = [r[3] for r in sm._conn.execute("EXPLAIN QUERY PLAN " + sql, args)]
            assert any("npc_perception_pending" in row for row in plan), plan
            assert not any("TEMP B-TREE" in row for row in plan), plan
        sm.grant_npc_access(sid, N1, "replacement", package, context=context)
        with pytest.raises(StateMachineError) as error:
            sm.get_pending_npc_perception_queue(sid, package, context=one)
        assert error.value.code == "unauthorized"


def test_cursor_reads_share_snapshot_with_observations_during_concurrent_commit(
    tmp_path, queue_data, context
):
    sm, db, sid, package, (one, _) = prepared(tmp_path, queue_data, context)
    with sm:
        seq, _, _ = change(sm, sid, package, context, ObjectState.OPEN, "pending")
        rendezvous = Barrier(2)

        def commit():
            with StateMachine(db) as writer:
                rendezvous.wait(timeout=10)
                writer.project_npc_event(sid, seq, package, context=one, judgment=synthetic_notice)
                unrelated(writer, sid, package, 100)
                rendezvous.wait(timeout=10)

        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(commit)
            with sm._transaction():
                assert sm._npc_perception_cursor(sid, N1) == seq - 1
                rendezvous.wait(timeout=10)
                rendezvous.wait(timeout=10)
                assert sm._npc_perception_cursor(sid, N1) == seq - 1
                assert not any(r["event_seq"] == seq for r in sm._observation_rows(sid, N1))
            future.result(timeout=10)
        assert sm.npc_perception_cursor(sid, package, context=one) == seq + 100
        assert sm.read_observations(sid, package, context=one)[-1].observer_id == N1
        assert sm.npc_completed_cursor(sid, package, context=one) == seq - 1


def schema19_save(tmp_path, data):
    source = tmp_path / "schema19"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "e23cde5", "src"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as files:
        files.extractall(source)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(data), encoding="utf-8")
    db = tmp_path / "legacy.db"
    script = """
import json,sys
sys.path.append(sys.argv[1])
from test_npc_perception_queue import prepared,change
from trpg_state_machine import ObjectState,TrustedContext
from pathlib import Path
context=TrustedContext('authenticated:alice','player.alice')
sm,db,sid,package,contexts=prepared(Path(sys.argv[2]),json.loads(Path(sys.argv[3]).read_text()),context)
change(sm,sid,package,context,ObjectState.OPEN,'pending')
sm.close()
print(sid)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT / "tests"), str(tmp_path), str(fixture)],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=str(source / "src")),
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "queue.db").rename(db)
    return db, result.stdout.strip()


@pytest.mark.parametrize(
    "crash_at",
    [
        "CREATE INDEX npc_perception_pending",
        "DROP TABLE npc_perception_cursors",
        "DROP TABLE scene_instances",
        "DROP TABLE objects",
        "ALTER TABLE actors ADD COLUMN cell",
    ],
)
def test_schema20_21_process_crash_rollback_and_multiprocess_recovery(
    tmp_path, queue_data, context, crash_at
):
    db, sid = schema19_save(tmp_path, queue_data)
    before = stored_rows(db)
    script = """
import os,sqlite3,sys
connect=sqlite3.connect
class Crashing(sqlite3.Connection):
    def execute(self,sql,*args):
        result=super().execute(sql,*args)
        if sys.argv[2] in sql:
            os._exit(73)
        return result
sqlite3.connect=lambda *a,**k: connect(*a,factory=Crashing,**k)
from trpg_state_machine import StateMachine
StateMachine(sys.argv[1])
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(db), crash_at],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
        capture_output=True,
    )
    assert result.returncode == 73, result.stderr
    assert stored_rows(db) == before
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 19
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='npc_perception_pending'"
        ).fetchone()
    opener = """
from trpg_state_machine import StateMachine
import sys
with StateMachine(sys.argv[1]) as sm:
    print(sm._conn.execute('PRAGMA user_version').fetchone()[0])
"""
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", opener, str(db)],
            cwd=tmp_path,
            env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(3)
    ]
    for worker in workers:
        stdout, stderr = worker.communicate(timeout=30)
        assert worker.returncode == 0 and stdout.strip() == b"34", stderr
    package = _validate(queue_data)
    for _ in range(2):
        with StateMachine(db) as sm:
            for npc in (N1, N2):
                ctx = NPCContext("worker:" + npc, sid, npc)
                assert sm.npc_perception_cursor(sid, package, context=ctx) == 2
                assert [
                    r.event_seq
                    for r in sm.get_pending_npc_perception_queue(sid, package, context=ctx)
                ] == [3]
            assert sm._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    after = stored_rows(db)
    assert "npc_perception_cursors" not in after
    assert set(before) - {"npc_perception_cursors"} == set(after) - {
        "scene_environments",
        "world_time_bindings",
        "navigation_activities",
        "mechanical_components",
        "door_locks",
        "door_credentials",
        "object_durability",
        "world_result_adoptions",
        "object_openables",
        "object_lights",
        "object_mechanisms",
    }
    assert after["navigation_activities"][1] == set()
    assert after["mechanical_components"][1] == set()
    assert after["door_locks"][1] == set()
    assert after["door_credentials"][1] == set()
    for table in (
        "scene_environments",
        "world_time_bindings",
        "object_durability",
        "world_result_adoptions",
        "object_openables",
        "object_lights",
        "object_mechanisms",
    ):
        assert after[table][1] == set()
    assert_original_rows_equal(before, after, excluded=("npc_perception_cursors",))


def test_event_submit_and_pending_read_concurrent(tmp_path, queue_data, context):
    sm, db, sid, package, (one, _) = prepared(tmp_path, queue_data, context)
    with sm:
        rendezvous = Barrier(2)

        def submit():
            with StateMachine(db) as writer:
                cmd = command(
                    writer,
                    sid,
                    package,
                    context,
                    SetSceneFlag(A, "flag.lamp_checked", True),
                    "set_scene_flag",
                    "concurrent",
                )
                rendezvous.wait(timeout=10)
                receipt = writer.submit_command(cmd, package, context=context)
                rendezvous.wait(timeout=10)
                return receipt

        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(submit)
            with sm._transaction():
                before = sm._npc_perception_cursor(sid, N1)
                rendezvous.wait(timeout=10)
                rendezvous.wait(timeout=10)
                assert sm._npc_perception_cursor(sid, N1) == before
            receipt = future.result(timeout=10)
        assert sm.get_pending_npc_perception_queue(sid, package, context=one) == ()
        assert sm.npc_perception_cursor(sid, package, context=one) == receipt.event_seqs[-1]


def test_schema20_sql_failure_rolls_back_then_reopens(tmp_path, queue_data, monkeypatch):
    db, _ = schema19_save(tmp_path, queue_data)
    before = stored_rows(db)
    connect = sqlite3.connect

    class Failing(sqlite3.Connection):
        def execute(self, sql, *args):
            result = super().execute(sql, *args)
            if "DROP TABLE npc_perception_cursors" in sql:
                raise sqlite3.OperationalError("injected migration failure after DROP")
            return result

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connect(*a, factory=Failing, **k))
    with pytest.raises(StateMachineError) as error:
        StateMachine(db)
    assert error.value.code == "storage_error"
    monkeypatch.undo()
    assert stored_rows(db) == before
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 19
    with StateMachine(db) as sm:
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 34
