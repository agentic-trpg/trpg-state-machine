"""Distinct-source Invisibility and Faerie Fire through public cast commands."""

import json

from dnd5e_engine.evaluation_world_effect import EffectExpiryPolicy, WorldEffectApplication
from dnd5e_srd_data import BundledAssetLoader
from item_support import NPC
from spatial_support import submit as submit_world
from test_timed_spell_cast import cast_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import DropWorldConcentration, world_command_from_data
from trpg_state_machine.visual_target_state import target_is_invisible


def test_distinct_caster_invisibility_faerie_overlap(timed_spell_world):
    """Faerie suppresses a failed saver until its own source ends."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        data["actors"][0]["initial_cell"] = "2,0"

    session = timed_spell_world(
        database="faerie-overlap.sqlite",
        caster_class="bard",
        caster_subclass="lore",
        additional_spells=("invisibility", "faerie-fire"),
        additional_conditions=(BundledAssetLoader().get_condition("invisible"),),
        configure=configure,
        seed=2,
    )
    sm, sid, package, context = session
    with sm:
        caster = sm.read_session(sid, package, context=context).pc_actor_id
        command = cast_command(
            session,
            spell_id="invisibility",
            source_id="class:bard",
            target_id=caster,
            slot_level=2,
            command_id="first-invisible",
        )
        first = sm.submit_command(command, package, context=context)
        assert first.status == "committed", first
        (effect,) = sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        assert effect.flags == {"concentration": True}
        # Fixture seeding uses the existing verified Effects owner so the
        # later Faerie caster can have an independent concentration source.
        later = effect.model_copy(update={"target_id": NPC, "origin": f"cast:invisibility:{NPC}"})
        with sm._store.transaction(write=True):
            row = sm._world.sessions._session(sid, package, context)
            sm._session_runtime.time.consumers["effects"].apply_verified(
                sid,
                WorldEffectApplication(
                    session_id=sid,
                    command_id="npc-invisible",
                    source_id=NPC,
                    effect=later,
                    expiry=EffectExpiryPolicy(action="remove"),
                    concentration=True,
                    ruleset_binding=sm._rules.session_binding(row),
                ),
                spell_id="invisibility",
            )
        dropped = submit_world(sm, sid, package, context, DropWorldConcentration(), "drop-own")
        assert dropped.status == "committed", dropped
        assert any(
            state.origin == later.origin
            for state in sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        )
        assert target_is_invisible(sm._world, sid, NPC)
        before = sm.read_noncombat_snapshot(sid, package, context=context).resource_state
        rng = sm.read_rng_context(sid, package, context=context)
        state = sm.read_session(sid, package, context=context)
        wire = {
            "schema_version": "sm-command/1",
            "session_id": sid,
            "command_id": "public-faerie",
            "actor_id": caster,
            "command_type": "world.cast_spell",
            "expected_world_version": state.world_version,
            "payload": {
                "kind": "world.cast_spell",
                "spell_id": "faerie-fire",
                "spell_source_id": "class:bard",
                "slot_level": 1,
                "target": {"kind": "point", "cell_id": "2,0"},
            },
        }
        faerie = world_command_from_data(json.loads(json.dumps(wire)))
        result = sm.submit_command(faerie, package, context=context)
        assert result.status == "committed", result
        with sm._store.transaction():
            selected = sm._store.connection.execute(
                "SELECT 1 FROM target_visual_effects WHERE session_id=? "
                "AND target_kind='creature' AND target_id=?",
                (sid, NPC),
            ).fetchone()
            assert selected is not None
        assert not target_is_invisible(sm._world, sid, NPC)
        assert sm.read_noncombat_snapshot(sid, package, context=context).resource_state != before
        assert sm.read_rng_context(sid, package, context=context) != rng
        assert any(
            state.origin == later.origin
            for state in sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        )
        ended = submit_world(sm, sid, package, context, DropWorldConcentration(), "end-faerie")
        assert ended.status == "committed", ended
        assert target_is_invisible(sm._world, sid, NPC)
        assert any(
            state.origin == later.origin
            for state in sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        )


def test_faerie_cast_ends_casters_own_invisibility(timed_spell_world):
    """The /47 cast transport preserves the existing Invisibility cast trigger."""
    session = timed_spell_world(
        database="faerie-own-invisible.sqlite",
        caster_class="bard",
        caster_subclass="lore",
        additional_spells=("invisibility", "faerie-fire"),
        additional_conditions=(BundledAssetLoader().get_condition("invisible"),),
        seed=2,
        configure=lambda data: data["rule_bindings"].pop("combat_foundation"),
    )
    sm, sid, package, context = session
    with sm:
        caster = sm.read_session(sid, package, context=context).pc_actor_id
        first = sm.submit_command(
            cast_command(
                session,
                spell_id="invisibility",
                source_id="class:bard",
                target_id=caster,
                slot_level=2,
                command_id="own-invisible",
            ),
            package,
            context=context,
        )
        assert first.status == "committed", first
        assert target_is_invisible(sm._world, sid, caster)
        state = sm.read_session(sid, package, context=context)
        wire = {
            "schema_version": "sm-command/1",
            "session_id": sid,
            "command_id": "faerie-ends-own",
            "actor_id": caster,
            "command_type": "world.cast_spell",
            "expected_world_version": state.world_version,
            "payload": {
                "kind": "world.cast_spell",
                "spell_id": "faerie-fire",
                "spell_source_id": "class:bard",
                "slot_level": 1,
                "target": {"kind": "point", "cell_id": "0,0"},
            },
        }
        result = sm.submit_command(
            world_command_from_data(json.loads(json.dumps(wire))), package, context=context
        )
        assert result.status == "committed", result
        assert not any(
            effect.id == "effect:invisibility"
            for effect in sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        )
