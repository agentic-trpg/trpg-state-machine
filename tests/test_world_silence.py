"""Silence uses the caster's current position when freezing exploration spell inputs."""

import json

from dnd5e_srd_data import BundledAssetLoader
from spatial_support import submit as submit_world
from test_staff_of_healing import CURE, SLUG, STAFF
from test_timed_spell_cast import PERCEPTION_SPELL
from test_timed_spell_cast import timed_spell_world as timed_spell_world
from test_world_magic_light import sources

from trpg_state_machine import (
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
        where = (
            "WHERE session_id=? AND fixed_scene_id=? AND fixed_cell IS NOT NULL "
            "AND json_extract(environment_json, '$.kind') = 'magical_silence' "
        )
        scene = sm.read_session(sid, package, context=context).current_scene_id
        for projection, order, name, parameters in (
            ("radius_ft", "ORDER BY radius_ft DESC LIMIT 1", "spatial_silence_radius", ()),
            ("*", "AND fixed_cell IN (?)", "spatial_silence_cell", ("0,0",)),
        ):
            plan = sm._conn.execute(
                f"EXPLAIN QUERY PLAN SELECT {projection} FROM spatial_light_effects {where}{order}",
                (sid, scene, *parameters),
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
