"""Preflight legacy inventory before replacing aggregate storage with item facts.

This runs under the Store's opening migration transaction. Any ambiguous owner,
identity, placement or unit rejects the entire upgrade without changing the save.
"""

import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from dnd5e_engine.evaluation_state import InventoryState

from .errors import StateMachineError
from .item_units import valid_item_unit
from .package import canonical_json


def _reject(reason):
    """Stop the atomic upgrade with a diagnostic that names the uncertain fact."""
    raise StateMachineError("unsupported_storage_schema", f"Inventory migration: {reason}")


def _legacy_id(sid, owner, position):
    """Derive a stable, bounded identity for one explicit legacy list occurrence."""
    digest = hashlib.sha256(f"{sid}\0{owner}".encode()).hexdigest()[:20]
    return f"legacy.inventory.{digest}.{position:08d}"


def is_generated_placeholder(conn, sid, oid):
    """Recognize schema41 promoted items by persisted provenance and placement."""
    row = conn.execute(
        "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, oid)
    ).fetchone()
    if row is None or row["carrier_id"] is None or row["item_json"] is None:
        return False
    prefix = _legacy_id(sid, row["carrier_id"], 0)[:-8]
    suffix = oid[len(prefix) :] if oid.startswith(prefix) else ""
    config = conn.execute(
        "SELECT config_json FROM instance_configs WHERE session_id=? AND domain='object' "
        "AND instance_id=?",
        (sid, oid),
    ).fetchone()
    stored = json.loads(config[0]) if config is not None else None
    base = {
        "definition": "null",
        "movable": False,
        "blocks_passage": False,
        "portable_policy": None,
    }
    generated = stored == {**base, "inventory_origin": "schema41"}
    core = len(suffix) == 8 and suffix.isdecimal() and stored == base
    return (
        row["scene_id"] is None
        and row["container_id"] is None
        and row["state"] is None
        and config is not None
        and (generated or core)
        and valid_item_unit(json.loads(row["item_json"]))
    )


def _owner_of(conn, sid, obj):
    """Follow an existing object's containment chain to its explicit carrier."""
    seen = set()
    while obj is not None:
        if obj["object_id"] in seen or len(seen) >= 8:
            _reject("containment ancestry is ambiguous")
        seen.add(obj["object_id"])
        if obj["carrier_id"] is not None:
            return obj["carrier_id"]
        if obj["scene_id"] is not None:
            return None
        parent = obj["container_id"]
        obj = (
            conn.execute(
                "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, parent)
            ).fetchone()
            if parent is not None
            else None
        )
        if parent is not None and obj is None:
            _reject("missing containment ancestor")
    return None


def _effective_accessible(conn, sid, obj):
    """Reproduce item and enclosing-shell access from persisted World facts."""
    hp = conn.execute(
        "SELECT current_hp FROM object_durability WHERE session_id=? AND object_id=?",
        (sid, obj["object_id"]),
    ).fetchone()
    if hp is not None and hp[0] == 0:
        return False
    seen = {obj["object_id"]}
    parent = obj["container_id"]
    while parent is not None:
        if parent in seen or len(seen) >= 8:
            _reject("containment ancestry is ambiguous")
        seen.add(parent)
        shell = conn.execute(
            "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, parent)
        ).fetchone()
        if shell is None:
            _reject("missing containment ancestor")
        hp = conn.execute(
            "SELECT current_hp FROM object_durability WHERE session_id=? AND object_id=?",
            (sid, parent),
        ).fetchone()
        if hp is None or hp[0] != 0:
            sealed = conn.execute(
                "SELECT 1 FROM door_locks WHERE session_id=? AND object_id=? AND locked=1",
                (sid, parent),
            ).fetchone()
            barred = conn.execute(
                "SELECT barred FROM object_openables WHERE session_id=? AND object_id=?",
                (sid, parent),
            ).fetchone()
            if shell["state"] == "closed" or sealed or (barred and barred[0]):
                return False
        parent = shell["container_id"]
    return True


def _references(value, identity):
    """Find an exact instance identity in structured retained rule data."""
    if isinstance(value, dict):
        return any(_references(v, identity) for v in value.values())
    if isinstance(value, list):
        return any(_references(v, identity) for v in value)
    return value == identity


def _put_unit(conn, sid, iid, owner, unit):
    """Promote one proved unit or check a preexisting World identity."""
    if not valid_item_unit(unit):
        _reject(f"invalid or inaccessible detached unit {iid}")
    if (
        conn.execute(
            "SELECT 1 FROM actors WHERE session_id=? AND actor_id=?", (sid, owner)
        ).fetchone()
        is None
    ):
        _reject(f"missing owner for {iid}")
    old = conn.execute(
        "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, iid)
    ).fetchone()
    if old is not None:
        if old["item_json"] is None or json.loads(old["item_json"]) != unit:
            _reject(f"object identity or unit conflicts for {iid}")
        if _owner_of(conn, sid, old) != owner:
            _reject(f"object owner conflicts for {iid}")
        return
    conn.execute(
        "INSERT INTO objects(session_id,object_id,scene_id,state,cell,carrier_id,item_json) "
        "VALUES (?,?,NULL,NULL,NULL,?,?)",
        (sid, iid, owner, canonical_json(unit)),
    )
    conn.execute(
        "INSERT INTO instance_configs(session_id,domain,instance_id,config_json) "
        "VALUES (?,'object',?,?)",
        (
            sid,
            iid,
            canonical_json(
                {
                    "definition": "null",
                    "movable": False,
                    "blocks_passage": False,
                    "portable_policy": None,
                    "inventory_origin": "schema41",
                }
            ),
        ),
    )


def preflight_inventory(conn):
    """Promote unrepresented owned units, then remove only proved legacy mirrors.

    The legacy core list has one occurrence per unit. For mechanical saves the
    typed inventory supplies identity and charges; a core list must agree with
    its owner slugs. Without mechanical components, each valid core occurrence
    receives a stable identity and the only explicit location, its carrier.
    """
    sessions = conn.execute("SELECT session_id FROM sessions ORDER BY session_id").fetchall()
    for (sid,) in sessions:
        component = conn.execute(
            "SELECT * FROM mechanical_components WHERE session_id=?", (sid,)
        ).fetchone()
        entries = []
        if component is not None:
            try:
                raw = json.loads(component["inventory_json"])
                InventoryState.model_validate({**raw, "entries": tuple(raw["entries"])})
                entries = raw["entries"]
            except (ValueError, TypeError, KeyError) as exc:
                _reject(f"invalid mechanical inventory in {sid}: {exc}")
        seen = set()
        by_owner = {}
        for entry in entries:
            iid, owner = entry["instance_id"], entry["owner_id"]
            if (
                conn.execute(
                    "SELECT 1 FROM mechanical_actors m JOIN characters c "
                    "USING(session_id,actor_id) WHERE m.session_id=? AND m.actor_id=?",
                    (sid, owner),
                ).fetchone()
                is None
            ):
                _reject(f"mechanical item owner lacks actor facts: {iid}")
            if iid in seen:
                _reject(f"duplicate item identity {iid}")
            seen.add(iid)
            unit = {
                key: entry[key]
                for key in ("item_slug", "quantity", "charges_remaining_per_unit", "accessible")
            }
            if type(unit["quantity"]) is not int or unit["quantity"] < 1:
                _reject(f"invalid quantity for {iid}")
            # One independently usable instance per legacy quantity. Only an
            # already represented World unit may explain inaccessible state.
            count = unit["quantity"]
            unit["quantity"] = 1
            if count > 1 and count > 128:
                _reject(f"cannot split ambiguous stack {iid}")
            if count > 1:
                retained = [
                    json.loads(component[field])
                    for field in ("resources_json", "casting_json", "effects_json")
                ]
                retained.extend(
                    json.loads(row[0])
                    for row in conn.execute(
                        "SELECT state_json FROM mechanical_actors WHERE session_id=?", (sid,)
                    )
                )
                if any(_references(value, iid) for value in retained):
                    _reject(f"stack identity is referenced outside inventory: {iid}")
            existing = conn.execute(
                "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, iid)
            ).fetchone()
            if count > 1 and existing is not None:
                _reject(f"existing World item cannot be split: {iid}")
            if (
                existing is not None
                and (
                    _effective_accessible(conn, sid, existing)
                    and json.loads(existing["item_json"])["accessible"]
                    if existing["item_json"] is not None
                    else False
                )
                != entry["accessible"]
            ):
                _reject(f"effective accessibility conflicts for {iid}")
            if existing is not None and unit["accessible"] is False:
                # An existing container may derive this bit from its ancestors;
                # its own item unit remains independently usable when removed.
                old = conn.execute(
                    "SELECT item_json FROM objects WHERE session_id=? AND object_id=?",
                    (sid, iid),
                ).fetchone()
                if old is not None and old[0] is not None:
                    old_unit = json.loads(old[0])
                    if {k: v for k, v in old_unit.items() if k != "accessible"} != {
                        k: v for k, v in unit.items() if k != "accessible"
                    }:
                        _reject(f"World unit conflicts with mechanical item: {iid}")
                    unit["accessible"] = old_unit["accessible"]
            _put_unit(conn, sid, iid, owner, unit)
            for n in range(1, count):
                split_id = f"{iid}.unit.{n:08d}"
                if split_id in seen:
                    _reject(f"split identity conflicts for {iid}")
                if conn.execute(
                    "SELECT 1 FROM objects WHERE session_id=? AND object_id=?", (sid, split_id)
                ).fetchone():
                    _reject(f"split identity already exists: {split_id}")
                seen.add(split_id)
                _put_unit(conn, sid, split_id, owner, unit)
            by_owner.setdefault(owner, []).append(entry["item_slug"])
        if component is not None:
            for world_item in conn.execute(
                "SELECT * FROM objects WHERE session_id=? AND item_json IS NOT NULL", (sid,)
            ).fetchall():
                if (
                    _owner_of(conn, sid, world_item) is not None
                    and world_item["object_id"] not in seen
                ):
                    _reject(
                        "owned World item absent from mechanical inventory: "
                        f"{world_item['object_id']}"
                    )
        for core in conn.execute(
            "SELECT actor_id,inventory_json FROM characters WHERE session_id=? ORDER BY actor_id",
            (sid,),
        ).fetchall():
            owner = core["actor_id"]
            try:
                slugs = json.loads(core["inventory_json"])
            except (ValueError, TypeError):
                _reject(f"invalid character inventory for {owner}")
            if not isinstance(slugs, list) or any(type(s) is not str for s in slugs):
                _reject(f"invalid character inventory for {owner}")
            if component is not None:
                mechanical_slugs = set(by_owner.get(owner, ()))
                if not mechanical_slugs <= set(slugs) or len(slugs) != len(set(slugs)):
                    _reject(f"character and mechanical inventories disagree for {owner}")
                for n, slug in enumerate(slugs):
                    if slug not in mechanical_slugs:
                        _put_unit(
                            conn,
                            sid,
                            _legacy_id(sid, owner, n),
                            owner,
                            {
                                "item_slug": slug,
                                "quantity": 1,
                                "charges_remaining_per_unit": 0,
                                "accessible": True,
                            },
                        )
            else:
                existing = [
                    row
                    for row in conn.execute(
                        "SELECT * FROM objects WHERE session_id=? AND item_json IS NOT NULL "
                        "ORDER BY object_id",
                        (sid,),
                    ).fetchall()
                    if _owner_of(conn, sid, row) == owner
                ]
                existing_slugs = [json.loads(row["item_json"])["item_slug"] for row in existing]
                if (
                    len(slugs) != len(set(slugs))
                    or len(existing_slugs) != len(set(existing_slugs))
                    or (existing_slugs and existing_slugs != slugs)
                ):
                    _reject(f"core and World inventories cannot be matched for {owner}")
                for n, slug in enumerate(slugs):
                    if slug in existing_slugs:
                        continue
                    _put_unit(
                        conn,
                        sid,
                        _legacy_id(sid, owner, n),
                        owner,
                        {
                            "item_slug": slug,
                            "quantity": 1,
                            "charges_remaining_per_unit": 0,
                            "accessible": True,
                        },
                    )


def preview_save(path):
    """Apply every pending migration to a backup, leaving the specified save untouched."""
    source = Path(path).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="inventory-preflight-") as scratch:
        target = Path(scratch) / "preview.sqlite"
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
            closing(sqlite3.connect(target)) as copy,
        ):
            original.backup(copy)
        from .storage import SqliteStore

        store = SqliteStore(target)
        try:
            return store.connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            store.close()


def main():
    """Report whether one existing save can be upgraded without touching its bytes."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("save", help="SQLite save path to check read-only")
    args = parser.parse_args()
    try:
        version = preview_save(args.save)
    except (OSError, sqlite3.Error, StateMachineError) as exc:
        parser.exit(1, f"NOT READY: {exc}\n")
    print(f"READY: copy upgraded through schema {version}; original save unchanged")


if __name__ == "__main__":
    main()
