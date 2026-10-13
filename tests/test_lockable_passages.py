"""GC4 public command/SQLite boundary and actual reliable NPC handoff tests."""

from dataclasses import replace

import pytest
from item_support import navigate
from lock_support import (
    CRED,
    DOOR,
    NPC,
    OTHER_CRED,
    OTHER_DOOR,
    TWIN,
    A,
    B,
    C,
    command,
    lock_loader,
    setup,
    submit,
)
from test_gc1_doors_and_effects import evidence, unchanged_except_receipt
from test_migrations import stored_rows
from test_npc_evaluation_tasks import create_task

from trpg_state_machine import (
    CancelNavigation,
    DropItem,
    LockDoor,
    ObjectState,
    PickupItem,
    SetObjectState,
    StartNavigation,
    StateMachine,
    StateMachineError,
    UnlockDoor,
    pump_navigation,
)
from trpg_state_machine.npc_results import NPCEvaluationResult, NPCLockIntent, NPCUnlockIntent
from trpg_state_machine.perception import NPCContext


def ok(receipt):
    assert receipt.status == "committed", receipt
    return receipt


def lock_state(sm, sid, door=DOOR):
    return tuple(
        sm._conn.execute(
            "SELECT o.state,l.locked FROM objects o JOIN door_locks l USING(session_id,object_id) "
            "WHERE session_id=? AND object_id=?",
            (sid, door),
        ).fetchone()
    )


def acquire(sm, sid, p, ctx, *, iid=CRED, actor=None, key="acquire"):
    navigate(sm, sid, p, ctx, (A, "1,0"), key + ".near", actor)
    ok(submit(sm, sid, p, ctx, PickupItem(iid, A, "2,0"), key + ".pick", actor))
    navigate(sm, sid, p, ctx, (A, "0,0"), key + ".return", actor)


def pump(sm, sid, p, ctx, actor, aid, key):
    return pump_navigation(
        sm.submit_command,
        sm.read_navigation_activity,
        sm.get_command_receipt,
        sid,
        actor,
        aid,
        p,
        context=ctx,
        request_id=key,
        budget=64,
    )


def test_navigation_block_cancel_acquire_unlock_cross_other_side_lock_and_restore(
    tmp_path, context
):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        rng = sm.read_rng_context(sid, p, context=context)
        ok(submit(sm, sid, p, context, StartNavigation("activity.first", B, "0,0"), "start"))
        blocked = pump(sm, sid, p, context, pc, "activity.first", "first")
        assert blocked.activity.status == "blocked"
        assert sm.read_actor_position(sid, pc, p, context=context).scene_id == A
        ok(
            submit(
                sm,
                sid,
                p,
                context,
                CancelNavigation("activity.first", blocked.activity.activity_version),
                "cancel",
            )
        )
        acquire(sm, sid, p, context)
        before = stored_rows(db)
        cmd = command(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "unlock")
        receipt = ok(sm.submit_command(cmd, p, context=context))
        assert lock_state(sm, sid) == ("closed", 0)
        event = sm.read_events(sid, p, context=context)[-1]
        assert event.event_type == "door.lock_changed"
        assert event.payload["credential_instance_id"] == CRED
        assert all(CRED not in s.content for s in event.signals)
        frozen = evidence(sm, sid, receipt.event_seqs[0])
        assert frozen["phase"]["code"] == "world.after_door_lock_update"
        assert frozen["source_actors"][0]["cell"] == "0,0"
        assert stored_rows(db)["objects"] == before["objects"]
        assert stored_rows(db)["mechanical_components"] == before["mechanical_components"]
        committed = stored_rows(db)
        assert sm.submit_command(cmd, p, context=context) == receipt
        assert sm.get_command_receipt(sid, "unlock", p, context=context) == receipt
        assert stored_rows(db) == committed
        with pytest.raises(StateMachineError, match="different content"):
            sm.submit_command(
                replace(cmd, payload=UnlockDoor(A, DOOR, TWIN, "0,0")), p, context=context
            )
        noop = ok(submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "noop"))
        assert noop.event_seqs == () and noop.world_version == receipt.world_version
        navigate(sm, sid, p, context, (B, "0,0"), "cross")
        assert lock_state(sm, sid) == ("open", 0)
        assert (
            submit(sm, sid, p, context, LockDoor(B, DOOR, CRED, "0,0"), "open.lock").error_code
            == "door_must_be_closed"
        )
        ok(submit(sm, sid, p, context, SetObjectState(B, DOOR, ObjectState.CLOSED), "close"))
        assert lock_state(sm, sid) == ("closed", 0)
        ok(submit(sm, sid, p, context, LockDoor(B, DOOR, CRED, "0,0"), "lock"))
        assert lock_state(sm, sid) == ("closed", 1)
        assert sm.read_rng_context(sid, p, context=context) == rng
        assert evidence(sm, sid, receipt.event_seqs[0]) == frozen
        before_restore = stored_rows(db)
    with StateMachine(db, dnd_loader=lock_loader()) as reopened:
        reopened.resume_session(sid, p, context=context)
        assert lock_state(reopened, sid) == ("closed", 1)
        assert stored_rows(db) == before_restore


@pytest.mark.parametrize("iid", [TWIN, OTHER_CRED, "object.unknown"])
def test_owned_same_slug_and_missing_credentials_fail_before_noop(tmp_path, context, iid):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        if iid != "object.unknown":
            acquire(sm, sid, p, context, iid=iid)
        before = stored_rows(db)
        operation = UnlockDoor(A, DOOR, iid, "0,0")
        query = sm.query_world_action(
            sid, sm.read_session(sid, p, context=context).pc_actor_id, operation, p, context=context
        )
        assert query.reason == "credential_unavailable"
        assert stored_rows(db) == before
        assert (
            submit(sm, sid, p, context, operation, "wrong").error_code == "credential_unavailable"
        )
        unchanged_except_receipt(before, stored_rows(db))


def test_latest_ownership_drop_transfer_no_automatic_relock_and_two_doors(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        acquire(sm, sid, p, context)
        ok(submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "unlock"))
        ok(submit(sm, sid, p, context, DropItem(CRED, A, "1,0"), "drop"))
        assert lock_state(sm, sid) == ("closed", 0)
        for operation in (UnlockDoor(A, DOOR, CRED, "0,0"), LockDoor(A, DOOR, CRED, "0,0")):
            before = stored_rows(db)
            assert (
                submit(sm, sid, p, context, operation, type(operation).__name__).error_code
                == "credential_unavailable"
            )
            unchanged_except_receipt(before, stored_rows(db))
        navigate(sm, sid, p, context, (A, "1,1"), "npc.item", NPC)
        ok(submit(sm, sid, p, context, PickupItem(CRED, A, "1,0"), "npc.pick", NPC))
        navigate(sm, sid, p, context, (B, "1,2"), "pc.clear")
        navigate(sm, sid, p, context, (A, "0,0"), "npc.near", NPC)
        ok(
            submit(
                sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.CLOSED), "npc.close", NPC
            )
        )
        ok(submit(sm, sid, p, context, LockDoor(A, DOOR, CRED, "0,0"), "npc.lock", NPC))
        ok(submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "npc.unlock", NPC))
        navigate(sm, sid, p, context, (B, "2,1"), "npc.second", NPC)
        ok(submit(sm, sid, p, context, UnlockDoor(B, OTHER_DOOR, CRED, "2,1"), "second", NPC))
        assert lock_state(sm, sid, OTHER_DOOR) == ("closed", 0)
        assert lock_state(sm, sid) == ("open", 0)
        navigate(sm, sid, p, context, (C, "0,0"), "npc.store", NPC)
        ok(
            submit(
                sm,
                sid,
                p,
                context,
                SetObjectState(C, OTHER_DOOR, ObjectState.CLOSED),
                "second.close",
                NPC,
            )
        )
        ok(submit(sm, sid, p, context, LockDoor(C, OTHER_DOOR, CRED, "0,0"), "second.lock", NPC))
        assert lock_state(sm, sid, OTHER_DOOR) == ("closed", 1)
        assert lock_state(sm, sid) == ("open", 0)


@pytest.mark.parametrize(
    "table", ["door_locks", "event_evidence", "event_outbox", "command_receipts", "sessions"]
)
@pytest.mark.parametrize("locking", [False, True])
def test_atomic_sql_faults_retry_and_replay(tmp_path, context, table, locking):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        acquire(sm, sid, p, context)
        if locking:
            ok(submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "prepare"))
        operation = (LockDoor if locking else UnlockDoor)(A, DOOR, CRED, "0,0")
        cmd = command(sm, sid, p, context, operation, "fault")
        verb = "UPDATE" if table in ("door_locks", "sessions") else "INSERT"
        sm._conn.execute(
            f"CREATE TRIGGER fault BEFORE {verb} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'GC4 fault'); END"
        )
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(cmd, p, context=context)
        assert exc.value.code == "storage_error"
        assert stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER fault")
        receipt = ok(sm.submit_command(cmd, p, context=context))
        saved = stored_rows(db)
        assert sm.submit_command(cmd, p, context=context) == receipt
        assert stored_rows(db) == saved


def test_npc_typed_task_handoff_lock_unlock_and_control_revocation(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        acquire(sm, sid, p, context)
        ok(submit(sm, sid, p, context, DropItem(CRED, A, "1,0"), "give"))
        navigate(sm, sid, p, context, (A, "2,0"), "pc.clear")
        navigate(sm, sid, p, context, (A, "1,0"), "npc.acquire", NPC)
        ok(submit(sm, sid, p, context, PickupItem(CRED, A, "1,0"), "npc.pick", NPC))
        navigate(sm, sid, p, context, (A, "0,0"), "npc.port", NPC)
        sm.grant_npc_access(sid, NPC, "agent:gc4", p, context=context)
        npc_ctx = NPCContext("agent:gc4", sid, NPC)
        for i, intent_type in enumerate((NPCUnlockIntent, NPCLockIntent, NPCUnlockIntent)):
            task = create_task(
                sm,
                sid,
                p,
                npc_ctx,
                key=f"task.{i}",
                expected_npc_state_version=sm.get_npc_view(
                    sid, NPC, p, context=npc_ctx
                ).npc_state_version,
            )
            version = sm.read_session(sid, p, context=context).world_version
            result = NPCEvaluationResult(
                action_intents=(intent_type(A, DOOR, CRED, "0,0", version),)
            )
            before = lock_state(sm, sid)
            selection = sm.submit_npc_evaluation_result(
                task.evaluation_id, result, p, context=npc_ctx
            )
            assert selection.outputs[0].status == "pending"
            assert lock_state(sm, sid) == before
            if i == 2:
                saved = stored_rows(db)
                with pytest.raises(StateMachineError):
                    sm.dispatch_npc_output(
                        task.evaluation_id, 0, p, context=replace(context, controlled_npc_ids=())
                    )
                assert stored_rows(db) == saved
                sm.grant_npc_access(sid, NPC, "agent:replacement", p, context=context)
                saved = stored_rows(db)
                with pytest.raises(StateMachineError):
                    sm.dispatch_npc_output(task.evaluation_id, 0, p, context=context)
                assert stored_rows(db) == saved
                break
            delivery = sm.dispatch_npc_output(task.evaluation_id, 0, p, context=context)
            ok(delivery.command_receipt)
            assert lock_state(sm, sid) == ("closed", int(i == 1))
            assert sm.dispatch_npc_output(task.evaluation_id, 0, p, context=context) == delivery
            assert (
                sm.submit_npc_evaluation_result(task.evaluation_id, result, p, context=npc_ctx)
                == selection
            )
        assert sm.read_actor_position(sid, pc, p, context=context).cell == "2,0"


def test_response_loss_recovery_and_capture_failure_are_atomic(tmp_path, context, monkeypatch):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        acquire(sm, sid, p, context)
        cmd = command(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "lost")
        capture = sm._event_capture._capture_event

        def fail_capture(*args, **kwargs):
            capture(*args, **kwargs)
            raise StateMachineError("storage_error", "Injected failure after stage capture")

        monkeypatch.setattr(sm._event_capture, "_capture_event", fail_capture)
        before = stored_rows(db)
        with pytest.raises(StateMachineError):
            sm.submit_command(cmd, p, context=context)
        assert stored_rows(db) == before
        monkeypatch.undo()

        def lose_response():
            ok(sm.submit_command(cmd, p, context=context))
            raise ConnectionError("Response lost after durable commit")

        with pytest.raises(ConnectionError):
            lose_response()
        committed = stored_rows(db)
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        receipt = restored.get_command_receipt(sid, cmd.command_id, p, context=context)
        assert receipt.status == "committed"
        assert restored.submit_command(cmd, p, context=context) == receipt
        assert stored_rows(db) == committed


@pytest.mark.parametrize("locking", [False, True])
def test_target_state_noop_still_requires_current_credential(tmp_path, context, locking):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        acquire(sm, sid, p, context)
        if not locking:
            ok(submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "unlock"))
        operation = (LockDoor if locking else UnlockDoor)(A, DOOR, CRED, "0,0")
        before = stored_rows(db)
        receipt = ok(submit(sm, sid, p, context, operation, "noop"))
        assert receipt.event_seqs == ()
        unchanged_except_receipt(before, stored_rows(db))
        ok(submit(sm, sid, p, context, DropItem(CRED, A, "1,0"), "drop"))
        before = stored_rows(db)
        assert (
            submit(sm, sid, p, context, operation, "unowned.noop").error_code
            == "credential_unavailable"
        )
        unchanged_except_receipt(before, stored_rows(db))
