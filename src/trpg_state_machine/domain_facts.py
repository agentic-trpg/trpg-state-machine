"""Controlled current facts. These services share the caller's SQLite scope.

No method commits, computes rules, or keeps an authoritative in-memory copy.
Rules metadata edits have a closed field set matching the pinned adapter.
"""

import json

from .errors import StateMachineError
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

    def __init__(self, store):
        self.store = store

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

    def rule_actors(self, sid):
        result = {}
        for actor in self._conn.execute(
            "SELECT m.actor_id,m.state_json,c.current_hp,c.max_hp,c.inventory_json "
            "FROM mechanical_actors m JOIN characters c USING(session_id,actor_id) "
            "WHERE session_id=?",
            (sid,),
        ):
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
    def __init__(self, store):
        self.store = store

    @property
    def _conn(self):
        return self.store.connection

    def initialize_world(self, sid, package):
        self.store.require_write()
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
            "INSERT INTO scene_instances VALUES (?,?,?,1)",
            (sid, scene_id, canonical_json(dict(scene.flags))),
        )
        self._conn.executemany(
            "INSERT INTO objects(session_id,object_id,scene_id,state,cell) VALUES (?,?,?,?,?)",
            [(sid, ob.object_id, scene_id, ob.state, ob.initial_cell) for ob in scene.objects],
        )
        return True

    def mark_unvisited(self, sid, scene_id):
        self.store.require_write()
        self._conn.execute(
            "UPDATE scene_instances SET visited_count=0 WHERE session_id=? AND scene_id=?",
            (sid, scene_id),
        )

    def visit(self, sid, scene_id):
        self.store.require_write()
        self._conn.execute(
            "UPDATE scene_instances SET visited_count=visited_count+1 "
            "WHERE session_id=? AND scene_id=?",
            (sid, scene_id),
        )

    def place(self, sid, object_id, cell):
        self.store.require_write()
        self._conn.execute(
            "UPDATE objects SET cell=? WHERE session_id=? AND object_id=?", (cell, sid, object_id)
        )

    def switch(self, sid, object_id, state):
        self.store.require_write()
        self._conn.execute(
            "UPDATE objects SET state=? WHERE session_id=? AND object_id=?", (state, sid, object_id)
        )

    def set_scene_flags(self, sid, scene_id, flags):
        self.store.require_write()
        self._conn.execute(
            "UPDATE scene_instances SET flags_json=? WHERE session_id=? AND scene_id=?",
            (canonical_json(flags), sid, scene_id),
        )

    def set_tick(self, sid, tick):
        self.store.require_write()
        self._conn.execute("UPDATE world_states SET game_tick=? WHERE session_id=?", (tick, sid))

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
        scene = self._conn.execute(
            "SELECT state_json FROM mechanical_scenes "
            "WHERE session_id=? AND scene_id=? AND spatial_enabled=1",
            (sid, scene_id),
        ).fetchone()
        return json.loads(scene[0]) if scene else None


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
                snapshot.combat_state.model_dump_json(
                    exclude={"event_log", "actor_zone"} if spatial else {"event_log"}
                ),
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
