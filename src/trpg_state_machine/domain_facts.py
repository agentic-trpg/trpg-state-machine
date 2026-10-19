"""Controlled current facts. These services share the caller's SQLite scope.

No method commits, computes rules, or keeps an authoritative in-memory copy.
Rules metadata edits have a closed field set matching the pinned adapter.
"""

import json

from dnd5e_engine.evaluation_casting import CastingState
from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_resources import ResourceState
from dnd5e_engine.evaluation_state import InventoryState
from pydantic import TypeAdapter

from .errors import StateMachineError
from .item_units import valid_item_unit
from .package import canonical_json


class CharacterFacts:
    RULE_FIELDS = frozenset(
        (
            "temp_hp",
            "is_alive",
            "death_saves",
            "conditions",
            "last_damaged_by",
            "action_available",
            "bonus_action_available",
            "reaction_available",
            "action_taken_this_turn",
            "bonus_action_taken_this_turn",
            "attack_action_attacks_made",
            "attack_rolls_made_this_turn",
            "action_grants_spent",
            "action_grant_groups_spent",
            "movement_remaining",
            "disengaging_this_turn",
            "sneak_attack_spent_this_turn",
            "attacks_remaining",
            "attack_action_engaged",
            "light_weapon_swing_slug",
            "restricted_light_weapon_swing_slug",
            "offhand_attack_spent",
            "dodging",
            "loading_weapon_fired_this_action",
            "cleave_spent_this_turn",
            "flurry_strikes_remaining",
            "extra_actions_remaining",
            "action_surge_used_this_turn",
        )
    )

    def __init__(self, store, world):
        self.store = store
        self.world = world

    @property
    def _conn(self):
        return self.store.connection

    def initialize_pc(self, sid, actor_id, scene_id, pc):
        self.store.require_write()
        self._conn.execute(
            "INSERT INTO actors(session_id,actor_id,kind,current_scene_id,cell) "
            "VALUES (?,?,'pc',?,?)",
            (sid, actor_id, scene_id, pc.initial_cell),
        )
        self._conn.execute(
            "INSERT INTO characters(session_id,actor_id,current_hp,max_hp,"
            "resources_json,inventory_json) VALUES (?,?,?,?,?,?)",
            (
                sid,
                actor_id,
                pc.current_hp,
                pc.max_hp,
                canonical_json(dict(pc.resources)),
                canonical_json(pc.inventory),
            ),
        )

    def initialize_npc_actor(self, sid, actor):
        self.store.require_write()
        return bool(
            self._conn.execute(
                "INSERT INTO actors(session_id,actor_id,kind,current_scene_id,cell) "
                "SELECT ?,?,'npc',?,? WHERE NOT EXISTS "
                "(SELECT 1 FROM actors WHERE session_id=? AND actor_id=?)",
                (
                    sid,
                    actor.actor_id,
                    actor.initial_scene_id,
                    actor.initial_cell,
                    sid,
                    actor.actor_id,
                ),
            ).rowcount
        )

    def move(self, sid, actor_id, destination):
        self.store.require_write()
        self._conn.execute(
            "UPDATE actors SET current_scene_id=?,cell=? WHERE session_id=? AND actor_id=?",
            (destination.scene_id, destination.cell, sid, actor_id),
        )

    def set_hp(self, sid, actor_id, value):
        self.store.require_write()
        self._conn.execute(
            "UPDATE characters SET current_hp=? WHERE session_id=? AND actor_id=?",
            (value, sid, actor_id),
        )

    def spend_resource(self, sid, actor_id, resource_id, amount):
        self.store.require_write()
        row = self._conn.execute(
            "SELECT resources_json FROM characters WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()
        resources = json.loads(row[0])
        resources[resource_id] -= amount
        self._conn.execute(
            "UPDATE characters SET resources_json=? WHERE session_id=? AND actor_id=?",
            (canonical_json(resources), sid, actor_id),
        )

    def award_xp(self, sid, actor_id, value):
        self.store.require_write()
        current = self._conn.execute(
            "SELECT xp FROM characters WHERE session_id=? AND actor_id=?", (sid, actor_id)
        ).fetchone()
        if current is None or not 0 < value <= (2**63 - 1) - current[0]:
            raise StateMachineError(
                "invalid_rule_result", "XP increment cannot be represented in authoritative storage"
            )
        self._conn.execute(
            "UPDATE characters SET xp=xp+? WHERE session_id=? AND actor_id=?",
            (value, sid, actor_id),
        )

    def update_rule_metadata(self, sid, actor_id, field, value):
        self.store.require_write()
        if field not in self.RULE_FIELDS:
            raise StateMachineError("invalid_rule_result", "Unsupported actor fact")
        self._conn.execute(
            "UPDATE mechanical_actors SET state_json=json_set(state_json,?,json(?)) "
            "WHERE session_id=? AND actor_id=?",
            ("$." + field, canonical_json(value), sid, actor_id),
        )

    def initialize_rule_actor(self, row, actor, *, xp=None, encounter=False):
        self.store.require_write()
        sid = row["session_id"]
        core = self._conn.execute(
            "SELECT * FROM characters WHERE session_id=? AND actor_id=?", (sid, actor.entity_id)
        ).fetchone()
        if core is not None:
            if encounter:
                if (core["current_hp"], core["max_hp"]) != (actor.hp_current, actor.hp_max):
                    raise StateMachineError(
                        "invalid_combat_foundation", "Initialization cannot reset HP"
                    )
                if json.loads(core["resources_json"]) or json.loads(core["effects_json"]):
                    raise StateMachineError(
                        "unsupported_combat_foundation",
                        "Legacy resources/effects cannot be converted implicitly",
                    )
                if json.loads(core["inventory_json"]) != list(actor.carried_item_slugs):
                    raise StateMachineError(
                        "invalid_combat_foundation", "Equipment must match selected PC"
                    )
            if (
                (core["current_hp"], core["max_hp"]) != (actor.hp_current, actor.hp_max)
                or json.loads(core["inventory_json"]) != list(actor.carried_item_slugs)
                or json.loads(core["resources_json"])
                or json.loads(core["effects_json"])
            ):
                raise StateMachineError(
                    "invalid_combat_foundation", "Initialization cannot reset core state"
                )
            if xp is not None:
                self._conn.execute(
                    "UPDATE characters SET xp=? WHERE session_id=? AND actor_id=?",
                    (xp, sid, actor.entity_id),
                )
        else:
            self._conn.execute(
                "INSERT INTO characters(session_id,actor_id,current_hp,max_hp,resources_json,"
                "inventory_json,effects_json,xp) VALUES (?,?,?,?,?,?,?,?)",
                (
                    sid,
                    actor.entity_id,
                    actor.hp_current,
                    actor.hp_max,
                    "{}",
                    canonical_json(actor.carried_item_slugs),
                    "[]",
                    0 if xp is None else xp,
                ),
            )
        state = actor.model_dump(
            mode="json", exclude={"hp_current", "hp_max", "carried_item_slugs"}
        )
        self._conn.execute(
            "INSERT INTO mechanical_actors VALUES (?,?,?)",
            (sid, actor.entity_id, canonical_json(state)),
        )

    def initial_rule_actor(self, sid, approved):
        """Overlay existing core balances at the first mechanical initialization."""
        core = self._conn.execute(
            "SELECT current_hp,max_hp,inventory_json FROM characters "
            "WHERE session_id=? AND actor_id=?",
            (sid, approved.entity_id),
        ).fetchone()
        if core is None:
            return approved
        if json.loads(core["inventory_json"]) != list(approved.carried_item_slugs):
            raise StateMachineError(
                "invalid_combat_foundation",
                "Changed equipment requires explicit current inventory instance facts",
            )
        return approved.model_copy(
            update={"hp_current": core["current_hp"], "hp_max": core["max_hp"]}
        )

    def initialize_rule_components(self, sid, snapshot):
        """Initial explicit facts only; an existing owner record may never be reset."""
        self.store.require_write()
        fields = ("inventory_state", "resource_state", "casting_state", "effect_states")
        data = snapshot.model_dump(mode="json", include=set(fields))
        encoded = tuple(canonical_json(data[field]) for field in fields)
        old = self._conn.execute(
            "SELECT schema_version,inventory_json,resources_json,casting_json,effects_json "
            "FROM mechanical_components WHERE session_id=?",
            (sid,),
        ).fetchone()
        expected = (snapshot.snapshot_schema_version, *encoded)
        if old is not None:
            if tuple(old) != expected:
                raise StateMachineError(
                    "invalid_combat_foundation", "Initialization cannot reset component facts"
                )
            return
        self._conn.execute(
            "INSERT INTO mechanical_components VALUES (?,?,?,?,?,?)", (sid, *expected)
        )

    def rule_components(self, sid, owner_ids):
        """Read persisted facts, then project owners; absence is never an empty component."""
        row = self._conn.execute(
            "SELECT * FROM mechanical_components WHERE session_id=?", (sid,)
        ).fetchone()
        if row is None or row["schema_version"] != "engine-snapshot/17":
            raise StateMachineError(
                "unsupported_mechanical_version", "Explicit component facts are missing"
            )
        try:
            inventory, resources, casting, effects = (
                json.loads(row[field])
                for field in ("inventory_json", "resources_json", "casting_json", "effects_json")
            )
            inventory = InventoryState.model_validate_json(row["inventory_json"]).model_dump(
                mode="json"
            )
            resources = (
                ResourceState.model_validate_json(row["resources_json"]).model_dump(mode="json")
                if resources is not None
                else None
            )
            casting = (
                CastingState.model_validate_json(row["casting_json"]).model_dump(mode="json")
                if casting is not None
                else None
            )
            effects = TypeAdapter(tuple[EffectState, ...]).validate_json(row["effects_json"])
            effects = [v.model_dump(mode="json") for v in effects]
            known = set(self.rule_actors(sid))
            references = {v["owner_id"] for v in inventory["entries"]}
            references |= {v["target_id"] for v in effects}
            if resources is not None:
                references |= {v["owner_id"] for v in resources["pools"]}
            if casting is not None:
                references |= {v["actor_id"] for v in casting["actors"]}
            if not references <= known:
                raise ValueError("component owner is missing")
            self.validate_portable_inventory(sid, inventory)
            inventory["entries"] = [v for v in inventory["entries"] if v["owner_id"] in owner_ids]
            if resources is not None:
                resources["pools"] = [v for v in resources["pools"] if v["owner_id"] in owner_ids]
            if casting is not None:
                casting["actors"] = [v for v in casting["actors"] if v["actor_id"] in owner_ids]
            return {
                "inventory_state": inventory,
                "resource_state": resources,
                "casting_state": casting,
                "effect_states": [v for v in effects if v["target_id"] in owner_ids],
            }
        except (ValueError, KeyError, TypeError) as exc:
            raise StateMachineError(
                "invalid_mechanical_state", "Stored component closure is incomplete"
            ) from exc

    def validate_portable_inventory(self, sid, inventory):
        """Validate the complete portable subset before any owner projection."""
        identities, expected = self.world.portable_inventory(sid)
        actual = {
            e["instance_id"]: e for e in inventory["entries"] if e["instance_id"] in identities
        }
        if actual != expected:
            raise StateMachineError(
                "invalid_mechanical_state", "Portable inventory differs from location facts"
            )
        if identities:
            for aid, actor in self.rule_actors(sid).items():
                slugs = {
                    e["item_slug"]
                    for e in inventory["entries"]
                    if e["owner_id"] == aid and e["quantity"] > 0
                }
                if (
                    len(actor["carried_item_slugs"]) != len(slugs)
                    or set(actor["carried_item_slugs"]) != slugs
                ):
                    raise StateMachineError(
                        "invalid_mechanical_state",
                        "Carried projection differs from owned instances",
                    )

    def project_item_transfer(self, sid, before, after):
        self.store.require_write()
        if before["object_id"] != after["object_id"] or before["item_json"] != after["item_json"]:
            raise StateMachineError("invalid_mechanical_state", "Invalid instance transition")
        stored = self._conn.execute(
            "SELECT inventory_json FROM mechanical_components WHERE session_id=?", (sid,)
        ).fetchone()
        inventory = json.loads(stored[0]) if stored else None
        if inventory is None or next(
            (e for e in inventory["entries"] if e["instance_id"] == before["object_id"]), None
        ) != self.world.item_entry(before):
            raise StateMachineError(
                "invalid_mechanical_state", "Item projection precondition differs"
            )
        self.sync_world_inventory(sid)

    def sync_world_inventory(self, sid):
        """Rebuild only the authoritative portable subset; preserve every other entry."""
        self.store.require_write()
        stored = self._conn.execute(
            "SELECT inventory_json FROM mechanical_components WHERE session_id=?", (sid,)
        ).fetchone()
        if stored is None:
            if self._conn.execute(
                "SELECT 1 FROM objects WHERE session_id=? AND item_json IS NOT NULL", (sid,)
            ).fetchone():
                raise StateMachineError(
                    "unsupported_mechanical_version", "Explicit inventory missing"
                )
            return
        inventory = InventoryState.model_validate_json(stored[0]).model_dump(mode="json")
        identities, entries = self.world.portable_inventory(sid)
        old_order = [
            e["instance_id"] for e in inventory["entries"] if e["instance_id"] in identities
        ]
        others = [e for e in inventory["entries"] if e["instance_id"] not in identities]
        inventory["entries"] = others + [entries[i] for i in old_order if i in entries]
        inventory["entries"].extend(entries[i] for i in entries if i not in old_order)
        InventoryState.model_validate_json(canonical_json(inventory))
        self._conn.execute(
            "UPDATE mechanical_components SET inventory_json=? WHERE session_id=?",
            (canonical_json(inventory), sid),
        )
        for actor in self.rule_actors(sid):
            slugs = sorted(
                {
                    e["item_slug"]
                    for e in inventory["entries"]
                    if e["owner_id"] == actor and e["quantity"] > 0
                }
            )
            core = self._conn.execute(
                "SELECT inventory_json FROM characters WHERE session_id=? AND actor_id=?",
                (sid, actor),
            ).fetchone()
            old = json.loads(core[0])
            slugs = [slug for slug in old if slug in slugs] + [
                slug for slug in slugs if slug not in old
            ]
            self._conn.execute(
                "UPDATE characters SET inventory_json=? WHERE session_id=? AND actor_id=?",
                (canonical_json(slugs), sid, actor),
            )
        self.validate_portable_inventory(sid, inventory)

    def initialize_rule_positions(self, row, cells, scene_id):
        """Called only at fresh session creation before events or world movement."""
        self.store.require_write()
        if row["world_version"] != 0 or row["event_seq"] != 0:
            raise StateMachineError(
                "invalid_combat_foundation", "Initial positions require a fresh session"
            )
        for actor_id, cell in cells.items():
            current = self.position(row["session_id"], actor_id)
            if current.scene_id != scene_id:
                raise StateMachineError(
                    "invalid_combat_foundation", "Initial position scenes disagree"
                )
            if current.cell is not None:
                continue
            self._conn.execute(
                "UPDATE actors SET cell=? WHERE session_id=? AND actor_id=?",
                (cell, row["session_id"], actor_id),
            )

    def rule_actors(self, sid):
        result = {}
        for actor in self._conn.execute(
            "SELECT m.actor_id,m.state_json,c.current_hp,c.max_hp,c.inventory_json,"
            "c.resources_json,c.effects_json "
            "FROM mechanical_actors m JOIN characters c USING(session_id,actor_id) "
            "WHERE session_id=?",
            (sid,),
        ):
            if json.loads(actor["resources_json"]) or json.loads(actor["effects_json"]):
                raise StateMachineError(
                    "invalid_mechanical_state", "Legacy core resources/effects have no typed source"
                )
            data = json.loads(actor["state_json"])
            data.update(
                hp_current=actor["current_hp"],
                hp_max=actor["max_hp"],
                carried_item_slugs=json.loads(actor["inventory_json"]),
            )
            result[actor["actor_id"]] = data
        return result

    def position(self, sid, actor_id):
        from .spatial import SpatialPosition

        actor = self._conn.execute(
            "SELECT current_scene_id,cell FROM actors WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()
        if actor is None:
            raise StateMachineError("invalid_actor", "No authorized actor")
        return SpatialPosition(actor[0], actor[1])


class WorldFacts:
    def environment(self, sid, scene_id):
        from .environment_commands import facts_from_data

        row = self._conn.execute(
            "SELECT facts_json FROM scene_environments WHERE session_id=? AND scene_id=?",
            (sid, scene_id),
        ).fetchone()
        return facts_from_data(json.loads(row[0])) if row else None

    def set_environment(self, sid, scene_id, facts):
        from dataclasses import asdict

        from .environment_models import EnvironmentSceneFacts

        self.store.require_write()
        if type(facts) is not EnvironmentSceneFacts:
            raise StateMachineError("invalid_environment", "Expected typed Scene environment facts")
        self._conn.execute(
            "INSERT INTO scene_environments VALUES (?,?,?) "
            "ON CONFLICT(session_id,scene_id) DO UPDATE SET facts_json=excluded.facts_json",
            (sid, scene_id, canonical_json(asdict(facts))),
        )

    def object(self, sid, oid):
        return self._conn.execute(
            "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, oid)
        ).fetchone()

    @staticmethod
    def object_definition(package, oid):
        return next((d for d in package.world_objects if d.object_id == oid), None)

    def __init__(self, store):
        self.store = store

    @property
    def _conn(self):
        return self.store.connection

    def initialize_world(self, sid, package):
        self.store.require_write()
        self._conn.executemany(
            "INSERT OR IGNORE INTO scene_environment_versions(session_id,scene_id) VALUES (?,?)",
            [(sid, scene.scene_id) for scene in package.scenes],
        )
        self._conn.execute(
            "INSERT INTO world_states(session_id,flags_json) VALUES (?,?)",
            (sid, canonical_json(dict(package.world_flags))),
        )
        self._conn.executemany(
            "INSERT INTO world_facts VALUES (?,?,?,?,?)",
            [
                (sid, f.fact_id, f.text, f.visibility, canonical_json(f.authorized_npc_ids))
                for f in package.world_facts
            ],
        )

    def initialize_scene(self, sid, scene_id, package):
        self.store.require_write()
        if self._conn.execute(
            "SELECT 1 FROM scene_instances WHERE session_id=? AND scene_id=?", (sid, scene_id)
        ).fetchone():
            return False
        scene = package.scene(scene_id)
        self._conn.execute(
            "INSERT INTO scene_instances VALUES (?,?,?,0)",
            (sid, scene_id, canonical_json(dict(scene.flags))),
        )
        self._conn.executemany(
            "INSERT INTO objects(session_id,object_id,scene_id,state,cell,orientation) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(session_id,object_id) DO NOTHING",
            [
                (sid, ob.object_id, scene_id, ob.state, ob.initial_cell, ob.initial_orientation)
                for ob in scene.objects
            ],
        )
        return True

    def initialize_object_capabilities(self, sid, scene_id, package):
        self.store.require_write()
        for d in package.world_objects:
            obj = self.object(sid, d.object_id)
            if obj is None or obj["scene_id"] != scene_id:
                continue
            if c := d.capability("mechanism"):
                components = (d, *(self.object_definition(package, oid) for oid in c.components))
                broken = any(
                    definition.capability("durability")
                    and definition.capability("durability").initial_hp == 0
                    for definition in components
                )
                self._conn.execute(
                    "INSERT INTO object_mechanisms VALUES (?,?,?,?,0,NULL,NULL) "
                    "ON CONFLICT(session_id,object_id) DO NOTHING",
                    (sid, d.object_id, "broken" if broken else c.initial_status, c.max_uses),
                )
            if c := d.capability("light"):
                self._conn.execute(
                    "INSERT INTO object_lights VALUES (?,?,?,?) "
                    "ON CONFLICT(session_id,object_id) DO NOTHING",
                    (sid, d.object_id, int(c.initial_enabled), c.initial_mode),
                )
            if c := d.capability("openable"):
                self._conn.execute(
                    "INSERT INTO object_openables VALUES (?,?,?,?) "
                    "ON CONFLICT(session_id,object_id) DO NOTHING",
                    (
                        sid,
                        d.object_id,
                        int(c.initial_jammed),
                        None if c.initial_barred is None else int(c.initial_barred),
                    ),
                )
            if c := d.capability("durability"):
                self._conn.execute(
                    "INSERT INTO object_durability VALUES (?,?,?) "
                    "ON CONFLICT(session_id,object_id) DO NOTHING",
                    (sid, d.object_id, c.initial_hp),
                )

    def mechanism_status(self, sid, oid):
        row = self._conn.execute(
            "SELECT status FROM object_mechanisms WHERE session_id=? AND object_id=?", (sid, oid)
        ).fetchone()
        return row[0] if row else None

    def mechanism_state(self, sid, oid):
        from .mechanism_models import MechanismState

        row = self._conn.execute(
            "SELECT status,remaining_uses,activation_count,reset_at_tick,resume_status "
            "FROM object_mechanisms WHERE session_id=? AND object_id=?",
            (sid, oid),
        ).fetchone()
        return MechanismState(*row) if row else None

    def set_mechanism_state(self, sid, oid, state):
        from .mechanism_models import MechanismState

        self.store.require_write()
        if type(state) is not MechanismState:
            raise StateMachineError("invalid_rule_result", "Typed mechanism state required")
        if not self._conn.execute(
            "UPDATE object_mechanisms SET status=?,remaining_uses=?,activation_count=?,"
            "reset_at_tick=?,resume_status=? WHERE session_id=? AND object_id=?",
            (
                state.status,
                state.remaining_uses,
                state.activation_count,
                state.reset_at_tick,
                state.resume_status,
                sid,
                oid,
            ),
        ).rowcount:
            raise StateMachineError("invalid_target", "Mechanism not initialized")

    def light_state(self, sid, oid):
        row = self._conn.execute(
            "SELECT enabled,mode FROM object_lights WHERE session_id=? AND object_id=?", (sid, oid)
        ).fetchone()
        return {"enabled": bool(row[0]), "mode": row[1]} if row else None

    def set_light_state(self, sid, oid, enabled, mode):
        self.store.require_write()
        if self.light_state(sid, oid) is None or type(enabled) is not bool or type(mode) is not str:
            raise StateMachineError("unsupported_object_capability", "No valid emitter state")
        self._conn.execute(
            "UPDATE object_lights SET enabled=?,mode=? WHERE session_id=? AND object_id=?",
            (int(enabled), mode, sid, oid),
        )

    def openable_state(self, sid, oid):
        row = self._conn.execute(
            "SELECT jammed,barred FROM object_openables WHERE session_id=? AND object_id=?",
            (sid, oid),
        ).fetchone()
        return (
            {"jammed": bool(row[0]), "barred": None if row[1] is None else bool(row[1])}
            if row
            else None
        )

    def set_object_bar(self, sid, oid, barred):
        self.store.require_write()
        obj = self.object(sid, oid)
        state = self.openable_state(sid, oid)
        if state is None or state["barred"] is None:
            raise StateMachineError("unsupported_object_capability", "Object has no bar")
        if self.object_hp(sid, oid) == 0:
            raise StateMachineError("object_destroyed", "Destroyed object is unusable")
        if barred and obj["state"] != "closed":
            raise StateMachineError("object_must_be_closed", "Close object before barring")
        self._conn.execute(
            "UPDATE object_openables SET barred=? WHERE session_id=? AND object_id=?",
            (int(barred), sid, oid),
        )

    def clear_object_jam(self, sid, oid):
        self.store.require_write()
        if self.openable_state(sid, oid) is None:
            raise StateMachineError("unsupported_object_capability", "No jam state")
        if self.object_hp(sid, oid) == 0:
            raise StateMachineError("object_destroyed", "Destroyed object is unusable")
        self._conn.execute(
            "UPDATE object_openables SET jammed=0 WHERE session_id=? AND object_id=?", (sid, oid)
        )

    def object_hp(self, sid, oid):
        row = self._conn.execute(
            "SELECT current_hp FROM object_durability WHERE session_id=? AND object_id=?",
            (sid, oid),
        ).fetchone()
        return row[0] if row else None

    def apply_object_hp(self, sid, oid, expected, value):
        self.store.require_write()
        if (
            type(value) is not int
            or not 0 <= value <= expected
            or not self._conn.execute(
                "UPDATE object_durability SET current_hp=? "
                "WHERE session_id=? AND object_id=? AND current_hp=?",
                (value, sid, oid, expected),
            ).rowcount
        ):
            raise StateMachineError("invalid_rule_result", "Invalid durability transition")

    def visit(self, sid, scene_id):
        self.store.require_write()
        self._conn.execute(
            "UPDATE scene_instances SET visited_count=visited_count+1 "
            "WHERE session_id=? AND scene_id=?",
            (sid, scene_id),
        )

    def place(self, sid, object_id, cell, *, orientation=None):
        self.store.require_write()
        self.require_ordinary_object(sid, object_id)
        self._conn.execute(
            "UPDATE objects SET cell=?,orientation=COALESCE(?,orientation) "
            "WHERE session_id=? AND object_id=?",
            (cell, orientation, sid, object_id),
        )

    def switch(self, sid, object_id, state, *, jam_policy="prevent_transitions"):
        self.store.require_write()
        self.validate_door_switch(sid, object_id, state, jam_policy=jam_policy)
        self._conn.execute(
            "UPDATE objects SET state=? WHERE session_id=? AND object_id=?", (state, sid, object_id)
        )

    def locks_for_object(self, sid, oid):
        return self._conn.execute(
            "SELECT lock_id,locked FROM door_locks WHERE session_id=? AND object_id=? "
            "ORDER BY lock_id",
            (sid, oid),
        ).fetchall()

    def door_lock(self, sid, object_id):
        locks = self.locks_for_object(sid, object_id)
        if not locks:
            return None
        obj = self.object(sid, object_id)
        if (
            obj is None
            or obj["state"] not in ("open", "closed")
            or (any(lock[1] for lock in locks) and obj["state"] != "closed")
        ):
            raise StateMachineError("invalid_lock_state", "Contradictory shared object state")
        return any(bool(lock[1]) for lock in locks)

    def validate_door_switch(
        self, sid, object_id, state, *, jam_policy="prevent_transitions", current_state=None
    ):
        if jam_policy not in ("prevent_transitions", "prevent_opening", "prevent_closing"):
            raise StateMachineError("invalid_rule_result", "Unknown jam policy")
        if state not in ("open", "closed"):
            raise StateMachineError("invalid_rule_result", "Unknown open state")
        if self.object_hp(sid, object_id) == 0:
            raise StateMachineError("object_destroyed", "Destroyed object is unusable")
        current = self.object(sid, object_id)
        openable = self.openable_state(sid, object_id)
        if openable and (current["state"] if current_state is None else current_state) != state:
            if openable["jammed"] and (
                jam_policy == "prevent_transitions"
                or (jam_policy == "prevent_opening" and state == "open")
                or (jam_policy == "prevent_closing" and state == "closed")
            ):
                raise StateMachineError("object_jammed", "Object is jammed")
            if openable["barred"]:
                raise StateMachineError("object_barred", "Object is barred")
        locked = self.door_lock(sid, object_id)
        if locked and state == "open":
            raise StateMachineError("door_locked", "Door is locked")

    def set_door_lock(self, sid, object_id, locked, lock_id=None):
        self.store.require_write()
        if self.door_lock(sid, object_id) is None:
            raise StateMachineError("unsupported_object_capability", "Door has no lock")
        state = self._conn.execute(
            "SELECT state FROM objects WHERE session_id=? AND object_id=?", (sid, object_id)
        ).fetchone()[0]
        if locked and state != "closed":
            raise StateMachineError("door_must_be_closed", "Close the door before locking")
        locks = self.locks_for_object(sid, object_id)
        if lock_id is None:
            if len(locks) != 1:
                raise StateMachineError("ambiguous_lock", "Explicit lock identity required")
            lock_id = locks[0][0]
        if lock_id not in {lock[0] for lock in locks}:
            raise StateMachineError("invalid_target", "Lock does not belong to object")
        if self.object_hp(sid, object_id) == 0:
            raise StateMachineError("object_destroyed", "Destroyed object is unusable")
        self._conn.execute(
            "UPDATE door_locks SET locked=? WHERE session_id=? AND object_id=? AND lock_id=?",
            (int(locked), sid, object_id, lock_id),
        )

    def initialize_locks(self, row, package):
        self.store.require_write()
        if row["world_version"] or row["event_seq"]:
            raise StateMachineError("invalid_lock_foundation", "Locks require a fresh session")
        for d in package.world_objects:
            c = d.capability("locks")
            if c:
                for lock in c.locks:
                    self._conn.execute(
                        "INSERT INTO door_locks VALUES (?,?,?,?)",
                        (row["session_id"], d.object_id, int(lock.initial_locked), lock.lock_id),
                    )
                    self._conn.executemany(
                        "INSERT INTO door_credentials VALUES (?,?,?,?)",
                        [
                            (row["session_id"], d.object_id, iid, lock.lock_id)
                            for iid in lock.credential_instance_ids
                        ],
                    )
        for door in package.lockable_doors:
            self._conn.execute(
                "INSERT INTO door_locks VALUES (?,?,?,?)",
                (
                    row["session_id"],
                    door.object_id,
                    int(door.initial_locked),
                    "lock." + door.object_id,
                ),
            )
            self._conn.executemany(
                "INSERT INTO door_credentials VALUES (?,?,?,?)",
                [
                    (row["session_id"], door.object_id, iid, "lock." + door.object_id)
                    for iid in door.credential_instance_ids
                ],
            )

    def accepts_credential(self, sid, object_id, instance_id, lock_id=None):
        return (
            self._conn.execute(
                "SELECT 1 FROM door_credentials WHERE session_id=? AND object_id=? "
                "AND instance_id=? AND (? IS NULL OR lock_id=?)",
                (sid, object_id, instance_id, lock_id, lock_id),
            ).fetchone()
            is not None
        )

    def require_ordinary_object(self, sid, object_id):
        row = self._conn.execute(
            "SELECT item_json FROM objects WHERE session_id=? AND object_id=?", (sid, object_id)
        ).fetchone()
        if row and row[0] is not None:
            raise StateMachineError(
                "unsupported_object_capability", "Portable instances require item transfer"
            )

    def initialize_items(self, row, package, loader):
        self.store.require_write()
        sid = row["session_id"]
        if row["world_version"] or row["event_seq"]:
            raise StateMachineError(
                "invalid_item_foundation", "Item initialization requires a fresh session"
            )
        for item in package.portable_items:
            if loader.get_item(item.item_slug) is None:
                raise StateMachineError("invalid_item_foundation", "Unknown pinned mechanical item")
            owner = (
                row["pc_actor_id"]
                if item.initial_owner_id == row["pc_template_id"]
                else item.initial_owner_id
            )
            data = {
                "item_slug": item.item_slug,
                "quantity": item.quantity,
                "charges_remaining_per_unit": item.charges_remaining_per_unit,
                "accessible": True,
            }
            old = self._conn.execute(
                "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, item.instance_id)
            ).fetchone()
            if old is None or old["item_json"] is not None:
                raise StateMachineError(
                    "invalid_item_foundation", "Portable instance must exist exactly once"
                )
            self._conn.execute(
                "UPDATE objects SET item_json=?,carrier_id=?,scene_id=?,cell=? "
                "WHERE session_id=? AND object_id=?",
                (
                    canonical_json(data),
                    owner,
                    None if owner else old["scene_id"],
                    None if owner else old["cell"],
                    sid,
                    item.instance_id,
                ),
            )

    def placement(self, sid, oid, *, row=None):
        obj = row if row is not None else self.object(sid, oid)
        if obj is None:
            raise StateMachineError("invalid_target", "Missing object")
        ancestry, seen = [], {oid}
        current = obj
        while current["container_id"] is not None:
            parent = current["container_id"]
            if parent in seen or len(ancestry) >= 8:
                raise StateMachineError("invalid_world_state", "Invalid containment ancestry")
            seen.add(parent)
            ancestry.append(parent)
            current = self.object(sid, parent)
            if current is None:
                raise StateMachineError("invalid_world_state", "Missing parent container")
        if current["carrier_id"] is not None:
            position = self._conn.execute(
                "SELECT current_scene_id,cell FROM actors WHERE session_id=? AND actor_id=?",
                (sid, current["carrier_id"]),
            ).fetchone()
            scene, cell = position
        else:
            scene, cell = current["scene_id"], current["cell"]
        return {
            "scene_id": scene,
            "cell": cell,
            "carrier_id": current["carrier_id"],
            "container_id": obj["container_id"],
            "ancestry": tuple(ancestry),
        }

    def item_accessible(self, sid, oid, *, row=None, include_self=True):
        from .world_openable import OpenableService

        placement = self.placement(sid, oid, row=row)
        if include_self and self.object_hp(sid, oid) == 0:
            return False
        for parent in placement["ancestry"]:
            if not OpenableService.allows_contents(self, sid, parent):
                return False
        return True

    def contents(self, sid, oid):
        return self._conn.execute(
            "SELECT * FROM objects WHERE session_id=? AND container_id=? ORDER BY object_id",
            (sid, oid),
        ).fetchall()

    def item_entry(self, row):
        sid, oid = row["session_id"], row["object_id"]
        placement = self.placement(sid, oid, row=row)
        if placement["carrier_id"] is None:
            return None
        data = json.loads(row["item_json"])
        data["accessible"] = data["accessible"] and self.item_accessible(sid, oid, row=row)
        return {"instance_id": oid, "owner_id": placement["carrier_id"], **data}

    def portable_inventory(self, sid):
        identities, entries = set(), {}
        for row in self._conn.execute(
            "SELECT * FROM objects WHERE session_id=? AND item_json IS NOT NULL", (sid,)
        ):
            identities.add(row["object_id"])
            data = json.loads(row["item_json"])
            # Ground metadata is validated without constructing a fictional owner.
            if not valid_item_unit(data):
                raise StateMachineError(
                    "invalid_mechanical_state", "Portable unit metadata is invalid"
                )
            if entry := self.item_entry(row):
                entries[row["object_id"]] = entry
        return identities, entries

    def transfer_item(
        self, sid, before, *, carrier_id=None, scene_id=None, cell=None, container_id=None
    ):
        self.store.require_write()
        current = self._conn.execute(
            "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, before["object_id"])
        ).fetchone()
        if current is None or dict(current) != dict(before) or current["item_json"] is None:
            raise StateMachineError("position_conflict", "Item location changed")
        self._conn.execute(
            "UPDATE objects SET carrier_id=?,scene_id=?,cell=?,container_id=? "
            "WHERE session_id=? AND object_id=?",
            (carrier_id, scene_id, cell, container_id, sid, before["object_id"]),
        )
        return self._conn.execute(
            "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, before["object_id"])
        ).fetchone()

    def object_position(self, sid, object_id):
        obj = self.object(sid, object_id)
        return (
            {
                **self.placement(sid, object_id),
                "state": obj["state"],
                "orientation": obj["orientation"],
            }
            if obj
            else None
        )

    def scene_flags(self, sid, scene_id):
        row = self._conn.execute(
            "SELECT flags_json FROM scene_instances WHERE session_id=? AND scene_id=?",
            (sid, scene_id),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set_scene_flags(self, sid, scene_id, flags):
        self.store.require_write()
        self._conn.execute(
            "UPDATE scene_instances SET flags_json=? WHERE session_id=? AND scene_id=?",
            (canonical_json(flags), sid, scene_id),
        )

    def set_tick(self, sid, tick):
        self.store.require_write()
        self._conn.execute("UPDATE world_states SET game_tick=? WHERE session_id=?", (tick, sid))

    def initialize_time_binding(self, sid, binding):
        from dataclasses import asdict

        self.store.require_write()
        self._conn.execute(
            "INSERT INTO world_time_bindings VALUES (?,?)", (sid, canonical_json(asdict(binding)))
        )

    def time_state(self, sid):
        from .time_binding import LegacyTimeBinding, time_binding_from_data

        row = self._conn.execute(
            "SELECT w.game_tick,b.binding_json FROM world_states w "
            "LEFT JOIN world_time_bindings b USING(session_id) WHERE w.session_id=?",
            (sid,),
        ).fetchone()
        if row is None:
            raise StateMachineError("session_not_found", "Unknown clock")
        binding = time_binding_from_data(json.loads(row[1])) if row[1] else LegacyTimeBinding()
        return binding, row[0]

    def initialize_rule_scene(self, sid, scene):
        self.store.require_write()
        self._conn.execute(
            "INSERT INTO mechanical_scenes(session_id,scene_id,state_json) VALUES (?,?,?)",
            (sid, scene.scene_id, scene.model_dump_json()),
        )

    def initialize_mechanical_scene(self, sid, scene_id, package):
        self.store.require_write()
        definition = package.scene(scene_id)
        if definition.grid_json is not None:
            self._conn.execute(
                "INSERT INTO mechanical_scenes(session_id,scene_id,state_json,spatial_enabled) "
                "SELECT ?,?,?,1 WHERE NOT EXISTS "
                "(SELECT 1 FROM mechanical_scenes WHERE session_id=? AND scene_id=?)",
                (
                    sid,
                    scene_id,
                    canonical_json(
                        {"scene_id": scene_id, "grid": json.loads(definition.grid_json)}
                    ),
                    sid,
                    scene_id,
                ),
            )
            return
        if package.noncombat_foundation_json is None:
            return
        initial = json.loads(package.noncombat_foundation_json)
        scenes = [initial["snapshot"]["scene_state"], *initial["additional_scenes"]]
        scene = next((s for s in scenes if s["scene_id"] == scene_id), None)
        if scene is not None:
            self._conn.execute(
                "INSERT INTO mechanical_scenes(session_id,scene_id,state_json) "
                "SELECT ?,?,? WHERE NOT EXISTS "
                "(SELECT 1 FROM mechanical_scenes WHERE session_id=? AND scene_id=?)",
                (sid, scene_id, canonical_json(scene), sid, scene_id),
            )

    def spatial_scene(self, sid, scene_id):
        scene = self.maps.scene(sid, scene_id, spatial_only=True)
        return scene.copy_data() if scene else None

    @property
    def maps(self):
        # Grid owns the representation; Store owns its bounded lifetime. All
        # Facts/Reader instances on this connection use the same disposable pool.
        from .grid_maps import GridMaps

        if self.store._grid_maps is None:
            self.store._grid_maps = GridMaps(self.store)
        return self.store._grid_maps

    def mechanical_scene(self, sid, scene_id):
        scene = self.maps.scene(sid, scene_id)
        return scene.copy_data() if scene else None


class CombatRecords:
    RULE_FIELDS = frozenset(
        (
            "current_turn_index",
            "round_number",
            "turn_serial",
            "monster_turn_start_done",
            "last_ended_turn",
            "departed_actor_id",
            "ended",
            "damage_instance_sequence",
            "processed_zero_hp_damage_instances",
            "dead_ids",
            "deaths_recorded",
        )
    )

    def __init__(self, store):
        self.store = store

    @property
    def _conn(self):
        return self.store.connection

    def initialize(self, sid, snapshot, *, spatial, process_id):
        self.store.require_write()
        self._conn.execute(
            "INSERT INTO combats VALUES (?,?,?,?,?)",
            (
                sid,
                snapshot.combat_state.combat_id,
                snapshot.scene_state.scene_id,
                snapshot.combat_state.model_dump_json(exclude={"actor_zone"}),
                process_id,
            ),
        )

    def freeze_positions(self, sid, cells):
        self.store.require_write()
        self._conn.execute(
            "UPDATE combats SET state_json=json_set(state_json,'$.actor_zone',json(?)) "
            "WHERE session_id=?",
            (canonical_json(cells), sid),
        )

    def update_rule_state(self, sid, field, value):
        self.store.require_write()
        if field not in self.RULE_FIELDS:
            raise StateMachineError("invalid_rule_result", "Unsupported combat fact")
        self._conn.execute(
            "UPDATE combats SET state_json=json_set(state_json,?,json(?)) WHERE session_id=?",
            ("$." + field, canonical_json(value), sid),
        )

    def update_ledger(self, sid, actor_id, value):
        self.store.require_write()
        self._conn.execute(
            "UPDATE combats SET state_json=json_set(state_json,?,json(?)) WHERE session_id=?",
            ("$.movement_ledgers." + canonical_json(actor_id), canonical_json(value), sid),
        )

    def active(self, session_id):
        combat = self._conn.execute(
            "SELECT state_json FROM combats WHERE session_id=?", (session_id,)
        ).fetchone()
        return bool(combat and not json.loads(combat[0])["ended"])

    def require_noncombat_save(self, row):
        combat = self._conn.execute(
            "SELECT state_json FROM combats WHERE session_id=?", (row["session_id"],)
        ).fetchone()
        if combat and (not json.loads(combat[0])["ended"]):
            raise StateMachineError(
                "combat_unrestorable", "MVP does not support active-combat save/resume"
            )
