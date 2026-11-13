"""SQLite pending scans, on-demand prefixes, transaction snapshots and Schema 20."""

import io
import json
import os
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
from test_event_local_evidence import N1, A, command
from test_migrations import stored_rows
from test_npc_perception_queue import change, negative, prepared
from test_npc_perception_queue import queue_data as queue_data

from trpg_state_machine import ObjectState, SetSceneFlag, StateMachine, StateMachineError
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
