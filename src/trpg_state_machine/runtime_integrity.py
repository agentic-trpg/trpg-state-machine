"""Transactional derived spatial index and structural scene admission certificates.

Facts remain authoritative. Migration/initialization certify required rows once;
deletion/configuration changes invalidate certificates in the same transaction.
Ordinary re-entry reads the certificate, never performs a complete scene audit.
"""

import json

from .instance_data import decode
from .object_protection import transform_local_cells


def spatial_cells(config, anchor, orientation, state, hp):
    if anchor is None:
        return "[]"
    data = json.loads(config) if config else {}
    definition = json.loads(data.get("definition", "null"))
    caps = definition["fields"]["capabilities"]["tuple"] if definition else ()
    spatial = next((c["fields"] for c in caps if c["type"] == "SpatialObject"), None)
    durability = next((c["fields"] for c in caps if c["type"] == "Durability"), None)
    if spatial is None:
        return json.dumps([[anchor, bool(data.get("blocks_passage"))]])
    footprint = decode(spatial["footprint"])
    cells = transform_local_cells(anchor, orientation, footprint)
    kind = (
        "destroyed"
        if hp == 0
        and durability
        and durability["destruction_policy_ref"] == "disable_spatial_policy"
        else "open"
        if state == "open"
        else "closed"
    )
    return json.dumps([[cell, spatial[kind + "_blocks_movement"]] for cell in cells])


def certify_scene(conn, sid, scene):
    """Only initialization, migration or explicit diagnostic/upgrade callers."""
    required = {("config:scene", scene)}
    complete = True
    row = conn.execute(
        "SELECT config_json FROM instance_configs WHERE session_id=? AND "
        "domain='scene' AND instance_id=?",
        (sid, scene),
    ).fetchone()
    if row is None:
        complete = False
    else:
        data = decode(json.loads(row[0]))
        if data["has_grid"]:
            required.add(("mechanical_scenes", scene))
        for actor in data["actor_initial_ids"]:
            required.update((("actors", actor), ("config:actor", actor)))
        for oid in data["object_ids"]:
            required.update((("objects", oid), ("config:object", oid)))
            obj = conn.execute(
                "SELECT config_json FROM instance_configs WHERE session_id=? AND "
                "domain='object' AND instance_id=?",
                (sid, oid),
            ).fetchone()
            if obj is None:
                complete = False
                continue
            definition = json.loads(json.loads(obj[0])["definition"])
            if definition is None:
                continue
            tables = {
                "Openable": "object_openables",
                "LightEmitter": "object_lights",
                "Mechanism": "object_mechanisms",
                "Durability": "object_durability",
            }
            for cap in definition["fields"]["capabilities"]["tuple"]:
                if cap["type"] in tables:
                    required.add((tables[cap["type"]], oid))
                if cap["type"] == "LockSet":
                    for lock in cap["fields"]["locks"]["tuple"]:
                        required.add(("door_locks", lock["fields"]["lock_id"]))
    for domain, identity in required:
        if domain.startswith("config:"):
            exists = conn.execute(
                "SELECT 1 FROM instance_configs WHERE session_id=? AND domain=? AND instance_id=?",
                (sid, domain.split(":")[1], identity),
            ).fetchone()
        else:
            column = (
                "scene_id"
                if domain == "mechanical_scenes"
                else "actor_id"
                if domain == "actors"
                else "lock_id"
                if domain == "door_locks"
                else "object_id"
            )
            exists = conn.execute(
                f"SELECT 1 FROM {domain} WHERE session_id=? AND {column}=?", (sid, identity)
            ).fetchone()
        complete = complete and exists is not None
    conn.execute(
        "DELETE FROM scene_integrity_dependencies WHERE session_id=? AND scene_id=?", (sid, scene)
    )
    conn.executemany(
        "INSERT INTO scene_integrity_dependencies VALUES (?,?,?,?)",
        [(sid, scene, domain, identity) for domain, identity in sorted(required)],
    )
    conn.execute(
        "INSERT INTO scene_integrity VALUES (?,?,?) ON "
        "CONFLICT(session_id,scene_id) DO UPDATE SET complete=excluded.complete",
        (sid, scene, complete),
    )
    return complete
