"""Public See Invisibility casting and current target disclosure."""

import json

from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_timed_spells import _passive_profile
from dnd5e_srd_data.loader import BundledAssetLoader
from item_support import NPC
from lock_support import CRED, A
from test_session_time_coordination import submit
from test_timed_spell_cast import cast_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world
from test_world_lights import lighting_data
from world_support import setup
from world_support import submit as submit_world

from trpg_state_machine import (
    AdvanceSessionTime,
    SetLightEnabled,
    StateMachine,
    world_command_from_data,
)
from trpg_state_machine.perception import GameViewContext, NPCContext, visible_actor_in_range
from trpg_state_machine.visual_observer_state import can_see_invisible


def test_public_see_invisibility_issues_current_grant(timed_spell_world):
    session = timed_spell_world(additional_spells=("see-invisibility", "invisibility"))
    sm, sid, package, context = session
    with sm:
        typed = cast_command(session, spell_id="see-invisibility", slot_level=2)
        command = world_command_from_data(
            json.loads(
                json.dumps(
                    {
                        "session_id": typed.session_id,
                        "command_id": typed.command_id,
                        "actor_id": typed.actor_id,
                        "command_type": typed.command_type,
                        "expected_world_version": typed.expected_world_version,
                        "schema_version": typed.schema_version,
                        "payload": typed.payload.model_dump(mode="json"),
                    }
                )
            )
        )
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        effects = sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        assert any(
            effect.id == "effect:see_invisibility"
            and any(
                change.key == "vision.see_invisible" and change.value is True
                for change in effect.changes
            )
            for effect in effects
        )

        pc = sm.read_session(sid, package, context=context).pc_actor_id
        hidden = cast_command(session, spell_id="invisibility", slot_level=2)
        assert sm.submit_command(hidden, package, context=context).status == "committed"
        positions = {actor: sm._world._actor_position(sid, actor).cell for actor in (pc, NPC)}
        with sm._store.transaction():
            assert not visible_actor_in_range(
                sm._world, sid, NPC, pc, positions[pc], package, range_ft=60
            )
            assert visible_actor_in_range(
                sm._world, sid, pc, NPC, positions[NPC], package, range_ft=60
            )


def test_see_invisibility_reopens_and_expires(timed_spell_world, tmp_path):
    session = timed_spell_world(additional_spells=("see-invisibility",))
    sm, sid, package, context = session
    with sm:
        command = cast_command(session, spell_id="see-invisibility", slot_level=2)
        assert sm.submit_command(command, package, context=context).status == "committed"
        assert can_see_invisible(sm._characters, sid, command.actor_id)
    with StateMachine(tmp_path / "timed-spell.sqlite") as reopened:
        assert can_see_invisible(reopened._characters, sid, command.actor_id)
        reopened_session = (reopened, sid, package, context)
        assert (
            submit(reopened_session, AdvanceSessionTime(3599), "before-expiry").status
            == "committed"
        )
        assert can_see_invisible(reopened._characters, sid, command.actor_id)
        assert submit(reopened_session, AdvanceSessionTime(3600), "at-expiry").status == "committed"
        assert not can_see_invisible(reopened._characters, sid, command.actor_id)


def test_two_authorized_scene_observers_use_current_visual_facts(tmp_path, context):
    """A current typed grant lets the PC see, while Invisible hides it from an NPC."""
    data = lighting_data()
    data["actors"][0]["initial_cell"] = "2,0"
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        sm.grant_npc_access(sid, NPC, "agent:visual-observer", package, context=context)
        npc_viewer = NPCContext("agent:visual-observer", sid, NPC)
        player = GameViewContext(context.principal_id, sid, context.player_id, "player")
        on = submit_world(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "on")
        assert on.status == "committed"
        assert pc in sm.get_scene_view(sid, package, context=npc_viewer).visible_actor_ids
        with sm._transaction(write=True):
            sm._characters.add_condition(
                sid,
                pc,
                {"condition": "invisible", "source_entity_id": "implied:test", "scope": "session"},
            )
            sm._characters.add_condition(
                sid,
                NPC,
                {"condition": "invisible", "source_entity_id": "implied:test", "scope": "session"},
            )
            spell = BundledAssetLoader().get_spell("see-invisibility")
            _, active, _ = _passive_profile(spell, target_id=pc, caster_id=pc)
            sm._characters.effects.add_effect(sid, effect_state(active).model_dump(mode="json"))
        assert pc not in sm.get_scene_view(sid, package, context=npc_viewer).visible_actor_ids
        assert NPC in sm.get_scene_view(sid, package, context=player).visible_actor_ids


def test_declared_nonvisual_observer_can_use_mechanical_blindsight(tmp_path, context):
    data = lighting_data()
    next(actor for actor in data["actors"] if actor["actor_id"] == NPC)["senses"]["visual"] = False
    npc = next(
        actor
        for actor in data["rule_bindings"]["noncombat_foundation"]["snapshot"]["character_states"]
        if actor["entity_id"] == NPC
    )
    npc["senses"]["blindsight"] = 30
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        sm.grant_npc_access(sid, NPC, "agent:blind-observer", package, context=context)
        observer = NPCContext("agent:blind-observer", sid, NPC)
        assert pc in sm.get_scene_view(sid, package, context=observer).visible_actor_ids
