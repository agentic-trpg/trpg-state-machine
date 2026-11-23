"""Public location clues from authored and live World surface facts."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange, ActiveEffectDuration
from lock_support import NPC, lock_loader
from spatial_support import A, command
from test_world_spatial import OBSTACLE, spatial_world_data
from world_support import setup

from trpg_state_machine import RepositionObject, StateMachine, StateMachineError, StepActor


def surface_data():
    """Author three stone surfaces with one live object-anchored connection."""
    data = spatial_world_data()
    scene = data["scenes"][0]
    pc_cell = data["pc_templates"][0]["initial_cell"]
    npc_cell = next(actor["initial_cell"] for actor in data["actors"] if actor["actor_id"] == NPC)
    bridge = next(obj for obj in scene["objects"] if obj["object_id"] == OBSTACLE)
    scene["surfaces"] = {
        "surfaces": [
            {"surface_id": "a", "kind": "floor", "material": "stone"},
            {"surface_id": "b", "kind": "wall", "material": "stone"},
            {"surface_id": "c", "kind": "floor", "material": "stone"},
        ],
        "floor_cells": {pc_cell: "a", npc_cell: "c"},
        "connections": [
            {"first": "a", "second": "b", "object_id": None, "anchor_cell": None},
            {
                "first": "b",
                "second": "c",
                "object_id": bridge["object_id"],
                "anchor_cell": bridge["initial_cell"],
            },
        ],
        "wall_contacts": {},
        "airborne_actors": {},
    }
    for foundation in ("combat_foundation", "noncombat_foundation"):
        data["rule_bindings"][foundation]["snapshot"]["character_states"][0]["senses"][
            "tremorsense"
        ] = 60
    return data, bridge


def test_public_clue_follows_reposition_and_reopen(tmp_path, context):
    data, bridge = surface_data()
    sm, sid, package, db = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        before = sm.get_spatial_view(sid, actor, package, context=context)
        npc_cell = next(
            actor["initial_cell"] for actor in data["actors"] if actor["actor_id"] == NPC
        )
        assert before.tremor_cells == (npc_cell,)
        assert NPC not in repr(before.tremor_cells)
        operation = RepositionObject(A, bridge["object_id"], bridge["initial_cell"], "1,0")
        gm = replace(context, can_place_world_objects=True)
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(
                command(sm, sid, package, context, operation, "cut.denied"),
                package,
                context=context,
            )
        assert exc.value.code == "unauthorized"
        assert sm.get_spatial_view(sid, actor, package, context=context).tremor_cells == (npc_cell,)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_tremor_receipt BEFORE INSERT ON event_evidence "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        failed = command(sm, sid, package, gm, operation, "cut")
        with pytest.raises(StateMachineError):
            sm.submit_command(failed, package, context=gm)
        assert sm.get_spatial_view(sid, actor, package, context=context).tremor_cells == (npc_cell,)
        sm._conn.execute("DROP TRIGGER fail_tremor_receipt")
        receipt = sm.submit_command(failed, package, context=gm)
        assert receipt.status == "committed", receipt
        after = sm.get_spatial_view(sid, actor, package, context=context)
        assert after.tremor_cells == ()
        assert before.tremor_cells == (npc_cell,)
        sm.save_session(sid, package, context=context)
    with StateMachine(db, dnd_loader=lock_loader()) as reopened:
        reopened.resume_session(sid, package, context=context)
        assert reopened.get_spatial_view(sid, actor, package, context=context).tremor_cells == ()


def test_public_view_requires_authorization(tmp_path, context):
    data, _ = surface_data()
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        with pytest.raises(StateMachineError) as exc:
            sm.get_spatial_view(sid, NPC, package, context=replace(context, controlled_npc_ids=()))
        assert exc.value.code == "unauthorized"
        assert sm.get_spatial_view(sid, actor, package, context=context).tremor_cells


def test_explicit_wall_contact_clears_on_actor_move(tmp_path, context):
    data, _ = surface_data()
    facts = data["scenes"][0]["surfaces"]
    cell = next(actor["initial_cell"] for actor in data["actors"] if actor["actor_id"] == NPC)
    del facts["floor_cells"][cell]
    facts["wall_contacts"] = {NPC: {"cell": cell, "surface_ids": ["b"]}}
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        assert sm.get_spatial_view(sid, actor, package, context=context).tremor_cells == (cell,)
        moved = sm.submit_command(
            command(sm, sid, package, context, StepActor(A, cell, "1,2"), "wall.move", NPC),
            package,
            context=context,
        )
        assert moved.status == "committed", moved
        assert sm.get_spatial_view(sid, actor, package, context=context).tremor_cells == ()


def test_empty_authored_wall_contact_is_omitted_and_reopens(tmp_path, context):
    """An empty wall declaration leaves ordinary floor contact available."""
    data, _ = surface_data()
    cell = next(actor["initial_cell"] for actor in data["actors"] if actor["actor_id"] == NPC)
    data["scenes"][0]["surfaces"]["wall_contacts"] = {NPC: {"cell": cell, "surface_ids": []}}
    sm, sid, package, db = setup(tmp_path, context, data)
    assert NPC not in json.loads(package.scene(A).surfaces_json)["wall_contacts"]
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        assert sm.get_spatial_view(sid, actor, package, context=context).tremor_cells == (cell,)
        assert (
            sm._conn.execute(
                "SELECT 1 FROM actor_surface_stance WHERE session_id=? AND actor_id=?", (sid, NPC)
            ).fetchone()
            is None
        )
        sm.save_session(sid, package, context=context)
    with StateMachine(db, dnd_loader=lock_loader()) as reopened:
        reopened.resume_session(sid, package, context=context)
        assert reopened.get_spatial_view(sid, actor, package, context=context).tremor_cells == (
            cell,
        )


def test_authored_airborne_target_has_no_contact(tmp_path, context):
    data, _ = surface_data()
    cell = next(actor["initial_cell"] for actor in data["actors"] if actor["actor_id"] == NPC)
    facts = data["scenes"][0]["surfaces"]
    facts["airborne_actors"][NPC] = cell
    facts["floor_cells"]["1,2"] = "c"
    sm, sid, package, db = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        assert sm.get_spatial_view(sid, actor, package, context=context).tremor_cells == ()
        moved = sm.submit_command(
            command(sm, sid, package, context, StepActor(A, cell, "1,2"), "air.land", NPC),
            package,
            context=context,
        )
        assert moved.status == "committed", moved
        assert sm.get_spatial_view(sid, actor, package, context=context).tremor_cells == ("1,2",)
        sm.save_session(sid, package, context=context)
    with StateMachine(db, dnd_loader=lock_loader()) as reopened:
        reopened.resume_session(sid, package, context=context)
        assert reopened.get_spatial_view(sid, actor, package, context=context).tremor_cells == (
            "1,2",
        )


@pytest.mark.parametrize("stance", ("wall", "airborne"))
def test_missing_required_stance_row_is_rejected(tmp_path, context, stance):
    """A lost nonempty authored stance cannot silently become floor contact."""
    data, _ = surface_data()
    cell = next(actor["initial_cell"] for actor in data["actors"] if actor["actor_id"] == NPC)
    facts = data["scenes"][0]["surfaces"]
    if stance == "wall":
        facts["wall_contacts"][NPC] = {"cell": cell, "surface_ids": ["b"]}
    else:
        facts["airborne_actors"][NPC] = cell
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        with sm._store.transaction(write=True):
            sm._conn.execute(
                "DELETE FROM actor_surface_stance WHERE session_id=? AND actor_id=?",
                (sid, NPC),
            )
        with pytest.raises(StateMachineError) as exc:
            sm.get_spatial_view(sid, actor, package, context=context)
        assert exc.value.code == "incomplete_session"


def test_active_stonecunning_requires_connected_stone(tmp_path, context):
    """A current trusted effect supplies the profile; range alone does not."""
    data, _ = surface_data()
    data["scenes"][0]["surfaces"]["surfaces"][1]["material"] = "other"
    for foundation in ("combat_foundation", "noncombat_foundation"):
        snapshot = data["rule_bindings"][foundation]["snapshot"]
        snapshot["character_states"][0]["senses"]["tremorsense"] = None

    def observe(case, authored, *, other_generic=False):
        """Install an already active trusted effect through the Effects owner."""
        sm, sid, package, _ = setup(case, context, deepcopy(authored))
        with sm:
            actor = sm.read_session(sid, package, context=context).pc_actor_id
            active = effect_state(
                ActiveEffect(
                    id="SAbuNo2AO3XXtkdv",
                    name="Stonecunning",
                    origin="feature:stonecunning",
                    target_id=actor,
                    duration=ActiveEffectDuration(seconds=600),
                    changes=[
                        ActiveEffectChange(
                            key="system.attributes.senses.tremorsense",
                            mode="upgrade",
                            value=60,
                        )
                    ],
                )
            ).model_dump(mode="json")
            with sm._store.transaction(write=True):
                sm._characters.effects.add_effect(sid, active)
                if other_generic:
                    generic = effect_state(
                        ActiveEffect(
                            id="generic-tremor",
                            name="Generic tremorsense",
                            origin="feature:generic",
                            target_id=actor,
                            changes=[
                                ActiveEffectChange(
                                    key="system.attributes.senses.tremorsense",
                                    mode="upgrade",
                                    value=90,
                                )
                            ],
                        )
                    ).model_dump(mode="json")
                    sm._characters.effects.add_effect(sid, generic)
            return sm.get_spatial_view(sid, actor, package, context=context).tremor_cells

    assert observe(tmp_path, data) == ()
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    assert observe(mixed, data, other_generic=True)
    data["scenes"][0]["surfaces"]["surfaces"][1]["material"] = "stone"
    other = tmp_path / "stone"
    other.mkdir()
    assert observe(other, data)


@pytest.mark.parametrize("unrelated", (0, 30))
def test_public_read_traverses_only_reached_component(tmp_path, context, monkeypatch, unrelated):
    """Fixed observer and target keep RE adjacency work fixed as distant graph grows."""
    import trpg_state_machine.world_surfaces as world_surfaces

    data, _ = surface_data()
    facts = data["scenes"][0]["surfaces"]
    for index in range(unrelated):
        sid = f"remote.{index:02d}"
        facts["surfaces"].append({"surface_id": sid, "kind": "wall", "material": "stone"})
        if index:
            facts["connections"].append(
                {
                    "first": f"remote.{index - 1:02d}",
                    "second": sid,
                    "object_id": None,
                    "anchor_cell": None,
                }
            )
    sm, session_id, package, _ = setup(tmp_path, context, data)
    visits = []
    candidate_counts = []
    original = world_surfaces.reachable_surfaces
    original_region = sm._world.grid.reader.region_occupants

    def counted(contacts, *, material, neighbors, stonecunning=False):
        def track(surface_id):
            visits.append(surface_id)
            return neighbors(surface_id)

        return original(contacts, material=material, neighbors=track, stonecunning=stonecunning)

    monkeypatch.setattr(world_surfaces, "reachable_surfaces", counted)

    def counted_region(*args, **kwargs):
        actors, objects = original_region(*args, **kwargs)
        actors, objects = tuple(actors), tuple(objects)
        candidate_counts.append(len(actors))
        return actors, objects

    monkeypatch.setattr(sm._world.grid.reader, "region_occupants", counted_region)
    with sm:
        actor = sm.read_session(session_id, package, context=context).pc_actor_id
        sm.get_spatial_view(session_id, actor, package, context=context)
    assert sorted(visits) == ["a", "b", "c"]
    assert candidate_counts == [2]
