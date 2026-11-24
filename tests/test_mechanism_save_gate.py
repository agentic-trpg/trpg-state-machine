"""Authored exploration save gates through real light and object-state triggers."""

import json
from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from lock_support import CRED, DOOR, A
from test_mechanism_effects import effect, rule_action, state_rows
from test_mechanism_service import extended_data, method, trigger
from test_world_mechanisms import LEVER
from world_support import command, setup

from trpg_state_machine import (
    ActivateMechanism,
    ObjectState,
    SetLightEnabled,
    SetObjectState,
    StateMachine,
    StateMachineError,
    SwitchLightMode,
    world_command_from_data,
)
from trpg_state_machine.package import _validate


def gated_data(*, dc):
    """Author one local ward whose failed Dex save opens a declared door."""
    data = extended_data(
        triggers=[
            trigger(
                "object_operation",
                target_object_id=CRED,
                event_type="object.light_changed",
                value=True,
            )
        ]
    )
    config = data["world_objects"][-1]["capabilities"][0]
    config["schema_version"] = "mechanism/4"
    config["operations"] = [
        dict(
            operation_id="up",
            actions=[dict(kind="set_open_state", target_object_id=DOOR, value="open")],
            consume_on_noop=True,
            save_gate=dict(
                source_ref="mechanism.fixture-ward", ability="dex", dc=dc, is_magical=False
            ),
        )
    ]
    next(o for o in data["scenes"][0]["objects"] if o["object_id"] == CRED)["initial_cell"] = "1,0"
    return data


def state_gated_data(*, dc):
    """Use a direct door switch as the save source and a light as its consequence."""
    data = gated_data(dc=dc)
    config = data["world_objects"][-1]["capabilities"][0]
    config["triggers"] = [
        trigger(
            "object_operation",
            target_object_id=DOOR,
            event_type="object.state_changed",
            value="open",
        )
    ]
    config["operations"][0]["actions"] = [
        dict(
            kind="set_light_enabled",
            target_object_id=CRED,
            value=True,
        )
    ]
    return data


@pytest.mark.parametrize("dc,failed", [(100, True), (0, False)])
def test_direct_object_state_save_gate_commits_and_replays(tmp_path, context, dc, failed):
    sm, sid, package, db = setup(tmp_path, context, state_gated_data(dc=dc))
    with sm:
        cmd = command(sm, sid, package, context, SetObjectState(A, DOOR, ObjectState.OPEN), "state")
        before_rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm._world_facts.object(sid, DOOR)["state"] == "open"
        assert sm._world_facts.light_state(sid, CRED)["enabled"] is failed
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1
        rolls = sm.get_command_rolls(sid, "state", package, context=context)["rolls"]
        assert len(rolls) == 1 and rolls[0]["succeeded"] is not failed
        assert sm.read_rng_context(sid, package, context=context) != before_rng
        assert sm.submit_command(cmd, package, context=context) == receipt
    with StateMachine(db, dnd_loader=__import__("lock_support").lock_loader()) as reopened:
        assert reopened.submit_command(cmd, package, context=context) == receipt
        assert reopened.get_command_rolls(sid, "state", package, context=context)["rolls"] == rolls


def test_success_skips_failure_only_preflight_but_failure_rolls_back(
    tmp_path, context, monkeypatch
):
    for dc, expected in [(0, "committed"), (100, "rejected")]:
        case_path = tmp_path / str(dc)
        case_path.mkdir()
        sm, sid, package, _ = setup(case_path, context, state_gated_data(dc=dc))
        with sm:

            def unavailable(*_args, **_kwargs):
                raise StateMachineError("invalid_target", "Failure-only target unavailable")

            monkeypatch.setattr(sm._world.objects.mechanisms, "preflight", unavailable)
            cmd = command(
                sm, sid, package, context, SetObjectState(A, DOOR, ObjectState.OPEN), str(dc)
            )
            before = state_rows(sm)
            receipt = sm.submit_command(cmd, package, context=context)
            assert receipt.status == expected, receipt
            if expected == "rejected":
                assert state_rows(sm) == before
                assert sm._world_facts.object(sid, DOOR)["state"] == "closed"
                assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 0
            else:
                assert sm._world_facts.object(sid, DOOR)["state"] == "open"
                assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1


def test_derived_object_state_cannot_impersonate_direct_pc_source(tmp_path, context):
    data = state_gated_data(dc=100)
    gated_object = deepcopy(data["scenes"][0]["objects"][-1])
    gated_object["object_id"] = "object.state_ward"
    gated_definition = deepcopy(data["world_objects"][-1])
    gated_definition.update(object_id="object.state_ward", definition_ref="definition.state_ward")
    data["scenes"][0]["objects"].append(gated_object)
    data["world_objects"].append(gated_definition)
    first = data["world_objects"][-2]["capabilities"][0]
    first["operations"][0]["save_gate"] = None
    first["operations"][0]["actions"] = [
        dict(kind="set_open_state", target_object_id=DOOR, value="open")
    ]
    first["triggers"] = [
        trigger(
            "object_operation", target_object_id=CRED, event_type="object.light_changed", value=True
        )
    ]
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        before = state_rows(sm)
        cmd = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "derived")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "rejected" and receipt.error_code == "unsupported_mechanism_source"
        assert state_rows(sm) == before
        assert sm.get_command_rolls(sid, "derived", package, context=context)["rolls"] == []


def test_mode_switch_on_enabled_light_skips_save_gate(tmp_path, context):
    data = gated_data(dc=100)
    emitter = next(obj for obj in data["world_objects"] if obj["object_id"] == CRED)
    next(cap for cap in emitter["capabilities"] if cap["kind"] == "light")["initial_enabled"] = True
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        before_rng = sm.read_rng_context(sid, package, context=context)
        cmd = command(sm, sid, package, context, SwitchLightMode(A, CRED, "0,0", "hooded"), "mode")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm._world_facts.light_state(sid, CRED)["mode"] == "hooded"
        assert sm._world_facts.object(sid, DOOR)["state"] == "closed"
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 0
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm.get_command_rolls(sid, "mode", package, context=context)["rolls"] == []


def test_v4_preserves_ungated_effect_definition_and_rejects_gated_effect():
    data = gated_data(dc=12)
    config = data["world_objects"][-1]["capabilities"][0]
    config["rule_effects"] = [effect()]
    config["operations"].append(
        dict(operation_id="other", actions=[rule_action()], consume_on_noop=False)
    )
    package = _validate(data)
    mechanism = package.world_objects[-1].capability("mechanism")
    assert mechanism.rule_effects[0].effect_ref == "effect.dart"
    assert mechanism.operation("other").actions[0].effect_ref == "effect.dart"
    config["operations"][0]["actions"] = [rule_action()]
    with pytest.raises(StateMachineError) as denied:
        _validate(data)
    assert denied.value.code == "unsupported_mechanism"


def test_service_failure_cannot_reach_save_gated_operation():
    data = gated_data(dc=12)
    service_method = method("disarm")
    service_method.update(ability="dex", skill="sleight_of_hand", dc=12, trigger_on_failure="up")
    data["world_objects"][-1]["capabilities"][0]["methods"] = [service_method]
    with pytest.raises(StateMachineError) as denied:
        _validate(data)
    assert denied.value.code == "schema_error"


@pytest.mark.parametrize("dc,failed", [(100, True), (0, False)])
def test_light_trigger_rolls_save_and_applies_only_failed_branch(
    tmp_path, context, dc, failed, monkeypatch
):
    sm, sid, package, db = setup(tmp_path, context, gated_data(dc=dc))
    with sm:
        actors = sm._rules.characters.rule_actors
        owner_reads = []

        def track_owners(session_id, owners=None):
            owner_reads.append(frozenset(owners) if owners is not None else None)
            return actors(session_id, owners)

        monkeypatch.setattr(sm._rules.characters, "rule_actors", track_owners)
        cmd = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "ward")
        raw = asdict(cmd)
        raw.pop("check_world_effects")
        cmd = world_command_from_data(json.loads(json.dumps(raw)))
        before_rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed", receipt
        assert owner_reads and set(owner_reads) == {frozenset((cmd.actor_id,))}
        assert sm._world_facts.object(sid, DOOR)["state"] == ("open" if failed else "closed")
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1
        rolls = sm.get_command_rolls(sid, "ward", package, context=context)["rolls"]
        assert len(rolls) == 1 and rolls[0]["type"] == "save_rolled"
        assert rolls[0]["succeeded"] is not failed
        events = sm.read_events(sid, package, context=context)
        save_event = next(e for e in events if e.event_type == "rules.save_rolled")
        assert save_event.payload["source_ref"] == "mechanism.fixture-ward"
        assert save_event.payload["mechanism_object_id"] == LEVER
        after_rng = sm.read_rng_context(sid, package, context=context)
        assert after_rng.version == before_rng.version + 1
        assert after_rng.state != before_rng.state
        assert sm.submit_command(cmd, package, context=context) == receipt
    with StateMachine(db, dnd_loader=__import__("lock_support").lock_loader()) as reopened:
        assert reopened.submit_command(cmd, package, context=context) == receipt
        assert reopened.get_command_rolls(sid, "ward", package, context=context)["rolls"] == rolls
        assert reopened.read_rng_context(sid, package, context=context) == after_rng


def test_manual_save_gate_is_rejected_without_roll(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, gated_data(dc=10))
    with sm:
        op = ActivateMechanism(A, LEVER, "0,0", "up")
        before = sm.read_rng_context(sid, package, context=context)
        cmd = command(sm, sid, package, context, op, "manual.ward")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "rejected"
        assert receipt.error_code == "unsupported_mechanism_operation"
        assert sm.read_rng_context(sid, package, context=context) == before
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 0


def test_save_gate_rechecks_trusted_actor_and_late_rollback(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, gated_data(dc=100))
    with sm:
        cmd = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "fault")
        before = state_rows(sm)
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(cmd, package, context=replace(context, player_id="other.player"))
        assert denied.value.code == "unauthorized"
        assert state_rows(sm) == before
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_save_activation BEFORE INSERT ON mechanism_activations "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(StateMachineError) as fault:
            sm.submit_command(cmd, package, context=context)
        assert fault.value.code == "storage_error"
        assert state_rows(sm) == before
        assert sm.get_command_receipt(sid, "fault", package, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_save_activation")
        assert sm.submit_command(cmd, package, context=context).status == "committed"


def test_two_light_activations_use_each_prior_candidate_rng(tmp_path, context, monkeypatch):
    from dnd5e_engine import evaluation

    data = gated_data(dc=100)
    obj = deepcopy(data["scenes"][0]["objects"][-1])
    definition = deepcopy(data["world_objects"][-1])
    obj["object_id"] = "object.ward_two"
    definition.update(object_id="object.ward_two", definition_ref="definition.ward_two")
    data["scenes"][0]["objects"].append(obj)
    data["world_objects"].append(definition)
    sm, sid, package, _ = setup(tmp_path, context, data)
    real = evaluation.evaluate_bound_save
    seen = []

    def capture(request, *, loader):
        result = real(request, loader=loader)
        seen.append((request.rng_context, result.rng_transition))
        return result

    monkeypatch.setattr(evaluation, "evaluate_bound_save", capture)
    with sm:
        cmd = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "two")
        assert sm.submit_command(cmd, package, context=context).status == "committed"
        assert len(seen) == 2
        assert seen[1][0].state == seen[0][1].next_state
        assert seen[1][0].version == seen[0][0].version + 1
        assert len(sm.get_command_rolls(sid, "two", package, context=context)["rolls"]) == 2


def test_ineligible_first_light_gate_does_not_hide_armed_second(tmp_path, context, monkeypatch):
    from dnd5e_engine import evaluation

    data = gated_data(dc=100)
    obj = deepcopy(data["scenes"][0]["objects"][-1])
    definition = deepcopy(data["world_objects"][-1])
    obj["object_id"] = "object.ward_two"
    definition.update(object_id="object.ward_two", definition_ref="definition.ward_two")
    data["scenes"][0]["objects"].append(obj)
    data["world_objects"].append(definition)
    sm, sid, package, _ = setup(tmp_path, context, data)
    seen = []
    real = evaluation.evaluate_bound_save

    def capture(request, *, loader):
        seen.append(request)
        return real(request, loader=loader)

    monkeypatch.setattr(evaluation, "evaluate_bound_save", capture)
    with sm:
        with sm._transaction(write=True):
            state = sm._world_facts.mechanism_state(sid, LEVER)
            sm._world_facts.set_mechanism_state(sid, LEVER, replace(state, status="unprepared"))
        cmd = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "skip")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed", receipt
        assert len(seen) == 1
        assert len(sm.get_command_rolls(sid, "skip", package, context=context)["rolls"]) == 1
