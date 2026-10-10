"""Real Engine proposals, strict schema-valid faults, and independent SQLite reads."""

import sqlite3
from contextlib import closing
from copy import deepcopy

import pytest
from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.evaluation_contracts import RuleEvaluationResult
from dnd5e_engine.evaluation_projection import attack_delta
from dnd5e_engine.evaluation_state import ConditionRecord
from dnd_support import NPC, create, initialize, loader
from dnd_support import package_data as dnd_package_data
from test_dnd_npc_availability import npc_command
from test_dnd_runtime import attack

from trpg_state_machine import StateMachine, StateMachineError
from trpg_state_machine.package import _validate

OTHER = "npc.attendant"
HISTORICAL = "npc.retired_sentinel"


def stored_state(database):
    """Read every persisted table through a separate, explicitly closed connection."""
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {
            name: tuple(sorted(db.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr))
            for (name,) in tables
        }


def encounter(tmp_path, base, context, case="pc_lethal", *, history=True):
    assets = loader()
    data = dnd_package_data(deepcopy(base), assets)
    # The third living Actor makes roster membership insufficient for authorization.
    data["actors"][1]["initial_scene_id"] = "scene.platform"
    data["scenes"][0]["actor_ids"].append(OTHER)
    foundation = data["rule_bindings"]["combat_foundation"]
    snapshot = foundation["snapshot"]
    state = snapshot["combat_state"]
    third = deepcopy(snapshot["character_states"][1])
    third.update(entity_id=OTHER, name="Original ledger witness", initiative=8, temp_hp=2)
    snapshot["character_states"].append(third)
    state["initiative_ids"].append(OTHER)
    state["encounter_ids"].append(OTHER)
    state["actor_zone"][OTHER] = "2,1"
    state["movement_ledgers"][OTHER] = deepcopy(state["movement_ledgers"][NPC])
    state["monster_slug_by_entity"][OTHER] = "bandit"
    state["xp_value_by_entity"][OTHER] = state["xp_value_by_entity"][NPC]
    if case == "pc_lethal":
        snapshot["character_states"][1].update(hp_current=1, hp_max=1)
    elif case.startswith("npc_"):
        maximum = 1 if case == "npc_lethal" else 8
        data["pc_templates"][0]["character"]["hp"] = {"current": 1, "max": maximum}
        snapshot["character_states"][0].update(hp_current=1, hp_max=maximum)
        state.update(current_turn_index=1, turn_serial=2, monster_turn_start_done=[2, NPC])
    if history:
        # Explicit approved input history, including a former participant absent
        # from this encounter. No runtime SQL or Snapshot overwrite seeds it.
        data["actors"].append(
            {
                "actor_id": HISTORICAL,
                "name": "Original historical sentinel",
                "kind": "npc",
                "controller": {"kind": "scripted_event"},
                "initial_scene_id": "scene.workshop",
            }
        )
        data["scenes"][1]["actor_ids"].append(HISTORICAL)
        state["processed_zero_hp_damage_instances"] = [[OTHER, "prior:damage"]]
        state["dead_ids"] = [HISTORICAL]
        state["deaths_recorded"] = [
            {
                "target_id": HISTORICAL,
                "target_kind": "npc",
                "location_id": "scene.workshop",
                "reason": "damage",
                "killer_id": OTHER,
            }
        ]
    package = _validate(data)
    sm = StateMachine(tmp_path / "ledger.db", dnd_loader=assets)
    sid = create(sm, package, context)
    assert initialize(sm, sid, package, context).status == "committed"
    command = (
        npc_command(sm, sid, package, context)
        if case.startswith("npc_")
        else attack(sm, sid, package, context)
    )
    return sm, sid, package, assets, command


@pytest.mark.parametrize("case", ["pc_damage", "pc_lethal", "npc_zero", "npc_lethal"])
@pytest.mark.parametrize("history", [False, True])
def test_real_ledger_updates_preserve_prior_history(tmp_path, package_data, context, case, history):
    sm, sid, package, assets, command = encounter(
        tmp_path, package_data, context, case, history=history
    )
    results = []
    real = sm._evaluate_dnd

    async def capture(request):
        result = await real(request)
        results.append(result)
        return result

    with sm:
        sm._evaluate_dnd = capture
        before = sm.read_combat_snapshot(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed" and results[0].status == "accepted"
        after = sm.read_combat_snapshot(sid, package, context=context)
        result = results[0]
        assert (
            attack_delta(before, after.model_copy(update={"world_version": before.world_version}))
            == result.state_delta
        )
        assert (
            sm.read_rng_context(sid, package, context=context).state
            == result.rng_transition.next_state
        )
        assert after.world_version == before.world_version + 1
        assert (
            after.combat_state.damage_instance_sequence
            > before.combat_state.damage_instance_sequence
        )
        old_records = before.combat_state.deaths_recorded
        assert after.combat_state.deaths_recorded[: len(old_records)] == old_records
        assert before.combat_state.dead_ids <= after.combat_state.dead_ids
        old_entries = before.combat_state.processed_zero_hp_damage_instances
        assert old_entries <= after.combat_state.processed_zero_hp_damage_instances
        if case == "npc_zero":
            assert any(
                type(op) is delta.ProcessedDamageUpdate for op in result.state_delta.operations
            )
            new_entries = after.combat_state.processed_zero_hp_damage_instances - old_entries
            assert new_entries and {actor for actor, _ in new_entries} == {
                command.payload.target_id
            }
            assert after.combat_state.initiative_ids[after.combat_state.current_turn_index] == OTHER
            assert any(
                type(op) is delta.ActionBudgetUpdate and op.actor_id == command.payload.target_id
                for op in result.state_delta.operations
            )
        elif case.endswith("lethal"):
            assert any(type(op) is delta.DeathLedgerUpdate for op in result.state_delta.operations)
            new_records = after.combat_state.deaths_recorded[len(old_records) :]
            assert len(new_records) == 1
            record = new_records[0]
            assert record.target_id == command.payload.target_id
            assert record.killer_id == command.actor_id
            assert record.location_id == after.scene_state.scene_id
            assert record.target_kind == ("character" if case == "npc_lethal" else "monster")
            if case == "npc_lethal":
                assert record.reason == "instant_kill" and not after.character_states[0].is_alive
        saved = stored_state(tmp_path / "ledger.db")
        assert sm.submit_command(command, package, context=context) == receipt
        assert stored_state(tmp_path / "ledger.db") == saved and len(results) == 1
    with StateMachine(tmp_path / "ledger.db", dnd_loader=assets) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after
        assert reopened.submit_command(command, package, context=context) == receipt
    assert stored_state(tmp_path / "ledger.db") == saved


def corrupt_ledger(request, result, fault):
    state = request.state_snapshot.combat_state
    operations = list(result.state_delta.operations)
    if fault.startswith("processed_"):
        old = frozenset(state.processed_zero_hp_damage_instances)
        target = {
            "processed_self": request.actor_id,
            "processed_other": OTHER,
            "processed_absent": "npc.absent",
        }.get(fault)
        value = frozenset() if fault == "processed_delete" else old | {(target, "fault:damage")}
        operations.append(
            delta.ProcessedDamageUpdate(
                kind="combat.processed_damage_update",
                combat_id=state.combat_id,
                expected=old,
                value=value,
            )
        )
    elif fault.startswith("adjacent_"):
        target = next(a for a in request.state_snapshot.character_states if a.entity_id == OTHER)
        data = target.model_dump()
        if fault == "adjacent_hp":
            injected = delta.HPDelta(
                kind="actor.hp_delta",
                target_id=OTHER,
                expected_hp=target.hp_current,
                amount=-1,
                resulting_hp=target.hp_current - 1,
            )
        elif fault == "adjacent_temp_hp":
            injected = delta.TempHPSet(
                kind="actor.temp_hp_set",
                target_id=OTHER,
                expected_temp_hp=target.temp_hp,
                temp_hp=target.temp_hp - 1,
            )
        elif fault == "adjacent_death_state":
            expected = delta.DeathState(is_alive=target.is_alive, death_saves=target.death_saves)
            injected = delta.DeathStateUpdate(
                kind="actor.death_state_update",
                target_id=OTHER,
                expected=expected,
                value=expected.model_copy(update={"is_alive": False}),
            )
        elif fault == "adjacent_conditions":
            condition = ConditionRecord(
                condition="unconscious",
                source_entity_id="implied:event",
                scope="combat",
                duration_rounds=None,
                save_dc=None,
                applied_round=1,
                exhaustion_level=1,
                source_effect_id=None,
            )
            injected = delta.ConditionsUpdate(
                kind="actor.conditions_update",
                target_id=OTHER,
                expected=tuple(target.conditions),
                value=(*target.conditions, condition),
            )
        elif fault in ("adjacent_attribution_target", "adjacent_attribution_source"):
            wrong_target = fault == "adjacent_attribution_target"
            injected = delta.DamageAttributionUpdate(
                kind="actor.damage_attribution_update",
                target_id=OTHER if wrong_target else request.payload.target_id,
                expected=None,
                value=request.actor_id if wrong_target else OTHER,
            )
        elif fault == "adjacent_budget":
            expected = delta.AttackBudgetState.model_validate(
                {name: data[name] for name in delta.AttackBudgetState.model_fields}
            )
            injected = delta.ActionBudgetUpdate(
                kind="combat.action_budget_update",
                actor_id=OTHER,
                expected=expected,
                value=expected.model_copy(update={"action_available": False}),
            )
        elif fault == "adjacent_movement":
            expected = state.movement_ledgers[OTHER]
            injected = delta.MovementLedgerUpdate(
                kind="combat.movement_ledger_update",
                actor_id=OTHER,
                expected=expected,
                value=expected.model_copy(update={"spent_ft": 5, "distance_ft": 5}),
            )
        elif fault == "adjacent_combat":
            injected = delta.DamageSequenceUpdate(
                kind="combat.damage_sequence_update",
                combat_id="combat.other",
                expected=state.damage_instance_sequence,
                value=1,
            )
        else:
            index = next(i for i, op in enumerate(operations) if type(op) is delta.TurnUpdate)
            op = operations.pop(index)
            field = fault.removeprefix("adjacent_")
            value = (
                True
                if field == "ended"
                else OTHER
                if field == "departed_actor_id"
                else (op.value.turn_serial, OTHER)
            )
            injected = op.model_copy(update={"value": op.value.model_copy(update={field: value})})
        # Attribution uses an existing operation; replace it to avoid duplicate
        # field rejection masking the ownership check under test.
        operations = [op for op in operations if type(op) is not type(injected)]
        operations.append(injected)
    else:
        index = next(i for i, op in enumerate(operations) if type(op) is delta.DeathLedgerUpdate)
        op = operations[index]
        changes = {}
        records = op.records
        if fault.startswith("dead_id_"):
            target = request.actor_id if fault == "dead_id_self" else OTHER
            changes["dead_ids"] = op.dead_ids | {target}
        elif fault == "death_delete_ids":
            changes["dead_ids"] = op.dead_ids - op.expected_dead_ids
        elif fault == "death_delete_records":
            changes["records"] = records[len(op.expected_records) :]
        elif fault == "death_rewrite_history":
            changes["records"] = (
                records[0].model_copy(update={"killer_id": request.actor_id}),
                *records[1:],
            )
        elif fault == "death_duplicate":
            changes["records"] = (*records, records[-1])
        elif fault == "death_no_record":
            changes["records"] = op.expected_records
        elif fault == "death_no_id":
            changes["dead_ids"] = op.expected_dead_ids
        else:
            field, value = {
                "death_record_self": ("target_id", request.actor_id),
                "death_record_other": ("target_id", OTHER),
                "death_killer_other": ("killer_id", OTHER),
                "death_killer_absent": ("killer_id", "npc.absent"),
                "death_killer_null": ("killer_id", None),
                "death_location": ("location_id", "scene.workshop"),
                "death_kind": ("target_kind", "character"),
            }[fault]
            changes["records"] = (*records[:-1], records[-1].model_copy(update={field: value}))
        operations[index] = op.model_copy(update=changes)
    altered = result.model_copy(
        update={
            "state_delta": result.state_delta.model_copy(update={"operations": tuple(operations)})
        }
    )
    return RuleEvaluationResult.model_validate_json(altered.model_dump_json())


@pytest.mark.parametrize(
    "fault",
    [
        "processed_self",
        "processed_other",
        "processed_absent",
        "processed_delete",
        "dead_id_self",
        "dead_id_other",
        "death_record_self",
        "death_record_other",
        "death_killer_other",
        "death_killer_absent",
        "death_killer_null",
        "death_location",
        "death_kind",
        "death_delete_ids",
        "death_delete_records",
        "death_rewrite_history",
        "death_duplicate",
        "death_no_record",
        "death_no_id",
        "adjacent_hp",
        "adjacent_temp_hp",
        "adjacent_death_state",
        "adjacent_conditions",
        "adjacent_attribution_target",
        "adjacent_attribution_source",
        "adjacent_budget",
        "adjacent_movement",
        "adjacent_combat",
        "adjacent_last_ended_turn",
        "adjacent_monster_turn_start_done",
        "adjacent_departed_actor_id",
        "adjacent_ended",
    ],
)
def test_schema_valid_ledger_fault_has_no_writes_and_original_command_retries(
    tmp_path, package_data, context, fault
):
    history = fault in {
        "processed_delete",
        "death_delete_ids",
        "death_delete_records",
        "death_rewrite_history",
    }
    sm, sid, package, assets, command = encounter(tmp_path, package_data, context, history=history)
    calls = []
    real = sm._evaluate_dnd

    async def broken(request):
        result = await real(request)
        assert result.status == "accepted"
        calls.append(request.command_id)
        return corrupt_ledger(request, result, fault)

    with sm:
        sm._evaluate_dnd = broken
        before = stored_state(tmp_path / "ledger.db")
        for _ in range(2):
            with pytest.raises(StateMachineError) as caught:
                sm.submit_command(command, package, context=context)
            assert caught.value.code == "invalid_rule_result"
            # Includes HP, all mechanical components, RNG, versions, both ledgers,
            # Events/Outbox, Receipts, Signals/Evidence, Perception and Inbox tables.
            assert stored_state(tmp_path / "ledger.db") == before
            assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert calls == [command.command_id] * 2
        sm._evaluate_dnd = real
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        saved = stored_state(tmp_path / "ledger.db")
        assert sm.submit_command(command, package, context=context) == receipt
        assert stored_state(tmp_path / "ledger.db") == saved
    with StateMachine(tmp_path / "ledger.db", dnd_loader=assets) as reopened:
        assert reopened.submit_command(command, package, context=context) == receipt
    assert stored_state(tmp_path / "ledger.db") == saved
