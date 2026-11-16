"""Public instant spell adoption with the pinned stateless evaluator."""

from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from test_cure_wounds import NPC, SOURCE, actor, cast_command, cure_world

from trpg_state_machine import StateMachine, StateMachineError


@pytest.mark.parametrize("seed", [0, 2])
def test_save_damage_commits_or_saves_without_slot(tmp_path, context, seed):
    """Adopt either real RE save outcome and retain the prior fixed input."""
    sm, sid, package, loader, db = cure_world(
        tmp_path,
        context,
        extra_spells=("sacred-flame",),
        rng_seed=seed,
        target_hp=40,
        prepared=False,
    )
    with sm:
        command = cast_command(sm, sid, package, context, target=NPC, key=f"flame.{seed}")
        command = replace(
            command,
            payload=CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="sacred-flame",
                target_id=NPC,
                slot_level=None,
                spell_source_id=SOURCE,
            ),
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        old_rng = sm.read_rng_context(sid, package, context=context)
        observed = []
        evaluate = sm._rules._evaluate_dnd

        async def observe(request):
            """Record the genuine RE outcome without changing its result."""
            result = await evaluate(request)
            observed.append(result)
            return result

        sm._rules._evaluate_dnd = observe
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        saves = [event for event in observed[0].proposed_events if event.type == "save_rolled"]
        damage = [event for event in observed[0].proposed_events if event.type == "damage_applied"]
        assert len(saves) == len(damage) == 1
        assert saves[0].succeeded is (seed == 0)
        assert saves[0].target_id == damage[0].target_id == NPC
        assert actor(after, NPC).hp_current == actor(before, NPC).hp_current - damage[0].amount
        assert (
            actor(after, command.actor_id).spell_slots
            == actor(before, command.actor_id).spell_slots
        )
        assert after.combat_state.expended_resources == before.combat_state.expended_resources
        assert sm.read_rng_context(sid, package, context=context) != old_rng
        assert sm.submit_command(command, package, context=context) == receipt
        assert actor(before, NPC).hp_current == 40
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after
        assert (
            reopened.get_command_receipt(sid, command.command_id, package, context=context)
            == receipt
        )


def test_save_damage_lethal_result_is_durable_rejection(tmp_path, context):
    """Keep lethal RE consequences outside the current spell adoption boundary."""
    sm, sid, package, _, _ = cure_world(
        tmp_path, context, extra_spells=("sacred-flame",), rng_seed=2, target_hp=1
    )
    with sm:
        command = cast_command(sm, sid, package, context, target=NPC, key="flame.lethal")
        command = replace(
            command,
            payload=CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="sacred-flame",
                target_id=NPC,
                spell_source_id=SOURCE,
            ),
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "spell.capability")
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng


def test_save_damage_receipt_failure_rolls_back(tmp_path, context):
    """The spell, RNG and events remain one write scope after a late SQL failure."""
    sm, sid, package, _, _ = cure_world(
        tmp_path, context, extra_spells=("sacred-flame",), rng_seed=2, target_hp=40
    )
    with sm:
        command = cast_command(sm, sid, package, context, target=NPC, key="flame.rollback")
        command = replace(
            command,
            payload=CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="sacred-flame",
                target_id=NPC,
                spell_source_id=SOURCE,
            ),
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_flame_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='flame.rollback' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(command, package, context=context)
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_flame_receipt")
        assert sm.submit_command(command, package, context=context).status == "committed"


@pytest.mark.parametrize("seed", [0, 2])
def test_second_save_damage_spell_uses_same_adoption(tmp_path, context, seed):
    """A newly admitted save spell spends its slot without SM spell-ID dispatch."""
    sm, sid, package, _, _ = cure_world(
        tmp_path, context, extra_spells=("inflict-wounds",), rng_seed=seed, target_hp=40
    )
    with sm:
        command = cast_command(sm, sid, package, context, target=NPC, key=f"inflict.{seed}")
        command = replace(
            command,
            payload=CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="inflict-wounds",
                target_id=NPC,
                slot_level=1,
                spell_source_id=SOURCE,
            ),
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        observed = []
        evaluate = sm._rules._evaluate_dnd

        async def observe(request):
            """Retain genuine RE event evidence for the proposed change."""
            result = await evaluate(request)
            observed.append(result)
            return result

        sm._rules._evaluate_dnd = observe
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        damage = [event for event in observed[0].proposed_events if event.type == "damage_applied"]
        assert len(damage) == 1
        assert actor(after, NPC).hp_current == actor(before, NPC).hp_current - damage[0].amount
        assert actor(after, command.actor_id).spell_slots == {1: 1}
        assert after.combat_state.expended_resources[command.actor_id]["spell_slot:1"] == 1
        assert sm.submit_command(command, package, context=context) == receipt


def test_damage_attribution_may_already_equal_caster(tmp_path, context):
    """A repeated source need not appear as a redundant RE field change."""
    sm, sid, package, _, _ = cure_world(
        tmp_path,
        context,
        extra_spells=("sacred-flame",),
        rng_seed=2,
        target_hp=40,
        same_attribution=True,
    )
    with sm:
        command = cast_command(sm, sid, package, context, target=NPC, key="same.source")
        command = replace(
            command,
            payload=CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="sacred-flame",
                target_id=NPC,
                spell_source_id=SOURCE,
            ),
        )
        assert sm.submit_command(command, package, context=context).status == "committed"


def test_unprepared_spell_receipt_precedes_unknown_target(tmp_path, context):
    """Retain SPELL-01's source rejection before RE inspects an invalid target."""
    sm, sid, package, _, _ = cure_world(tmp_path, context, prepared=False)
    with sm:
        command = cast_command(sm, sid, package, context, target="npc.unknown")
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "spell.source")
        assert sm.submit_command(command, package, context=context) == receipt


@pytest.mark.parametrize("field", ["source_id", "damage_instance_id"])
def test_forged_damage_provenance_rolls_back(tmp_path, context, field):
    """Uncovered stage evidence cannot relabel a spell or damage instance."""
    sm, sid, package, _, _ = cure_world(
        tmp_path, context, extra_spells=("sacred-flame",), rng_seed=2, target_hp=40
    )
    with sm:
        command = cast_command(sm, sid, package, context, target=NPC, key=f"forged.{field}")
        command = replace(
            command,
            payload=CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="sacred-flame",
                target_id=NPC,
                spell_source_id=SOURCE,
            ),
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        evaluate = sm._rules._evaluate_dnd

        async def forge(request):
            """Relabel one real event without changing its valid RE Delta."""
            result = await evaluate(request)
            events = list(result.proposed_events)
            index = next(i for i, event in enumerate(events) if event.type == "damage_applied")
            events[index] = events[index].model_copy(update={field: "forged"})
            return result.model_copy(update={"proposed_events": tuple(events)})

        sm._rules._evaluate_dnd = forge
        with pytest.raises(StateMachineError) as failure:
            sm.submit_command(command, package, context=context)
        assert failure.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
