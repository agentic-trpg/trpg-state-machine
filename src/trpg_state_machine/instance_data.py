"""Domain configuration records; no current positions, balances or capability state.

Only creation/explicit upgrade reads templates. Runtime projections combine these
records with existing fact owners; a missing record is never repaired on read.
The closed tagged codec preserves the existing immutable domain value types.
"""

import dataclasses
import json
from functools import lru_cache
from types import SimpleNamespace

from .errors import StateMachineError
from .package import canonical_json
from .scoped_reads import scoped_read


@lru_cache(maxsize=1)
def _types():
    from . import mechanism_effects, mechanism_models, navigation_models, package, world_package

    return {
        cls.__name__: cls
        for module in (
            package,
            world_package,
            mechanism_models,
            mechanism_effects,
            navigation_models,
        )
        for cls in vars(module).values()
        if isinstance(cls, type) and dataclasses.is_dataclass(cls)
    }


def encode(value):
    if dataclasses.is_dataclass(value):
        return {
            "type": type(value).__name__,
            "fields": {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)},
        }
    if isinstance(value, tuple):
        return {"tuple": [encode(v) for v in value]}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, dict):
        if set(value) == {"type", "fields"}:
            return _types()[value["type"]](**{k: decode(v) for k, v in value["fields"].items()})
        if set(value) == {"tuple"}:
            return tuple(decode(v) for v in value["tuple"])
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


class SceneConfig(SimpleNamespace):
    """Read projection; metadata reads never enumerate objects or sample a map."""

    def __init__(self, instances, sid, data):
        planned_grid = data.pop("grid_json")
        super().__init__(**data)
        self._instances, self._sid, self._planned_grid = instances, sid, planned_grid

    def _collection(self, field):
        row = self._instances.conn.execute(
            "SELECT json_extract(config_json,?) FROM instance_configs WHERE session_id=? "
            "AND domain='scene' AND instance_id=?",
            ("$." + field, self._sid, self.scene_id),
        ).fetchone()
        return decode(json.loads(row[0]))

    @property
    def object_ids(self):
        return self._collection("object_ids")

    @property
    def actor_initial_ids(self):
        return self._collection("actor_initial_ids")

    @property
    def actor_ids(self):
        return self._collection("actor_ids")

    @property
    def pending_objects(self):
        return self._collection("pending_objects")

    @property
    def pending_actors(self):
        return self._collection("pending_actors")

    @property
    def objects(self):
        instances = self._instances
        pending = {o.object_id: o for o in self.pending_objects}
        result = []
        for oid in self.object_ids:
            if instances.facts.object(self._sid, oid) is not None:
                result.append(instances.legacy_object(self._sid, oid))
            elif oid in pending:
                result.append(pending[oid])
        return tuple(result)

    @property
    def grid_json(self):
        if not self.has_grid:
            return None
        facts, sid = self._instances.facts, self._sid
        stored = facts.mechanical_scene(sid, self.scene_id)
        if stored is not None:
            return canonical_json(stored["grid"])
        if self._instances.conn.execute(
            "SELECT 1 FROM scene_instances WHERE session_id=? AND scene_id=?", (sid, self.scene_id)
        ).fetchone():
            raise StateMachineError("incomplete_session", "Instantiated scene is missing its grid")
        return self._planned_grid


class InstanceData:
    def __init__(self, facts):
        self.facts = facts

    @property
    def conn(self):
        return self.facts._conn

    def put(self, sid, domain, identity, value):
        self.facts.store.require_write()
        self.conn.execute(
            "INSERT INTO instance_configs VALUES (?,?,?,?) "
            "ON CONFLICT(session_id,domain,instance_id) DO NOTHING",
            (sid, domain, identity, canonical_json(encode(value))),
        )

    def get(self, sid, domain, identity):
        row = self.conn.execute(
            "SELECT config_json FROM instance_configs WHERE session_id=? AND domain=? "
            "AND instance_id=?",
            (sid, domain, identity),
        ).fetchone()
        if row is None:
            raise StateMachineError(
                "incomplete_instance_config", "Explicit instance upgrade required"
            )
        try:
            return decode(json.loads(row[0]))
        except (KeyError, TypeError, ValueError) as exc:
            raise StateMachineError(
                "incomplete_instance_config", "Invalid instance configuration"
            ) from exc

    @scoped_read
    def session_value(self, sid, field):
        row = self.conn.execute(
            "SELECT json_extract(config_json,?) FROM instance_configs WHERE session_id=? "
            "AND domain='session' AND instance_id=?",
            ("$." + field, sid, sid),
        ).fetchone()
        if row is None:
            raise StateMachineError("incomplete_instance_config", "Missing Session configuration")
        value = row[0]
        return (
            decode(json.loads(value))
            if isinstance(value, str) and value[:1] in ("{", "[")
            else value
        )

    def initialize_world(self, sid, package):
        self.put(
            sid,
            "session",
            sid,
            {
                "scene_ids": tuple(s.scene_id for s in package.scenes),
                "passages": package.passages,
                "navigation": package.navigation,
                "evidence_schema": "event-evidence/6"
                if any(d.capability("light") for d in package.world_objects)
                else "event-evidence/5"
                if any(
                    s.grid_json or any(o.state is None for o in s.objects) for s in package.scenes
                )
                else "event-evidence/4",
                "has_noncombat": package.noncombat_foundation_json is not None,
                "legacy_lock_ids": tuple(d.object_id for d in package.lockable_doors),
            },
        )
        # Static scene metadata and maps are Session configuration even before
        # activation. Changing grid facts still live only in mechanical_scenes.
        for scene in package.scenes:
            self.put(
                sid,
                "scene",
                scene.scene_id,
                {
                    "scene_id": scene.scene_id,
                    "location_id": scene.location_id,
                    "exits": scene.exits,
                    "actor_ids": scene.actor_ids,
                    "object_ids": tuple(o.object_id for o in scene.objects),
                    "actor_initial_ids": tuple(
                        a.actor_id for a in package.actors if a.initial_scene_id == scene.scene_id
                    ),
                    "has_grid": scene.grid_json is not None,
                    "pending_objects": scene.objects,
                    "pending_definitions": tuple(
                        d
                        for d in package.world_objects
                        if d.object_id in {o.object_id for o in scene.objects}
                    ),
                    "pending_actors": tuple(
                        (a.actor_id, a.initial_cell)
                        for a in package.actors
                        if a.initial_scene_id == scene.scene_id
                    ),
                    "exit_passages": scene.exit_passages,
                    "public_interactions": scene.public_interactions,
                    "grid_json": scene.grid_json,
                },
            )

    def initialize_scene(self, sid, scene_id, package):
        definitions = {d.object_id: d for d in package.world_objects}
        portable = {i.instance_id: i.policy for i in package.portable_items}
        for obj in package.scene(scene_id).objects:
            definition = encode(definitions.get(obj.object_id))
            if definition:
                for c in definition["fields"]["capabilities"]["tuple"]:
                    fields = c["fields"]
                    # State remains exclusively in its existing domain table.
                    for key in tuple(fields):
                        if key.startswith("initial_"):
                            value = fields.pop(key)
                            if key == "initial_barred":
                                fields["bar_present"] = value is not None
                    if c["type"] == "LockSet":
                        for lock in fields["locks"]["tuple"]:
                            lock["fields"].pop("initial_locked")
                            lock["fields"].pop("credential_instance_ids")
            self.put(
                sid,
                "object",
                obj.object_id,
                {
                    "definition": canonical_json(definition),
                    "movable": obj.movable,
                    "blocks_passage": obj.blocks_passage,
                    "portable_policy": portable.get(obj.object_id),
                },
            )
        for actor in package.actors:
            if actor.initial_scene_id == scene_id:
                self.put(
                    sid,
                    "actor",
                    actor.actor_id,
                    {
                        "actor_id": actor.actor_id,
                        "senses": actor.senses,
                    },
                )

    @scoped_read
    def object_definition(self, sid, oid):
        if self.facts.object(sid, oid) is None:
            row = self.conn.execute(
                "SELECT d.value FROM instance_configs i, "
                "json_each(i.config_json,'$.pending_definitions.tuple') d "
                "WHERE i.session_id=? AND i.domain='scene' "
                "AND json_extract(d.value,'$.fields.object_id')=? LIMIT 1",
                (sid, oid),
            ).fetchone()
            return decode(json.loads(row[0])) if row else None
        data = json.loads(self.get(sid, "object", oid)["definition"])
        if data is None:
            return None
        # Fill constructor-only initial fields from CURRENT facts, never templates.
        for c in data["fields"]["capabilities"]["tuple"]:
            f, kind = c["fields"], c["type"]
            if kind == "Openable":
                state = self.facts.openable_state(sid, oid)
                if state is None:
                    raise StateMachineError("incomplete_session", "Missing openable state")
                f["initial_barred"] = state["barred"] if f.pop("bar_present") else None
                f["initial_jammed"] = state["jammed"]
            elif kind == "Durability":
                f["initial_hp"] = self.facts.object_hp(sid, oid)
                if f["initial_hp"] is None:
                    raise StateMachineError("incomplete_session", "Missing durability state")
            elif kind == "LightEmitter":
                state = self.facts.light_state(sid, oid)
                if state is None:
                    raise StateMachineError("incomplete_session", "Missing emitter state")
                f["initial_enabled"], f["initial_mode"] = state["enabled"], state["mode"]
            elif kind == "Mechanism":
                f["initial_status"] = self.facts.mechanism_status(sid, oid)
                if f["initial_status"] is None:
                    raise StateMachineError("incomplete_session", "Missing mechanism state")
            elif kind == "LockSet":
                locks = dict(self.facts.locks_for_object(sid, oid))
                for lock in f["locks"]["tuple"]:
                    lock_id = lock["fields"]["lock_id"]
                    if lock_id not in locks:
                        raise StateMachineError("invalid_lock_state", "Missing configured lock")
                    lock["fields"]["initial_locked"] = locks[lock_id]
                    lock["fields"]["credential_instance_ids"] = encode(
                        tuple(
                            r[0]
                            for r in self.conn.execute(
                                "SELECT instance_id FROM door_credentials "
                                "WHERE session_id=? AND lock_id=? "
                                "ORDER BY instance_id",
                                (sid, lock_id),
                            )
                        )
                    )
        definition = decode(data)
        if definition.object_id != oid:
            raise StateMachineError("incomplete_instance_config", "Object config identity differs")
        return definition

    def pending_object(self, sid, oid):
        row = self.conn.execute(
            "SELECT i.instance_id,p.value FROM instance_configs i, "
            "json_each(i.config_json,'$.pending_objects.tuple') p "
            "WHERE i.session_id=? AND i.domain='scene' "
            "AND json_extract(p.value,'$.fields.object_id')=? LIMIT 1",
            (sid, oid),
        ).fetchone()
        if row is None:
            raise StateMachineError("invalid_target", "Object instance has not been created")
        return row[0], decode(json.loads(row[1]))

    def objects(self, sid):
        return tuple(
            d
            for r in self.conn.execute(
                "SELECT object_id FROM objects WHERE session_id=? ORDER BY object_id", (sid,)
            )
            if (d := self.object_definition(sid, r[0])) is not None
        )

    def mechanism_configs(self, sid):
        """Only configured mechanism capability data, no unrelated state hydration."""
        for row in self.conn.execute(
            "SELECT m.object_id,c.value FROM object_mechanisms m "
            "JOIN instance_configs i ON i.session_id=m.session_id AND i.domain='object' "
            "AND i.instance_id=m.object_id, "
            "json_each(json_extract(i.config_json,'$.definition'),'$.fields.capabilities.tuple') c "
            "WHERE m.session_id=? AND json_extract(c.value,'$.type')='Mechanism' "
            "ORDER BY m.object_id",
            (sid,),
        ):
            data = json.loads(row[1])
            data["fields"]["initial_status"] = self.facts.mechanism_status(sid, row[0])
            yield row[0], decode(data)

    def legacy_object(self, sid, oid):
        data = self.get(sid, "object", oid)
        obj = self.facts.object(sid, oid)
        return SimpleNamespace(
            object_id=oid,
            movable=data["movable"],
            blocks_passage=data["blocks_passage"],
            state=obj["state"],
            initial_cell=obj["cell"],
            initial_orientation=obj["orientation"],
        )

    @scoped_read
    def scene(self, sid, scene_id):
        row = self.conn.execute(
            "SELECT json_remove(config_json,'$.object_ids','$.actor_ids','$.actor_initial_ids',"
            "'$.pending_objects','$.pending_actors','$.pending_definitions') FROM instance_configs "
            "WHERE session_id=? AND domain='scene' AND instance_id=?",
            (sid, scene_id),
        ).fetchone()
        if row is None:
            session = self.conn.execute(
                "SELECT config_json FROM instance_configs WHERE session_id=? "
                "AND domain='session' AND instance_id=?",
                (sid, sid),
            ).fetchone()
            declared = (
                session is not None
                and self.conn.execute(
                    "SELECT 1 FROM json_each(?,'$.scene_ids.tuple') WHERE value=? LIMIT 1",
                    (session[0], scene_id),
                ).fetchone()
            )
            if session is None or declared:
                raise StateMachineError(
                    "incomplete_instance_config", "Explicit instance upgrade required"
                )
            raise StateMachineError("unknown_scene", f"Unknown scene: {scene_id}")
        return SceneConfig(self, sid, decode(json.loads(row[0])))

    def scenes(self, sid):
        return tuple(self.scene(sid, scene_id) for scene_id in self.session_value(sid, "scene_ids"))

    def legacy_locks(self, sid):
        return tuple(
            SimpleNamespace(object_id=oid) for oid in self.session_value(sid, "legacy_lock_ids")
        )

    def finish_scene(self, sid, scene_id):
        """Remove the uninstantiated map plan once the grid owner stores it."""
        self.facts.store.require_write()
        data = self.get(sid, "scene", scene_id)
        data["pending_objects"] = ()
        data["pending_definitions"] = ()
        data["pending_actors"] = ()
        if data["has_grid"]:
            if self.facts.mechanical_scene(sid, scene_id) is None:
                raise StateMachineError(
                    "incomplete_session", "Instantiated scene is missing its grid"
                )
            data["grid_json"] = None
        self.conn.execute(
            "UPDATE instance_configs SET config_json=? WHERE session_id=? AND domain='scene' "
            "AND instance_id=?",
            (canonical_json(encode(data)), sid, scene_id),
        )

    def is_portable(self, sid, oid):
        obj = self.facts.object(sid, oid)
        return bool(
            obj is not None
            and obj["item_json"] is not None
            and self.get(sid, "object", oid)["portable_policy"] == "ordinary_public"
        )

    def has_portable(self, sid):
        return (
            self.conn.execute(
                "SELECT 1 FROM objects WHERE session_id=? AND item_json IS NOT NULL LIMIT 1", (sid,)
            ).fetchone()
            is not None
        )

    def actor(self, sid, actor_id):
        row = self.conn.execute(
            "SELECT kind FROM actors WHERE session_id=? AND actor_id=?", (sid, actor_id)
        ).fetchone()
        if row is None or row[0] == "pc":
            return None
        return SimpleNamespace(**self.get(sid, "actor", actor_id))
