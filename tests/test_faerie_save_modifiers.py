"""Public Faerie Fire adoption with fixed, target-owned saving throw facts."""

from copy import deepcopy

import pytest
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_state import ConditionRecord
from dnd5e_engine.evaluation_world_effect import EffectExpiryPolicy, WorldEffectApplication
from dnd5e_srd_data import BundledAssetLoader
from item_support import NPC
from spatial_support import submit as submit_world
from test_faerie_fire import _cast
from test_spell_attack_cast import OTHER_ENEMY
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import AdvanceSessionTime, StateMachine


def _session(
    timed_spell_world,
    *,
    condition="restrained",
    second_target=False,
    magic_resistance=False,
    bind_trait=False,
    seed=80,
    database="faerie-save-modifiers.sqlite",
):
    """Set DC 13 and place the selected NPCs in a cube outside the caster's cell."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        data["actors"][0]["initial_cell"] = "2,0"
        foundation = data["rule_bindings"]["noncombat_foundation"]
        pc, npc = foundation["snapshot"]["character_states"]
        pc["wisdom"] = 14
        npc.update(dexterity=10, save_proficiencies=[])
        if second_target:
            attendant = deepcopy(data["actors"][0])
            attendant.update(actor_id=OTHER_ENEMY, name="Attendant", initial_cell="2,1")
            data["actors"].append(attendant)
            data["scenes"][0]["actor_ids"].append(OTHER_ENEMY)
            data["navigation"]["actor_grants"].append(
                {"actor_id": OTHER_ENEMY, "region_ids": ["region.public"]}
            )
            foundation["snapshot"]["character_states"].append(
                {**deepcopy(npc), "entity_id": OTHER_ENEMY, "name": "Attendant"}
            )
            foundation["xp"][OTHER_ENEMY] = 0
        npc["conditions"] = [
            ConditionRecord(
                condition=condition,
                source_entity_id=f"implied:{condition}",
                scope="session",
                duration_rounds=None,
                save_dc=None,
                applied_round=0,
                exhaustion_level=1,
                source_effect_id=None,
            ).model_dump(mode="json")
        ]
        if magic_resistance:
            npc["trait_mechanics"] = ["magic_resistance"]

    return timed_spell_world(
        database=database,
        caster_class="druid",
        caster_subclass="land",
        additional_spells=("faerie-fire",),
        additional_unprepared_spells=("bless", "bane"),
        additional_traits=("magic-resistance",) if bind_trait else (),
        configure=configure,
        seed=seed,
    )


def _apply_effect(session, spell_id, caster_id, target_id, *, altered=False):
    """Seed an active spell through the Effects owner with canonical duration and source."""
    sm, sid, package, context = session
    spell = BundledAssetLoader().get_spell(spell_id)
    effect = effect_state(
        passive_effect_to_active_effect(
            spell.passive_effects[0],
            target_id=target_id,
            caster_id=caster_id,
            concentration=True,
        )
    )
    if altered:
        changes = [change.model_copy(update={"value": "2d4"}) for change in effect.changes]
        effect = effect.model_copy(update={"changes": changes})
    with sm._store.transaction(write=True):
        row = sm._world.sessions._session(sid, package, context)
        sm._session_runtime.time.consumers["effects"].apply_verified(
            sid,
            WorldEffectApplication(
                session_id=sid,
                command_id=f"seed-{spell_id}",
                source_id=caster_id,
                effect=effect,
                expiry=EffectExpiryPolicy(action="remove"),
                concentration=True,
                ruleset_binding=sm._rules.session_binding(row),
            ),
            spell_id=spell_id,
            cast_slot_level=1,
        )
    return effect


def _saves(session, command):
    """Read the cast's durable saving throw evidence in event sequence order."""
    sm, sid, package, context = session
    return tuple(
        event.payload["engine_event"]
        for event in sm.read_events(sid, package, context=context)
        if event.source_command_id == command.command_id
        and event.payload.get("engine_event", {}).get("type") == "save_rolled"
    )


def _d4_modifiers(save):
    """Extract the ordered bonus dice from committed roll detail."""
    return [modifier for modifier in save["roll_detail"]["modifiers"] if modifier["kind"] == "die"]


def _slot(session):
    """Read the first-level slot from public current mechanical facts."""
    sm, sid, package, context = session
    snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
    return next(pool.current for pool in snapshot.resource_state.pools if pool.pool_id == "spell:1")


def _creature_targets(session, command):
    """Read the creature members of the exact visual source written by the cast."""
    sm, sid, _, _ = session
    with sm._store.transaction():
        rows = sm._conn.execute(
            "SELECT target_id FROM target_visual_effects "
            "WHERE session_id=? AND source_id=? AND target_kind='creature' ORDER BY target_id",
            (sid, f"visual:{command.command_id}"),
        ).fetchall()
    return tuple(row["target_id"] for row in rows)


def test_public_faerie_saves_use_each_targets_spell_and_conserve_order(timed_spell_world, tmp_path):
    """Bless lifts one save to DC 13, and Bane drops the Restrained target below it."""
    database = "faerie-signed-dice.sqlite"
    session = _session(timed_spell_world, second_target=True, database=database)
    sm, sid, package, context = session
    with sm:
        bless = _apply_effect(session, "bless", NPC, OTHER_ENEMY)
        bane = _apply_effect(session, "bane", OTHER_ENEMY, NPC)
        prior_rng = sm.read_rng_context(sid, package, context=context)
        prior_slot = _slot(session)
        command, receipt = _cast(session, "signed-dice")
        assert receipt.status == "committed", receipt
        a, b = saves = _saves(session, command)
        assert [(save["target_id"], save["ability"], save["dc"]) for save in saves] == [
            (OTHER_ENEMY, "dex", 13),
            (NPC, "dex", 13),
        ]
        assert (a["natural"], a["roll_total"], a["succeeded"], a["sources"]) == (9, 13, True, [])
        assert (b["natural"], b["roll_total"], b["succeeded"], b["sources"]) == (
            14,
            11,
            False,
            ["condition:target"],
        )
        assert (a["advantage"], b["advantage"]) == ("normal", "disadvantage")
        assert a["roll_detail"]["d20"] == {"faces": [9], "kept_index": 0, "origin": "rng"}
        assert b["roll_detail"]["d20"] == {"faces": [18, 14], "kept_index": 1, "origin": "rng"}
        assert _d4_modifiers(a) == [
            {
                "kind": "die",
                "source": f"{bless.id}@{bless.origin}",
                "sides": 4,
                "face": 4,
                "value": 4,
            }
        ]
        assert _d4_modifiers(b) == [
            {
                "kind": "die",
                "source": f"{bane.id}@{bane.origin}",
                "sides": 4,
                "face": 3,
                "value": -3,
            }
        ]
        expected = prior_rng.state.restore()
        assert [expected.randint(1, sides) for sides in (20, 4, 20, 20, 4)] == [9, 4, 18, 14, 3]
        rng = sm.read_rng_context(sid, package, context=context)
        assert rng.state == RNGState.capture(expected)
        assert rng.version == prior_rng.version + 1
        assert _slot(session) == prior_slot - 1
        assert _creature_targets(session, command) == (NPC,)
        assert sm.submit_command(command, package, context=context) == receipt
        loader = sm._rules.loader

    with StateMachine(tmp_path / database, dnd_loader=loader) as reopened:
        session = reopened, sid, package, context
        assert reopened.submit_command(command, package, context=context) == receipt
        assert _slot(session) == prior_slot - 1
        assert _saves(session, command) == saves
        assert _creature_targets(session, command) == (NPC,)
        assert reopened.read_rng_context(sid, package, context=context) == rng

        elapsed = submit_world(reopened, sid, package, context, AdvanceSessionTime(60), "expired")
        assert elapsed.status == "committed", elapsed
        assert not reopened.read_noncombat_snapshot(sid, package, context=context).effect_states
        assert reopened.read_rng_context(sid, package, context=context) == rng
        second, receipt = _cast(session, "expired-dice")
        assert receipt.status == "committed", receipt
        after_a, after_b = _saves(session, second)
        assert [save["target_id"] for save in (after_a, after_b)] == [OTHER_ENEMY, NPC]
        assert [_d4_modifiers(save) for save in (after_a, after_b)] == [[], []]
        assert after_a["roll_detail"]["d20"]["faces"] == [expected.randint(1, 20)]
        assert after_b["roll_detail"]["d20"]["faces"] == [expected.randint(1, 20) for _ in range(2)]
        after_rng = reopened.read_rng_context(sid, package, context=context)
        assert after_rng.state == RNGState.capture(expected)
        assert after_rng.version == rng.version + 1


@pytest.mark.parametrize("bind_trait", (True, False))
def test_faerie_magic_resistance_requires_bound_trait(timed_spell_world, bind_trait):
    """The admitted magical advantage cancels Restrained, with one draw and both sources."""
    session = _session(timed_spell_world, magic_resistance=True, bind_trait=bind_trait, seed=47)
    sm, sid, package, context = session
    with sm:
        before = sm.read_rng_context(sid, package, context=context)
        prior_slot = _slot(session)
        command, receipt = _cast(session, "magic-resistance")
        if not bind_trait:
            assert (receipt.status, receipt.error_code) == ("rejected", "spell.visual_targets")
            assert _saves(session, command) == ()
            assert _creature_targets(session, command) == ()
            assert _slot(session) == prior_slot
            assert sm.read_rng_context(sid, package, context=context) == before
            return

        assert receipt.status == "committed", receipt
        (event,) = _saves(session, command)
        assert (event["target_id"], event["advantage"], event["sources"]) == (
            NPC,
            "normal",
            ["trait", "condition:target"],
        )
        assert event["roll_detail"]["d20"] == {"faces": [12], "kept_index": 0, "origin": "rng"}
        expected = before.state.restore()
        assert expected.randint(1, 20) == 12
        assert sm.read_rng_context(sid, package, context=context).state == RNGState.capture(
            expected
        )
        assert _slot(session) == prior_slot - 1
        assert _creature_targets(session, command) == (NPC,)


def test_faerie_paralyzed_save_draws_neither_d20_nor_active_bless_d4(timed_spell_world):
    """Automatic Dexterity failure commits the visual effect without sampling the RNG."""
    session = _session(timed_spell_world, condition="paralyzed")
    sm, sid, package, context = session
    with sm:
        caster = sm.read_session(sid, package, context=context).pc_actor_id
        _apply_effect(session, "bless", caster, NPC)
        before = sm.read_rng_context(sid, package, context=context)
        prior_slot = _slot(session)
        command, receipt = _cast(session, "automatic-failure")
        assert receipt.status == "committed", receipt
        (event,) = _saves(session, command)
        assert (event["target_id"], event["ability"], event["succeeded"]) == (NPC, "dex", False)
        assert event["natural"] is None
        assert event["roll_detail"] == {"status": "automatic_failure", "d20": None, "modifiers": []}
        assert sm.read_rng_context(sid, package, context=context) == before
        assert _slot(session) == prior_slot - 1
        assert _creature_targets(session, command) == (NPC,)


def test_faerie_rejects_unreviewed_save_effect_without_publishing(timed_spell_world):
    """A lookalike spell effect cannot be silently used or ignored in a multi-target cast."""
    session = _session(timed_spell_world, second_target=True)
    sm, sid, package, context = session
    with sm:
        _apply_effect(session, "bless", NPC, OTHER_ENEMY)
        _apply_effect(session, "bane", OTHER_ENEMY, NPC, altered=True)
        before_rng = sm.read_rng_context(sid, package, context=context)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        command, receipt = _cast(session, "unreviewed-effect")
        assert (receipt.status, receipt.error_code) == ("rejected", "spell.visual_targets")
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert (after.world_version, after.resource_state, after.effect_states) == (
            before.world_version,
            before.resource_state,
            before.effect_states,
        )
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm.read_events(sid, package, context=context) == events
        assert _creature_targets(session, command) == ()
