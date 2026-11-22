"""Silence uses the caster's current position when freezing exploration spell inputs."""

import asyncio
import json
from unittest.mock import patch

from dnd5e_engine.evaluation_state import ConditionRecord
from dnd5e_srd_data import BundledAssetLoader
from item_support import NPC
from spatial_support import submit as submit_world
from test_scoped_runtime_validation import measure
from test_session_time_coordination import submit as submit_time
from test_staff_of_healing import CURE, SLUG, STAFF
from test_timed_spell_cast import PERCEPTION_SPELL, cast_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world
from test_world_magic_light import sources

from trpg_state_machine import (
    AdvanceSessionTime,
    DropWorldConcentration,
    EquipWeapon,
    StartSpellCast,
    StateMachine,
    StepActor,
    UnequipWeapon,
    world_command_from_data,
)


def cast_json(session, command_id, spell, level, *, target=None):
    """Submit a direct exploration cast through the public command JSON adapter."""
    sm, sid, package, context = session
    state = sm.read_session(sid, package, context=context)
    wire = {
        "schema_version": "sm-command/1",
        "session_id": sid,
        "command_id": command_id,
        "actor_id": state.pc_actor_id,
        "command_type": "world.cast_spell",
        "expected_world_version": state.world_version,
        "payload": {
            "kind": "world.cast_spell",
            "spell_id": spell,
            "spell_source_id": "class:cleric",
            "slot_level": level,
            **({"target": target} if target else {"target_id": state.pc_actor_id}),
        },
    }
    command = world_command_from_data(json.loads(json.dumps(wire)))
    return command, sm.submit_command(command, package, context=context)


def test_silence_public_cast_projection_and_reopen(timed_spell_world, tmp_path):
    # The fixture adds an admissible non-Verbal spell to exercise the same slot path.
    def somatic_only(spell):
        return spell.model_copy(update={"components": spell.components & {"S"}})

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        pc = data["pc_templates"][0]
        owner = pc["pc_template_id"]
        pc["character"]["inventory"] = [*pc["character"]["inventory"], SLUG]
        pc["character"]["hp"]["current"] = 2
        data["scenes"][0]["objects"].append(
            {
                "object_id": STAFF,
                "state": None,
                "movable": False,
                "blocks_passage": False,
                "initial_cell": "0,1",
            }
        )
        data["item_transfers"].append(
            {
                "instance_id": STAFF,
                "item_slug": SLUG,
                "initial_owner_id": owner,
                "quantity": 1,
                "charges_remaining_per_unit": 10,
                "policy": "charge_exhaust_retain",
                "initially_attuned": True,
            }
        )
        for scene in data["scenes"]:
            scene["grid"]["width"] = 14
        for foundation in data["rule_bindings"].values():
            snapshot = foundation["snapshot"]
            character = snapshot["character_states"][0]
            character["carried_item_slugs"] = [*character["carried_item_slugs"], SLUG]
            character["hp_current"] = 2
            snapshot["inventory_state"]["entries"].append(
                {
                    "instance_id": STAFF,
                    "owner_id": owner,
                    "item_slug": SLUG,
                    "quantity": 1,
                    "charges_remaining_per_unit": 10,
                    "accessible": True,
                }
            )
            snapshot["scene_state"]["grid"]["width"] = 14
            for scene in foundation.get("additional_scenes", ()):
                scene["grid"]["width"] = 14

    session = timed_spell_world(
        material=False,
        spell_id=PERCEPTION_SPELL,
        additional_spells=("silence", "cure-wounds", "mass-cure-wounds", "detect-magic"),
        additional_items=(BundledAssetLoader().get_item(SLUG),),
        caster_class="cleric",
        caster_subclass="life",
        configure_spell=somatic_only,
        configure=configure,
        walking=True,
    )
    sm, sid, package, context = session
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        _, silence = cast_json(
            session, "silence", "silence", 2, target={"kind": "point", "cell_id": "0,0"}
        )
        assert silence.status == "committed", silence
        assert sources(session)[0].spec.kind == "magical_silence"
        assert sources(session)[0].radius_ft == 20
        scene = sm.read_session(sid, package, context=context).current_scene_id
        owner = sm._world.magic.owner
        with measure(sm) as (_, statements):
            owner.maximum_fixed_radius(sid, scene, silence=True)
            owner.fixed_at(sid, scene, ("0,0",), silence=True)
        for match, name, parameters in (
            ("ORDER BY radius_ft DESC", "spatial_silence_radius", ()),
            ("fixed_cell IN (", "spatial_silence_cell", ("0,0",)),
        ):
            (query,) = (stmt for stmt in statements if match in stmt)
            plan = sm._conn.execute(
                f"EXPLAIN QUERY PLAN {query}", (sid, scene, *parameters)
            ).fetchall()
            assert any(name in row[3] for row in plan)
        during = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        command, denied = cast_json(
            session, "inside-silence", "silence", 2, target={"kind": "point", "cell_id": "10,0"}
        )
        assert (denied.status, denied.error_code) == ("rejected", "spell.source")
        assert not denied.event_seqs
        assert sm.read_noncombat_snapshot(sid, package, context=context) == during
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert during.casting_state == before.casting_state
        ritual = StartSpellCast("ritual", "detect-magic", "class:cleric", as_ritual=True)
        refused = submit_world(sm, sid, package, context, ritual, "inside-verbal-ritual")
        assert (refused.status, refused.error_code) == ("rejected", "spell.source")
        equipped = submit_world(sm, sid, package, context, EquipWeapon(STAFF), "equip-staff")
        assert equipped.status == "committed", equipped
        state = sm.read_session(sid, package, context=context)
        staff_before = sm.read_noncombat_snapshot(sid, package, context=context)
        wire = {
            "schema_version": "sm-command/1",
            "session_id": sid,
            "command_id": "inside-componentless-item",
            "actor_id": state.pc_actor_id,
            "command_type": "world.use_item",
            "expected_world_version": state.world_version,
            "payload": {
                "kind": "world.use_item",
                "instance_id": STAFF,
                "activity_id": CURE,
                "target_id": state.pc_actor_id,
                "cast_level": 1,
            },
        }
        staff = world_command_from_data(json.loads(json.dumps(wire)))
        healed = sm.submit_command(staff, package, context=context)
        assert healed.status == "committed", healed
        staff_after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert staff_after.resource_state == staff_before.resource_state
        assert (
            next(
                entry.charges_remaining_per_unit
                for entry in staff_after.inventory_state.entries
                if entry.instance_id == STAFF
            )
            == 9
        )
        assert sm.read_session(sid, package, context=context).character.current_hp > 2
        stowed = submit_world(sm, sid, package, context, UnequipWeapon(STAFF), "stow-staff")
        assert stowed.status == "committed", stowed
        loader = sm._rules.loader

    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as reopened:
        after = reopened, sid, package, context
        assert reopened.submit_command(command, package, context=context) == denied
        _, still_denied = cast_json(
            after, "reopened-silence", "silence", 2, target={"kind": "point", "cell_id": "10,0"}
        )
        assert (still_denied.status, still_denied.error_code) == ("rejected", "spell.source")
        scene = reopened.read_session(sid, package, context=context).current_scene_id
        for column in range(5):
            moved = submit_world(
                reopened,
                sid,
                package,
                context,
                StepActor(scene, f"{column},0", f"{column + 1},0"),
                f"outside-{column}",
            )
            assert moved.status == "committed", moved
        assert sources(after)[0].spec.kind == "magical_silence"
        _, outside = cast_json(
            after, "outside-silence", "silence", 2, target={"kind": "point", "cell_id": "10,0"}
        )
        assert outside.status == "committed", outside
        assert [source.effect_id for source in sources(after)] == ["spatial:outside-silence"]
        moved = submit_world(
            reopened, sid, package, context, StepActor(scene, "5,0", "6,0"), "enter-second-silence"
        )
        assert moved.status == "committed", moved
        _, denied_again = cast_json(
            after, "inside-second-silence", "silence", 2, target={"kind": "point", "cell_id": "0,0"}
        )
        assert (denied_again.status, denied_again.error_code) == ("rejected", "spell.source")
        _, no_verbal = cast_json(after, "inside-somatic", PERCEPTION_SPELL, 1)
        assert no_verbal.status == "committed", no_verbal
        ended = submit_world(
            reopened, sid, package, context, DropWorldConcentration(), "end-silence"
        )
        assert ended.status == "committed", ended
        assert not sources(after)
        started = submit_world(reopened, sid, package, context, ritual, "ended-verbal-ritual")
        assert started.status == "committed", started


def test_silence_also_blocks_the_condition_choice_preview(timed_spell_world):
    """Preview and normal casting share the same effective verbal availability."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        snapshot = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
        snapshot["character_states"][0]["conditions"] = [
            ConditionRecord(
                condition="poisoned",
                source_entity_id=f"implied:{poison}",
                scope="session",
                duration_rounds=None,
                save_dc=None,
                applied_round=0,
                exhaustion_level=1,
                source_effect_id=None,
            ).model_dump(mode="json")
            for poison in ("toxin", "venom")
        ]

    session = timed_spell_world(
        additional_spells=("lesser-restoration", "silence"),
        caster_class="cleric",
        caster_subclass="life",
        configure=configure,
    )
    sm, sid, package, context = session

    def options():
        command = cast_command(
            session, spell_id="lesser-restoration", slot_level=2, source_id="class:cleric"
        )
        return asyncio.run(
            sm.query_spell_condition_choices(command, package, context=context)
        ).options

    with sm:
        ordinary = options()
        assert len(ordinary) == 2
        _, placed = cast_json(
            session, "affect-choice", "silence", 2, target={"kind": "point", "cell_id": "0,0"}
        )
        assert placed.status == "committed", placed
        assert not options()
        ended = submit_world(
            sm, sid, package, context, DropWorldConcentration(), "end-choice-silence"
        )
        assert ended.status == "committed", ended
        assert options() == ordinary


def test_stationary_caster_is_blocked_by_another_creatures_silence(timed_spell_world):
    """An established caster's position controls admission independently of effect ownership."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")

    session = timed_spell_world(
        material=False,
        additional_spells=("silence",),
        caster_class="cleric",
        caster_subclass="life",
        configure=configure,
    )
    sm, sid, package, context = session
    with sm:
        magic = sm._world.magic
        with patch.object(magic, "apply", wraps=magic.apply) as adopt:
            command, cast = cast_json(
                session,
                "canonical-silence",
                "silence",
                2,
                target={"kind": "point", "cell_id": "0,0"},
            )
            assert cast.status == "committed", cast
        adopt.assert_called_once()
        _, _, _, proposal = adopt.call_args.args
        ended = submit_world(sm, sid, package, context, DropWorldConcentration(), "end-template")
        assert ended.status == "committed", ended

        # The public exploration entry controls the PC. Seed another living owner's
        # application using the verified canonical proposal and the real Effects owner.
        actor = command.actor_id
        with sm._transaction(write=True):
            row = sm._session(sid, package, context)
            position = tuple(sm._world.facts.carrier_position(sid, actor))
            assert position == (row["current_scene_id"], "0,0")
            assert sm._characters.life(sid, NPC)["is_alive"]
            effect_id, _ = magic.apply(
                row, package, "npc-owned-silence", proposal.model_copy(update={"source_id": NPC})
            )
        (area,) = sources(session)
        assert area.concentration_identity == (NPC, effect_id, effect_id)
        assert position[1] in area.cells
        assert not sm._characters.effects.has_world_concentration(sid, actor)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        _, denied = cast_json(
            session, "stationary-silenced", "silence", 2, target={"kind": "point", "cell_id": "1,0"}
        )
        assert (denied.status, denied.error_code) == ("rejected", "spell.source")
        assert not denied.event_seqs
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert tuple(sm._world.facts.carrier_position(sid, actor)) == position
        assert sm._characters.effects.has_world_concentration(sid, NPC)

        elapsed = submit_time(session, AdvanceSessionTime(600), "npc-silence-expires")
        assert elapsed.status == "committed", elapsed
        assert not sources(session)
        assert not sm._characters.effects.has_world_concentration(sid, NPC)
        assert tuple(sm._world.facts.carrier_position(sid, actor)) == position
        _, allowed = cast_json(
            session, "stationary-restored", "silence", 2, target={"kind": "point", "cell_id": "1,0"}
        )
        assert allowed.status == "committed", allowed
