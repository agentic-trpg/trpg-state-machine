"""GC1-F01/F02 public-command SQLite regressions; no success-path SQL writes."""

import json
from dataclasses import asdict, replace

import pytest
from spatial_support import NPC, PASSAGE, A, B, command, create, spatial_data, submit
from test_events import event_data
from test_migrations import stored_rows

from trpg_state_machine import (
    CheckWorldEffects,
    EnterScene,
    ObjectState,
    SetObjectState,
    SetSceneFlag,
    StateMachine,
    StateMachineError,
    StepActor,
    world_command_from_data,
)
from trpg_state_machine.package import _validate

DOOR = "object.platform_gate"


def door_data(base, storage=A):
    data = spatial_data(base)
    gate = data["scenes"][0]["objects"].pop(0)
    gate.update(initial_cell="3,1" if storage == A else "0,1", state="closed")
    data["scenes"][0 if storage == A else 1]["objects"].append(gate)
    return data


def ok(receipt):
    assert receipt.status == "committed", receipt
    return receipt


@pytest.mark.parametrize("storage", [A, B])
def test_f01_shared_door_both_sides_one_authority(tmp_path, package_data, context, storage):
    p = _validate(door_data(package_data, storage))
    with StateMachine(tmp_path / "door.db") as sm:
        sid = create(sm, p, context)
        ok(submit(sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.OPEN), "a.open"))
        ok(submit(sm, sid, p, context, EnterScene(B), "cross"))
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        view = sm.get_spatial_view(sid, pc, p, context=context)
        assert [(d.object_id, d.state, d.standing_cell, d.threshold_cell) for d in view.doors] == [
            (DOOR, "open", "1,1", "0,1")
        ]
        close = SetObjectState(B, DOOR, ObjectState.CLOSED)
        assert sm.query_world_action(sid, pc, close, p, context=context).status == "available"
        raw = asdict(command(sm, sid, p, context, close, "b.close"))
        del raw["check_world_effects"]
        closed = ok(sm.submit_command(world_command_from_data(raw), p, context=context))
        event = next(
            e for e in sm.read_events(sid, p, context=context) if e.event_seq in closed.event_seqs
        )
        assert event.signals[0].scope.scene_id == B
        assert event.signals[0].origin.entity_id == pc
        frozen = evidence(sm, sid, closed.event_seqs[0])
        assert frozen["source_actors"][0]["cell"] == "1,1"
        assert frozen["source_actors"][0]["current_scene_id"] == B
        assert sm.read_session(sid, p, context=context).scenes[storage].objects[DOOR] == "closed"
        ok(submit(sm, sid, p, context, SetObjectState(B, DOOR, ObjectState.OPEN), "b.open"))
        ok(submit(sm, sid, p, context, EnterScene(A), "return"))
        assert evidence(sm, sid, closed.event_seqs[0]) == frozen
        assert (
            sm._conn.execute("SELECT count(*) FROM objects WHERE object_id=?", (DOOR,)).fetchone()[
                0
            ]
            == 1
        )


def remote_effect_data(base):
    data = spatial_data(base)
    data["pc_templates"][0]["initial_cell"] = "0,0"
    data["scenes"][0]["objects"][0].update(initial_cell="4,4", state="closed")
    data["events"] = [event_data()]
    data["scenes"][0]["event_definition_ids"] = ["event.open_gate"]
    return data


def test_f02_package_consequence_is_not_manual_pc_reach(tmp_path, package_data, context):
    p = _validate(remote_effect_data(package_data))
    with StateMachine(tmp_path / "mechanism.db") as sm:
        sid = create(sm, p, context)
        before = sm.read_session(sid, p, context=context)
        manual = SetObjectState(A, DOOR, ObjectState.OPEN)
        assert (
            sm.query_world_action(sid, before.pc_actor_id, manual, p, context=context).reason
            == "object_out_of_reach"
        )
        assert (
            submit(sm, sid, p, context, manual, "manual.remote").error_code == "object_out_of_reach"
        )
        receipt = submit(sm, sid, p, context, SetSceneFlag(A, "flag.lamp_checked", True), "trigger")
        ok(receipt)
        snapshot = sm.read_session(sid, p, context=context)
        assert snapshot.scenes[A].objects[DOOR] == "open"
        assert snapshot.scenes[A].local_flags["flag.lamp_checked"] is True
        assert snapshot.actors == before.actors and snapshot.rng == before.rng
        event = next(
            e
            for e in sm.read_events(sid, p, context=context)
            if e.event_type == "object.state_changed"
        )
        assert event.payload["execution"] == {"kind": "package", "reference_id": "event.open_gate"}
        assert event.signals[0].origin.entity_id == DOOR
        frozen = evidence(sm, sid, event.event_seq)
        assert frozen["source_actors"] == []
        assert frozen["objects"][DOOR]["cell"] == "4,4"


def evidence(sm, sid, seq):
    return json.loads(
        sm._conn.execute(
            "SELECT evidence_json FROM event_evidence WHERE session_id=? AND event_seq=?",
            (sid, seq),
        ).fetchone()[0]
    )


def unchanged_except_receipt(before, after):
    assert {k: v for k, v in before.items() if k != "command_receipts"} == {
        k: v for k, v in after.items() if k != "command_receipts"
    }


@pytest.mark.parametrize("storage", [A, B])
def test_npc_uses_own_port_with_current_pc_elsewhere_and_revoked_grant(
    tmp_path, package_data, context, storage
):
    p = _validate(door_data(package_data, storage))
    db = tmp_path / "npc.db"
    with StateMachine(db) as sm:
        sid = create(sm, p, context)
        opening = SetObjectState(B, DOOR, ObjectState.OPEN)
        assert (
            sm.query_world_action(sid, NPC, opening, p, context=context).reason
            == "object_out_of_reach"
        )
        assert (
            submit(sm, sid, p, context, opening, "remote.npc", NPC).error_code
            == "object_out_of_reach"
        )
        ok(submit(sm, sid, p, context, StepActor(B, "1,2", "1,1"), "approach", NPC))
        ok(submit(sm, sid, p, context, opening, "npc.open", NPC))
        snap = sm.read_session(sid, p, context=context)
        assert snap.current_scene_id == A and snap.actors[NPC].current_scene_id == B
        event = sm.read_events(sid, p, context=context)[-1]
        assert event.payload["execution"] == {"kind": "actor", "actor_id": NPC}
        assert event.signals[0].origin.entity_id == NPC
        revoked = replace(context, controlled_npc_ids=())
        before = stored_rows(db)
        for action in (
            lambda: sm.query_world_action(sid, NPC, opening, p, context=revoked),
            lambda: submit(
                sm, sid, p, revoked, replace(opening, state=ObjectState.CLOSED), "revoked", NPC
            ),
        ):
            with pytest.raises(StateMachineError) as exc:
                action()
            assert exc.value.code == "unauthorized"
        assert stored_rows(db) == before
        assert (
            submit(
                sm, sid, p, context, replace(opening, scene_id=A), "foreign.port", NPC
            ).error_code
            == "invalid_target"
        )
        with pytest.raises(StateMachineError) as exc:
            submit(sm, sid, p, context, SetSceneFlag(B, "flag.lamp_checked", True), "npc.flag", NPC)
        assert exc.value.code == "invalid_actor"


def test_one_way_traversal_still_has_two_door_ports(tmp_path, package_data, context):
    data = door_data(package_data, B)
    data["scenes"][1]["exits"] = []
    p = _validate(data)
    with StateMachine(tmp_path / "oneway.db") as sm:
        sid = create(sm, p, context)
        ok(submit(sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.OPEN), "open"))
        ok(submit(sm, sid, p, context, EnterScene(B), "cross"))
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        view = sm.get_spatial_view(sid, pc, p, context=context)
        assert view.exits == () and view.doors[0].passage_id == PASSAGE
        ok(submit(sm, sid, p, context, SetObjectState(B, DOOR, ObjectState.CLOSED), "close.b"))
        ok(submit(sm, sid, p, context, SetObjectState(B, DOOR, ObjectState.OPEN), "open.b"))
        assert submit(sm, sid, p, context, EnterScene(A), "reverse").error_code == "invalid_target"


def test_unbound_local_switch_does_not_gain_shared_or_npc_authority(
    tmp_path, package_data, context
):
    data = spatial_data(package_data)
    data["passages"][0]["door_object_id"] = None
    p = _validate(data)
    with StateMachine(tmp_path / "switch.db") as sm:
        sid = create(sm, p, context)
        ok(submit(sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.CLOSED), "local"))
        ok(submit(sm, sid, p, context, EnterScene(B), "cross"))
        assert (
            submit(
                sm, sid, p, context, SetObjectState(B, DOOR, ObjectState.OPEN), "other.side"
            ).error_code
            == "invalid_target"
        )
        local = p.scene(B).objects[0].object_id
        assert (
            submit(
                sm, sid, p, context, SetObjectState(B, local, ObjectState.CLOSED), "npc.switch", NPC
            ).error_code
            == "invalid_target"
        )


@pytest.mark.parametrize("side", [A, B])
def test_close_checks_actor_at_either_threshold(tmp_path, package_data, context, side):
    data = door_data(package_data)
    data["scenes"][0]["objects"][-1]["state"] = "open"
    if side == A:
        for actor in data["actors"]:
            actor["initial_cell"] = "3,1" if actor["initial_scene_id"] == A else "1,1"
    p = _validate(data)
    db = tmp_path / "occupied.db"
    with StateMachine(db) as sm:
        sid = create(sm, p, context)
        if side == B:
            ok(submit(sm, sid, p, context, StepActor(B, "1,2", "0,1"), "occupy", NPC))
        before = stored_rows(db)
        close = SetObjectState(B if side == A else A, DOOR, ObjectState.CLOSED)
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        actor = NPC if side == A else pc
        assert (
            sm.query_world_action(sid, actor, close, p, context=context).reason
            == "passage_unavailable"
        )
        assert (
            submit(sm, sid, p, context, close, "close", actor).error_code == "passage_unavailable"
        )
        unchanged_except_receipt(before, stored_rows(db))


@pytest.mark.parametrize("blocker", ["crate", "door", "wall", "hidden"])
def test_local_port_never_ignores_other_obstacles(tmp_path, package_data, context, blocker):
    from copy import deepcopy

    data = door_data(package_data, B)
    if blocker == "crate":
        data["scenes"][0]["objects"][0]["initial_cell"] = "3,1"
    elif blocker == "door":
        gate = deepcopy(data["scenes"][1]["objects"][-1])
        gate.update(object_id="object.other_gate", initial_cell="3,1")
        data["scenes"][0]["objects"].append(gate)
        passage = deepcopy(data["passages"][0])
        passage.update(passage_id="passage.other", door_object_id="object.other_gate")
        data["passages"].append(passage)
        data["scenes"][1]["exits"][0]["passage_id"] = "passage.other"
    else:
        # The port itself is valid; the caller is across a wall, away from its stand.
        data["passages"][0]["endpoints"][0].update(standing_cell="4,3", threshold_cell="4,4")
        data["scenes"][0]["grid"]["wall_segments"] = [{"x1": 3, "y1": 0, "x2": 3, "y2": 5}]
        if blocker == "wall":
            data["scenes"][0]["grid"]["wall_segments"][0].update(y1=1, y2=2)
    p = _validate(data)
    db = tmp_path / "blocker.db"
    with StateMachine(db) as sm:
        sid = create(sm, p, context)
        before = stored_rows(db)
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        opening = SetObjectState(A, DOOR, ObjectState.OPEN)
        query = sm.query_world_action(sid, pc, opening, p, context=context)
        assert query.status == "unavailable"
        assert submit(sm, sid, p, context, opening, "blocked").error_code == query.reason
        unchanged_except_receipt(before, stored_rows(db))


@pytest.mark.parametrize("mode", ["door", "package"])
@pytest.mark.parametrize("table", ["objects", "event_evidence", "event_outbox", "command_receipts"])
def test_sql_fault_has_no_partial_effect_or_evidence(tmp_path, package_data, context, mode, table):
    p = _validate(door_data(package_data) if mode == "door" else remote_effect_data(package_data))
    db = tmp_path / "fault.db"
    with StateMachine(db) as sm:
        sid = create(sm, p, context)
        op = (
            SetObjectState(A, DOOR, ObjectState.OPEN)
            if mode == "door"
            else SetSceneFlag(A, "flag.lamp_checked", True)
        )
        cmd = command(sm, sid, p, context, op, "fault")
        before = stored_rows(db)
        action = "UPDATE" if table == "objects" else "INSERT"
        sm._conn.execute(
            f"CREATE TEMP TRIGGER fail BEFORE {action} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(cmd, p, context=context)
        assert exc.value.code == "storage_error"
        assert stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER fail")
        ok(sm.submit_command(cmd, p, context=context))


@pytest.mark.parametrize("mode", ["door", "package"])
def test_retry_response_loss_version_and_domain_rollback(
    tmp_path, package_data, context, monkeypatch, mode
):
    from trpg_state_machine.game_time import sync_npc_schedules

    p = _validate(door_data(package_data) if mode == "door" else remote_effect_data(package_data))
    db = tmp_path / "retry.db"
    with StateMachine(db) as sm:
        sid = create(sm, p, context)
        op = (
            SetObjectState(A, DOOR, ObjectState.OPEN)
            if mode == "door"
            else SetSceneFlag(A, "flag.lamp_checked", True)
        )
        cmd = command(sm, sid, p, context, op, "lost")

        def lose():
            ok(sm.submit_command(cmd, p, context=context))
            raise ConnectionError("lost response after real commit")

        with pytest.raises(ConnectionError):
            lose()
        frozen = stored_rows(db)
    with StateMachine(db) as sm:
        assert sm.submit_command(cmd, p, context=context) == sm.get_command_receipt(
            sid, "lost", p, context=context
        )
        assert stored_rows(db) == frozen
        conflict = sm.submit_command(replace(cmd, command_id="stale"), p, context=context)
        assert conflict.error_code == "version_conflict"
        before = stored_rows(db)

        def fail(*args):
            sync_npc_schedules(*args)
            raise StateMachineError("event_cascade_limit", "tail failure after mutation")

        monkeypatch.setattr("trpg_state_machine.commands.sync_npc_schedules", fail)
        closing = command(sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.CLOSED), "tail")
        # Package target is far away: trigger a different declared flag change instead.
        if mode == "package":
            closing = command(
                sm, sid, p, context, SetSceneFlag(A, "flag.lamp_checked", False), "tail"
            )
        assert sm.submit_command(closing, p, context=context).error_code == "event_cascade_limit"
        unchanged_except_receipt(before, stored_rows(db))


def test_public_envelope_cannot_claim_automatic_origin(tmp_path, package_data, context):
    p = _validate(remote_effect_data(package_data))
    with StateMachine(tmp_path / "claim.db") as sm:
        sid = create(sm, p, context)
        cmd = command(sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.OPEN), "claim")
        for key, value in (
            ("trusted", True),
            ("source", "package"),
            ("execution", {"kind": "package"}),
            ("passage_id", "passage.unknown"),
        ):
            raw = asdict(cmd)
            del raw["check_world_effects"]
            raw["payload"][key] = value
            with pytest.raises(StateMachineError):
                world_command_from_data(raw)
        pc = cmd.actor_id
        bad = replace(cmd.payload, state="open")
        assert sm.query_world_action(sid, pc, bad, p, context=context).reason == "invalid_operation"


@pytest.mark.parametrize("reverse", [False, True])
def test_multiple_ports_for_one_door_accept_any_clear_local_port(
    tmp_path, package_data, context, reverse
):
    from copy import deepcopy

    data = door_data(package_data)
    second = deepcopy(data["passages"][0])
    second["passage_id"] = "passage.other"
    second["endpoints"][0]["threshold_cell"] = "2,0"
    second["endpoints"][1]["threshold_cell"] = "1,0"
    data["passages"].append(second)
    data["scenes"][1]["exits"][0]["passage_id"] = "passage.other"
    data["scenes"][0]["objects"][0]["initial_cell"] = "3,1"
    if reverse:
        data["passages"].reverse()
    p = _validate(data)
    with StateMachine(tmp_path / "ports.db") as sm:
        sid = create(sm, p, context)
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        opening = SetObjectState(A, DOOR, ObjectState.OPEN)
        assert sm.query_world_action(sid, pc, opening, p, context=context).status == "available"
        receipt = ok(submit(sm, sid, p, context, opening, "open"))
        event = next(
            e for e in sm.read_events(sid, p, context=context) if e.event_seq in receipt.event_seqs
        )
        assert event.payload["passage_id"] == "passage.other"
        snap = sm.read_session(sid, p, context=context)
        assert snap.scenes[A].object_cells["object.crate"] == "3,1"
        assert (
            submit(sm, sid, p, context, EnterScene(B), "still.blocked").error_code
            == "passage_unavailable"
        )


@pytest.mark.parametrize("dc", [1, 99])
@pytest.mark.parametrize("tail_failure", [False, True])
def test_verified_real_check_remote_consequence_and_original_action_rollback(
    tmp_path, package_data, context, monkeypatch, dc, tail_failure
):
    from modern_support import modern_loader
    from test_real_checks import check_command
    from test_spatial_world import combat_data

    context = replace(context, can_adjudicate_checks=True)
    assets = modern_loader()
    data = combat_data(package_data, assets)
    data["passages"][0]["door_object_id"] = None
    data["scenes"][0]["objects"][0].update(initial_cell="4,4", state="closed")
    p = _validate(data)
    db = tmp_path / "check.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, p, context)
        opening = SetObjectState(A, DOOR, ObjectState.OPEN)
        cmd = replace(
            check_command(sm, sid, p, context, dc=dc),
            check_world_effects=CheckWorldEffects(
                "gm.remote_mechanism",
                (replace(opening, state=ObjectState.CLOSED),),
                (opening,),
                (opening,),
            ),
        )
        before = stored_rows(db)
        snapshot = sm.read_session(sid, p, context=context)
        real = sm._expand_events

        def fail_after_effect(*args):
            events = real(*args)
            assert any(e.event_type == "object.state_changed" for e in events)
            raise StateMachineError("event_cascade_limit", "tail failure after check consequence")

        if tail_failure:
            monkeypatch.setattr(sm, "_expand_events", fail_after_effect)
        receipt = sm.submit_command(cmd, p, context=context)
        if tail_failure:
            assert receipt.error_code == "event_cascade_limit"
            unchanged_except_receipt(before, stored_rows(db))
            assert sm.read_session(sid, p, context=context) == snapshot
        else:
            ok(receipt)
            event = sm.read_events(sid, p, context=context)[-1]
            assert event.payload["execution"] == {
                "kind": "check_consequence",
                "reference_id": cmd.command_id,
            }
            assert event.signals[0].origin.entity_id == DOOR
            assert evidence(sm, sid, event.event_seq)["source_actors"] == []
            assert sm.read_session(sid, p, context=context).actors == snapshot.actors
            assert (
                submit(
                    sm, sid, p, context, replace(opening, state=ObjectState.CLOSED), "manual"
                ).error_code
                == "object_out_of_reach"
            )
        frozen = stored_rows(db)
        assert sm.submit_command(cmd, p, context=context) == receipt
        assert stored_rows(db) == frozen


@pytest.mark.parametrize("fault", ["none", "remote", "revoked", "invalid_switch"])
def test_bounded_npc_door_handoff_rechecks_grants_and_keeps_receipts(tmp_path, context, fault):
    import yaml
    from test_npc_evaluation_tasks import create_task
    from test_npc_state import SOURCE, grant

    from trpg_state_machine.npc_results import NPCDoorIntent, NPCEvaluationResult

    data = door_data(yaml.safe_load(SOURCE.read_text("utf-8")))
    for actor in data["actors"]:
        actor["npc_state"]["runtime"].update(schedule=[], current_activity=None)
    p = _validate(data)
    db = tmp_path / "handoff.db"
    with StateMachine(db) as sm:
        sid = create(sm, p, context)
        npc = grant(sm, sid, p, context, NPC)
        if fault != "remote":
            ok(submit(sm, sid, p, context, StepActor(B, "1,2", "1,1"), "approach", NPC))
        task = create_task(sm, sid, p, npc)
        version = sm.read_session(sid, p, context=context).world_version
        intent = NPCDoorIntent(B, DOOR, ObjectState.OPEN, version)
        if fault == "invalid_switch":
            intent = replace(intent, object_id=p.scene(B).objects[0].object_id)
        selected = sm.submit_npc_evaluation_result(
            task.evaluation_id, NPCEvaluationResult(action_intents=(intent,)), p, context=npc
        )
        if fault == "invalid_switch":
            assert selected.outputs[0].status == "rejected"
            assert selected.outputs[0].error_code == "invalid_npc_action"
            assert selected.outputs[0].command_id is None
            return
        assert selected.outputs[0].status == "pending"
        if fault == "revoked":
            # Changing the persisted controller invalidates the already selected Task.
            sm.grant_npc_access(sid, NPC, "agent:replacement", p, context=context)
            before = stored_rows(db)
            with pytest.raises(StateMachineError) as exc:
                sm.dispatch_npc_output(task.evaluation_id, 0, p, context=context)
            assert exc.value.code == "unauthorized"
            assert stored_rows(db) == before
            return
    with StateMachine(db) as sm:
        delivery = sm.dispatch_npc_output(task.evaluation_id, 0, p, context=context)
        if fault == "remote":
            assert delivery.command_receipt.error_code == "object_out_of_reach"
        else:
            ok(delivery.command_receipt)
        frozen = stored_rows(db)
        assert sm.dispatch_npc_output(task.evaluation_id, 0, p, context=context) == delivery
        assert stored_rows(db) == frozen


def test_package_effect_cannot_use_other_scene_shared_door_alias(tmp_path, package_data, context):
    data = door_data(package_data, B)
    data["events"] = [event_data()]
    data["scenes"][0]["event_definition_ids"] = ["event.open_gate"]
    # Package validation must reject this effect instead of granting manual port semantics.
    with pytest.raises(StateMachineError):
        _validate(data)


def test_package_close_preserves_remote_threshold_occupancy_and_trigger_rollback(
    tmp_path, package_data, context
):
    data = remote_effect_data(package_data)
    data["scenes"][0]["objects"][0]["state"] = "open"
    data["events"][0]["effect"]["state"] = "closed"
    p = _validate(data)
    db = tmp_path / "auto-occupied.db"
    with StateMachine(db) as sm:
        sid = create(sm, p, context)
        ok(submit(sm, sid, p, context, StepActor(B, "1,2", "0,1"), "occupy", NPC))
        before = stored_rows(db)
        cmd = command(sm, sid, p, context, SetSceneFlag(A, "flag.lamp_checked", True), "trigger")
        result = sm.submit_command(cmd, p, context=context)
        assert result.error_code == "passage_unavailable"
        unchanged_except_receipt(before, stored_rows(db))
        assert sm.submit_command(cmd, p, context=context) == result


def test_matching_bare_cell_in_unrelated_scene_confers_no_door_control(
    tmp_path, package_data, context
):
    from copy import deepcopy

    data = door_data(package_data)
    unrelated = deepcopy(data["scenes"][1])
    unrelated.update(scene_id="scene.unrelated", objects=[], exits=[])
    data["scenes"].append(unrelated)
    data["world"]["locations"][0]["scene_ids"].append("scene.unrelated")
    data["scenes"][1]["exits"].append(
        {"to_scene_id": "scene.unrelated", "passage_id": "passage.unrelated"}
    )
    data["passages"].append(
        {
            "passage_id": "passage.unrelated",
            "door_object_id": None,
            "endpoints": [
                {"scene_id": B, "standing_cell": "3,3", "threshold_cell": "3,4"},
                {"scene_id": "scene.unrelated", "standing_cell": "1,1", "threshold_cell": "0,1"},
            ],
        }
    )
    for actor in data["actors"]:
        if actor["actor_id"] == NPC:
            actor.update(initial_scene_id="scene.unrelated", initial_cell="2,1")
    p = _validate(data)
    db = tmp_path / "unrelated.db"
    with StateMachine(db) as sm:
        sid = create(sm, p, context)
        before = stored_rows(db)
        for target in (DOOR, "object.unknown"):
            opening = SetObjectState("scene.unrelated", target, ObjectState.OPEN)
            assert (
                sm.query_world_action(sid, NPC, opening, p, context=context).reason
                == "invalid_target"
            )
            assert submit(sm, sid, p, context, opening, target, NPC).error_code == "invalid_target"
        assert sm.get_spatial_view(sid, NPC, p, context=context).doors == ()
        unchanged_except_receipt(before, stored_rows(db))
