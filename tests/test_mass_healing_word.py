"""Exercise ordered multi-creature healing and atomic payment at the public command."""

from dataclasses import replace
from random import Random

import pytest
from dnd5e_engine.build_spec import derive_multiclass_slots, proficiency_grants
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_delta import (
    ActionBudgetUpdate,
    HPDelta,
    ResourceUpdate,
    SlotSpendLedgerUpdate,
)
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import get_default_ruleset, ruleset_binding
from dnd5e_engine.events import HealingApplied
from dnd5e_srd_data import MemoryAssetLoader
from dnd_support import create, initialize
from item_support import NPC, item_data, item_loader
from test_cure_wounds import actor

from trpg_state_machine import StateMachine, StateMachineError, TypedCommand
from trpg_state_machine.package import _validate

SPELL = "mass-healing-word"
SOURCE = "class:bard"


def mass_healing_world(tmp_path, context, *, npc_hp=9, npc_cell="2,0"):
    """Author a complete Lore Bard 5 source and two selectable creatures."""
    canonical = get_default_ruleset()
    bard = canonical.get_class("bard")
    lore = canonical.get_subclass("lore")
    features = sorted(
        {
            grant.slug
            for source in (bard, lore)
            for grant in source.granted_features
            if grant.level <= 5 and grant.ref_type == "feature"
        }
    )
    base = item_loader()
    loader = MemoryAssetLoader(
        classes=[bard],
        subclasses=[lore],
        features=[canonical.get_feature(feature) for feature in features],
        spells=[canonical.get_spell(SPELL)],
        items=list(base._items.values()),
        monsters=list(base._monsters.values()),
    )
    slots = derive_multiclass_slots({"bard": 5}, loader=loader)
    assert slots == {1: 4, 2: 3, 3: 2}
    proficiency = proficiency_grants([(bard, 5, "primary")])
    data = item_data()
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    data["pc_templates"][0]["character"].update(hp={"current": 2, "max": 30}, inventory=[])
    data["actors"][0]["initial_cell"] = npc_cell
    for scene in data["scenes"]:
        if "grid" in scene:
            scene["grid"]["width"] = 16
    for foundation in data["rule_bindings"].values():
        foundation["rng_state"] = RNGState.capture(Random(0)).model_dump(mode="json")
        snapshot = foundation["snapshot"]
        snapshot["snapshot_schema_version"] = "engine-snapshot/20"
        snapshot["scene_state"]["grid"]["width"] = 16
        for scene in foundation.get("additional_scenes", ()):
            scene["grid"]["width"] = 16
        pc, npc = snapshot["character_states"]
        pc.update(
            hp_current=2,
            hp_max=30,
            class_slug="bard",
            classes={"bard": 5},
            subclass_slug="lore",
            granted_features=features,
            character_level=5,
            charisma=16,
            proficiency_bonus_override=None,
            spellcasting_ability="cha",
            save_proficiencies=sorted(proficiency.saves),
            weapon_proficiencies=sorted(proficiency.weapons),
            spell_slots=slots,
            custom_counters={"feature_use:bardic-inspiration": {"spent": 0}},
            carried_item_slugs=[],
            weapon_in_hands=None,
            weapon_grip="none",
        )
        npc.update(hp_current=npc_hp, hp_max=50, granted_features=[])
        for state in (pc, npc):
            state.pop("spells_known", None)
        if "combat_state" in snapshot:
            snapshot["combat_state"]["actor_zone"][NPC] = npc_cell
        owner = pc["entity_id"]
        snapshot["inventory_state"]["entries"] = [
            entry for entry in snapshot["inventory_state"]["entries"] if entry["owner_id"] != owner
        ]
        snapshot["resource_state"] = {
            "pools": [
                {
                    "kind": "spell_slot",
                    "owner_id": owner,
                    "pool_id": f"spell:{level}",
                    "current": value,
                    "maximum": value,
                    "level": level,
                }
                for level, value in slots.items()
            ]
            + [
                {
                    "kind": "feature_uses",
                    "owner_id": owner,
                    "pool_id": "bardic-inspiration",
                    "current": 3,
                    "maximum": 3,
                    "feature_slug": "bardic-inspiration",
                }
            ]
        }
        snapshot["casting_state"] = {
            "actors": [
                {
                    "actor_id": owner,
                    "verbal_available": True,
                    "sources": [
                        {
                            "source_id": SOURCE,
                            "class_slug": "bard",
                            "ability": "cha",
                            "spells": [{"spell_id": SPELL, "prepared_order": 0, "material": None}],
                        }
                    ],
                }
            ]
        }
    package = _validate(data)
    db = tmp_path / "mass-healing.sqlite"
    sm = StateMachine(db, dnd_loader=loader)
    sid = create(sm, package, context)
    assert initialize(sm, sid, package, context).status == "committed"
    return sm, sid, package, loader, db


def cast(sm, sid, package, context):
    """Select the nearby NPC first and the caster second."""
    session = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        "heal.both",
        session.pc_actor_id,
        "combat.intent",
        CombatIntentPayload(
            intent_type="cast_spell",
            spell_id=SPELL,
            target_id=NPC,
            target_ids=(NPC, session.pc_actor_id),
            slot_level=3,
            spell_source_id=SOURCE,
        ),
        session.world_version,
    )


@pytest.mark.parametrize("npc_hp", [9, 49, 50])
def test_one_bonus_action_pays_for_ordered_capped_healing(tmp_path, context, npc_hp):
    sm, sid, package, loader, db = mass_healing_world(tmp_path, context, npc_hp=npc_hp)
    with sm:
        command = cast(sm, sid, package, context)
        old = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        results = []
        original = sm._rules._evaluate_dnd

        async def observe(request):
            result = await original(request)
            results.append(result)
            return result

        sm._rules._evaluate_dnd = observe
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        (result,) = results
        assert [e.type for e in result.proposed_events] == [
            "intent_submitted",
            "spell_cast",
            "healing_applied",
            "healing_applied",
        ]
        assert [
            (event.target_id, event.amount)
            for event in result.proposed_events
            if isinstance(event, HealingApplied)
        ] == [(NPC, 11), (command.actor_id, 7)]
        operations = result.state_delta.operations
        assert {op.target_id: op.amount for op in operations if type(op) is HPDelta} == (
            {command.actor_id: 7} | ({NPC: min(11, 50 - npc_hp)} if npc_hp < 50 else {})
        )
        for model in (ResourceUpdate, SlotSpendLedgerUpdate, ActionBudgetUpdate):
            assert sum(type(op) is model for op in operations) == 1
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert actor(old, NPC).hp_current == npc_hp
        assert actor(old, command.actor_id).hp_current == 2
        assert actor(after, NPC).hp_current == min(npc_hp + 11, 50)
        caster = actor(after, command.actor_id)
        assert caster.hp_current == 9
        assert caster.action_available and not caster.action_taken_this_turn
        assert not caster.bonus_action_available and caster.bonus_action_taken_this_turn
        assert caster.spell_slots == {1: 4, 2: 3, 3: 1}
        assert caster.custom_counters == actor(old, command.actor_id).custom_counters
        assert after.combat_state.expended_resources == {command.actor_id: {"spell_slot:3": 1}}
        assert after.combat_state.turn_serial == old.combat_state.turn_serial
        next_rng = sm.read_rng_context(sid, package, context=context)
        expected = rng.state.restore()
        for _ in range(4):
            expected.randint(1, 4)
        assert next_rng.state == RNGState.capture(expected)
        assert sm.submit_command(command, package, context=context) == receipt
        current = sm.read_session(sid, package, context=context)
        repeat = replace(
            command, command_id="heal.repeat", expected_world_version=current.world_version
        )
        assert sm.submit_command(repeat, package, context=context).error_code == "no_action_economy"
        assert sm.read_combat_snapshot(sid, package, context=context) == after
        assert sm.read_rng_context(sid, package, context=context) == next_rng
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after
        assert reopened.read_rng_context(sid, package, context=context) == next_rng


@pytest.mark.parametrize(
    "invalid", ["duplicate", "over-count", "wrong-primary", "range", "unknown"]
)
def test_invalid_selection_is_refused_without_payment(tmp_path, context, invalid):
    sm, sid, package, _, _ = mass_healing_world(
        tmp_path, context, npc_cell="13,0" if invalid == "range" else "2,0"
    )
    with sm:
        command = cast(sm, sid, package, context)
        targets = {
            "duplicate": (NPC, NPC),
            "over-count": (
                NPC,
                command.actor_id,
                "npc.third",
                "npc.fourth",
                "npc.fifth",
                "npc.sixth",
                "npc.seventh",
            ),
            "wrong-primary": (command.actor_id, NPC),
            "range": (command.actor_id, NPC),
            "unknown": (NPC, "npc.absent"),
        }[invalid]
        command = replace(
            command,
            payload=command.payload.model_copy(
                update={
                    "target_id": command.actor_id if invalid == "range" else NPC,
                    "target_ids": targets,
                }
            ),
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status != "committed" and receipt.error_code == "target_invalid", receipt
        assert not receipt.event_seqs
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng


def test_plain_pass_reopens_the_multi_heal_slot_window(tmp_path, context):
    sm, sid, package, _, _ = mass_healing_world(tmp_path, context)
    with sm:
        command = cast(sm, sid, package, context)
        assert sm.submit_command(command, package, context=context).status == "committed"
        paid = sm.read_combat_snapshot(sid, package, context=context)
        for actor_id in (command.actor_id, NPC):
            state = sm.read_combat_snapshot(sid, package, context=context)
            end_turn = replace(
                command,
                command_id=f"pass.{actor_id}",
                actor_id=actor_id,
                payload=CombatIntentPayload(intent_type="pass"),
                expected_world_version=state.world_version,
            )
            receipt = sm.submit_command(end_turn, package, context=context)
            assert receipt.status == "committed", receipt
        reset = sm.read_combat_snapshot(sid, package, context=context)
        assert reset.combat_state.turn_serial == paid.combat_state.turn_serial + 2
        assert actor(reset, command.actor_id).bonus_action_available
        recast = replace(
            command, command_id="heal.again", expected_world_version=reset.world_version
        )
        assert sm.submit_command(recast, package, context=context).status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert actor(after, command.actor_id).spell_slots == {1: 4, 2: 3, 3: 0}
        assert after.combat_state.expended_resources == {command.actor_id: {"spell_slot:3": 2}}


@pytest.mark.parametrize(
    "failure", ["missing-hp", "duplicate-hp", "wrong-event", "order", "receipt"]
)
def test_second_target_and_all_payment_roll_back(tmp_path, context, failure):
    sm, sid, package, loader, db = mass_healing_world(tmp_path, context)
    with sm:
        command = cast(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        session = sm.read_session(sid, package, context=context)
        original = sm._rules._evaluate_dnd
        before_sql = mechanical_rows(sm, sid)

        async def forge(request):
            result = await original(request)
            assert result.status == "accepted", result.error
            if failure in ("missing-hp", "duplicate-hp"):
                operations = tuple(
                    op
                    for op in result.state_delta.operations
                    if not isinstance(op, HPDelta) or op.target_id != command.actor_id
                )
                if failure == "duplicate-hp":
                    operations += (next(op for op in operations if isinstance(op, HPDelta)),)
                return result.model_copy(
                    update={
                        "state_delta": result.state_delta.model_copy(
                            update={"operations": operations}
                        )
                    }
                )
            events = list(result.proposed_events)
            if failure == "wrong-event":
                events[-1] = events[-1].model_copy(update={"target_id": "npc.absent"})
            else:
                events[-2:] = reversed(events[-2:])
            return result.model_copy(update={"proposed_events": tuple(events)})

        if failure == "receipt":
            sm._conn.execute(
                "CREATE TEMP TRIGGER fail_mass_healing_receipt BEFORE INSERT ON command_receipts "
                "WHEN NEW.command_id='heal.both' BEGIN SELECT CASE WHEN "
                "(SELECT COUNT(*) FROM characters WHERE session_id=NEW.session_id AND "
                "((actor_id='npc.caretaker' AND current_hp=20) OR "
                "(actor_id<>'npc.caretaker' AND current_hp=9)))=2 "
                "AND (SELECT current FROM character_resources WHERE session_id=NEW.session_id "
                "AND resource_id='spell:3')=1 "
                "AND EXISTS (SELECT 1 FROM event_records WHERE session_id=NEW.session_id "
                "AND source_command_id=NEW.command_id) "
                "THEN RAISE(ABORT,'injected after both heals and payment') "
                "ELSE RAISE(ABORT,'receipt attempted before mechanical writes') END; END"
            )
        else:
            sm._rules._evaluate_dnd = forge
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        if failure != "receipt":
            assert error.value.code == "invalid_rule_result"
        else:
            assert "injected after both heals and payment" in str(error.value.__cause__)
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_session(sid, package, context=context).world_version == session.world_version
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert mechanical_rows(sm, sid) == before_sql
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == before
        assert reopened.read_rng_context(sid, package, context=context) == rng
        assert reopened.submit_command(command, package, context=context).status == "committed"


def mechanical_rows(sm, sid):
    """Return SQL copies of the session's owned facts and durable command evidence."""
    return {
        table: [
            tuple(row)
            for row in sm._conn.execute(f"SELECT * FROM {table} WHERE session_id=?", (sid,))
        ]
        for table in (
            "characters",
            "mechanical_actors",
            "character_resources",
            "combats",
            "rng_states",
            "events",
            "event_records",
            "command_receipts",
        )
    }
