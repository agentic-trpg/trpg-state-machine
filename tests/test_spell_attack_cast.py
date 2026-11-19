"""Exercise canonical ranged spell attacks through public SM combat commands."""

from random import Random

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.rules.character import proficiency_grants
from dnd5e_engine.spell_delivery import EffectSelection
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd_support import create, initialize
from item_support import NPC, ROPE, item_data, item_loader
from test_cure_wounds import actor, pool
from test_dnd_runtime import authority
from test_ordinary_spells import assert_committed_events, assert_npc_death, observe_evaluation

from trpg_state_machine import StateMachine, StateMachineError, TypedCommand
from trpg_state_machine.package import _validate

SOURCE = "class:wizard"
FIRE_BOLT = "fire-bolt"
POISON_SPRAY = "poison-spray"
SPELLS = {
    FIRE_BOLT: (24, 10, "fire"),
    POISON_SPRAY: (6, 12, "poison"),
}


@pytest.fixture
def open_spell_combat(tmp_path, context):
    """Create the same Wizard 1 Package for both spells, varying only test facts."""

    def open_world(*, seed=16, column=6, hp=40, temp_hp=1, omit_spell=None):
        canonical = BundledAssetLoader()
        base = item_loader()
        wizard = canonical.get_class("wizard")
        features = ["spellcasting", "ritual-adept", "arcane-recovery"]
        proficiency = proficiency_grants([(wizard, 1, "primary")])
        loader = MemoryAssetLoader(
            classes=[wizard],
            features=[canonical.get_feature(slug) for slug in features],
            spells=[canonical.get_spell(slug) for slug in SPELLS],
            items=list(base._items.values()),
            monsters=list(base._monsters.values()),
        )
        data = item_data()
        data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
        data["pc_templates"][0]["character"]["inventory"] = []
        data["actors"][0]["initial_cell"] = f"{column},0"
        data["scenes"][0]["grid"]["width"] = 26

        for foundation in data["rule_bindings"].values():
            foundation["rng_state"] = RNGState.capture(Random(seed)).model_dump(mode="json")
            snapshot = foundation["snapshot"]
            snapshot["snapshot_schema_version"] = "engine-snapshot/20"
            snapshot["scene_state"]["grid"]["width"] = 26
            if "combat_state" in snapshot:
                snapshot["combat_state"]["actor_zone"][NPC] = f"{column},0"
            pc, npc = snapshot["character_states"]
            pc.update(
                class_slug="wizard",
                classes={"wizard": 1},
                character_level=1,
                granted_features=features,
                intelligence=16,
                spellcasting_ability="int",
                spell_slots={1: 2},
                custom_counters={"feature_use:arcane-recovery": {"spent": 0}},
                save_proficiencies=sorted(proficiency.saves),
                weapon_proficiencies=sorted(proficiency.weapons),
                carried_item_slugs=[],
                weapon_in_hands=None,
                weapon_grip="none",
                proficiency_bonus_override=None,
                attack_bonus=None,
            )
            npc.update(hp_current=hp, ac=12, temp_hp=temp_hp, granted_features=[])
            pc.pop("spells_known", None)
            npc.pop("spells_known", None)
            snapshot["inventory_state"]["entries"] = []
            snapshot["resource_state"] = {
                "pools": [
                    {
                        "kind": "spell_slot",
                        "owner_id": pc["entity_id"],
                        "pool_id": "spell:1",
                        "current": 2,
                        "maximum": 2,
                        "level": 1,
                    },
                    {
                        "kind": "feature_uses",
                        "owner_id": pc["entity_id"],
                        "pool_id": "arcane-recovery",
                        "feature_slug": "arcane-recovery",
                        "current": 1,
                        "maximum": 1,
                    },
                ]
            }
            snapshot["casting_state"] = {
                "actors": [
                    {
                        "actor_id": pc["entity_id"],
                        "verbal_available": True,
                        "sources": [
                            {
                                "source_id": SOURCE,
                                "class_slug": "wizard",
                                "ability": "int",
                                "spells": [
                                    {"spell_id": slug, "prepared_order": None, "material": None}
                                    for slug in SPELLS
                                    if slug != omit_spell
                                ],
                            }
                        ],
                    }
                ]
            }

        package = _validate(data)
        db = tmp_path / "spell-attack.sqlite"
        sm = StateMachine(db, dnd_loader=loader)
        try:
            sid = create(sm, package, context)
            receipt = initialize(sm, sid, package, context)
            assert receipt.status == "committed", receipt
        except Exception:
            sm.close()
            raise
        return sm, sid, package, loader, db

    return open_world


def cast_command(sm, sid, package, context, spell_id, **choices):
    """Build an ordinary combat intent against the current world version."""
    state = sm.read_session(sid, package, context=context)
    payload = {
        "intent_type": "cast_spell",
        "spell_id": spell_id,
        "spell_source_id": SOURCE,
        "target_id": NPC,
        **choices,
    }
    return TypedCommand(
        sid,
        "attack.cast",
        state.pc_actor_id,
        "combat.intent",
        CombatIntentPayload(**payload),
        state.world_version,
    )


@pytest.mark.parametrize("spell_id", SPELLS)
@pytest.mark.parametrize(
    "seed,natural,hit,crit",
    [(31, 1, False, False), (16, 12, True, False), (5, 20, True, True)],
    ids=["natural-1", "hit", "critical"],
)
def test_ranged_spell_attacks_commit_at_canonical_range(
    open_spell_combat, context, monkeypatch, spell_id, seed, natural, hit, crit
):
    """Check the roll draw, damage dice, resource mirror and turn handoff for both spells."""
    column, damage_die, damage_type = SPELLS[spell_id]
    sm, sid, package, loader, db = open_spell_combat(seed=seed, column=column)
    with sm:
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        command = cast_command(sm, sid, package, context, spell_id)
        assert before.combat_state.actor_zone[command.actor_id] == "0,0"
        assert before.combat_state.actor_zone[NPC] == f"{column},0"
        assert before.scene_state.grid.cell_size_ft == 5
        assert actor(before, command.actor_id).action_available
        observed = observe_evaluation(sm, monkeypatch)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        (result,) = observed
        assert result.status == "accepted"
        (cast,) = [event for event in result.proposed_events if event.type == "spell_cast"]
        (roll,) = [event for event in result.proposed_events if event.type == "attack_rolled"]
        (turn,) = [event for event in result.proposed_events if event.type == "turn_started"]
        assert (cast.actor_id, cast.spell_id, cast.slot_level) == (command.actor_id, spell_id, None)
        assert (roll.attacker_id, roll.target_id, roll.advantage) == (
            command.actor_id,
            NPC,
            "normal",
        )
        assert (roll.natural, roll.modifier, roll.roll_total, roll.is_hit, roll.is_crit) == (
            natural,
            5,
            natural + 5,
            hit,
            crit,
        )
        assert turn.actor_id == NPC

        expected_rolls = Random(seed)
        assert expected_rolls.randint(1, 20) == natural
        after = sm.read_combat_snapshot(sid, package, context=context)
        target = actor(after, NPC)
        damage_events = [
            event for event in result.proposed_events if event.type == "damage_applied"
        ]
        if hit:
            (damage,) = damage_events
            (activity,) = loader.get_spell(spell_id).activities
            assert damage.source_id == f"spell:{spell_id}:{activity.id}"
            seq = before.combat_state.damage_instance_sequence + 1
            assert after.combat_state.damage_instance_sequence == seq
            assert damage.damage_instance_id == f"damage:{seq}:{command.actor_id}:{NPC}"
            assert (damage.target_id, damage.source_actor_id) == (NPC, command.actor_id)
            expected_damage = sum(
                expected_rolls.randint(1, damage_die) for _ in range(2 if crit else 1)
            )
            assert (damage.amount, damage.is_crit, damage.damage_type) == (
                expected_damage,
                crit,
                damage_type,
            )
            temp_ops = [
                op for op in result.state_delta.operations if op.kind == "actor.temp_hp_set"
            ]
            hp_ops = [op for op in result.state_delta.operations if op.kind == "actor.hp_delta"]
            (temp,), (hp,) = temp_ops, hp_ops
            assert (temp.target_id, temp.expected_temp_hp, temp.temp_hp) == (NPC, 1, 0)
            assert hp.target_id == NPC
            assert target.temp_hp == 0
            assert target.hp_current == hp.resulting_hp == 40 - (damage.amount - 1)
            assert target.last_damaged_by == command.actor_id
        else:
            assert damage_events == []
            assert (
                after.combat_state.damage_instance_sequence
                == before.combat_state.damage_instance_sequence
            )
            assert (target.hp_current, target.temp_hp, target.last_damaged_by) == (40, 1, None)

        assert (actor(before, NPC).hp_current, actor(before, NPC).temp_hp) == (40, 1)
        caster = actor(after, command.actor_id)
        previous_caster = actor(before, command.actor_id)
        assert not caster.action_available and caster.action_taken_this_turn
        assert caster.attack_rolls_made_this_turn == previous_caster.attack_rolls_made_this_turn + 1
        assert caster.attack_action_engaged == previous_caster.attack_action_engaged is False
        assert caster.attack_action_attacks_made == previous_caster.attack_action_attacks_made == 0
        assert caster.attacks_remaining == previous_caster.attacks_remaining
        assert caster.spell_slots == {1: 2}
        assert pool(after).current == pool(after).maximum == 2
        assert after.resource_state == before.resource_state
        assert after.combat_state.expended_resources == before.combat_state.expended_resources
        assert after.combat_state.current_turn_index == 1
        assert after.combat_state.turn_serial == before.combat_state.turn_serial + 1
        after_rng = sm.read_rng_context(sid, package, context=context)
        assert after_rng.version == rng.version + 1
        assert (
            after_rng.state == result.rng_transition.next_state == RNGState.capture(expected_rolls)
        )
        assert_committed_events(sm, sid, package, context, receipt, result)
        assert sm.submit_command(command, package, context=context) == receipt
        assert len(observed) == 1

    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after
        assert reopened.read_rng_context(sid, package, context=context) == after_rng
        assert (
            reopened.get_command_receipt(sid, command.command_id, package, context=context)
            == receipt
        )


def test_adjacent_hostile_imposes_disadvantage(open_spell_combat, context, monkeypatch):
    """Use the kept lower die when the hostile target starts within five feet."""
    sm, sid, package, _, _ = open_spell_combat(seed=5, column=1)
    with sm:
        command = cast_command(sm, sid, package, context, FIRE_BOLT)
        observed = observe_evaluation(sm, monkeypatch)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        (result,) = observed
        (roll,) = [event for event in result.proposed_events if event.type == "attack_rolled"]
        assert (roll.advantage, roll.natural, roll.roll_total, roll.is_crit) == (
            "disadvantage",
            9,
            14,
            False,
        )
        assert roll.is_hit


@pytest.mark.parametrize(
    "spell_id,column,omit_spell,choices,code",
    [
        (FIRE_BOLT, 25, None, {}, "target_invalid"),
        (POISON_SPRAY, 7, None, {}, "target_invalid"),
        (FIRE_BOLT, 6, FIRE_BOLT, {}, "spell.source"),
        (FIRE_BOLT, 6, None, {"spell_source_id": "class:cleric"}, "spell.source"),
        (FIRE_BOLT, 6, None, {"target_id": ROPE}, "target_invalid"),
        (FIRE_BOLT, 6, None, {"target_ids": (NPC,)}, "spell.capability"),
        (
            POISON_SPRAY,
            6,
            None,
            {
                "effect_selections": (
                    EffectSelection(
                        spell_id=POISON_SPRAY,
                        activity_id="UbLJ36AaSvIMQ2dy",
                        target_id=NPC,
                        effect_id="extra",
                    ),
                )
            },
            "spell.capability",
        ),
        (POISON_SPRAY, 6, None, {"slot_level": 1}, "spell.capability"),
    ],
    ids=[
        "fire-125ft",
        "poison-35ft",
        "unprepared",
        "source",
        "object",
        "targets",
        "selection",
        "slot",
    ],
)
def test_invalid_spell_attack_is_atomic(
    open_spell_combat, context, spell_id, column, omit_spell, choices, code
):
    """Preserve authority, events and RNG when the ordinary spell input is refused."""
    sm, sid, package, _, _ = open_spell_combat(column=column, omit_spell=omit_spell)
    with sm:
        command = cast_command(sm, sid, package, context, spell_id, **choices)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        stored = authority(sm, sid)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", code), receipt
        assert receipt.world_version == command.expected_world_version
        assert receipt.event_seqs == ()
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        current = authority(sm, sid)
        current.pop("command_receipts")
        stored.pop("command_receipts")
        assert current == stored
        assert sm.submit_command(command, package, context=context) == receipt


def test_lethal_spell_attack_rolls_back_late_receipt_failure(
    open_spell_combat, context, monkeypatch
):
    """Fail after domain writes, then replay the same lethal result exactly once."""
    sm, sid, package, _, _ = open_spell_combat(seed=5, hp=1, temp_hp=0)
    with sm:
        command = cast_command(sm, sid, package, context, POISON_SPRAY)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        stored = authority(sm, sid)
        observed = observe_evaluation(sm, monkeypatch)
        sm._conn.execute(
            f"""
            CREATE TEMP TRIGGER fail_spell_attack_receipt BEFORE INSERT ON command_receipts
            WHEN NEW.command_id='attack.cast'
            BEGIN
                SELECT CASE WHEN
                    EXISTS (
                        SELECT 1 FROM characters
                        WHERE session_id=NEW.session_id AND actor_id='{NPC}' AND current_hp=0
                    ) AND EXISTS (
                        SELECT 1 FROM event_records
                        WHERE session_id=NEW.session_id AND source_command_id=NEW.command_id
                    )
                    THEN RAISE(ABORT, 'injected after spell damage and events')
                    ELSE RAISE(ABORT, 'receipt attempted before spell damage and events')
                END;
            END
            """
        )
        with pytest.raises(StateMachineError) as failure:
            sm.submit_command(command, package, context=context)
        assert str(failure.value.__cause__) == "injected after spell damage and events"
        (first,) = observed
        assert first.status == "accepted"
        assert authority(sm, sid) == stored
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None

        sm._conn.execute("DROP TRIGGER fail_spell_attack_receipt")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        assert observed == [first, first]
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert_npc_death(before, after, first, command.actor_id)
        assert actor(after, command.actor_id).attack_rolls_made_this_turn == 0
        assert after.resource_state == before.resource_state
        assert (
            sm.read_rng_context(sid, package, context=context).state
            == first.rng_transition.next_state
        )
        assert_committed_events(sm, sid, package, context, receipt, first)
        assert sm.submit_command(command, package, context=context) == receipt
        assert observed == [first, first]


@pytest.mark.parametrize("fault", ["natural", "modifier", "rng"])
def test_spell_attack_rejects_incomplete_roll_commitment(
    open_spell_combat, context, monkeypatch, fault
):
    """Refuse forged evidence on a real hit and allow the same command to retry."""
    sm, sid, package, _, _ = open_spell_combat()
    with sm:
        command = cast_command(sm, sid, package, context, FIRE_BOLT)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        stored = authority(sm, sid)
        evaluate = sm._rules._evaluate_dnd
        observed = []

        async def alter_evidence(request):
            """Preserve the genuine state delta while removing one required proof."""
            result = await evaluate(request)
            observed.append(result)
            assert result.status == "accepted"
            (roll,) = [event for event in result.proposed_events if event.type == "attack_rolled"]
            assert roll.is_hit and roll.natural is not None and roll.modifier is not None
            if fault == "rng":
                transition = result.rng_transition
                assert transition.state_changed
                return result.model_copy(
                    update={
                        "rng_transition": transition.model_copy(
                            update={"next_state": transition.input_state, "state_changed": False}
                        )
                    }
                )
            forged = roll.model_copy(update={fault: None})
            return result.model_copy(
                update={
                    "proposed_events": tuple(
                        forged if event is roll else event for event in result.proposed_events
                    )
                }
            )

        with monkeypatch.context() as injected:
            injected.setattr(sm._rules, "_evaluate_dnd", alter_evidence)
            with pytest.raises(StateMachineError) as failure:
                sm.submit_command(command, package, context=context)
        assert failure.value.code == "invalid_rule_result"
        (genuine,) = observed
        assert authority(sm, sid) == stored
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None

        retried = observe_evaluation(sm, monkeypatch)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        assert retried == [genuine]
        assert_committed_events(sm, sid, package, context, receipt, genuine)
