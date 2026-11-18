"""Public exploration spell adoption, expiration, and rollback."""

from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_contracts import ExplorationSpellCastPayload
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.rules.character import proficiency_grants
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd_support import create, initialize
from item_support import NPC, item_data, item_loader
from test_hold_effect_candidate import pass_command
from test_session_time_coordination import submit

from trpg_state_machine import (
    AdvanceSessionTime,
    StateMachine,
    StateMachineError,
    TimeBinding,
    TypedCommand,
)
from trpg_state_machine.package import _validate


@pytest.fixture
def timed_spell_world(tmp_path, context):
    """Create a second-scale exploration Session with authored wizard facts."""

    def open_world(*, material=True, first_level_slots=4):
        canonical = BundledAssetLoader()
        base = item_loader()
        wizard = canonical.get_class("wizard")
        evoker = canonical.get_subclass("evoker")
        features = sorted(
            {
                grant.slug
                for definition in (wizard, evoker)
                for grant in definition.granted_features
                if grant.ref_type == "feature" and grant.level <= 5
            }
        )
        loader = MemoryAssetLoader(
            items=[*base._items.values(), canonical.get_item("component-pouch")],
            monsters=list(base._monsters.values()),
            classes=[wizard],
            subclasses=[evoker],
            features=[canonical.get_feature(slug) for slug in features],
            spells=[canonical.get_spell("longstrider")],
        )
        data = item_data()
        data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
        data["time_binding"] = asdict(TimeBinding())
        for actor in data["actors"]:
            actor.pop("npc_state", None)
            actor["initial_cell"] = "1,0"
        template = data["pc_templates"][0]
        template["character"]["inventory"] = ["component-pouch"] if material else []
        if material:
            data["scenes"][0]["objects"].append(
                {
                    "object_id": "object.pouch",
                    "state": None,
                    "movable": False,
                    "blocks_passage": False,
                    "initial_cell": "2,1",
                }
            )
            data["item_transfers"].append(
                {
                    "instance_id": "object.pouch",
                    "item_slug": "component-pouch",
                    "initial_owner_id": template["pc_template_id"],
                    "quantity": 1,
                    "charges_remaining_per_unit": 0,
                    "policy": "ordinary_public",
                }
            )
        proficiency = proficiency_grants([(wizard, 5, "primary")])
        for foundation in data["rule_bindings"].values():
            fixed = foundation["snapshot"]
            fixed["snapshot_schema_version"] = "engine-snapshot/20"
            pc, npc = fixed["character_states"]
            pc.update(
                class_slug="wizard",
                classes={"wizard": 5},
                character_level=5,
                subclass_slug="evoker",
                granted_features=features,
                intelligence=18,
                spellcasting_ability="int",
                spell_slots={1: first_level_slots, 2: 3, 3: 2},
                custom_counters={"feature_use:arcane-recovery": {"spent": 0}},
                save_proficiencies=sorted(proficiency.saves),
                weapon_proficiencies=sorted(proficiency.weapons),
                carried_item_slugs=template["character"]["inventory"],
                weapon_in_hands=None,
                weapon_grip="none",
                other_hand_occupied=False,
                ac=10 + (pc["dexterity"] - 10) // 2,
                proficiency_bonus_override=None,
                attack_bonus=None,
                stealth_disadvantage=False,
            )
            pc.pop("spells_known", None)
            npc.pop("spells_known", None)
            fixed["inventory_state"]["entries"] = (
                [
                    {
                        "instance_id": "object.pouch",
                        "owner_id": pc["entity_id"],
                        "item_slug": "component-pouch",
                        "quantity": 1,
                        "charges_remaining_per_unit": 0,
                        "accessible": True,
                    }
                ]
                if material
                else []
            )
            fixed["resource_state"] = {
                "pools": [
                    {
                        "kind": "spell_slot",
                        "owner_id": pc["entity_id"],
                        "pool_id": f"spell:{level}",
                        "current": current,
                        "maximum": maximum,
                        "level": level,
                    }
                    for level, current, maximum in ((1, first_level_slots, 4), (2, 3, 3), (3, 2, 2))
                ]
                + [
                    {
                        "kind": "feature_uses",
                        "owner_id": pc["entity_id"],
                        "pool_id": "arcane-recovery",
                        "feature_slug": "arcane-recovery",
                        "current": 1,
                        "maximum": 1,
                    }
                ]
            }
            prepared = {"spell_id": "longstrider", "prepared_order": 0, "material": None}
            if material:
                prepared["material"] = {
                    "inventory_instance_id": "object.pouch",
                    "available": True,
                }
            fixed["casting_state"] = {
                "actors": [
                    {
                        "actor_id": pc["entity_id"],
                        "verbal_available": True,
                        "sources": [
                            {
                                "source_id": "class:wizard",
                                "class_slug": "wizard",
                                "ability": "int",
                                "spells": [prepared],
                            }
                        ],
                    }
                ]
            }
        package = _validate(data)
        authorized = replace(context, can_advance_time=True)
        sm = StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader)
        sid = create(sm, package, authorized)
        return sm, sid, package, authorized

    return open_world


def cast_command(session, *, target_id=None, command_id="cast.longstrider"):
    """Submit the bound source, prepared spell, target, and exact slot level."""
    sm, sid, package, context = session
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        command_id,
        state.pc_actor_id,
        "world.cast_spell",
        ExplorationSpellCastPayload(
            kind="world.cast_spell",
            spell_id="longstrider",
            spell_source_id="class:wizard",
            target_id=target_id or state.pc_actor_id,
            slot_level=1,
        ),
        state.world_version,
    )


def first_level_balance(sm, command):
    """Read the guarded spell slot from the authoritative resource row."""
    return sm._conn.execute(
        "SELECT current FROM character_resources "
        "WHERE session_id=? AND actor_id=? AND resource_id='spell:1'",
        (command.session_id, command.actor_id),
    ).fetchone()[0]


@pytest.mark.parametrize("target_id", [None, NPC])
def test_public_longstrider_cast_and_expiry(timed_spell_world, target_id):
    session = timed_spell_world()
    sm, sid, package, context = session
    with sm:
        assert submit(session, AdvanceSessionTime(11), "start-tick").status == "committed"
        command = cast_command(session, target_id=target_id)
        target = command.payload.target_id
        old_rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        assert first_level_balance(sm, command) == 3
        assert sm.read_rng_context(sid, package, context=context) == old_rng
        cast_events = tuple(
            event
            for event in sm.read_events(sid, package, context=context)
            if event.source_command_id == command.command_id
        )
        assert tuple(event.event_seq for event in cast_events) == receipt.event_seqs
        assert tuple(event.payload["engine_event"]["type"] for event in cast_events) == (
            "intent_submitted",
            "spell_cast",
            "effect_applied",
        )
        (effect,) = sm._characters.effects.for_targets(sid, {target})
        assert effect["id"] == "effect:longstrider"
        assert effect["target_id"] == target
        assert sm._session_runtime.time.consumers["effects"].next_boundary(sid, package) == 3611

        assert submit(session, AdvanceSessionTime(3610), "before-expiry").status == "committed"
        assert sm._characters.effects.for_targets(sid, {target}) == [effect]
        expiry = submit(session, AdvanceSessionTime(3611), "at-expiry")
        assert expiry.status == "committed"
        assert sm._characters.effects.for_targets(sid, {target}) == []
        assert any(
            event.event_type == "effect.expired"
            for event in sm.read_events(sid, package, context=context)
        )
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm._characters.effects.for_targets(sid, {target}) == []
        assert first_level_balance(sm, command) == 3


def test_public_longstrider_expires_on_combat_round_boundary(timed_spell_world):
    """Two authorized passes settle the cast's deadline in the shared game clock."""
    session = timed_spell_world()
    sm, sid, package, context = session
    with sm:
        cast = cast_command(session)
        cast_receipt = sm.submit_command(cast, package, context=context)
        assert cast_receipt.status == "committed", cast_receipt
        assert submit(session, AdvanceSessionTime(3594), "before-combat").status == "committed"
        initialized = initialize(sm, sid, package, context)
        assert initialized.status == "committed", initialized
        initial = sm.read_combat_snapshot(sid, package, context=context)
        (effect,) = initial.effect_states
        assert effect.target_id == cast.actor_id

        first = pass_command(sid, initial, "first-pass")
        first_receipt = sm.submit_command(first, package, context=context)
        assert first_receipt.status == "committed", first_receipt
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 3594
        midway = sm.read_combat_snapshot(sid, package, context=context)
        assert midway.combat_state.round_number == initial.combat_state.round_number
        assert midway.effect_states == (effect,)

        second = pass_command(sid, midway, "round-ending-pass")
        assert {first.actor_id, second.actor_id} == {cast.actor_id, NPC}
        second_receipt = sm.submit_command(second, package, context=context)
        assert second_receipt.status == "committed", second_receipt
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 3600
        rollover = sm.read_combat_snapshot(sid, package, context=context)
        assert rollover.combat_state.round_number == initial.combat_state.round_number + 1
        assert rollover.effect_states == ()
        assert first_level_balance(sm, cast) == 3
        (expired,) = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_type == "effect.expired"
        ]
        assert expired.event_seq in second_receipt.event_seqs
        assert expired.source_command_id == second.command_id
        assert expired.payload["boundary_tick"] == 3600
        assert tuple(expired.payload["identity"]) == (effect.target_id, effect.id, effect.origin)


@pytest.mark.parametrize(
    "material,slots,error_code", [(False, 4, "spell.source"), (True, 0, "spell.slot")]
)
def test_missing_material_or_slot_keeps_facts(timed_spell_world, material, slots, error_code):
    session = timed_spell_world(material=material, first_level_slots=slots)
    sm, sid, package, context = session
    with sm:
        command = cast_command(session)
        old_rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", error_code), receipt
        assert receipt.world_version == command.expected_world_version
        assert receipt.event_seqs == ()
        assert first_level_balance(sm, command) == slots
        assert sm._characters.effects.for_targets(sid, {command.actor_id}) == []
        assert sm.read_rng_context(sid, package, context=context) == old_rng


def test_late_receipt_failure_reverts_effect_and_slot(timed_spell_world):
    session = timed_spell_world()
    sm, sid, package, context = session
    with sm:
        command = cast_command(session)
        old_state = sm.read_session(sid, package, context=context)
        old_events = sm.read_events(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_timed_spell_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='cast.longstrider' AND EXISTS ("
            "SELECT 1 FROM ongoing_effects WHERE session_id=NEW.session_id AND expires_tick=3600"
            ") AND EXISTS (SELECT 1 FROM character_resources "
            "WHERE session_id=NEW.session_id AND resource_id='spell:1' AND current=3) "
            "BEGIN SELECT RAISE(ABORT,'injected timed spell receipt failure'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(command, package, context=context)
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert first_level_balance(sm, command) == 4
        assert sm.read_session(sid, package, context=context) == old_state
        assert sm.read_events(sid, package, context=context) == old_events
        assert sm._characters.effects.for_targets(sid, {command.actor_id}) == []

        sm._conn.execute("DROP TRIGGER fail_timed_spell_receipt")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) == receipt
        assert sm.submit_command(command, package, context=context) == receipt
        assert first_level_balance(sm, command) == 3
        assert len(sm._characters.effects.for_targets(sid, {command.actor_id})) == 1
