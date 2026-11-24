"""Exercise a save-gated pressure plate through the public exploration step."""

import sqlite3
from collections import Counter
from copy import deepcopy
from dataclasses import replace

import pytest
from dnd5e_engine import evaluation
from dnd5e_engine.evaluation_ruleset import get_default_ruleset, ruleset_binding
from dnd5e_srd_data import MemoryAssetLoader
from lock_support import DOOR, NPC, A, lock_loader
from test_environment_region_effects import _seed
from test_mechanism_effects import state_rows
from test_mechanism_save_gate import gated_data, grant_bardic
from test_mechanism_service import trigger
from test_scoped_runtime_validation import grow, measure, observe
from test_world_mechanisms import LEVER
from world_support import command, create, setup, submit

from trpg_state_machine import (
    EnvironmentArea,
    EnvironmentConditions,
    EnvironmentSource,
    ServiceMechanism,
    StateMachine,
    StateMachineError,
    StepActor,
)
from trpg_state_machine.choice_models import ChoiceRespondOperationPayload
from trpg_state_machine.commands import TypedCommand
from trpg_state_machine.package import _validate


def step_gated_data(*, dc, cells=("1,0", "2,0")):
    """Replace the direct-light selector with a fixed pressure-plate region."""
    data = gated_data(dc=dc)
    data["world_objects"][-1]["capabilities"][0]["triggers"] = [
        trigger("enter_region", cells=list(cells))
    ]
    return data


def _stage_rows(sm):
    """Compare movement and consequence owners, including pending delivery rows."""
    return {
        **state_rows(sm),
        **{
            table: [tuple(row) for row in sm._conn.execute(f"SELECT * FROM {table}")]
            for table in ("actors", "sessions", "ongoing_effects", "command_receipts")
        },
    }


@pytest.mark.parametrize("dc, succeeded", [(0, True), (100, False)])
def test_step_gate_commits_the_actual_entry_and_replays(tmp_path, context, dc, succeeded):
    sm, sid, p, db = setup(tmp_path, context, step_gated_data(dc=dc))
    with sm:
        cmd = command(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "enter")
        before_rng = sm.read_rng_context(sid, p, context=context)
        receipt = sm.submit_command(cmd, p, context=context)
        assert receipt.status == "committed", receipt
        events = [
            e for e in sm.read_events(sid, p, context=context) if e.source_command_id == "enter"
        ]
        assert [e.event_type for e in events[:3]] == [
            "actor.stepped",
            "mechanism.activated",
            "rules.save_rolled",
        ]
        move, activated, save = events[:3]
        assert move.payload["actor_id"] == activated.payload["actor_id"] == cmd.actor_id
        assert activated.payload["source_event_seq"] == move.event_seq
        assert save.payload["mechanism_object_id"] == LEVER
        assert save.payload["engine_event"]["target_id"] == cmd.actor_id
        assert save.payload["source_ref"] == "mechanism.fixture-ward"
        assert sm.read_actor_position(sid, cmd.actor_id, p, context=context).cell == "1,0"
        assert sm._world_facts.object(sid, DOOR)["state"] == ("closed" if succeeded else "open")
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1
        rolls = sm.get_command_rolls(sid, "enter", p, context=context)["rolls"]
        assert len(rolls) == 1 and rolls[0]["succeeded"] is succeeded
        assert sm.read_rng_context(sid, p, context=context).version == before_rng.version + 1
        assert sm.submit_command(cmd, p, context=context) == receipt
    with StateMachine(db, dnd_loader=lock_loader()) as reopened:
        assert reopened.submit_command(cmd, p, context=context) == receipt
        assert reopened.get_command_rolls(sid, "enter", p, context=context)["rolls"] == rolls
        assert reopened.read_actor_position(sid, cmd.actor_id, p, context=context).cell == "1,0"


def test_step_gate_skips_outside_dwell_disarmed_and_npc_control(tmp_path, context, monkeypatch):
    data = step_gated_data(dc=0)
    next(actor for actor in data["actors"] if actor["actor_id"] == NPC)["initial_cell"] = "2,1"
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        real = evaluation.evaluate_bound_save
        seen = []

        def observe_save(request, *, loader):
            seen.append(request)
            return real(request, loader=loader)

        monkeypatch.setattr(evaluation, "evaluate_bound_save", observe_save)
        controlled = command(sm, sid, p, context, StepActor(A, "2,1", "2,0"), "npc", actor_id=NPC)
        with pytest.raises(StateMachineError, match="Trusted NPC control") as denied:
            sm.submit_command(controlled, p, context=replace(context, controlled_npc_ids=()))
        assert denied.value.code == "unauthorized"
        npc_context = replace(context, controlled_npc_ids=(NPC,))
        assert sm.submit_command(controlled, p, context=npc_context).status == "committed"
        assert (
            submit(
                sm, sid, p, npc_context, StepActor(A, "2,0", "2,1"), "npc-return", actor_id=NPC
            ).status
            == "committed"
        )
        assert seen == []
        for key, start, end in (
            ("outside", "0,0", "0,0"),
            ("enter", "0,0", "1,0"),
            ("dwell", "1,0", "2,0"),
            ("dwell-return", "2,0", "1,0"),
            ("exit", "1,0", "0,0"),
        ):
            assert submit(sm, sid, p, context, StepActor(A, start, end), key).status == "committed"
        assert len(seen) == 1
        assert (
            submit(
                sm, sid, p, context, ServiceMechanism(A, LEVER, "0,0", "disarm"), "disarm"
            ).status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "unarmed").status == "committed"
        )
        assert len(seen) == 1
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1


def _region_game(tmp_path, context, data=None, *, conditions=(), features=()):
    """Bind the pressure plate fixture to named canonical rule assets."""
    base = lock_loader()
    default = get_default_ruleset()
    loader = MemoryAssetLoader(
        items=[base.get_item(slug) for slug in base.list_slugs("items")],
        monsters=[base.get_monster(slug) for slug in base.list_slugs("monsters")],
        conditions=[default.get_condition(slug) for slug in conditions],
        features=[default.get_feature(slug) for slug in features],
    )
    if data is None:
        data = step_gated_data(dc=100)
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    p = _validate(data)
    db = tmp_path / "world.db"
    sm = StateMachine(db, dnd_loader=loader)
    sid = create(sm, p, context)
    return sm, sid, p, db, loader


def test_step_gate_fixes_post_move_condition_then_rolls_outside_write(
    tmp_path, context, monkeypatch
):
    sm, sid, p, db, bound_loader = _region_game(tmp_path, context, conditions=("poisoned",))
    with sm:
        source = EnvironmentSource(
            "plate.fog",
            EnvironmentConditions(),
            EnvironmentArea(("1,0",)),
            region_condition="poisoned",
        )
        _seed(sm, sid, p, context, (A, (source,)))
        cmd = command(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "trap")
        initial = sm.read_noncombat_snapshot(sid, p, context=context)
        assert not initial.effect_states
        before = _stage_rows(sm)
        real = evaluation.evaluate_bound_save
        seen = []

        def observe_save(request, *, loader):
            assert sm._store._scope is None and not sm._store._conn.in_transaction
            assert _stage_rows(sm) == before
            assert sm.read_actor_position(sid, cmd.actor_id, p, context=context).cell == "0,0"
            assert request.payload.target_id == cmd.actor_id
            assert not request.payload.requires_cover
            assert request.state_snapshot.effect_states[0].statuses == {"poisoned"}
            result = real(request, loader=loader)
            assert result.status == "accepted", result.error
            seen.append(result.proposed_events[0])
            return result

        def fail_commit():
            raise sqlite3.OperationalError("injected before durable commit")

        monkeypatch.setattr(evaluation, "evaluate_bound_save", observe_save)
        with monkeypatch.context() as fault:
            fault.setattr(sm._store, "_commit", fail_commit)
            with pytest.raises(StateMachineError) as failed:
                sm.submit_command(cmd, p, context=context)
            assert failed.value.code == "storage_error"
        assert len(seen) == 1 and seen[0].advantage == "normal"
        assert _stage_rows(sm) == before
        assert sm.get_command_receipt(sid, "trap", p, context=context) is None
        assert sm.read_noncombat_snapshot(sid, p, context=context) == initial
        receipt = sm.submit_command(cmd, p, context=context)
        assert receipt.status == "committed", receipt
        assert seen[0] == seen[1]
        assert not initial.effect_states
    with StateMachine(db, dnd_loader=bound_loader) as reopened:
        assert reopened.submit_command(cmd, p, context=context) == receipt
        assert len(seen) == 2


@pytest.mark.parametrize("option", ["accept", "decline"])
def test_step_gate_choice_is_provisional_and_reuses_the_entry_roll(tmp_path, context, option):
    sm, sid, p, _, _ = _region_game(tmp_path, context, features=("bardic-inspiration",))
    with sm:
        cmd = command(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "trap")
        grant_bardic(sm, sid, cmd.actor_id)
        before = _stage_rows(sm)
        pending = sm.submit_command(cmd, p, context=context)
        assert pending.status == "pending_choice", pending
        assert _stage_rows(sm) == before
        original = sm.get_command_rolls(sid, "trap", p, context=context)["rolls"][0]
        response = TypedCommand(
            sid,
            "answer",
            cmd.actor_id,
            "choice.respond",
            ChoiceRespondOperationPayload("trap", option),
            pending.world_version,
        )
        receipt = sm.submit_command(response, p, context=context)
        assert receipt.status == "committed", receipt
        assert sm.read_actor_position(sid, cmd.actor_id, p, context=context).cell == "1,0"
        final = sm.get_command_rolls(sid, "trap", p, context=context)["rolls"][0]
        assert final["natural"] == original["natural"]
        assert bool(sm.read_noncombat_snapshot(sid, p, context=context).effect_states) is (
            option == "decline"
        )


def test_step_gate_keeps_reads_bounded_as_unrelated_state_grows(tmp_path, context, monkeypatch):
    observed = []
    for unrelated in (0, 25):
        case = tmp_path / str(unrelated)
        case.mkdir()
        sm, sid, p, _ = setup(case, context, step_gated_data(dc=100))
        with sm, monkeypatch.context() as patch:
            if unrelated:
                grow(sm, sid, unrelated)
            counters = Counter()
            mechanisms = sm._objects.mechanisms
            observe(patch, mechanisms, "config", counters, "mechanism_configs")
            observe(patch, mechanisms, "_save_request", counters, "save_requests")
            cmd = command(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "measured")
            with measure(sm) as (counts, _):
                receipt = sm.submit_command(cmd, p, context=context)
            assert receipt.status == "committed", receipt
            observed.append(
                (
                    counts["sql_selects"],
                    counts["sql_rows"],
                    counters["mechanism_configs"],
                    counters["save_requests"],
                )
            )
    assert observed[0] == observed[1]
    print(f"unrelated 0/25, SELECTs / rows / mechanism configs / save requests: {observed}")


def test_step_gate_rolls_back_if_earlier_activation_changes_later_facts(tmp_path, context):
    """RUO-72-F1: a spent Bardic grant changes the second overlapping gate's input."""
    data = step_gated_data(dc=100)
    second = "object.ward_two"
    other = deepcopy(data["scenes"][0]["objects"][-1])
    other["object_id"] = second
    definition = deepcopy(data["world_objects"][-1])
    definition.update(object_id=second, definition_ref="definition.ward_two")
    data["scenes"][0]["objects"].append(other)
    data["world_objects"].append(definition)
    sm, sid, p, _, _ = _region_game(tmp_path, context, data, features=("bardic-inspiration",))
    with sm:
        cmd = command(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "double-trap")
        grant_bardic(sm, sid, cmd.actor_id)
        rng = sm.read_rng_context(sid, p, context=context)
        effects = sm.read_noncombat_snapshot(sid, p, context=context).effect_states
        pending = sm.submit_command(cmd, p, context=context)
        assert pending.status == "pending_choice", pending
        response = TypedCommand(
            sid,
            "answer",
            cmd.actor_id,
            "choice.respond",
            ChoiceRespondOperationPayload("double-trap", "accept"),
            pending.world_version,
        )
        receipt = sm.submit_command(response, p, context=context)
        assert (receipt.status, receipt.error_code) == ("conflict", "version_conflict")
        assert sm.read_actor_position(sid, cmd.actor_id, p, context=context).cell == "0,0"
        assert sm.read_rng_context(sid, p, context=context) == rng
        assert sm.read_noncombat_snapshot(sid, p, context=context).effect_states == effects
        assert sm._world_facts.object(sid, DOOR)["state"] == "closed"
        for oid in (LEVER, second):
            assert sm._world_facts.mechanism_state(sid, oid).activation_count == 0
        assert sm.get_command_rolls(sid, "double-trap", p, context=context)["rolls"] == []
