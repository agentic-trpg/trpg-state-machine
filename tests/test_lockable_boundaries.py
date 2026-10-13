"""GC4 admission, static knowledge, automatic effects and race regressions."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from threading import Barrier

import pytest
from dnd_support import initialize
from item_support import navigate
from lock_support import (
    CRED,
    DOOR,
    NPC,
    OTHER_DOOR,
    PASSAGE,
    A,
    B,
    command,
    create,
    lock_data,
    lock_loader,
    setup,
    submit,
)
from test_events import event_data
from test_gc1_doors_and_effects import unchanged_except_receipt
from test_lockable_passages import acquire, lock_state, ok, pump
from test_migrations import stored_rows
from test_npc_evaluation_tasks import create_task
from test_real_checks import check_command

from trpg_state_machine import (
    CheckWorldEffects,
    DropItem,
    EnterScene,
    LockDoor,
    MoveActor,
    ObjectState,
    PickupItem,
    ResumeNavigation,
    SetObjectState,
    SetSceneFlag,
    StartNavigation,
    StateMachine,
    StateMachineError,
    StepActor,
    TraversePassage,
    UnlockDoor,
    world_command_from_data,
)
from trpg_state_machine.npc_results import NPCEvaluationResult, NPCLockIntent, NPCUnlockIntent
from trpg_state_machine.package import _validate
from trpg_state_machine.perception import NPCContext


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_door",
        "duplicate_credential",
        "unknown_door",
        "nonpassage_switch",
        "unknown_instance",
        "slug",
        "empty",
        "open_locked",
        "integer",
        "policy",
        "extra",
        "nonportable",
        "private_side",
    ],
)
def test_closed_configuration_checks_instance_references_and_both_ports(mutation):
    d = lock_data()
    lock = d["lockable_doors"][0]
    if mutation == "duplicate_door":
        d["lockable_doors"].append(deepcopy(lock))
    elif mutation == "duplicate_credential":
        lock["credential_instance_ids"].append(CRED)
    elif mutation == "unknown_door":
        lock["object_id"] = "object.absent"
    elif mutation == "nonpassage_switch":
        lock["object_id"] = CRED
    elif mutation == "unknown_instance":
        lock["credential_instance_ids"] = ["object.absent"]
    elif mutation == "slug":
        lock["credential_instance_ids"] = ["amulet"]
    elif mutation == "empty":
        lock["credential_instance_ids"] = []
    elif mutation == "open_locked":
        d["scenes"][0]["objects"][0]["state"] = "open"
    elif mutation == "integer":
        lock["initial_locked"] = 1
    elif mutation == "policy":
        lock["policy"] = "admin_bypass"
    elif mutation == "extra":
        lock["bypass_lock"] = True
    elif mutation == "nonportable":
        lock["credential_instance_ids"] = [OTHER_DOOR]
    elif mutation == "private_side":
        d["scenes"][1]["access_policy"] = "adjudication_required"
    with pytest.raises(StateMachineError):
        _validate(d)


@pytest.mark.parametrize(
    "operation",
    [
        SetObjectState(A, DOOR, ObjectState.OPEN),
        EnterScene(B),
        TraversePassage(PASSAGE, A, "0,0"),
        StepActor(A, "0,0", "0,1"),
    ],
)
def test_old_open_and_all_pc_traversal_entrances_cannot_bypass(tmp_path, context, operation):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        before = stored_rows(db)
        receipt = submit(sm, sid, p, context, operation, "bypass")
        assert receipt.status == "rejected"
        assert receipt.error_code in ("door_locked", "passage_unavailable", "movement_unavailable")
        unchanged_except_receipt(before, stored_rows(db))
        assert lock_state(sm, sid) == ("closed", 1)


def test_npc_move_and_door_intent_do_not_bypass(tmp_path, context):
    from trpg_state_machine.npc_results import NPCDoorIntent

    data = lock_data()
    data["pc_templates"][0]["initial_cell"] = "2,0"
    data["actors"][0]["initial_cell"] = "0,0"
    sm, sid, p, db = setup(tmp_path, context, data)
    with sm:
        before = stored_rows(db)
        assert (
            submit(sm, sid, p, context, MoveActor(NPC, B), "move", NPC).error_code
            == "passage_unavailable"
        )
        unchanged_except_receipt(before, stored_rows(db))
        npc = NPCContext("agent:gc4", sid, NPC)
        task = create_task(sm, sid, p, npc)
        version = sm.read_session(sid, p, context=context).world_version
        result = NPCEvaluationResult(
            action_intents=(NPCDoorIntent(A, DOOR, ObjectState.OPEN, version),)
        )
        sm.submit_npc_evaluation_result(task.evaluation_id, result, p, context=npc)
        before = stored_rows(db)
        delivery = sm.dispatch_npc_output(task.evaluation_id, 0, p, context=context)
        assert delivery.command_receipt.error_code == "door_locked"
        assert stored_rows(db)["door_locks"] == before["door_locks"]
        assert stored_rows(db)["objects"] == before["objects"]
        assert stored_rows(db)["event_evidence"] == before["event_evidence"]


@pytest.mark.parametrize("locked", [True, False])
def test_package_automatic_open_is_atomic_and_retains_effect_provenance(tmp_path, context, locked):
    data = lock_data()
    data["lockable_doors"][0]["initial_locked"] = locked
    data["events"] = [event_data()]
    data["scenes"][0]["event_definition_ids"] = ["event.open_gate"]
    sm, sid, p, db = setup(tmp_path, context, data)
    with sm:
        before = stored_rows(db)
        receipt = submit(sm, sid, p, context, SetSceneFlag(A, "flag.lamp_checked", True), "trigger")
        if locked:
            assert receipt.error_code == "door_locked"
            unchanged_except_receipt(before, stored_rows(db))
        else:
            ok(receipt)
            assert lock_state(sm, sid) == ("open", 0)
            event = next(
                e
                for e in sm.read_events(sid, p, context=context)
                if e.event_type == "object.state_changed"
            )
            assert event.payload["execution"] == {
                "kind": "package",
                "reference_id": "event.open_gate",
            }


def test_real_check_world_effect_refusal_rolls_back_rng_and_every_consequence(tmp_path, context):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        cmd = replace(
            check_command(sm, sid, p, context, dc=1),
            check_world_effects=CheckWorldEffects(
                "gm.locked",
                (),
                (
                    SetSceneFlag(A, "flag.lamp_checked", True),
                    SetObjectState(A, DOOR, ObjectState.OPEN),
                ),
                (),
            ),
        )
        before = stored_rows(db)
        result = sm.submit_command(cmd, p, context=context)
        assert result.status == "rejected" and result.error_code == "door_locked"
        unchanged_except_receipt(before, stored_rows(db))
        committed = stored_rows(db)
        assert sm.submit_command(cmd, p, context=context) == result
        assert stored_rows(db) == committed


def test_same_blocked_position_can_receive_credential_unlock_and_explicitly_resume(
    tmp_path, context
):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        ok(submit(sm, sid, p, context, StartNavigation("activity.wait", B, "0,0"), "start"))
        blocked = pump(sm, sid, p, context, pc, "activity.wait", "block")
        assert blocked.activity.status == "blocked"
        navigate(sm, sid, p, context, (A, "1,0"), "npc.fetch", NPC)
        ok(submit(sm, sid, p, context, PickupItem(CRED, A, "2,0"), "npc.pick", NPC))
        ok(submit(sm, sid, p, context, DropItem(CRED, A, "1,0"), "npc.give", NPC))
        navigate(sm, sid, p, context, (A, "1,1"), "npc.clear", NPC)
        ok(submit(sm, sid, p, context, PickupItem(CRED, A, "1,0"), "pc.pick"))
        ok(submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "unlock"))
        assert sm.read_actor_position(sid, pc, p, context=context).cell == "0,0"
        old = sm.read_navigation_activity(sid, pc, "activity.wait", p, context=context)
        assert old.status == "blocked" and old.cursor == blocked.activity.cursor
        ok(
            submit(
                sm,
                sid,
                p,
                context,
                ResumeNavigation(old.activity_id, old.activity_version),
                "resume",
            )
        )
        completed = pump(sm, sid, p, context, pc, old.activity_id, "continue")
        assert completed.activity.status == "completed"
        assert (
            submit(
                sm,
                sid,
                p,
                context,
                ResumeNavigation(old.activity_id, completed.activity.activity_version),
                "completed.resume",
            ).error_code
            == "activity_inactive"
        )
        saved = stored_rows(db)
        for receipt in blocked.receipts:
            assert sm.get_command_receipt(sid, receipt.command_id, p, context=context) == receipt
        assert stored_rows(db) == saved


@pytest.mark.parametrize("initial_locked", [True, False])
def test_far_lock_state_does_not_change_static_plan_or_preceding_primitives(
    tmp_path, context, initial_locked
):
    data = lock_data()
    data["lockable_doors"][1]["initial_locked"] = initial_locked
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        ok(
            submit(
                sm, sid, p, context, StartNavigation("activity.far", "scene.store", "0,0"), "start"
            )
        )
        plan = sm.read_navigation_activity(sid, pc, "activity.far", p, context=context).plan
        assert plan == (
            {
                "kind": "passage",
                "passage_id": PASSAGE,
                "from_scene_id": A,
                "expected_cell": "0,0",
                "schema_version": "passage-traversal/1",
            },
            {
                "kind": "step",
                "scene_id": B,
                "expected_cell": "0,0",
                "to_cell": "1,0",
                "schema_version": "actor-step/1",
            },
            {
                "kind": "step",
                "scene_id": B,
                "expected_cell": "1,0",
                "to_cell": "2,1",
                "schema_version": "actor-step/1",
            },
            {
                "kind": "passage",
                "passage_id": "passage.store",
                "from_scene_id": B,
                "expected_cell": "2,1",
                "schema_version": "passage-traversal/1",
            },
        )
        result = pump(sm, sid, p, context, pc, "activity.far", "pump")
        assert result.activity.status == "blocked" and result.activity.cursor == 0
        assert [e.event_type for e in sm.read_events(sid, p, context=context)] == [
            "session.created",
            "scene.entered",
            "activity.navigation_started",
            "activity.navigation_blocked",
        ]


@pytest.mark.parametrize("locking", [False, True])
def test_closed_typed_command_adapter_and_no_caller_bypass(tmp_path, context, locking):
    sm, sid, p, _ = setup(tmp_path, context)
    with sm:
        cmd = command(
            sm,
            sid,
            p,
            context,
            (LockDoor if locking else UnlockDoor)(A, DOOR, CRED, "0,0"),
            "typed",
        )
        data = asdict(cmd)
        del data["check_world_effects"]
        assert world_command_from_data(data) == cmd
        for extra in (
            {"trusted": True},
            {"bypass_lock": True},
            {"actor_id": NPC},
            {"operation": "unlock"},
        ):
            with pytest.raises(StateMachineError):
                world_command_from_data({**data, "payload": {**data["payload"], **extra}})
        with pytest.raises(StateMachineError):
            world_command_from_data(
                {**data, "payload": {**data["payload"], "schema_version": "door-lock/2"}}
            )


def test_oneway_exit_still_allows_reverse_side_lock_control(tmp_path, context):
    data = lock_data()
    data["scenes"][1]["exits"] = [e for e in data["scenes"][1]["exits"] if e["to_scene_id"] != A]
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        acquire(sm, sid, p, context)
        ok(submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "unlock"))
        navigate(sm, sid, p, context, (B, "0,0"), "cross")
        ok(submit(sm, sid, p, context, SetObjectState(B, DOOR, ObjectState.CLOSED), "close"))
        ok(submit(sm, sid, p, context, LockDoor(B, DOOR, CRED, "0,0"), "lock"))
        ok(submit(sm, sid, p, context, UnlockDoor(B, DOOR, CRED, "0,0"), "unlock.reverse"))
        assert submit(sm, sid, p, context, EnterScene(A), "reverse").error_code == "invalid_target"


def test_concurrent_lock_and_open_with_occ_then_current_state_refusal(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        acquire(sm, sid, p, context)
        ok(submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "unlock"))
        commands = [
            command(sm, sid, p, context, op, key)
            for op, key in (
                (LockDoor(A, DOOR, CRED, "0,0"), "race.lock"),
                (SetObjectState(A, DOOR, ObjectState.OPEN), "race.open"),
            )
        ]
        before = stored_rows(db)
    barrier = Barrier(2)

    def run(cmd):
        with StateMachine(db, dnd_loader=lock_loader()) as other:
            barrier.wait(timeout=30)
            return other.submit_command(cmd, p, context=context)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(run, commands))
    assert sorted(r.status for r in receipts) == ["committed", "conflict"]
    with StateMachine(db, dnd_loader=lock_loader()) as sm:
        state = lock_state(sm, sid)
        assert state in (("open", 0), ("closed", 1))
        loser = commands[next(i for i, r in enumerate(receipts) if r.status == "conflict")]
        current = replace(
            loser,
            command_id="race.fresh",
            expected_world_version=sm.read_session(sid, p, context=context).world_version,
        )
        result = sm.submit_command(current, p, context=context)
        assert result.error_code == ("door_locked" if state[1] else "door_must_be_closed")
        after = stored_rows(db)
        for table in ("mechanical_components", "characters", "rng_states", "navigation_activities"):
            assert after[table] == before[table]
        assert len(after["command_receipts"][1]) == len(before["command_receipts"][1]) + 3


@pytest.mark.parametrize(
    "fault", ["missing_lock", "open_locked", "inventory", "charges", "quantity"]
)
def test_corrupt_current_facts_fail_closed_without_repair(tmp_path, context, fault):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        acquire(sm, sid, p, context)
        if fault == "missing_lock":
            sm._conn.execute(
                "DELETE FROM door_credentials WHERE session_id=? AND object_id=?", (sid, DOOR)
            )
            sm._conn.execute(
                "DELETE FROM door_locks WHERE session_id=? AND object_id=?", (sid, DOOR)
            )
        elif fault == "open_locked":
            sm._conn.execute("DROP TRIGGER locked_door_switch")
            sm._conn.execute(
                "UPDATE objects SET state='open' WHERE session_id=? AND object_id=?", (sid, DOOR)
            )
        elif fault == "inventory":
            sm._conn.execute(
                "UPDATE mechanical_components SET inventory_json='{}' WHERE session_id=?", (sid,)
            )
        else:
            unit = json.loads(
                sm._conn.execute(
                    "SELECT item_json FROM objects WHERE session_id=? AND object_id=?", (sid, CRED)
                ).fetchone()[0]
            )
            unit["quantity" if fault == "quantity" else "charges_remaining_per_unit"] = 2
            sm._conn.execute(
                "UPDATE objects SET item_json=? WHERE session_id=? AND object_id=?",
                (json.dumps(unit), sid, CRED),
            )
        for i, operation in enumerate(
            (UnlockDoor(A, DOOR, CRED, "0,0"), SetObjectState(A, DOOR, ObjectState.OPEN))
        ):
            if fault not in ("missing_lock", "open_locked") and i:
                continue
            before = stored_rows(db)
            result = submit(sm, sid, p, context, operation, f"corrupt.{i}")
            assert result.status == "rejected"
            unchanged_except_receipt(before, stored_rows(db))


@pytest.mark.parametrize(
    "case",
    [
        "ground",
        "foreign",
        "wrong_scene",
        "wrong_cell",
        "far",
        "wall",
        "unknown_door",
        "busy",
        "dead",
        "condition",
        "combat",
        "no_control",
        "foreign_actor",
    ],
)
def test_admission_refusals_preserve_all_current_and_historical_facts(tmp_path, context, case):
    data = lock_data()
    if case == "wall":
        del data["rule_bindings"]["combat_foundation"]
        grid = data["scenes"][0]["grid"]
        grid["wall_segments"] = [{"x1": 1, "y1": 0, "x2": 1, "y2": 3}]
        data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"] = grid
        data["pc_templates"][0]["initial_cell"] = "2,0"
    sm, sid, p, db = setup(tmp_path, context, data)
    with sm:
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        actor, ctx = pc, context
        operation = UnlockDoor(A, DOOR, CRED, "0,0")
        if case not in ("ground", "wall", "unknown_door", "no_control", "foreign_actor"):
            acquire(sm, sid, p, context)
        if case == "foreign":
            ok(submit(sm, sid, p, context, DropItem(CRED, A, "1,0"), "give"))
            navigate(sm, sid, p, context, (A, "1,1"), "npc.near", NPC)
            ok(submit(sm, sid, p, context, PickupItem(CRED, A, "1,0"), "npc.pick", NPC))
        elif case == "wrong_scene":
            operation = replace(operation, scene_id=B)
        elif case == "wrong_cell":
            operation = replace(operation, expected_cell="1,0")
        elif case == "far":
            navigate(sm, sid, p, context, (A, "2,0"), "far")
            operation = replace(operation, expected_cell="2,0")
        elif case == "unknown_door":
            operation = replace(operation, object_id="object.unknown")
        elif case == "busy":
            ok(submit(sm, sid, p, context, StartNavigation("activity.busy", B, "0,0"), "busy"))
        elif case == "dead":
            sm._conn.execute(
                "UPDATE characters SET current_hp=0 WHERE session_id=? AND actor_id=?", (sid, pc)
            )
        elif case == "condition":
            raw = json.loads(
                sm._conn.execute(
                    "SELECT state_json FROM mechanical_actors WHERE session_id=? AND actor_id=?",
                    (sid, pc),
                ).fetchone()[0]
            )
            raw["conditions"] = ["incapacitated"]
            sm._conn.execute(
                "UPDATE mechanical_actors SET state_json=? WHERE session_id=? AND actor_id=?",
                (json.dumps(raw), sid, pc),
            )
        elif case == "combat":
            ok(submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "unlock"))
            ok(submit(sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.OPEN), "open"))
            ok(initialize(sm, sid, p, context))
        elif case == "no_control":
            actor, ctx = NPC, replace(context, controlled_npc_ids=())
        elif case == "foreign_actor":
            other = create(sm, p, context, "other")
            actor = sm.read_session(other, p, context=context).pc_actor_id
        cmd = command(sm, sid, p, context, operation, "refuse", actor)
        before = stored_rows(db)
        if case in ("no_control", "foreign_actor"):
            with pytest.raises(StateMachineError):
                sm.submit_command(cmd, p, context=ctx)
            assert stored_rows(db) == before
        else:
            query = sm.query_world_action(sid, pc, operation, p, context=context)
            assert query.status == "unavailable"
            assert stored_rows(db) == before
            receipt = sm.submit_command(cmd, p, context=context)
            assert receipt.status == "rejected" and receipt.event_seqs == ()
            assert receipt.error_code == query.reason
            unchanged_except_receipt(before, stored_rows(db))


def test_local_public_lock_view_never_exposes_credential_binding_and_reads_do_not_observe(
    tmp_path, context
):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        before = stored_rows(db)
        view = sm.get_spatial_view(sid, pc, p, context=context)
        assert [(d.object_id, d.locked) for d in view.doors] == [(DOOR, True)]
        assert CRED not in str([asdict(d) for d in view.doors])
        assert OTHER_DOOR not in str(asdict(view))
        for oid in (OTHER_DOOR, "object.unknown"):
            result = sm.query_world_action(
                sid, pc, UnlockDoor(A, oid, CRED, "0,0"), p, context=context
            )
            assert result.reason == "invalid_target"
        npc = NPCContext("agent:gc4", sid, NPC)
        with pytest.raises(StateMachineError):
            sm.get_spatial_view(sid, pc, p, context=npc)
        assert stored_rows(db) == before


def test_threshold_occupancy_is_preserved_and_locking_never_closes_a_door(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        acquire(sm, sid, p, context)
        ok(submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "unlock"))
        ok(submit(sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.OPEN), "open"))
        navigate(sm, sid, p, context, (A, "2,0"), "pc.clear")
        navigate(sm, sid, p, context, (B, "0,1"), "npc.threshold", NPC)
        navigate(sm, sid, p, context, (A, "0,0"), "pc.return")
        for operation, error in (
            (SetObjectState(A, DOOR, ObjectState.CLOSED), "passage_unavailable"),
            (LockDoor(A, DOOR, CRED, "0,0"), "door_must_be_closed"),
        ):
            before = stored_rows(db)
            receipt = submit(sm, sid, p, context, operation, error)
            assert receipt.error_code == error
            unchanged_except_receipt(before, stored_rows(db))
        assert lock_state(sm, sid) == ("open", 0)


def test_authored_npc_activity_cannot_be_bypassed_by_lock_operation(tmp_path, context):
    data = lock_data()
    data["actors"][0]["npc_state"]["runtime"]["current_activity"] = "Waiting for inspection."
    sm, sid, p, db = setup(tmp_path, context, data)
    with sm:
        before = stored_rows(db)
        receipt = submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "2,2"), "busy", NPC)
        assert receipt.error_code == "unsupported_actor_activity"
        unchanged_except_receipt(before, stored_rows(db))


@pytest.mark.parametrize("mutation", ["drop", "position", "type_conflict"])
def test_pending_npc_lock_result_revalidates_current_facts_and_types(tmp_path, context, mutation):
    data = lock_data()
    # Fresh explicit carrier setup must match the existing typed initial inventory.
    data["item_transfers"][0]["initial_owner_id"] = NPC
    for foundation in ("combat_foundation", "noncombat_foundation"):
        snapshot = data["rule_bindings"][foundation]["snapshot"]
        snapshot["inventory_state"]["entries"].append(
            {
                "instance_id": CRED,
                "owner_id": NPC,
                "item_slug": "amulet",
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
        )
        next(a for a in snapshot["character_states"] if a["entity_id"] == NPC)[
            "carried_item_slugs"
        ] = ["amulet"]
    data["actors"][0]["initial_cell"] = "0,0"
    data["pc_templates"][0]["initial_cell"] = "2,0"
    sm, sid, p, db = setup(tmp_path, context, data)
    with sm:
        npc = NPCContext("agent:gc4", sid, NPC)
        task = create_task(sm, sid, p, npc)
        version = sm.read_session(sid, p, context=context).world_version
        # Account for the one public mutation before eventual dispatch.
        intent = NPCUnlockIntent(A, DOOR, CRED, "0,0", version + (mutation != "type_conflict"))
        result = NPCEvaluationResult(action_intents=(intent,))
        selection = sm.submit_npc_evaluation_result(task.evaluation_id, result, p, context=npc)
        assert selection.outputs[0].status == "pending"
        if mutation == "type_conflict":
            changed = replace(
                result, action_intents=(NPCLockIntent(A, DOOR, CRED, "0,0", version),)
            )
            before = stored_rows(db)
            with pytest.raises(StateMachineError) as exc:
                sm.submit_npc_evaluation_result(task.evaluation_id, changed, p, context=npc)
            assert exc.value.code == "idempotency_conflict"
            assert stored_rows(db) == before
            ok(sm.dispatch_npc_output(task.evaluation_id, 0, p, context=context).command_receipt)
        else:
            operation = (
                DropItem(CRED, A, "1,0") if mutation == "drop" else StepActor(A, "0,0", "1,0")
            )
            ok(submit(sm, sid, p, context, operation, "intervening", NPC))
            before = stored_rows(db)
            delivery = sm.dispatch_npc_output(task.evaluation_id, 0, p, context=context)
            assert delivery.command_receipt.status == "rejected"
            assert delivery.command_receipt.error_code in (
                "credential_unavailable",
                "object_out_of_reach",
            )
            for table in (
                "door_locks",
                "objects",
                "event_evidence",
                "event_outbox",
                "rng_states",
                "navigation_activities",
            ):
                assert stored_rows(db)[table] == before[table]
