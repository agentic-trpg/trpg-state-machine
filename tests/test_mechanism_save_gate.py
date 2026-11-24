"""Authored exploration save gates through real light and object-state triggers."""

import json
from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_ruleset import get_default_ruleset, ruleset_binding
from dnd5e_srd_data import MemoryAssetLoader
from lock_support import CRED, DOOR, A, lock_loader
from test_equipment_save_modifier import STAFF, _save, _world
from test_mechanism_effects import effect, rule_action, state_rows
from test_mechanism_service import extended_data, method, trigger
from test_world_containers import BOX, container_data
from test_world_mechanisms import LEVER
from world_support import command, create, setup

from trpg_state_machine import (
    ActivateMechanism,
    EquipWeapon,
    ObjectState,
    PickupItem,
    PutItem,
    SetLightEnabled,
    SetObjectState,
    StateMachine,
    StateMachineError,
    SwitchLightMode,
    world_command_from_data,
)
from trpg_state_machine.choice_models import ChoiceRespondOperationPayload
from trpg_state_machine.commands import TypedCommand
from trpg_state_machine.package import _validate


def grant_bardic(sm, sid, actor):
    """Give the fixture PC one exact timed grant through the Effects owner."""
    feature = get_default_ruleset().get_feature("bardic-inspiration")
    inspired = passive_effect_to_active_effect(
        feature.passive_effects[0],
        target_id=actor,
        caster_id="npc.fixture",
        extra_flags={"bardic_inspiration": {"grant_id": "grant.fixture", "die_sides": 8}},
    )
    with sm._transaction(write=True):
        sm._world.characters.effects.add_effect(
            sid, effect_state(inspired).model_dump(mode="json"), expires_tick=3600
        )


def setup_bardic_mechanism(tmp_path, context):
    """Bind the canonical Inspired feature to the otherwise local mechanism fixture."""
    base = lock_loader()
    loader = MemoryAssetLoader(
        items=[base.get_item(slug) for slug in base.list_slugs("items")],
        monsters=[base.get_monster(slug) for slug in base.list_slugs("monsters")],
        features=[get_default_ruleset().get_feature("bardic-inspiration")],
    )
    data = gated_data(dc=100)
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    package = _validate(data)
    db = tmp_path / "world.db"
    sm = StateMachine(db, dnd_loader=loader)
    sid = create(sm, package, context)
    return sm, sid, package, db


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


@pytest.mark.parametrize("dc,failed", [(100, True), (0, False)])
def test_opening_carried_box_rechecks_only_save_relevant_inventory(tmp_path, context, dc, failed):
    """Opening a box may reveal a stowed item without altering this actor's save."""
    data = state_gated_data(dc=dc)
    container = container_data()
    data["scenes"][0]["objects"].extend(
        deepcopy(item) for item in container["scenes"][0]["objects"] if item["object_id"] == BOX
    )
    data["item_transfers"].extend(
        deepcopy(item) for item in container["item_transfers"] if item["instance_id"] == BOX
    )
    data["world_objects"].extend(
        deepcopy(item) for item in container["world_objects"] if item["object_id"] == BOX
    )
    for profile in data["world_objects"]:
        profile.update(volume_cu_ft=1, fit_profile="station.small", content_kind="station.unit")
    config = data["world_objects"][-2]["capabilities"][0]
    config["triggers"][0]["target_object_id"] = BOX
    config["operations"][0]["actions"] = [
        dict(kind="set_open_state", target_object_id=DOOR, value="open")
    ]
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        for key, operation in (
            ("pick-item", PickupItem(CRED, A, "1,0")),
            ("pick-box", PickupItem(BOX, A, "1,0")),
            ("stow", PutItem(A, CRED, BOX, "0,0")),
            ("close", SetObjectState(A, BOX, ObjectState.CLOSED)),
        ):
            assert (
                sm.submit_command(
                    command(sm, sid, package, context, operation, key), package, context=context
                ).status
                == "committed"
            )
        cmd = command(sm, sid, package, context, SetObjectState(A, BOX, ObjectState.OPEN), "open")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm._world_facts.object(sid, BOX)["state"] == "open"
        assert sm._world_facts.object(sid, DOOR)["state"] == ("open" if failed else "closed")
        rolls = sm.get_command_rolls(sid, "open", package, context=context)["rolls"]
        assert len(rolls) == 1 and rolls[0]["succeeded"] is not failed
        assert sm.submit_command(cmd, package, context=context) == receipt


def test_save_recheck_keeps_held_item_entry_dependency(tmp_path, context):
    """The exact equipped item remains in the save's staged dependency comparison."""
    sm, sid, package, _ = _world(tmp_path, context)
    with sm:
        equip = command(sm, sid, package, context, EquipWeapon(STAFF), "equip")
        assert sm.submit_command(equip, package, context=context).status == "committed"
        actor_id = sm.read_session(sid, package, context=context).pc_actor_id
        save = _save(sm, sid, package, context, "save")
        with sm._transaction():
            row = sm._session(sid, package, context)
            request = sm._rules.request(
                save,
                package,
                sm._rules._noncombat_snapshot(row, actor_ids=(actor_id,)),
                sm._rules._rng_context(row),
                row=row,
            )
        snapshot = request.state_snapshot
        assert any(entry.instance_id == STAFF for entry in snapshot.inventory_state.entries)
        changed_entries = tuple(
            entry.model_copy(update={"accessible": False}) if entry.instance_id == STAFF else entry
            for entry in snapshot.inventory_state.entries
        )
        changed_snapshot = snapshot.model_copy(
            update={
                "inventory_state": snapshot.inventory_state.model_copy(
                    update={"entries": changed_entries}
                )
            }
        )
        changed = request.model_copy(update={"state_snapshot": changed_snapshot})
        project = sm._world.objects.mechanisms._save_recheck_projection
        assert project(changed) != project(request)
        other = next(
            entry for entry in snapshot.inventory_state.entries if entry.instance_id != STAFF
        )
        for field, value, expected_equal in (
            ("accessible", not other.accessible, True),
            ("quantity", other.quantity + 1, False),
        ):
            entries = tuple(
                entry.model_copy(update={field: value})
                if entry.instance_id == other.instance_id
                else entry
                for entry in snapshot.inventory_state.entries
            )
            modified = request.model_copy(
                update={
                    "state_snapshot": snapshot.model_copy(
                        update={
                            "inventory_state": snapshot.inventory_state.model_copy(
                                update={"entries": entries}
                            )
                        }
                    )
                }
            )
            assert (project(modified) == project(request)) is expected_equal


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


@pytest.mark.parametrize("option", ["accept", "decline"])
def test_light_gate_bardic_choice_is_provisional_then_atomic(tmp_path, context, option):
    sm, sid, package, _ = setup_bardic_mechanism(tmp_path, context)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        grant_bardic(sm, sid, actor)
        from dnd5e_engine.evaluation_saves import _held_inspiration

        snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        assert _held_inspiration(snapshot.effect_states[0], sm._rules.loader)
        source = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "bard")
        before_rng = sm.read_rng_context(sid, package, context=context)
        before = state_rows(sm)
        pending = sm.submit_command(source, package, context=context)
        assert pending.status == "pending_choice", pending
        assert sm._has_pending_action_choice(sid)
        assert state_rows(sm) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm.submit_command(source, package, context=context) == pending
        original = sm.get_command_rolls(sid, "bard", package, context=context)["rolls"]
        assert len(original) == 1 and original[0]["succeeded"] is False
        answer = TypedCommand(
            sid,
            f"answer-{option}",
            actor,
            "choice.respond",
            ChoiceRespondOperationPayload("bard", option),
            pending.world_version,
        )
        receipt = sm.submit_command(answer, package, context=context)
        assert receipt.status == "committed", receipt
        assert not sm._has_pending_action_choice(sid)
        assert sm.get_command_receipt(sid, "bard", package, context=context).status == "committed"
        final = sm.get_command_rolls(sid, "bard", package, context=context)["rolls"]
        assert len(final) == 1 and final[0]["natural"] == original[0]["natural"]
        assert sm.submit_command(answer, package, context=context) == receipt
        assert bool(sm.read_noncombat_snapshot(sid, package, context=context).effect_states) is (
            option == "decline"
        )


def test_public_bard_grant_then_light_gate_choice(tmp_path, context, monkeypatch):
    """Exercise the actual feature command before the pending world action."""
    import test_bardic_feature_grant as bard_fixture

    monkeypatch.setattr(bard_fixture, "item_data", lambda: gated_data(dc=100))
    monkeypatch.setattr(bard_fixture, "item_loader", lock_loader)
    db, sid, package, loader, granted_context = bard_fixture.make_bard_world(tmp_path, context)
    with StateMachine(db, dnd_loader=loader) as sm:
        grant = bard_fixture.grant_command(sm, sid, package, granted_context)
        assert sm.submit_command(grant, package, context=granted_context).status == "committed"
        before = state_rows(sm)
        source = command(
            sm,
            sid,
            package,
            granted_context,
            SetLightEnabled(A, CRED, "0,0", True),
            "actual-grant-light",
        )
        pending = sm.submit_command(source, package, context=granted_context)
        assert pending.status == "pending_choice", pending
        assert state_rows(sm) == before
        original = sm.get_command_rolls(sid, source.command_id, package, context=granted_context)
        assert len(original["rolls"]) == 1
        answer = TypedCommand(
            sid,
            "actual-grant-answer",
            source.actor_id,
            "choice.respond",
            ChoiceRespondOperationPayload(source.command_id, "accept"),
            pending.world_version,
        )
        final = sm.submit_command(answer, package, context=granted_context)
        assert final.status == "committed", final
        assert sm.submit_command(answer, package, context=granted_context) == final
        assert not sm._has_pending_action_choice(sid)
        assert (
            sm.get_command_rolls(sid, source.command_id, package, context=granted_context)["rolls"][
                0
            ]["natural"]
            == original["rolls"][0]["natural"]
        )
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert (
            reopened.submit_command(source, package, context=granted_context).status == "committed"
        )
        assert reopened.submit_command(answer, package, context=granted_context) == final


def test_action_choice_late_answer_receipt_fault_rolls_back(tmp_path, context, monkeypatch):
    sm, sid, package, _ = setup_bardic_mechanism(tmp_path, context)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        grant_bardic(sm, sid, actor)
        source = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "fault")
        pending = sm.submit_command(source, package, context=context)
        before = state_rows(sm)
        before_rng = sm.read_rng_context(sid, package, context=context)
        answer = TypedCommand(
            sid,
            "answer-fault",
            actor,
            "choice.respond",
            ChoiceRespondOperationPayload(source.command_id, "accept"),
            pending.world_version,
        )
        original = sm._session_runtime.record_receipt

        def fail_answer(command, *args, **kwargs):
            if command.command_id == answer.command_id:
                raise RuntimeError("late answer receipt failure")
            return original(command, *args, **kwargs)

        monkeypatch.setattr(sm._session_runtime, "record_receipt", fail_answer)
        with pytest.raises(RuntimeError, match="late answer receipt failure"):
            sm.submit_command(answer, package, context=context)
        assert state_rows(sm) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm._has_pending_action_choice(sid)
        assert sm._session_runtime.receipt(sid, source.command_id) is None
        assert sm._session_runtime.receipt(sid, answer.command_id) is None
        monkeypatch.setattr(sm._session_runtime, "record_receipt", original)
        assert sm.submit_command(answer, package, context=context).status == "committed"


def test_cold_runtime_drops_uncommitted_action_choice(tmp_path, context):
    sm, sid, package, db = setup_bardic_mechanism(tmp_path, context)
    actor = sm.read_session(sid, package, context=context).pc_actor_id
    grant_bardic(sm, sid, actor)
    source = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "cold")
    assert sm.submit_command(source, package, context=context).status == "pending_choice"
    before = state_rows(sm)
    sm.close()
    assert not sm._has_pending_action_choice(sid)
    with StateMachine(db, dnd_loader=sm._rules.loader) as reopened:
        assert state_rows(reopened) == before
        assert (
            reopened.get_command_receipt(sid, source.command_id, package, context=context) is None
        )


def test_overlapping_action_evaluations_publish_one_choice(tmp_path, context, monkeypatch):
    """A second command can finish evaluation while the first is still outside SQL."""
    from dnd5e_engine import evaluation

    sm, sid, package, _ = setup_bardic_mechanism(tmp_path, context)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        grant_bardic(sm, sid, actor)
        first = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "first")
        second = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "second")
        before = state_rows(sm)
        real = evaluation.evaluate_bound_action_save
        second_receipt = []

        def overlap(request, *, loader):
            if request.command_id == first.command_id and not second_receipt:
                second_receipt.append(sm.submit_command(second, package, context=context))
            return real(request, loader=loader)

        monkeypatch.setattr(evaluation, "evaluate_bound_action_save", overlap)
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(first, package, context=context)
        assert exc.value.code == "action_choice_pending"
        assert second_receipt[0].status == "pending_choice"
        assert sm.get_command_receipt(sid, first.command_id, package, context=context) is None
        assert (
            sm.get_command_receipt(sid, second.command_id, package, context=context)
            == second_receipt[0]
        )
        assert state_rows(sm) == before


def test_stale_decline_releases_mechanism_action(tmp_path, context):
    """An externally removed grant closes the live choice without trapping the session."""
    sm, sid, package, _ = setup_bardic_mechanism(tmp_path, context)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        grant_bardic(sm, sid, actor)
        source = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "stale")
        pending = sm.submit_command(source, package, context=context)
        assert pending.status == "pending_choice"
        current = sm._world.characters.effects.held_bardic_inspiration(sid, actor, 0)
        with sm._transaction(write=True):
            sm._world.characters.effects.remove_effect(
                sid, actor, "effect:inspired", current.origin
            )
        answer = TypedCommand(
            sid,
            "stale-decline",
            actor,
            "choice.respond",
            ChoiceRespondOperationPayload(source.command_id, "decline"),
            pending.world_version,
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(answer, package, context=context)
        assert error.value.code == "choice_stale"
        assert not sm._has_pending_action_choice(sid)
        assert sm.get_command_receipt(sid, source.command_id, package, context=context) is None


def test_reply_to_discarded_candidate_cannot_commit_same_key_replacement(
    tmp_path, context, monkeypatch
):
    """A late answer must match the exact provisional attempt before any write."""
    from dnd5e_engine import evaluation_saves

    sm, sid, package, _ = setup_bardic_mechanism(tmp_path, context)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        grant_bardic(sm, sid, actor)
        source = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "ward")
        before = state_rows(sm)
        prompt = sm.submit_command(source, package, context=context)
        assert prompt.status == "pending_choice"
        answer = TypedCommand(
            sid,
            "reply-old-attempt",
            actor,
            "choice.respond",
            ChoiceRespondOperationPayload(source.command_id, "accept"),
            prompt.world_version,
        )
        resolve = evaluation_saves.resolve_bardic_save_choice

        def replace_attempt(*args, **kwargs):
            sm.unload_session(sid, package, context=context)
            replacement = sm.submit_command(source, package, context=context)
            assert replacement.status == "pending_choice"
            return resolve(*args, **kwargs)

        with monkeypatch.context() as test_patch:
            test_patch.setattr(evaluation_saves, "resolve_bardic_save_choice", replace_attempt)
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(answer, package, context=context)
        assert error.value.code == "choice_stale"
        assert state_rows(sm) == before
        assert sm.get_command_receipt(sid, source.command_id, package, context=context) == prompt
        assert (
            sm.submit_command(
                replace(answer, command_id="reply-current-attempt"), package, context=context
            ).status
            == "committed"
        )
        assert not sm._has_pending_action_choice(sid)
