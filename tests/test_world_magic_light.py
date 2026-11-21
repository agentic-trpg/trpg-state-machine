"""Public spatial spell casting and its durable Effects lifecycle."""

import pytest
from dnd5e_engine.evaluation_contracts import ExplorationEnvironmentalSpellCastPayload
from item_support import DAGGER, NPC, SECOND, A
from spatial_support import submit as submit_world
from test_mechanism_effects import install, rule_data, state_rows
from test_session_time_coordination import submit as submit_time
from test_timed_spell_cast import timed_spell_world as timed_spell_world
from test_world_mechanisms import LEVER

from trpg_state_machine import (
    ActivateMechanism,
    AdvanceSessionTime,
    DropWorldConcentration,
    ObjectState,
    PickupItem,
    PutItem,
    SetObjectState,
    StateMachine,
    StepActor,
    world_command_from_data,
)

ITEM = "object.pouch"
CASE = "object.dark_case"


@pytest.fixture
def magic_world(timed_spell_world, request):
    def configure(data):
        data["manifest"]["schema_version"] = "state-machine-foundation/6"
        data["lockable_doors"] = []
        data["world_objects"] = [
            {
                "object_id": ITEM,
                "definition_ref": "definition.component_pouch",
                "display_name": "Component pouch",
                "description_ref": None,
                "material": None,
                "size_category": "tiny",
                "weight_lb": 2,
                "capabilities": [],
            }
        ]
        if getattr(request, "param", False):
            data["world_objects"][0].update(
                volume_cu_ft=1, fit_profile="station.small", content_kind="station.unit"
            )
            data["world_objects"].append(
                {
                    **data["world_objects"][0],
                    "object_id": CASE,
                    "definition_ref": "definition.opaque_case",
                    "display_name": "Opaque case",
                    "capabilities": [
                        {"kind": "openable"},
                        {
                            "kind": "container",
                            "capacity_weight_lb": 10,
                            "capacity_volume_cu_ft": 10,
                            "opening_profiles": ["station.small"],
                            "allowed_content_kinds": ["station.unit"],
                            "nesting_limit": 2,
                            "light_transmission_open": "transmit",
                            "light_transmission_closed": "block",
                        },
                    ],
                }
            )
            data["world_objects"].append(
                {
                    **data["world_objects"][0],
                    "object_id": DAGGER,
                    "definition_ref": "definition.demo_dagger",
                    "display_name": "Dagger",
                }
            )
            dagger = next(obj for obj in data["scenes"][0]["objects"] if obj["object_id"] == DAGGER)
            dagger["initial_cell"] = "0,1"
            data["scenes"][0]["objects"].append(
                {
                    "object_id": CASE,
                    "state": "open",
                    "movable": False,
                    "blocks_passage": False,
                    "initial_cell": "0,1",
                }
            )
        for scene in data["scenes"]:
            scene["grid"]["width"] = 14
        for foundation in data["rule_bindings"].values():
            foundation["snapshot"]["scene_state"]["grid"]["width"] = 14
            for scene in foundation.get("additional_scenes", ()):
                scene["grid"]["width"] = 14

    return timed_spell_world(additional_spells=("light", "darkness"), configure=configure)


def cast(session, command_id, spell, slot, target):
    sm, sid, package, context = session
    state = sm.read_session(sid, package, context=context)
    payload = {
        "kind": "world.cast_spell",
        "spell_id": spell,
        "spell_source_id": "class:wizard",
        "slot_level": slot,
        "target": target,
    }
    command = world_command_from_data(
        {
            "schema_version": "sm-command/1",
            "session_id": sid,
            "command_id": command_id,
            "actor_id": state.pc_actor_id,
            "command_type": "world.cast_spell",
            "expected_world_version": state.world_version,
            "payload": payload,
        }
    )
    assert type(command.payload) is ExplorationEnvironmentalSpellCastPayload
    return command, sm.submit_command(command, package, context=context)


def sources(session):
    sm, sid, package, _ = session
    with sm._store.transaction():
        return sm._world.magic.active_magic_sources(sid, A, package)


def test_light_is_slotless_follows_the_actual_carrier_replays_and_expires(magic_world):
    sm, sid, package, context = magic_world
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        command, receipt = cast(
            magic_world, "light-1", "light", 0, {"kind": "item", "item_id": ITEM}
        )
        assert receipt.status == "committed", receipt
        (old,) = sources(magic_world)
        assert (old.effect_id, old.source_id, old.item_id, old.cell) == (
            "spatial:light-1",
            command.actor_id,
            ITEM,
            "0,0",
        )
        assert old.spec.kind == "light" and old.cells and old.dim_cells
        assert sm.submit_command(command, package, context=context) == receipt
        assert (
            sm.read_noncombat_snapshot(sid, package, context=context).resource_state
            == before.resource_state
        )
        assert sm.read_rng_context(sid, package, context=context) == rng
        moved = submit_world(sm, sid, package, context, StepActor(A, "0,0", "0,1"), "move-pouch")
        assert moved.status == "committed", moved
        assert sm._world_facts.instances.session_value(sid, "evidence_schema") == "event-evidence/6"
        (current,) = sources(magic_world)
        assert (old.cell, current.cell, current.effect_id) == ("0,0", "0,1", old.effect_id)
        assert (
            submit_time(magic_world, AdvanceSessionTime(3600), "light-hour").status == "committed"
        )
        assert sources(magic_world) == ()
        assert old.cell == "0,0"


def test_darkness_dispels_a_paid_overlap_and_releases_its_own_concentration(magic_world):
    sm, sid, package, context = magic_world
    with sm:
        _, light = cast(magic_world, "light", "light", 0, {"kind": "item", "item_id": ITEM})
        assert light.status == "committed", light
        command, dark = cast(
            magic_world, "dark", "darkness", 2, {"kind": "point", "cell_id": "2,0"}
        )
        assert dark.status == "committed", dark
        (source,) = sources(magic_world)
        assert source.spec.kind == "magical_darkness"
        assert (source.effect_id, source.item_id, source.cell, source.spell_level) == (
            "spatial:dark",
            None,
            "2,0",
            2,
        )
        assert source.concentration_identity is not None
        ended = [
            event.payload
            for event in sm.read_events(sid, package, context=context)
            if event.event_type == "effect.spatial_light_ended"
        ]
        assert [(event["effect_id"], event["cause_effect_id"]) for event in ended] == [
            ("spatial:light", "spatial:dark")
        ]
        assert (
            sm._conn.execute(
                "SELECT current FROM character_resources WHERE session_id=? AND actor_id=? "
                "AND resource_id='spell:2'",
                (sid, command.actor_id),
            ).fetchone()[0]
            == 2
        )
        dropped = submit_world(sm, sid, package, context, DropWorldConcentration(), "drop")
        assert dropped.status == "committed", dropped
        assert sources(magic_world) == ()
        assert sm._conn.execute("SELECT count(*) FROM effect_concentrations").fetchone()[0] == 0


def test_owned_item_target_and_failed_adoption_preserve_state(magic_world, monkeypatch):
    sm, sid, package, context = magic_world
    with sm:
        _, illegal = cast(
            magic_world, "owned-dark", "darkness", 2, {"kind": "item", "item_id": ITEM}
        )
        assert (illegal.status, illegal.error_code) == ("rejected", "target_invalid")
        baseline = sm.read_noncombat_snapshot(sid, package, context=context)

        def abort(*_):
            raise RuntimeError("failure during spatial rule adoption")

        monkeypatch.setattr(sm._world.magic, "reconcile_scene", abort)
        with pytest.raises(RuntimeError, match="spatial rule adoption"):
            cast(magic_world, "aborted", "darkness", 2, {"kind": "point", "cell_id": "2,0"})
        assert sources(magic_world) == ()
        assert sm.read_noncombat_snapshot(sid, package, context=context) == baseline
        assert sm._conn.execute("SELECT count(*) FROM effect_concentrations").fetchone()[0] == 0


def test_movement_into_dim_radius_reconciles_without_recasting(magic_world):
    sm, sid, package, context = magic_world
    with sm:
        _, light = cast(magic_world, "moving-light", "light", 0, {"kind": "item", "item_id": ITEM})
        assert light.status == "committed", light
        _, dark = cast(
            magic_world, "fixed-dark", "darkness", 2, {"kind": "point", "cell_id": "12,0"}
        )
        assert dark.status == "committed", dark
        assert {source.effect_id for source in sources(magic_world)} == {
            "spatial:moving-light",
            "spatial:fixed-dark",
        }
        first = submit_world(sm, sid, package, context, StepActor(A, "0,0", "0,1"), "north")
        assert first.status == "committed", first
        moved = submit_world(sm, sid, package, context, StepActor(A, "0,1", "1,1"), "overlap")
        assert moved.status == "committed", moved
        assert [source.effect_id for source in sources(magic_world)] == ["spatial:fixed-dark"]
        (dispel,) = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_type == "effect.spatial_light_ended"
        ]
        assert dispel.source_command_id == "overlap" and dispel.payload["reason"] == "dispelled"


@pytest.mark.parametrize("magic_world", [True], indirect=True)
def test_opaque_container_suppresses_effect_and_cannot_be_targeted_by_hidden_id(
    magic_world, tmp_path
):
    sm, sid, package, context = magic_world
    with sm:
        picked = submit_world(
            sm, sid, package, context, PickupItem(DAGGER, A, "0,1"), "take-dagger"
        )
        assert picked.status == "committed", picked
        _, light = cast(magic_world, "open-light", "light", 0, {"kind": "item", "item_id": DAGGER})
        assert light.status == "committed", light
        put = submit_world(sm, sid, package, context, PutItem(A, DAGGER, CASE, "0,0"), "stow")
        assert put.status == "committed", put
        (source,) = sources(magic_world)
        assert source.cell == "0,1" and source.effect_id == "spatial:open-light"
        closed = submit_world(
            sm, sid, package, context, SetObjectState(A, CASE, ObjectState.CLOSED), "cover"
        )
        assert closed.status == "committed", closed
        assert sources(magic_world) == ()
        _, denied = cast(
            magic_world, "hidden-light", "light", 0, {"kind": "item", "item_id": DAGGER}
        )
        assert (denied.status, denied.error_code) == ("rejected", "spell.target")
        opened = submit_world(
            sm, sid, package, context, SetObjectState(A, CASE, ObjectState.OPEN), "uncover"
        )
        assert opened.status == "committed", opened
        assert sources(magic_world) == (source,)
        loader = sm._rules.loader

    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as recovered:
        assert sources((recovered, sid, package, context)) == (source,)
        # Inspect the null-safe cold-load lookup SessionMemory actually uses.
        for table, column, value, index in (
            ("magic_light_anchors", "scene_id", A, "magic_light_anchors_on_scene"),
            ("spatial_light_effects", "fixed_scene_id", A, "spatial_light_effects_on_scene"),
            ("spatial_light_effects", "object_id", DAGGER, "spatial_light_effects_on_object"),
        ):
            plan = recovered._conn.execute(
                f'EXPLAIN QUERY PLAN SELECT * FROM "{table}" '
                f'WHERE "{column}" IS ? AND "session_id" IS ?',
                (value, sid),
            ).fetchall()
            assert any(index in row[3] and f"{column}=?" in row[3] for row in plan)


def test_mechanism_damage_refuses_spatial_concentration_and_preserves_candidate(timed_spell_world):
    def configure(data):
        trap = rule_data(max_uses=1)
        data["manifest"]["schema_version"] = "state-machine-foundation/10"
        data["lockable_doors"] = []
        data["world_objects"] = [
            entry for entry in trap["world_objects"] if entry["object_id"] == LEVER
        ]
        data["scenes"][0]["objects"].extend(
            entry for entry in trap["scenes"][0]["objects"] if entry["object_id"] == LEVER
        )

    session = timed_spell_world(additional_spells=("darkness",), configure=configure, walking=True)
    sm, sid, package, context = session
    with sm:
        _, dark = cast(session, "dark", "darkness", 2, {"kind": "point", "cell_id": "8,0"})
        assert dark.status == "committed", dark
        port = install(sm)
        before = state_rows(sm)
        (concentration,) = sources(session)
        op = ActivateMechanism(A, LEVER, "0,0", "up")
        denied = submit_world(sm, sid, package, context, op, "requires-concentration-save")
        assert (denied.status, denied.error_code) == (
            "rejected",
            "unsupported_mechanism_effect_lifecycle",
        )
        assert len(port.requests) == 1 and state_rows(sm) == before
        assert sources(session) == (concentration,)
        assert (
            submit_world(sm, sid, package, context, DropWorldConcentration(), "drop").status
            == "committed"
        )
        assert sources(session) == ()


def test_item_cast_preserves_disclosure_for_unseen_and_foreign_targets(timed_spell_world):
    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        next(item for item in data["item_transfers"] if item["instance_id"] == SECOND)[
            "initial_owner_id"
        ] = NPC
        data["scenes"][0]["grid"]["default_lighting"] = "dark"
        for foundation in data["rule_bindings"].values():
            snapshot = foundation["snapshot"]
            snapshot["scene_state"]["grid"]["default_lighting"] = "dark"
            next(actor for actor in snapshot["character_states"] if actor["entity_id"] == NPC)[
                "carried_item_slugs"
            ] = ["dagger"]
            snapshot["inventory_state"]["entries"].append(
                {
                    "instance_id": SECOND,
                    "owner_id": NPC,
                    "item_slug": "dagger",
                    "quantity": 1,
                    "charges_remaining_per_unit": 0,
                    "accessible": True,
                }
            )

    session = timed_spell_world(additional_spells=("darkness",), configure=configure)
    sm, sid, package, context = session
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        assert sm._world_facts.placement(sid, SECOND)["carrier_id"] == NPC
        assert sm._world_facts.placement(sid, DAGGER)["scene_id"] == A
        for target in (SECOND, DAGGER, "object.unrecognized"):
            _, denied = cast(
                session, f"cast.{target}", "darkness", 2, {"kind": "item", "item_id": target}
            )
            assert (denied.status, denied.error_code) == ("rejected", "spell.target")
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sources(session) == ()
