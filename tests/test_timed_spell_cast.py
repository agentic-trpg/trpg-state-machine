"""Public exploration spell adoption, expiration, and rollback."""

import random
from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, ExplorationSpellCastPayload
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.monster_rules import monster_rule_facts
from dnd5e_engine.rules.character import proficiency_grants
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import PassiveEffectChange
from dnd5e_srd_data.schema.spell import SpellDuration, SpellDurationUnits
from dnd_support import create, initialize
from item_support import NPC, A, item_data, item_loader
from lock_support import CRED
from spatial_support import submit as submit_world
from test_hold_effect_candidate import pass_command
from test_mechanism_service import extended_data, trigger
from test_real_checks import check_command
from test_session_time_coordination import submit
from test_spell_attack_cast import OTHER_ENEMY
from test_world_mechanisms import LEVER

from trpg_state_machine import (
    AdvanceSessionTime,
    EnvironmentArea,
    EnvironmentConditions,
    EnvironmentSceneFacts,
    EnvironmentSource,
    InitializeEnvironment,
    StateMachine,
    StateMachineError,
    StepActor,
    TimeBinding,
    TypedCommand,
)
from trpg_state_machine.package import _validate
from trpg_state_machine.perception import GameViewContext

PERCEPTION_SPELL = "fixture-keen-perception"
PERCEPTION_BONUS = "check.skill.perception.bonus"


@pytest.fixture
def timed_spell_world(tmp_path, context):
    """Create a second-scale exploration Session with authored wizard facts."""

    def open_world(
        *,
        material=True,
        first_level_slots=4,
        spell_id="longstrider",
        database="timed-spell.sqlite",
        seed=0,
        walking=False,
        attack_after_walk=False,
        region_mechanism=False,
        hit_rider=None,
        target_first=False,
        proficiency_override=None,
    ):
        canonical = BundledAssetLoader()
        base = item_loader()
        wizard = canonical.get_class("wizard")
        evoker = canonical.get_subclass("evoker")
        ogre = canonical.get_monster("ogre") if attack_after_walk else None
        spell = canonical.get_spell("longstrider")
        if spell_id == PERCEPTION_SPELL:
            # Reuse the supported activity shape under a distinct fixture-only rule identity.
            passive = spell.passive_effects[0].model_copy(
                update={
                    "name": "Fixture Keen Perception",
                    "description": "Grants +2 to Perception checks for one minute.",
                    "duration": {"seconds": 60},
                    "changes": [PassiveEffectChange(key=PERCEPTION_BONUS, mode=2, value="2")],
                }
            )
            spell = spell.model_copy(
                update={
                    "slug": spell_id,
                    "foundry_uuid": "",
                    "name": passive.name,
                    "description": passive.description,
                    "duration": SpellDuration(units=SpellDurationUnits.MINUTE, value=1),
                    "passive_effects": [passive],
                }
            )
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
            monsters=[ogre] if ogre else list(base._monsters.values()),
            classes=[wizard],
            subclasses=[evoker],
            features=[canonical.get_feature(slug) for slug in features],
            spells=[spell, *([canonical.get_spell(hit_rider)] if hit_rider else [])],
        )
        data = item_data()
        if region_mechanism:
            fixture = extended_data(triggers=[trigger("enter_region", cells=["1,0", "1,1", "1,2"])])
            objects = {LEVER, CRED}
            data["manifest"]["schema_version"] = "state-machine-foundation/10"
            data["lockable_doors"] = []
            data.setdefault("world_objects", []).extend(
                item for item in fixture["world_objects"] if item["object_id"] in objects
            )
            data["scenes"][0]["objects"].extend(
                item for item in fixture["scenes"][0]["objects"] if item["object_id"] in objects
            )
        data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
        data["time_binding"] = asdict(TimeBinding())
        foe_cell = "8,0" if attack_after_walk else "11,2"
        for actor in data["actors"]:
            actor.pop("npc_state", None)
            actor["initial_cell"] = foe_cell if walking else "1,0"
        if walking:
            for scene in data["scenes"]:
                scene["grid"]["width"] = 12
            combat = data["rule_bindings"]["combat_foundation"]["snapshot"]["combat_state"]
            combat["monster_slug_by_entity"] = {NPC: ogre.slug} if ogre else {}
            combat["initiative_ids"].reverse()
            combat["actor_zone"][NPC] = foe_cell
        if hit_rider:
            foe_cell = "1,0"
            data["actors"][0]["initial_cell"] = foe_cell
            other = deepcopy(data["actors"][0])
            other.update(actor_id=OTHER_ENEMY, name="Attendant", initial_cell="2,1")
            data["actors"].append(other)
            data["scenes"][0]["actor_ids"].append(OTHER_ENEMY)
            data["navigation"]["actor_grants"].append(
                {"actor_id": OTHER_ENEMY, "region_ids": ["region.public"]}
            )
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
            foundation["rng_state"] = RNGState.capture(random.Random(seed)).model_dump(mode="json")
            fixed = foundation["snapshot"]
            fixed["snapshot_schema_version"] = "engine-snapshot/20"
            if walking:
                fixed["scene_state"]["grid"]["width"] = 12
                for scene in foundation.get("additional_scenes", ()):
                    scene["grid"]["width"] = 12
            pc, npc = fixed["character_states"]
            pc.pop("spells_known", None)
            npc.pop("spells_known", None)
            if hit_rider:
                npc.update(hp_current=40, ac=12, temp_hp=0, granted_features=[])
                fixed["character_states"].append(
                    {**deepcopy(npc), "entity_id": OTHER_ENEMY, "name": "Attendant"}
                )
                if "combat_state" in fixed:
                    combat = fixed["combat_state"]
                    if not target_first:
                        combat["initiative_ids"].reverse()
                    combat["initiative_ids"].append(OTHER_ENEMY)
                    combat["encounter_ids"].append(OTHER_ENEMY)
                    combat["actor_zone"].update({NPC: foe_cell, OTHER_ENEMY: "2,1"})
                    combat["movement_ledgers"][OTHER_ENEMY] = deepcopy(
                        combat["movement_ledgers"][NPC]
                    )
                    combat["opportunity_attack_weapons"].clear()
                    combat["xp_value_by_entity"][OTHER_ENEMY] = 0
                else:
                    foundation["xp"][OTHER_ENEMY] = 0
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
                proficiency_bonus_override=proficiency_override,
                attack_bonus=None,
                stealth_disadvantage=False,
            )
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
            if ogre:
                npc.update(
                    {
                        key: value.model_dump(mode="json")
                        if hasattr(value, "model_dump")
                        else value
                        for key, value in monster_rule_facts(ogre).items()
                    },
                    dexterity=ogre.ability_scores.dex,
                    hp_current=ogre.hp,
                    hp_max=ogre.hp,
                    ac=ogre.ac,
                    creature_type=ogre.creature_type,
                    base_speed=ogre.movement.walk,
                    movement_remaining=ogre.movement.walk,
                    senses=ogre.senses.model_dump(mode="json", exclude={"passive_perception"}),
                    damage_resistances=list(ogre.damage_resistances),
                    damage_immunities=list(ogre.damage_immunities),
                    damage_vulnerabilities=list(ogre.damage_vulnerabilities),
                    condition_immunities=list(ogre.condition_immunities),
                    granted_features=[],
                    physical_resistances_nonmagical_only=False,
                    attack_bonus=None,
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
            prepared = {"spell_id": spell.slug, "prepared_order": 0, "material": None}
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
                                "spells": [
                                    prepared,
                                    *(
                                        [
                                            {
                                                "spell_id": hit_rider,
                                                "prepared_order": None,
                                                "material": None,
                                            }
                                        ]
                                        if hit_rider
                                        else []
                                    ),
                                ],
                            }
                        ],
                    }
                ]
            }
        package = _validate(data)
        authorized = replace(context, can_advance_time=True)
        sm = StateMachine(tmp_path / database, dnd_loader=loader)
        sid = create(sm, package, authorized)
        return sm, sid, package, authorized

    return open_world


def cast_command(session, *, target_id=None, spell_id="longstrider", command_id=None):
    """Submit the bound source, prepared spell, target, and exact slot level."""
    sm, sid, package, context = session
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        command_id or f"cast.{spell_id}",
        state.pc_actor_id,
        "world.cast_spell",
        ExplorationSpellCastPayload(
            kind="world.cast_spell",
            spell_id=spell_id,
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
    viewer = GameViewContext(context.principal_id, sid, context.player_id, "player")
    with sm:
        assert submit(session, AdvanceSessionTime(11), "start-tick").status == "committed"
        command = cast_command(session, target_id=target_id)
        target = command.payload.target_id
        assert sm.get_scene_view(sid, package, context=viewer).own_character.effects == ()
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
        (effect,) = sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        assert (effect.id, effect.name, effect.target_id) == (
            "effect:longstrider",
            "Longstrider",
            target,
        )
        own_effect = target == command.actor_id
        trusted_effects = (effect.model_dump(mode="json"),) if own_effect else ()
        player_effects = ({"id": effect.id, "name": effect.name},) if own_effect else ()
        trusted = sm.read_session(sid, package, context=context).character
        player = sm.get_scene_view(sid, package, context=viewer).own_character
        assert trusted.effects == trusted_effects
        assert player.effects == player_effects
        assert sm._session_runtime.time.consumers["effects"].next_boundary(sid, package) == 3611

        assert submit(session, AdvanceSessionTime(3610), "before-expiry").status == "committed"
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == (effect,)
        expiry = submit(session, AdvanceSessionTime(3611), "at-expiry")
        assert expiry.status == "committed"
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == ()
        assert sm.read_session(sid, package, context=context).character.effects == ()
        assert sm.get_scene_view(sid, package, context=viewer).own_character.effects == ()
        assert trusted.effects == trusted_effects
        assert player.effects == player_effects
        assert any(
            event.event_type == "effect.expired"
            for event in sm.read_events(sid, package, context=context)
        )
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == ()
        assert first_level_balance(sm, command) == 3


def test_public_timed_spell_modifies_only_perception_until_expiry(timed_spell_world):
    """A bound check bonus changes the real skill roll through the last active tick."""
    session = timed_spell_world(spell_id=PERCEPTION_SPELL)
    sm, sid, package, context = session
    check_context = replace(context, can_adjudicate_checks=True)

    def check_modifier(skill, phase):
        """Read the modifier from the real check event associated with this receipt."""
        command = check_command(
            sm,
            sid,
            package,
            check_context,
            f"check.{skill}.{phase}",
            actor_id=NPC,
            skill=skill,
            ability="wis",
        )
        receipt = sm.submit_command(command, package, context=check_context)
        assert receipt.status == "committed", receipt
        (event,) = [
            event
            for event in sm.read_events(sid, package, context=check_context)
            if event.event_seq in receipt.event_seqs and event.event_type == "rules.check_rolled"
        ]
        assert event.source_command_id == command.command_id
        result = event.payload["engine_event"]
        assert result["skill"] == skill and result["ability"] == "wis"
        assert result["advantage"] == "normal"
        assert result["roll_total"] == result["natural"] + result["modifier"]
        return result["modifier"]

    with sm:
        assert submit(session, AdvanceSessionTime(11), "start-tick").status == "committed"
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == ()
        perception = check_modifier("perception", "baseline")
        insight = check_modifier("insight", "baseline")

        command = cast_command(session, target_id=NPC, spell_id=PERCEPTION_SPELL)
        assert first_level_balance(sm, command) == 4
        cast = sm.submit_command(command, package, context=context)
        assert cast.status == "committed", cast
        assert first_level_balance(sm, command) == 3
        (effect,) = sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        identity = (effect.target_id, effect.id, effect.origin)
        assert identity == (
            NPC,
            "effect:fixture_keen_perception",
            f"cast:fixture_keen_perception:{command.actor_id}",
        )
        assert effect.duration.seconds == 60
        (change,) = effect.changes
        assert (change.key, change.mode, change.value) == (PERCEPTION_BONUS, "add", "2")
        assert check_modifier("perception", "cast") == perception + 2
        assert check_modifier("insight", "cast") == insight

        before = submit(session, AdvanceSessionTime(70), "before-expiry")
        assert before.status == "committed", before
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == (effect,)
        assert check_modifier("perception", "before-expiry") == perception + 2

        expiry = submit(session, AdvanceSessionTime(71), "at-expiry")
        assert expiry.status == "committed", expiry
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == ()
        (expired,) = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_type == "effect.expired"
        ]
        assert expired.event_seq in expiry.event_seqs
        assert expired.source_command_id == "at-expiry"
        assert expired.payload["boundary_tick"] == 71
        assert tuple(expired.payload["identity"]) == identity
        assert check_modifier("perception", "expired") == perception
        assert first_level_balance(sm, command) == 3


@pytest.mark.parametrize(
    ("spell_id", "effect_slug", "deadline", "seconds_before"),
    [
        ("longstrider", "longstrider", 3600, 12),
        ("longstrider", "longstrider", 3600, 6),
        (PERCEPTION_SPELL, "fixture_keen_perception", 60, 6),
    ],
    ids=["longstrider-retained", "longstrider-expires", "perception-expires"],
)
def test_public_ordinary_attack_with_active_timed_spell(
    timed_spell_world, spell_id, effect_slug, deadline, seconds_before
):
    """Compare a seeded attack with and without a timed speed or skill effect."""

    def attack(*, with_effect):
        """Run the same public combat commands with one independently seeded save."""
        session = timed_spell_world(
            spell_id=spell_id, database=f"attack-{with_effect}.sqlite", seed=9
        )
        sm, sid, package, context = session
        with sm:
            cast = cast_command(session, spell_id=spell_id)
            if with_effect:
                receipt = sm.submit_command(cast, package, context=context)
                assert receipt.status == "committed", receipt
                (effect,) = sm.read_noncombat_snapshot(sid, package, context=context).effect_states
                assert (effect.target_id, effect.id, effect.origin, effect.duration.seconds) == (
                    cast.actor_id,
                    f"effect:{effect_slug}",
                    f"cast:{effect_slug}:{cast.actor_id}",
                    deadline,
                )
                expected_effects = (effect,)
            else:
                expected_effects = ()
            before_tick = deadline - seconds_before
            advance = submit(session, AdvanceSessionTime(before_tick), "before-combat")
            assert advance.status == "committed", advance
            initialized = initialize(sm, sid, package, context)
            assert initialized.status == "committed", initialized
            before = sm.read_combat_snapshot(sid, package, context=context)
            assert before.effect_states == expected_effects
            turn = pass_command(sid, before, "open-npc-turn")
            assert turn.actor_id == cast.actor_id
            assert sm.submit_command(turn, package, context=context).status == "committed"
            before = sm.read_combat_snapshot(sid, package, context=context)
            assert before.effect_states == expected_effects
            intent = CombatIntentPayload(
                intent_type="attack",
                source_id=NPC,
                target_id=cast.actor_id,
                stat_block_action_id="scimitar",
            )
            command = TypedCommand(
                sid, "npc.attack", NPC, "combat.intent", intent, before.world_version
            )
            rng = sm.read_rng_context(sid, package, context=context)
            available = sm.query_action_availability(sid, NPC, intent, package, context=context)
            assert sm.read_rng_context(sid, package, context=context) == rng
            receipt = sm.submit_command(command, package, context=context)
            assert (receipt.status, receipt.error_code) == ("committed", None), available
            assert available.status == "available", available
            events = [
                event.payload["engine_event"]
                for event in sm.read_events(sid, package, context=context)
                if event.event_seq in receipt.event_seqs
                and event.event_type == "combat.engine_event"
            ]
            (roll,) = [event for event in events if event["type"] == "attack_rolled"]
            (damage,) = [event for event in events if event["type"] == "damage_applied"]
            assert (roll["attacker_id"], roll["target_id"], damage["target_id"]) == (
                NPC,
                cast.actor_id,
                cast.actor_id,
            )
            assert (roll["natural"], roll["roll_total"], roll["advantage"], roll["is_hit"]) == (
                15,
                15,
                "normal",
                True,
            )
            assert roll["modifier"] == 0
            after = sm.read_combat_snapshot(sid, package, context=context)
            hp_before = {actor.entity_id: actor.hp_current for actor in before.character_states}
            hp_after = {actor.entity_id: actor.hp_current for actor in after.character_states}
            assert (hp_before[cast.actor_id], damage["amount"], hp_after[cast.actor_id]) == (
                8,
                5,
                3,
            )
            assert hp_after[NPC] == hp_before[NPC]
            after_rng = sm.read_rng_context(sid, package, context=context)
            assert after_rng.version == rng.version + 1
            after_tick = before_tick + 6
            assert after.effect_states == (expected_effects if after_tick < deadline else ())
            assert sm.read_time_sync(sid, package, context=context).committed_tick == after_tick
            movement = {
                actor.entity_id: actor.movement_remaining for actor in after.character_states
            }
            budgets = (movement[cast.actor_id], movement[NPC])

            if with_effect:
                expiry_receipt, expiry_command = receipt, command
                if after_tick < deadline:
                    turn = pass_command(sid, after, "last-active-turn")
                    assert sm.submit_command(turn, package, context=context).status == "committed"
                    after = sm.read_combat_snapshot(sid, package, context=context)
                    assert after.effect_states == expected_effects
                    assert (
                        sm.read_time_sync(sid, package, context=context).committed_tick
                        == after_tick
                    )
                    expiry_command = pass_command(sid, after, "expire-effect")
                    expiry_receipt = sm.submit_command(expiry_command, package, context=context)
                    assert expiry_receipt.status == "committed", expiry_receipt
                assert sm.read_time_sync(sid, package, context=context).committed_tick == deadline
                assert sm.read_combat_snapshot(sid, package, context=context).effect_states == ()
                (expired,) = [
                    event
                    for event in sm.read_events(sid, package, context=context)
                    if event.event_type == "effect.expired"
                ]
                assert expired.event_seq in expiry_receipt.event_seqs
                assert expired.source_command_id == expiry_command.command_id
                assert expired.payload["boundary_tick"] == deadline
                assert tuple(expired.payload["identity"]) == (
                    effect.target_id,
                    effect.id,
                    effect.origin,
                )

            return (rng.version, rng.state), (after_rng.version, after_rng.state), budgets

    before_rng, after_rng, budgets = attack(with_effect=False)
    expected_walk_bonus = 10 if spell_id == "longstrider" and seconds_before > 6 else 0
    assert attack(with_effect=True) == (
        before_rng,
        after_rng,
        (budgets[0] + expected_walk_bonus, budgets[1]),
    )


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


@pytest.mark.parametrize("spell_id,expires_tick", [("longstrider", 3600), (PERCEPTION_SPELL, 60)])
def test_late_receipt_failure_reverts_effect_and_slot(timed_spell_world, spell_id, expires_tick):
    session = timed_spell_world(spell_id=spell_id)
    sm, sid, package, context = session
    with sm:
        command = cast_command(session, spell_id=spell_id, command_id="cast.rollback")
        old_state = sm.read_session(sid, package, context=context)
        old_events = sm.read_events(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_timed_spell_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='cast.rollback' AND EXISTS ("
            f"SELECT 1 FROM ongoing_effects WHERE session_id=NEW.session_id "
            f"AND expires_tick={expires_tick}"
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
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == ()

        sm._conn.execute("DROP TRIGGER fail_timed_spell_receipt")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) == receipt
        assert sm.submit_command(command, package, context=context) == receipt
        assert first_level_balance(sm, command) == 3
        assert len(sm.read_noncombat_snapshot(sid, package, context=context).effect_states) == 1


def move_command(session, destination, key):
    """Prepare an ordinary walking intent with the current public world version."""
    sm, sid, package, context = session
    snapshot = sm.read_combat_snapshot(sid, package, context=context)
    actor = snapshot.combat_state.initiative_ids[snapshot.combat_state.current_turn_index]
    return TypedCommand(
        sid,
        key,
        actor,
        "combat.intent",
        CombatIntentPayload(intent_type="move", target_zone_id=destination),
        snapshot.world_version,
    )


@pytest.mark.parametrize("total_distance", [35, 40])
def test_public_ordinary_walk_adopts_longstrider_and_preserves_spent_feet(
    timed_spell_world, total_distance
):
    """Move on the last active turn and use only base speed on the next own turn."""
    session = timed_spell_world(walking=True)
    sm, sid, package, context = session

    def read():
        return sm.read_combat_snapshot(sid, package, context=context)

    def execute(command):
        return sm.submit_command(command, package, context=context)

    def move(destination, key):
        return execute(move_command(session, destination, key))

    def movement(snapshot, actor_id):
        actor = next(actor for actor in snapshot.character_states if actor.entity_id == actor_id)
        ledger = snapshot.combat_state.movement_ledgers[actor_id]
        return actor.movement_remaining, ledger.spent_ft, ledger.distance_ft

    with sm:
        cast = cast_command(session)
        assert execute(cast).status == "committed"
        assert submit(session, AdvanceSessionTime(3594), "last-combat-round").status == "committed"
        assert initialize(sm, sid, package, context).status == "committed"
        assert execute(pass_command(sid, read(), "npc-pass")).status == "committed"
        start = read()
        actor_id = cast.actor_id
        rng = sm.read_rng_context(sid, package, context=context)
        assert movement(start, actor_id) == (40, 0, 0)
        invalid = move("999,0", "invalid-destination")
        assert (invalid.status, invalid.error_code) == ("rejected", "movement.destination")
        assert move("2,0", "walk-ten").status == "committed"
        first = read()
        assert movement(first, actor_id) == (30, 10, 10)
        assert first.combat_state.actor_zone[actor_id] == "2,0"
        stale_command = replace(
            move_command(session, "3,0", "stale-walk"), expected_world_version=start.world_version
        )
        assert execute(stale_command).status == "conflict"
        assert read() == first
        too_far = move("9,0", "walk-too-far")
        assert (too_far.status, too_far.error_code) == ("rejected", "movement.insufficient")
        destination = f"{total_distance // 5},0"
        finish = move(destination, "walk-over-base-limit")
        assert finish.status == "committed", finish
        used = read()
        assert movement(used, actor_id) == (40 - total_distance, total_distance, total_distance)
        assert used.combat_state.actor_zone[actor_id] == destination
        position = sm.read_actor_position(sid, actor_id, package, context=context)
        assert position.cell == destination
        assert start.combat_state.actor_zone[actor_id] == "0,0"
        assert movement(start, actor_id) == (40, 0, 0)
        assert sm.read_rng_context(sid, package, context=context) == rng

        expiry = execute(pass_command(sid, used, "expire-on-round-boundary"))
        assert expiry.status == "committed", expiry
        expired = read()
        assert not expired.effect_states
        assert movement(expired, actor_id) == (0, total_distance, total_distance)
        events = sm.read_events(sid, package, context=context)
        (event,) = [event for event in events if event.event_type == "effect.expired"]
        assert event.event_seq in expiry.event_seqs
        assert event.payload["boundary_tick"] == 3600
        assert execute(pass_command(sid, expired, "npc-next-pass")).status == "committed"
        reset = read()
        assert movement(reset, actor_id) == (30, 0, 0)
        out_of_base_range = move("0,0", "outside-base-range")
        assert (out_of_base_range.status, out_of_base_range.error_code) == (
            "rejected",
            "movement.insufficient",
        )
        assert move(f"{total_distance // 5 - 6},0", "walk-base-limit").status == "committed"
        assert movement(read(), actor_id) == (0, 30, 30)


@pytest.mark.parametrize("with_effect", [False, True])
def test_public_walk_rolls_back_late_failure(timed_spell_world, with_effect):
    """A receipt fault reverts the proposed route, actual spend, budget, and audit event."""
    session = timed_spell_world(walking=True)
    sm, sid, package, context = session
    remaining = 35 if with_effect else 25
    with sm:
        if with_effect:
            assert sm.submit_command(cast_command(session), package, context=context).status == (
                "committed"
            )
        assert initialize(sm, sid, package, context).status == "committed"
        first = sm.read_combat_snapshot(sid, package, context=context)
        assert (
            sm.submit_command(pass_command(sid, first, "npc-pass"), package, context=context).status
            == "committed"
        )
        fixed = sm.read_combat_snapshot(sid, package, context=context)
        command = move_command(session, "1,0", "walk.rollback")
        pc = command.actor_id
        before_position = sm.read_actor_position(sid, pc, package, context=context)
        before_events = sm.read_events(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_walk_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='walk.rollback' "
            "AND EXISTS (SELECT 1 FROM actors WHERE session_id=NEW.session_id AND cell='1,0') "
            "AND EXISTS (SELECT 1 FROM combats WHERE session_id=NEW.session_id "
            "AND 5 IN (SELECT json_extract(value,'$.spent_ft') "
            "FROM json_each(state_json,'$.movement_ledgers'))) "
            "AND EXISTS (SELECT 1 FROM mechanical_actors WHERE session_id=NEW.session_id "
            f"AND json_extract(state_json,'$.movement_remaining')={remaining}) "
            "BEGIN SELECT RAISE(ABORT,'injected walk receipt failure'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(command, package, context=context)
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert sm.read_actor_position(sid, pc, package, context=context) == before_position
        assert sm.read_combat_snapshot(sid, package, context=context) == fixed
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm.read_events(sid, package, context=context) == before_events

        sm._conn.execute("DROP TRIGGER fail_walk_receipt")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm.read_actor_position(sid, pc, package, context=context).cell == "1,0"
        after = sm.read_combat_snapshot(sid, package, context=context)
        ledger = after.combat_state.movement_ledgers[pc]
        actor = next(actor for actor in after.character_states if actor.entity_id == pc)
        assert (ledger.spent_ft, ledger.distance_ft, actor.movement_remaining) == (5, 5, remaining)
        assert actor.action_available and actor.bonus_action_available
        (event,) = [
            event.payload["engine_event"]
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in receipt.event_seqs
        ]
        assert (event["type"], event["distance_ft"], event["movement_cost_ft"]) == (
            "actor_moved",
            5,
            5,
        )
        assert event["path"] == ["0,0", "1,0"]
        assert fixed.combat_state.actor_zone[pc] == "0,0"


def test_public_attack_uses_position_adopted_by_longstrider_walk(timed_spell_world):
    """The Ogre's Greatclub reaches the target only after the committed walk."""
    session = timed_spell_world(walking=True, attack_after_walk=True, seed=1)
    sm, sid, package, context = session

    def read():
        """Read the fixed combat state published by the current command."""
        return sm.read_combat_snapshot(sid, package, context=context)

    with sm:
        cast = cast_command(session)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        assert initialize(sm, sid, package, context).status == "committed"
        fixed = read()
        rng = sm.read_rng_context(sid, package, context=context)
        strike = TypedCommand(
            sid,
            "strike.before-walk",
            NPC,
            "combat.intent",
            CombatIntentPayload(
                intent_type="attack", target_id=cast.actor_id, stat_block_action_id="greatclub"
            ),
            fixed.world_version,
        )
        far = sm.submit_command(strike, package, context=context)
        assert (far.status, far.error_code) == ("rejected", "out_of_range"), far
        assert (
            sm.submit_command(pass_command(sid, fixed, "npc-pass"), package, context=context).status
            == "committed"
        )
        walk = sm.submit_command(move_command(session, "7,0", "approach"), package, context=context)
        assert walk.status == "committed", walk
        occupied = sm.submit_command(
            move_command(session, "8,0", "occupied"), package, context=context
        )
        assert (occupied.status, occupied.error_code) == ("rejected", "movement.occupied")
        assert sm.read_rng_context(sid, package, context=context) == rng

        assert (
            sm.submit_command(pass_command(sid, read(), "pc-pass"), package, context=context).status
            == "committed"
        )
        strike = replace(
            strike, command_id="strike.after-walk", expected_world_version=read().world_version
        )
        attack = sm.submit_command(strike, package, context=context)
        assert attack.status == "committed", attack
        (rolled,) = [
            event.payload["engine_event"]
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in attack.event_seqs
            and event.payload.get("engine_event", {}).get("type") == "attack_rolled"
        ]
        assert (rolled["attacker_id"], rolled["target_id"]) == (NPC, cast.actor_id)
        after = read()
        assert after.combat_state.actor_zone[cast.actor_id] == "7,0"
        assert after.effect_states == fixed.effect_states
        assert sm.read_rng_context(sid, package, context=context).version == rng.version + 1


def test_timed_walk_refuses_consistent_but_nonadjacent_result(timed_spell_world, monkeypatch):
    """A correlated event and ledger cannot authorize a jump outside the fixed grid."""
    from dnd5e_engine.evaluation_delta import PositionUpdate

    session = timed_spell_world(walking=True)
    sm, sid, package, context = session
    with sm:
        cast = cast_command(session)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        assert initialize(sm, sid, package, context).status == "committed"
        fixed = sm.read_combat_snapshot(sid, package, context=context)
        assert (
            sm.submit_command(pass_command(sid, fixed, "npc-pass"), package, context=context).status
            == "committed"
        )
        fixed = sm.read_combat_snapshot(sid, package, context=context)
        command = move_command(session, "2,0", "walk-with-bad-route")
        real = sm._rules._evaluate_dnd
        path = ("0,0", "11,0", "2,0")

        async def forged(request):
            """Change only the matching paths in the real authenticated proposal."""
            result = await real(request)
            assert result.status == "accepted"
            assert result.stage_facts.coverage == "uncovered"
            edits = tuple(
                operation.model_copy(update={"path": path})
                if isinstance(operation, PositionUpdate)
                else operation
                for operation in result.state_delta.operations
            )
            (event,) = result.proposed_events
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(update={"operations": edits}),
                    "proposed_events": (event.model_copy(update={"path": path}),),
                }
            )

        with monkeypatch.context() as scope:
            scope.setattr(sm._rules, "_evaluate_dnd", forged)
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(command, package, context=context)
        assert error.value.code == "invalid_rule_result"
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert sm.read_combat_snapshot(sid, package, context=context) == fixed
        assert sm.submit_command(command, package, context=context).status == "committed"


@pytest.mark.parametrize(
    ("cells", "destination", "status"),
    [
        (("1,0",), "1,0", "rejected"),
        (("1,0", "1,1", "1,2"), "2,0", "rejected"),
        (("0,0", "1,0"), "1,0", "committed"),
        (("6,0",), "2,0", "committed"),
    ],
    ids=["enter", "enter-exit", "same-region", "unrelated"],
)
def test_timed_walk_respects_fixed_region_coverage(timed_spell_world, cells, destination, status):
    """Trace every routed cell through the trusted by-cell environmental index."""
    session = timed_spell_world(walking=True)
    sm, sid, package, context = session
    with sm:
        source = EnvironmentSource(
            "toxic.area",
            EnvironmentConditions(),
            EnvironmentArea(cells),
            region_condition="poisoned",
        )
        seeded = sm.environment.initialize(
            sid,
            (InitializeEnvironment(A, EnvironmentSceneFacts(sources=(source,))),),
            package,
            command_id="seed.region",
            expected_world_version=sm.read_session(sid, package, context=context).world_version,
            context=replace(context, can_manage_environment=True),
        )
        assert seeded.status == "committed", seeded
        cast = cast_command(session)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        assert initialize(sm, sid, package, context).status == "committed"
        fixed = sm.read_combat_snapshot(sid, package, context=context)
        assert (
            sm.submit_command(pass_command(sid, fixed, "npc-pass"), package, context=context).status
            == "committed"
        )
        fixed = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        command = move_command(session, destination, "walk.region")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == status, receipt
        current = sm.read_combat_snapshot(sid, package, context=context)
        assert sm.read_rng_context(sid, package, context=context) == rng
        if status == "rejected":
            assert receipt.error_code == "movement.capability"
            assert receipt.event_seqs == ()
            assert current == fixed
            assert sm.submit_command(command, package, context=context) == receipt
            assert (
                sm.read_actor_position(sid, cast.actor_id, package, context=context).cell == "0,0"
            )
        else:
            assert current.combat_state.actor_zone[cast.actor_id] == destination


def test_timed_walk_refuses_world_region_mechanism_entry(timed_spell_world):
    """A real armed regional trip cannot be skipped on a multi-edge walk."""
    control = timed_spell_world(walking=True, region_mechanism=True, database="trip-control.db")
    control_sm, control_sid, control_package, control_context = control
    with control_sm:
        entered = submit_world(
            control_sm,
            control_sid,
            control_package,
            control_context,
            StepActor(A, "0,0", "1,0"),
            "step.exploration.trip",
        )
        assert entered.status == "committed", entered
        assert control_sm._world_facts.mechanism_state(control_sid, LEVER).activation_count == 1
        assert control_sm._world_facts.light_state(control_sid, CRED)["enabled"]

    session = timed_spell_world(walking=True, region_mechanism=True)
    sm, sid, package, context = session
    with sm:
        cast = cast_command(session)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        assert initialize(sm, sid, package, context).status == "committed"
        fixed = sm.read_combat_snapshot(sid, package, context=context)
        assert (
            sm.submit_command(pass_command(sid, fixed, "npc-pass"), package, context=context).status
            == "committed"
        )
        fixed = sm.read_combat_snapshot(sid, package, context=context)
        mechanism = sm._world_facts.mechanism_state(sid, LEVER)
        assert mechanism.status == "armed"
        receipt = sm.submit_command(
            move_command(session, "2,0", "walk.across.trip"), package, context=context
        )
        assert (receipt.status, receipt.error_code) == ("rejected", "movement.capability")
        assert receipt.event_seqs == ()
        assert sm._world_facts.mechanism_state(sid, LEVER) == mechanism
        assert not sm._world_facts.light_state(sid, CRED)["enabled"]
        assert sm.read_combat_snapshot(sid, package, context=context) == fixed
