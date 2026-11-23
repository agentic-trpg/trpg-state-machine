"""Read authored World surfaces and produce location-only tremorsense clues.

Surface configuration is immutable scene-instance input. Active object-anchored
connections follow current object placement, so a reposition changes topology
without creating a second writable map authority.
"""

import json
from functools import lru_cache

from dnd5e_engine.spatial import canonical_cell_id
from dnd5e_engine.tremorsense import (
    Surface,
    SurfaceConnection,
    SurfaceTopology,
    reachable_surfaces,
    tremorsense_location,
)

from .errors import StateMachineError
from .grid import cells_within_range
from .package import canonical_json

_STONECUNNING_EFFECT = "SAbuNo2AO3XXtkdv"
_STONECUNNING_ORIGIN = "feature:stonecunning"


def validate_surface_config(raw, grid_json, actor_ids, object_ids):
    """Normalize a closed authored surface map against one bounded scene grid."""
    if not isinstance(raw, dict) or set(raw) != {
        "surfaces",
        "floor_cells",
        "connections",
        "wall_contacts",
        "airborne_actors",
    }:
        raise ValueError("surfaces requires five explicit fields")
    from dnd5e_engine.spatial import GridTopology
    from dnd5e_engine.specs import GridScene

    grid = GridTopology(GridScene(**json.loads(grid_json)))
    surfaces = []
    for value in raw["surfaces"]:
        if not isinstance(value, dict) or set(value) != {"surface_id", "kind", "material"}:
            raise ValueError("invalid surface")
        surfaces.append(Surface(**value))
    known = {s.surface_id: s for s in surfaces}
    floor = raw["floor_cells"]
    if not isinstance(floor, dict) or any(
        type(cell) is not str
        or canonical_cell_id(cell) != cell
        or grid.distance_ft(cell, cell) is None
        or surface_id not in known
        or known[surface_id].kind != "floor"
        for cell, surface_id in floor.items()
    ):
        raise ValueError("invalid floor surface cell")
    contacts = raw["wall_contacts"]
    if not isinstance(contacts, dict) or any(
        actor_id not in actor_ids
        or not isinstance(value, dict)
        or set(value) != {"cell", "surface_ids"}
        or type(value["cell"]) is not str
        or canonical_cell_id(value["cell"]) != value["cell"]
        or grid.distance_ft(value["cell"], value["cell"]) is None
        or not isinstance(value["surface_ids"], list)
        or any(
            surface_id not in known or known[surface_id].kind != "wall"
            for surface_id in value["surface_ids"]
        )
        or len(value["surface_ids"]) != len(set(value["surface_ids"]))
        for actor_id, value in contacts.items()
    ):
        raise ValueError("invalid explicit wall contact")
    airborne = raw["airborne_actors"]
    if not isinstance(airborne, dict) or any(
        actor_id not in actor_ids
        or type(cell) is not str
        or canonical_cell_id(cell) != cell
        or grid.distance_ft(cell, cell) is None
        for actor_id, cell in airborne.items()
    ):
        raise ValueError("invalid airborne actor")
    edges = []
    for value in raw["connections"]:
        if not isinstance(value, dict) or set(value) != {
            "first",
            "second",
            "object_id",
            "anchor_cell",
        }:
            raise ValueError("invalid surface connection")
        edge = SurfaceConnection(value["first"], value["second"])
        if (value["object_id"] is None) != (value["anchor_cell"] is None):
            raise ValueError("connection anchor requires both object and cell")
        if value["object_id"] is not None and (
            value["object_id"] not in object_ids
            or type(value["anchor_cell"]) is not str
            or canonical_cell_id(value["anchor_cell"]) != value["anchor_cell"]
            or grid.distance_ft(value["anchor_cell"], value["anchor_cell"]) is None
        ):
            raise ValueError("invalid connection object anchor")
        edges.append(edge)
    SurfaceTopology(0, tuple(surfaces), tuple(edges))
    # An empty declaration grants no wall contact and needs no durable stance row.
    normalized = {
        **raw,
        "wall_contacts": {
            actor_id: value for actor_id, value in contacts.items() if value["surface_ids"]
        },
    }
    return canonical_json(normalized)


@lru_cache(maxsize=128)
def _authored(raw):
    """Share immutable parsed authored facts; session placement remains live."""
    facts = json.loads(raw)
    by_surface = {value["surface_id"]: [] for value in facts["surfaces"]}
    for value in facts["connections"]:
        by_surface[value["first"]].append((value["second"], value))
        by_surface[value["second"]].append((value["first"], value))
    facts["_adjacency"] = by_surface
    facts["_materials"] = {value["surface_id"]: value["material"] for value in facts["surfaces"]}
    return facts


def initialize_actor_stance(conn, sid, actor_id, raw):
    """Persist authored NPC contact and air state in the scene creation transaction."""
    if raw is None:
        return
    facts = _authored(raw)
    wall = facts["wall_contacts"].get(actor_id)
    contacts = () if wall is None else wall["surface_ids"]
    airborne = int(actor_id in facts["airborne_actors"])
    cell = conn.execute(
        "SELECT cell FROM actors WHERE session_id=? AND actor_id=?", (sid, actor_id)
    ).fetchone()[0]
    if (wall is not None and wall["cell"] != cell) or (
        airborne and facts["airborne_actors"][actor_id] != cell
    ):
        raise StateMachineError(
            "invalid_surface_stance", "Authored stance differs from actor position"
        )
    if contacts or airborne:
        conn.execute(
            "INSERT INTO actor_surface_stance"
            "(session_id,actor_id,wall_contact_json,airborne) VALUES (?,?,?,?)",
            (sid, actor_id, json.dumps(contacts), airborne),
        )


def _sense_profiles(handler, sid, actor_id, effective_range):
    """Separate a known active Stonecunning grant from generic base tremorsense.

    The exact effect identity is persisted by Effects. Its independent action
    and uses are not granted here. Other sources retain the same pinned RE
    passive-sense interpreter after this exact effect is excluded.
    """
    row = handler.store.connection.execute(
        "SELECT payload_json FROM ongoing_effects WHERE session_id=? AND target_id=? "
        "AND effect_id=? AND origin=?",
        (sid, actor_id, _STONECUNNING_EFFECT, _STONECUNNING_ORIGIN),
    ).fetchone()
    active = False
    if row is not None:
        effect = json.loads(row[0])
        active = not effect["disabled"] and any(
            change["key"] == "system.attributes.senses.tremorsense"
            and str(change["value"]) == "60"
            and change["mode"] in ("upgrade", "override")
            for change in effect["changes"]
        )
    generic = (
        handler.characters.senses_excluding_effect(
            sid, actor_id, (actor_id, _STONECUNNING_EFFECT, _STONECUNNING_ORIGIN)
        ).tremorsense
        if active
        else effective_range
    )
    return tuple(
        (range_ft, stone_only)
        for range_ft, stone_only in ((generic, False), (60 if active else None, True))
        if range_ft
    )


def location_clues(handler, sid, actor_id, position, senses):
    """Select indexed nearby actors, then ask RE for anonymous position clues.

    Initial wall contact is stored with the actor and cleared on movement.
    Runtime stance changes require an explicit World fact and are outside this
    first slice (RUO-55-T-STANCE).
    """
    scene = handler.facts.instances.scene(sid, position.scene_id)
    raw = getattr(scene, "surfaces_json", None)
    if raw is None or not senses.tremorsense or position.cell is None:
        return ()
    profiles = _sense_profiles(handler, sid, actor_id, senses.tremorsense)
    if not profiles:
        return ()
    facts = _authored(raw)
    prepared = handler.grid.reader.maps.template(sid, position.scene_id, scene.grid_json)
    if not prepared.topology.within_range(position.cell, position.cell, 0):
        return ()
    cells = cells_within_range(prepared, position.cell, max(profile[0] for profile in profiles))
    nearby, _ = handler.grid.reader.region_occupants(
        sid, position.scene_id, cells, include_actors=True, include_objects=False
    )
    nearby = tuple(nearby)
    floor = facts["floor_cells"]
    actor_rows = {
        row[0]: row
        for row in handler.store.connection.execute(
            "SELECT actor_id,cell FROM actors WHERE session_id=? AND current_scene_id=? "
            "AND actor_id IN (SELECT value FROM json_each(?))",
            (sid, position.scene_id, json.dumps([row[0] for row in nearby] + [actor_id])),
        )
    }

    def stance(who, cell):
        """Read the actor-owned current contact stance at a fixed position."""
        row = handler.store.connection.execute(
            "SELECT wall_contact_json,airborne FROM actor_surface_stance "
            "WHERE session_id=? AND actor_id=?",
            (sid, who),
        ).fetchone()
        if row is None and (who in facts["wall_contacts"] or who in facts["airborne_actors"]):
            raise StateMachineError("incomplete_session", "Missing authored surface stance")
        airborne = bool(row[1]) if row is not None else False
        contacts = set() if airborne else ({floor[cell]} if cell in floor else set())
        if row is not None:
            wall_ids = json.loads(row[0])
            if not isinstance(wall_ids, list) or any(
                facts["_materials"].get(surface_id) is None for surface_id in wall_ids
            ):
                raise StateMachineError("invalid_surface_stance", "Invalid current wall contact")
            contacts.update(wall_ids)
        return frozenset(contacts), airborne

    observer_contacts, observer_airborne = stance(actor_id, position.cell)
    anchor_active = {}

    def current_neighbors(surface_id):
        """Check only edges incident to a reached surface against current anchors."""
        active = []
        for adjacent, value in facts["_adjacency"][surface_id]:
            oid = value["object_id"]
            if oid is not None:
                edge = (value["first"], value["second"])
                if edge not in anchor_active:
                    row = handler.store.connection.execute(
                        "SELECT o.cell,d.current_hp FROM objects o LEFT JOIN object_durability d "
                        "ON d.session_id=o.session_id AND d.object_id=o.object_id "
                        "WHERE o.session_id=? AND o.scene_id=? AND o.object_id=?",
                        (sid, position.scene_id, oid),
                    ).fetchone()
                    anchor_active[edge] = (
                        row is not None and row[0] == value["anchor_cell"] and row[1] != 0
                    )
                if not anchor_active[edge]:
                    continue
            active.append(adjacent)
        return tuple(active)

    reached_profiles = tuple(
        (
            range_ft,
            reachable_surfaces(
                observer_contacts,
                material=facts["_materials"].get,
                neighbors=current_neighbors,
                stonecunning=stone_only,
            ),
        )
        for range_ft, stone_only in profiles
    )
    clues = set()
    for target, cell in nearby:
        if target == actor_id or cell is None or target not in actor_rows:
            continue
        target_contacts, target_airborne = stance(target, cell)
        for range_ft, reached in reached_profiles:
            clue = tremorsense_location(
                topology=prepared.topology,
                surfaces=None,
                observer_cell=position.cell,
                candidate_cell=cell,
                observer_contacts=observer_contacts,
                candidate_contacts=target_contacts,
                range_ft=range_ft,
                candidate_kind="creature",
                observer_airborne=observer_airborne,
                candidate_airborne=target_airborne,
                reachable_surfaces=reached,
            )
            if clue is not None:
                clues.add(clue)
    return tuple(sorted(clues))
