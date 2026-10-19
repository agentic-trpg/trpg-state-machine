"""GC3 ordinary public transfers: real SQLite, public commands and pinned RE."""

import io
import json
import sqlite3
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from threading import Barrier

import pytest
from dnd_support import initialize
from item_support import (
    DAGGER,
    NPC,
    ROPE,
    SECOND,
    A,
    B,
    command,
    create,
    item_data,
    item_loader,
    navigate,
    submit,
)
from test_migrations import assert_original_rows_equal, stored_rows
from test_npc_evaluation_tasks import create_task
from test_real_checks import check_command

from trpg_state_machine import (
    DropItem,
    PickupItem,
    PlaceObject,
    StartNavigation,
    StateMachine,
    StateMachineError,
    world_command_from_data,
)
from trpg_state_machine.npc_results import NPCDropIntent, NPCEvaluationResult, NPCPickupIntent
from trpg_state_machine.package import _validate, load_adventure_package
from trpg_state_machine.perception import NPCContext


def setup(tmp_path, context, data=None):
    data = data or item_data()
    package = _validate(data)
    db = tmp_path / "items.db"
    sm = StateMachine(db, dnd_loader=item_loader())
    sid = create(sm, package, context)
    return sm, sid, package, db


def carried(sm, sid, package, context, actor=None):
    actor = actor or sm.read_session(sid, package, context=context).pc_actor_id
    return {
        e.instance_id: e
        for e in sm.read_carried_items(sid, actor, package, context=context).entries
    }


def ready(sm, sid, package, context):
    navigate(sm, sid, package, context, (A, "1,1"), "near")


def without_receipts(rows):
    return {k: v for k, v in rows.items() if k != "command_receipts"}


def test_public_navigation_transfer_two_types_same_type_instances_and_new_process(
    tmp_path, context
):
    sm, sid, package, db = setup(tmp_path, context)
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        initial = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        ready(sm, sid, package, context)
        assert DAGGER in dict(sm.read_session(sid, package, context=context).scenes[A].objects)
        for iid, cell in ((DAGGER, "2,1"), (SECOND, "2,1"), (ROPE, "1,0")):
            assert (
                submit(sm, sid, package, context, PickupItem(iid, A, cell), "pick." + iid).status
                == "committed"
            )
        entries = carried(sm, sid, package, context)
        assert {DAGGER, SECOND, ROPE} <= entries.keys()
        assert entries[DAGGER].instance_id != entries[SECOND].instance_id
        assert entries[DAGGER].owner_id == pc
        assert entries[DAGGER].quantity == 1 and entries[DAGGER].charges_remaining_per_unit == 0
        view = sm.get_spatial_view(sid, pc, package, context=context)
        assert not {DAGGER, SECOND, ROPE} & {o[0] for o in view.objects}
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        old_pc = next(a for a in initial.character_states if a.entity_id == pc)
        new_pc = next(a for a in after.character_states if a.entity_id == pc)
        assert new_pc.model_dump(exclude={"carried_item_slugs"}) == old_pc.model_dump(
            exclude={"carried_item_slugs"}
        )
        navigate(sm, sid, package, context, (B, "1,1"), "carry")
        assert {DAGGER, SECOND, ROPE} <= {
            e.instance_id
            for e in sm.read_noncombat_snapshot(
                sid, package, context=context
            ).inventory_state.entries
        }
        assert sm._world_facts.object_position(sid, DAGGER)["scene_id"] == B
        assert sm._world_facts.object_position(sid, DAGGER)["cell"] == "1,1"
        assert sm.read_rng_context(sid, package, context=context) == rng
        before_restart = stored_rows(db)
    document = tmp_path / "package.json"
    document.write_text(json.dumps(item_data()), "utf-8")
    worker = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parent / "_item_restart_worker.py"),
            str(db),
            str(document),
            sid,
            pc,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert set(json.loads(worker.stdout)) >= {DAGGER, SECOND, ROPE}
    assert stored_rows(db) == before_restart
    with StateMachine(db, dnd_loader=item_loader()) as sm:
        assert sm.resume_session(sid, package, context=context).current_scene_id == B
        assert (
            submit(sm, sid, package, context, DropItem(DAGGER, B, "1,1"), "drop").status
            == "committed"
        )
        assert DAGGER not in carried(sm, sid, package, context)
        assert DAGGER not in {
            e.instance_id
            for e in sm.read_noncombat_snapshot(
                sid, package, context=context
            ).inventory_state.entries
        }
        assert "dagger" in sm.read_session(sid, package, context=context).character.inventory
        assert DAGGER in {
            o[0] for o in sm.get_spatial_view(sid, pc, package, context=context).objects
        }
        navigate(sm, sid, package, context, (B, "2,2"), "away")
        sm.grant_npc_access(sid, NPC, "agent:gc3", package, context=context)
        navigate(sm, sid, package, context, (B, "0,0"), "npc.near", NPC)
        assert (
            submit(
                sm, sid, package, context, PickupItem(DAGGER, B, "1,1"), "other.pick", NPC
            ).status
            == "committed"
        )
        assert carried(sm, sid, package, context, NPC)[DAGGER].owner_id == NPC
        assert DAGGER not in carried(sm, sid, package, context)
        navigate(sm, sid, package, context, (B, "0,2"), "npc.clear", NPC)
        navigate(sm, sid, package, context, (A, "0,0"), "revisit")
        assert not {DAGGER, SECOND, ROPE} & set(
            sm.read_session(sid, package, context=context).scenes[A].objects
        )
        assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "kind",
    [
        "far",
        "remote",
        "wall",
        "dark",
        "busy",
        "wrong_cell",
        "unknown",
        "legacy",
        "no_control",
        "cross_session",
    ],
)
def test_refusals_leave_all_facts_rng_events_and_evidence_unchanged(tmp_path, context, kind):
    data = item_data()
    if kind in ("wall", "dark"):
        del data["rule_bindings"]["combat_foundation"]
        grid = data["scenes"][0]["grid"]
        if kind == "wall":
            grid["wall_segments"] = [{"x1": 2, "y1": 0, "x2": 2, "y2": 3}]
        else:
            grid["default_lighting"] = "dark"
        data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"] = grid
    sm, sid, package, db = setup(tmp_path, context, data)
    with sm:
        if kind not in ("far", "dark"):
            ready(sm, sid, package, context)
        if kind == "busy":
            assert (
                submit(
                    sm,
                    sid,
                    package,
                    context,
                    StartNavigation("activity.busy", B, "1,1"),
                    "start.busy",
                ).status
                == "committed"
            )
        operation = PickupItem(DAGGER, A, "2,1")
        actor = None
        call_context = context
        if kind == "remote":
            operation = PickupItem(DAGGER, B, "2,1")
        elif kind == "wrong_cell":
            operation = PickupItem(DAGGER, A, "0,0")
        elif kind in ("unknown", "cross_session"):
            if kind == "cross_session":
                other = create(sm, package, context, "create.other")
                assert other != sid
            operation = PickupItem("object.absent", A, "2,1")
        elif kind == "legacy":
            operation = PickupItem("object.platform_gate", A, "2,2")
        elif kind == "no_control":
            actor = NPC
            call_context = replace(context, controlled_npc_ids=())
        cmd = command(sm, sid, package, context, operation, "bad", actor)
        before = stored_rows(db)
        if kind == "no_control":
            with pytest.raises(StateMachineError) as exc:
                sm.submit_command(cmd, package, context=call_context)
            assert exc.value.code == "unauthorized"
            assert stored_rows(db) == before
        else:
            receipt = sm.submit_command(cmd, package, context=call_context)
            assert receipt.status == "rejected", receipt
            assert not receipt.event_seqs
            assert without_receipts(stored_rows(db)) == without_receipts(before)
            assert sm.submit_command(cmd, package, context=call_context) == receipt


def test_queries_own_inventory_and_closed_data_adapter_have_no_side_effects(tmp_path, context):
    sm, sid, package, db = setup(tmp_path, context)
    with sm:
        ready(sm, sid, package, context)
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        sm.grant_npc_access(sid, NPC, "agent:gc3", package, context=context)
        npc = NPCContext("agent:gc3", sid, NPC)
        before = stored_rows(db)
        assert (
            sm.query_world_action(
                sid, pc, PickupItem(DAGGER, A, "2,1"), package, context=context
            ).status
            == "available"
        )
        sm.read_carried_items(sid, NPC, package, context=npc)
        with pytest.raises(StateMachineError):
            sm.read_carried_items(sid, pc, package, context=npc)
        with pytest.raises(StateMachineError):
            sm.read_carried_items(
                sid, NPC, package, context=replace(context, controlled_npc_ids=())
            )
        assert stored_rows(db) == before
        cmd = command(sm, sid, package, context, PickupItem(DAGGER, A, "2,1"), "closed")
        data = asdict(cmd)
        del data["check_world_effects"]
        assert world_command_from_data(data) == cmd
        for extra in ({"accessible": True}, {"owner_id": pc}, {"quantity": 99}, {"script": "x"}):
            with pytest.raises(StateMachineError):
                world_command_from_data({**data, "payload": {**data["payload"], **extra}})
        with pytest.raises(StateMachineError):
            world_command_from_data({**data, "command_type": "world.steal_item"})


@pytest.mark.parametrize(
    "table",
    [
        "objects",
        "mechanical_components",
        "characters",
        "event_evidence",
        "event_outbox",
        "command_receipts",
    ],
)
@pytest.mark.parametrize("drop", [False, True])
def test_midwrite_storage_faults_roll_back_all_tables_and_retry(tmp_path, context, table, drop):
    sm, sid, package, db = setup(tmp_path, context)
    with sm:
        ready(sm, sid, package, context)
        if drop:
            assert (
                submit(sm, sid, package, context, PickupItem(DAGGER, A, "2,1"), "setup.pick").status
                == "committed"
            )
        operation = DropItem(DAGGER, A, "1,1") if drop else PickupItem(DAGGER, A, "2,1")
        cmd = command(sm, sid, package, context, operation, "fault")
        verb = "UPDATE" if table in {"objects", "mechanical_components", "characters"} else "INSERT"
        sm._conn.execute(
            f"CREATE TRIGGER gc3_fault BEFORE {verb} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'GC3 injected'); END"
        )
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(cmd, package, context=context)
        assert exc.value.code == "storage_error"
        assert stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER gc3_fault")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed"
        committed = stored_rows(db)
        assert sm.submit_command(cmd, package, context=context) == receipt
        assert stored_rows(db) == committed


def test_occ_idempotency_lost_reply_and_competing_actors(tmp_path, context):
    sm, sid, package, db = setup(tmp_path, context)
    with sm:
        ready(sm, sid, package, context)
        cmd = command(sm, sid, package, context, PickupItem(DAGGER, A, "2,1"), "once")
        receipt = sm.submit_command(cmd, package, context=context)
        # Caller loses the response after durable commit, then reconciles the original key.
        with pytest.raises(OSError):
            raise OSError("synthetic transport response loss after commit")
        before = stored_rows(db)
        assert sm.get_command_receipt(sid, cmd.command_id, package, context=context) == receipt
        assert sm.submit_command(cmd, package, context=context) == receipt
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(
                replace(cmd, payload=PickupItem(SECOND, A, "2,1")), package, context=context
            )
        assert exc.value.code == "idempotency_conflict"
        assert stored_rows(db) == before
        stale = replace(cmd, command_id="stale", payload=PickupItem(SECOND, A, "2,1"))
        assert sm.submit_command(stale, package, context=context).status == "conflict"
        npc = command(sm, sid, package, context, PickupItem(ROPE, A, "1,0"), "compete.npc", NPC)
        pc = command(sm, sid, package, context, PickupItem(ROPE, A, "1,0"), "compete.pc")
    barrier = Barrier(2)

    def compete(cmd):
        with StateMachine(db, dnd_loader=item_loader()) as other:
            barrier.wait()
            return other.submit_command(cmd, package, context=context)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(compete, (pc, npc)))
    assert sorted(r.status for r in receipts) == ["committed", "conflict"]
    with StateMachine(db, dnd_loader=item_loader()) as sm:
        instances = [
            e
            for actor in (pc.actor_id, NPC)
            for e in carried(sm, sid, package, context, actor).values()
            if e.instance_id == ROPE
        ]
        assert len(instances) == 1
        assert (
            sm._conn.execute(
                "SELECT scene_id,cell,carrier_id FROM objects WHERE session_id=? AND object_id=?",
                (sid, ROPE),
            ).fetchone()[2]
            == instances[0].owner_id
        )


def test_legacy_place_and_foreign_drop_cannot_move_carried_instance(tmp_path, context):
    sm, sid, package, db = setup(tmp_path, context)
    with sm:
        ready(sm, sid, package, context)
        assert (
            submit(sm, sid, package, context, PickupItem(DAGGER, A, "2,1"), "pick").status
            == "committed"
        )
        for operation, actor in (
            (PlaceObject(DAGGER, A, "2,1", "1,1"), None),
            (DropItem(DAGGER, A, "1,0"), NPC),
        ):
            before = stored_rows(db)
            receipt = submit(sm, sid, package, context, operation, "forbid." + str(actor), actor)
            assert receipt.status == "rejected"
            assert without_receipts(before) == without_receipts(stored_rows(db))


def test_real_pinned_check_sees_transferred_instance_and_never_restores_template(tmp_path, context):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _ = setup(tmp_path, context)
    with sm:
        ready(sm, sid, package, context)
        assert (
            submit(sm, sid, package, context, PickupItem(DAGGER, A, "2,1"), "pick").status
            == "committed"
        )
        assert (
            submit(sm, sid, package, context, PickupItem(ROPE, A, "1,0"), "rope").status
            == "committed"
        )
        real = sm._rules._evaluate_dnd
        captured = []

        async def evaluate(request):
            captured.append(request)
            result = await real(request)
            assert result.status == "accepted", result.error
            return result

        sm._rules._evaluate_dnd = evaluate
        receipt = sm.submit_command(
            check_command(sm, sid, package, context, dc=1), package, context=context
        )
        assert receipt.status == "committed"
        request = captured[0]
        assert {DAGGER, ROPE} <= {
            e.instance_id for e in request.state_snapshot.inventory_state.entries
        }
        actor = next(
            a for a in request.state_snapshot.character_states if a.entity_id == request.actor_id
        )
        assert set(actor.carried_item_slugs) == {"rapier", "dagger", "rope"}
        assert (
            submit(sm, sid, package, context, DropItem(DAGGER, A, "1,1"), "drop").status
            == "committed"
        )
        assert (
            sm.submit_command(
                check_command(sm, sid, package, context, key="check.after", dc=1),
                package,
                context=context,
            ).status
            == "committed"
        )
        assert DAGGER not in {
            e.instance_id for e in captured[-1].state_snapshot.inventory_state.entries
        }
        assert DAGGER not in carried(sm, sid, package, context)


def test_synthetic_npc_result_uses_real_task_reliable_handoff_and_revocation(tmp_path, context):
    sm, sid, package, db = setup(tmp_path, context)
    with sm:
        sm.grant_npc_access(sid, NPC, "agent:gc3", package, context=context)
        npc = NPCContext("agent:gc3", sid, NPC)
        task = create_task(sm, sid, package, npc)
        version = sm.read_session(sid, package, context=context).world_version
        result = NPCEvaluationResult(action_intents=(NPCPickupIntent(ROPE, A, "1,0", version),))
        before_objects = stored_rows(db)["objects"]
        selected = sm.submit_npc_evaluation_result(task.evaluation_id, result, package, context=npc)
        assert selected.outputs[0].status == "pending"
        assert stored_rows(db)["objects"] == before_objects
        delivered = sm.dispatch_npc_output(task.evaluation_id, 0, package, context=context)
        assert delivered.command_receipt.status == "committed"
        assert carried(sm, sid, package, context, NPC)[ROPE].owner_id == NPC
        before = stored_rows(db)
        assert sm.dispatch_npc_output(task.evaluation_id, 0, package, context=context) == delivered
        assert (
            sm.submit_npc_evaluation_result(task.evaluation_id, result, package, context=npc)
            == selected
        )
        assert stored_rows(db) == before
        successful_drop_task = create_task(
            sm,
            sid,
            package,
            npc,
            key="successful.drop.task",
            expected_npc_state_version=sm.get_npc_view(
                sid, NPC, package, context=npc
            ).npc_state_version,
        )
        version = sm.read_session(sid, package, context=context).world_version
        successful_drop = NPCEvaluationResult(
            action_intents=(NPCDropIntent(ROPE, A, "1,0", version),)
        )
        sm.submit_npc_evaluation_result(
            successful_drop_task.evaluation_id, successful_drop, package, context=npc
        )
        drop_delivery = sm.dispatch_npc_output(
            successful_drop_task.evaluation_id, 0, package, context=context
        )
        assert drop_delivery.command_receipt.status == "committed"
        assert ROPE not in carried(sm, sid, package, context, NPC)
        before = stored_rows(db)
        assert (
            sm.dispatch_npc_output(successful_drop_task.evaluation_id, 0, package, context=context)
            == drop_delivery
        )
        assert stored_rows(db) == before
        state = sm.get_npc_view(sid, NPC, package, context=npc)
        next_task = create_task(
            sm,
            sid,
            package,
            npc,
            key="drop.task",
            expected_npc_state_version=state.npc_state_version,
        )
        version = sm.read_session(sid, package, context=context).world_version
        drop = NPCEvaluationResult(action_intents=(NPCPickupIntent(ROPE, A, "1,0", version),))
        sm.submit_npc_evaluation_result(next_task.evaluation_id, drop, package, context=npc)
        before = stored_rows(db)
        with pytest.raises(StateMachineError):
            sm.dispatch_npc_output(
                next_task.evaluation_id, 0, package, context=replace(context, controlled_npc_ids=())
            )
        assert stored_rows(db) == before
        sm.grant_npc_access(sid, NPC, "agent:replacement", package, context=context)
        before = stored_rows(db)
        with pytest.raises(StateMachineError):
            sm.dispatch_npc_output(next_task.evaluation_id, 0, package, context=context)
        assert stored_rows(db) == before


def test_transfer_evidence_is_captured_at_ground_stage_and_survives_movement(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context)
    with sm:
        ready(sm, sid, package, context)
        assert (
            submit(sm, sid, package, context, PickupItem(DAGGER, A, "2,1"), "pick").status
            == "committed"
        )
        event = sm.read_events(sid, package, context=context)[-1]
        evidence = json.loads(
            sm._conn.execute(
                "SELECT evidence_json FROM event_evidence WHERE session_id=? AND event_seq=?",
                (sid, event.event_seq),
            ).fetchone()[0]
        )
        assert evidence["phase"]["code"] == "world.before_item_pickup"
        assert evidence["objects"][DAGGER]["cell"] == "2,1"
        navigate(sm, sid, package, context, (B, "1,1"), "carry")
        assert (
            submit(sm, sid, package, context, DropItem(DAGGER, B, "1,1"), "drop").status
            == "committed"
        )
        assert (
            json.loads(
                sm._conn.execute(
                    "SELECT evidence_json FROM event_evidence WHERE session_id=? AND event_seq=?",
                    (sid, event.event_seq),
                ).fetchone()[0]
            )
            == evidence
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "duplicate",
        "quantity",
        "owner",
        "collision",
        "legacy_version",
        "missing_foundation",
        "blocking",
        "unknown_asset",
    ],
)
def test_package_admission_and_unknown_assets_fail_closed(tmp_path, context, mutation):
    data = item_data()
    if mutation == "extra":
        data["item_transfers"][0]["accessible"] = False
    elif mutation == "duplicate":
        data["item_transfers"].append(data["item_transfers"][0].copy())
    elif mutation == "quantity":
        data["item_transfers"][0]["quantity"] = 2
    elif mutation == "owner":
        data["item_transfers"][0]["initial_owner_id"] = "npc.missing"
    elif mutation == "collision":
        data["item_transfers"][0]["instance_id"] = data["rule_bindings"]["noncombat_foundation"][
            "snapshot"
        ]["inventory_state"]["entries"][0]["instance_id"]
    elif mutation == "legacy_version":
        data["manifest"]["schema_version"] = "state-machine-foundation/3"
    elif mutation == "missing_foundation":
        del data["rule_bindings"]["noncombat_foundation"]
    elif mutation == "blocking":
        data["scenes"][0]["objects"][-3].update(movable=True, blocks_passage=True)
    else:
        data["item_transfers"][0]["item_slug"] = "invented-asset"
    if mutation == "unknown_asset":
        package = _validate(data)
        with StateMachine(tmp_path / "unknown.db", dnd_loader=item_loader()) as sm:
            with pytest.raises(StateMachineError) as exc:
                create(sm, package, context)
            assert exc.value.code == "invalid_item_foundation"
            assert sm._conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    else:
        with pytest.raises(StateMachineError):
            _validate(data)


def test_example_package_is_valid():
    loaded = load_adventure_package(Path(__file__).parents[1] / "examples/gc3_items.json")
    assert loaded.status == "valid", loaded.errors


def test_combat_initialization_and_real_availability_keep_current_inventory(tmp_path, context):
    from dnd5e_engine.evaluation_contracts import CombatIntentPayload

    sm, sid, package, db = setup(tmp_path, context)
    with sm:
        ready(sm, sid, package, context)
        assert (
            submit(sm, sid, package, context, PickupItem(DAGGER, A, "2,1"), "pick").status
            == "committed"
        )
        assert (
            submit(sm, sid, package, context, PickupItem(SECOND, A, "2,1"), "pick.second").status
            == "committed"
        )
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        assert initialize(sm, sid, package, context).status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.inventory_state == before.inventory_state
        assert {DAGGER, SECOND} <= {e.instance_id for e in after.inventory_state.entries}
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        payload = CombatIntentPayload(
            intent_type="attack", source_id=pc, target_id=NPC, weapon_id="rapier"
        )
        stored = stored_rows(db)
        assert (
            sm.query_action_availability(sid, pc, payload, package, context=context).status
            == "available"
        )
        assert stored_rows(db) == stored
        receipt = submit(sm, sid, package, context, DropItem(DAGGER, A, "1,1"), "combat.drop")
        assert (receipt.status, receipt.error_code) == (
            "rejected",
            "unsupported_combat_world_operation",
        )
        assert without_receipts(stored_rows(db)) == without_receipts(stored)


def initial_carrier_data(*, slug="dagger", charges=0):
    data = item_data()
    item = data["item_transfers"][0]
    owner = data["pc_templates"][0]["pc_template_id"]
    item.update(initial_owner_id=owner, item_slug=slug, charges_remaining_per_unit=charges)
    data["pc_templates"][0]["character"]["inventory"].append(slug)
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        snapshot["character_states"][0]["carried_item_slugs"].append(slug)
        snapshot["inventory_state"]["entries"].append(
            {
                "instance_id": DAGGER,
                "owner_id": owner,
                "item_slug": slug,
                "quantity": 1,
                "charges_remaining_per_unit": charges,
                "accessible": True,
            }
        )
    return data


def test_initial_carrier_legacy_stack_charges_and_other_components_preserved(tmp_path, context):
    data = initial_carrier_data(slug="wand-of-magic-missiles", charges=3)
    del data["rule_bindings"]["combat_foundation"]
    snapshot = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
    original = snapshot["inventory_state"]["entries"][0]
    original.update(quantity=7, charges_remaining_per_unit=2, accessible=False)
    sm, sid, package, db = setup(tmp_path, context, data)
    with sm:
        entry = carried(sm, sid, package, context)[DAGGER]
        assert entry.owner_id == sm.read_session(sid, package, context=context).pc_actor_id
        assert entry.charges_remaining_per_unit == 3
        assert DAGGER not in sm.read_session(sid, package, context=context).scenes[A].objects
        before = stored_rows(db)
        old_inventory = carried(sm, sid, package, context)
        old_inventory.pop(DAGGER)
        assert (
            submit(sm, sid, package, context, DropItem(DAGGER, A, "0,0"), "drop.initial").status
            == "committed"
        )
        assert carried(sm, sid, package, context) == old_inventory
        assert (
            submit(
                sm, sid, package, context, PickupItem(DAGGER, A, "0,0"), "recover.initial"
            ).status
            == "committed"
        )
        assert carried(sm, sid, package, context)[DAGGER] == entry
        after = stored_rows(db)
        for table in ("mechanical_actors", "rng_states"):
            assert before[table] == after[table]
        current = sm._conn.execute(
            "SELECT resources_json,casting_json,effects_json FROM mechanical_components "
            "WHERE session_id=?",
            (sid,),
        ).fetchone()
        columns, values = before["mechanical_components"]
        previous = next(iter(values))
        assert tuple(current) == tuple(
            previous[columns.index(k)] for k in ("resources_json", "casting_json", "effects_json")
        )


@pytest.mark.parametrize("equipment", ["weapon", "other_hand", "shield", "armor"])
def test_slug_equipment_ambiguity_refuses_drop_without_mutating_any_facts(
    tmp_path, context, equipment
):
    slug = {"weapon": "dagger", "other_hand": "rope", "shield": "shield", "armor": "leather-armor"}[
        equipment
    ]
    data = initial_carrier_data(slug=slug)
    del data["rule_bindings"]["combat_foundation"]
    actor = data["rule_bindings"]["noncombat_foundation"]["snapshot"]["character_states"][0]
    if equipment == "weapon":
        actor.update(weapon_in_hands=slug, weapon_grip="one_handed")
    elif equipment == "other_hand":
        actor["other_hand_occupied"] = True
    elif equipment == "shield":
        actor["shield_equipped"] = True
    else:
        actor["worn_armor"] = "light"
    sm, sid, package, db = setup(tmp_path, context, data)
    with sm:
        if equipment == "weapon":
            ready(sm, sid, package, context)
            assert (
                submit(sm, sid, package, context, PickupItem(SECOND, A, "2,1"), "same.slug").status
                == "committed"
            )
        before = stored_rows(db)
        receipt = submit(
            sm,
            sid,
            package,
            context,
            DropItem(DAGGER, A, "0,0" if equipment != "weapon" else "1,1"),
            "equipped",
        )
        assert (receipt.status, receipt.error_code) == ("rejected", "equipped_item_unsupported")
        assert without_receipts(before) == without_receipts(stored_rows(db))


@pytest.mark.parametrize(
    "fault",
    ["missing", "bad_other_owner", "portable_mismatch", "ground_ghost", "bad_slug_projection"],
)
def test_full_inventory_validation_precedes_actor_projection(tmp_path, context, fault):
    sm, sid, package, db = setup(tmp_path, context)
    with sm:
        ready(sm, sid, package, context)
        assert (
            submit(sm, sid, package, context, PickupItem(DAGGER, A, "2,1"), "pick").status
            == "committed"
        )
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        if fault == "missing":
            sm._conn.execute("DELETE FROM mechanical_components WHERE session_id=?", (sid,))
        elif fault == "bad_other_owner":
            sm._conn.execute(
                "UPDATE mechanical_components SET inventory_json="
                "json_set(inventory_json,'$.entries[0].owner_id','npc.absent') WHERE session_id=?",
                (sid,),
            )
        elif fault == "portable_mismatch":
            sm._conn.execute(
                "UPDATE mechanical_components SET inventory_json="
                "json_set(inventory_json,'$.entries[1].charges_remaining_per_unit',7) "
                "WHERE session_id=?",
                (sid,),
            )
        elif fault == "ground_ghost":
            entry = {
                "instance_id": ROPE,
                "owner_id": pc,
                "item_slug": "rope",
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
            sm._conn.execute(
                "UPDATE mechanical_components SET inventory_json="
                "json_insert(inventory_json,'$.entries[#]',json(?)) WHERE session_id=?",
                (json.dumps(entry), sid),
            )
        else:
            sm._conn.execute(
                "UPDATE characters SET inventory_json='[\"rapier\"]' "
                "WHERE session_id=? AND actor_id=?",
                (sid, pc),
            )
        before = stored_rows(db)
        # NPC-only projection must still validate corrupt PC/other facts first.
        with pytest.raises(StateMachineError):
            sm.read_carried_items(sid, NPC, package, context=context)
        with pytest.raises(StateMachineError):
            sm.read_noncombat_snapshot(sid, package, context=context)
        assert stored_rows(db) == before


@pytest.mark.parametrize("operation", ["place", "pickup"])
@pytest.mark.parametrize("hidden", ["remote", "wall"])
def test_unseen_portable_and_absent_references_have_same_public_refusal(
    tmp_path, context, operation, hidden
):
    data = item_data()
    if hidden == "remote":
        obj = data["scenes"][0]["objects"].pop(-3)
        data["scenes"][1]["objects"].append(obj)
    else:
        del data["rule_bindings"]["combat_foundation"]
        grid = data["scenes"][0]["grid"]
        grid["wall_segments"] = [{"x1": 2, "y1": 0, "x2": 2, "y2": 3}]
        data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"] = grid
    sm, sid, package, db = setup(tmp_path, context, data)
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        before = stored_rows(db)
        outcomes = []
        for iid in (DAGGER, "object.absent"):
            payload = (
                PlaceObject(iid, A, "2,1", "0,0")
                if operation == "place"
                else PickupItem(iid, A, "2,1")
            )
            outcomes.append(sm.query_world_action(sid, pc, payload, package, context=context))
        assert outcomes[0] == outcomes[1]
        assert outcomes[0].reason == "invalid_target"
        assert stored_rows(db) == before


def test_real_cross_session_actor_reference_is_rejected_before_receipt(tmp_path, context):
    sm, sid, package, db = setup(tmp_path, context)
    with sm:
        other = create(sm, package, context, "other.session")
        cmd = command(sm, sid, package, context, PickupItem(DAGGER, A, "2,1"), "cross.actor")
        before = stored_rows(db)
        with pytest.raises(StateMachineError):
            sm.submit_command(replace(cmd, session_id=other), package, context=context)
        assert stored_rows(db) == before


@pytest.mark.parametrize(
    "fault", [None, "CREATE TABLE objects_gc3", "DROP TABLE objects", "PRAGMA user_version = 24"]
)
def test_genuine_schema23_upgrade_is_atomic_preserves_all_history_and_adds_no_capabilities(
    tmp_path, context, monkeypatch, fault
):
    root = Path(__file__).parents[1]
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "0e380cc27e6c20516fe0f3f4c81b895310124f62", "src"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    source = tmp_path / "baseline"
    with zipfile.ZipFile(io.BytesIO(archive)) as contents:
        contents.extractall(source)
    data = item_data()
    data["manifest"]["schema_version"] = "state-machine-foundation/3"
    del data["item_transfers"]
    document = tmp_path / "legacy.json"
    document.write_text(json.dumps(data), "utf-8")
    db = tmp_path / "old.db"
    worker = subprocess.run(
        [
            sys.executable,
            str(root / "tests/_item_legacy_worker.py"),
            str(source),
            str(db),
            str(document),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    old = json.loads(worker.stdout)
    assert old["schema"] == 23
    before = stored_rows(db)
    if fault:
        connect = sqlite3.connect

        class Failing(sqlite3.Connection):
            def execute(self, sql, *args):
                result = super().execute(sql, *args)
                if fault in sql:
                    raise sqlite3.OperationalError("injected GC3 migration fault")
                return result

        monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connect(*a, factory=Failing, **k))
        with pytest.raises(StateMachineError):
            StateMachine(db, dnd_loader=item_loader())
        monkeypatch.undo()
        assert stored_rows(db) == before
    package = load_adventure_package(document).package
    with StateMachine(db, dnd_loader=item_loader()) as sm:
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 37
        assert_original_rows_equal(before, stored_rows(db))
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM objects WHERE item_json IS NOT NULL OR carrier_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert (
            sm.resume_session(old["sid"], package, context=context).package_digest
            == package.package_digest
        )
        assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert sm._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        ready(sm, old["sid"], package, context)
        receipt = submit(
            sm, old["sid"], package, context, PickupItem(DAGGER, A, "2,1"), "legacy.pick"
        )
        assert (receipt.status, receipt.error_code) == ("rejected", "unsupported_object_capability")
