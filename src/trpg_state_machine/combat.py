"""Pinned Engine schema adapter. SQLite components are the only durable authority.

This is a restricted, authored initial encounter, not the proposed combat.start API.
No initiative calculation, registry handle, snapshot replacement, or rule formula.
"""

import json
import os
from dataclasses import asdict, dataclass
from uuid import uuid4

from dnd5e_engine.evaluation_preflight import snapshot_support_failure, template_support_failure
from dnd5e_engine.evaluation_rng import RNGContext, RNGState
from dnd5e_engine.evaluation_ruleset import RulesetBinding, RulesetBindingError, verify_ruleset
from dnd5e_engine.evaluation_state import CombatSnapshot, NonCombatSnapshot, SceneState
from pydantic import ValidationError

from .errors import StateMachineError
from .package import canonical_json

DND_RULESET = "dnd-2024-srd-5.2.1"
# Eligibility to continue in this process is transient, never a game-state cache.
_PROCESS_TOKEN = uuid4().hex


def process_id():
    return f"{os.getpid()}:{_PROCESS_TOKEN}"


@dataclass(frozen=True, slots=True)
class InitializeCombat:
    """Initialize the one reviewed encounter in the pinned Adventure Package."""


def validate_foundation(value):
    """Validate all required Engine fields, including explicit empty components."""
    if type(value) is not dict or set(value) != {"snapshot", "rng_state"}:
        raise StateMachineError("invalid_combat_foundation", "Expected snapshot and rng_state")
    try:
        snapshot = CombatSnapshot.model_validate_json(canonical_json(value["snapshot"]))
        RNGState.model_validate_json(canonical_json(value["rng_state"]))
    except (ValidationError, ValueError, TypeError) as exc:
        raise StateMachineError("invalid_combat_foundation", "Incomplete Engine state") from exc
    if snapshot.combat_state.event_log or snapshot.world_version != 0:
        raise StateMachineError(
            "invalid_combat_foundation", "Initial history/version must be empty"
        )
    if snapshot.combat_state.ended:
        raise StateMachineError("invalid_combat_foundation", "An initial encounter must be active")
    if reason := snapshot_support_failure(snapshot):
        raise StateMachineError("unsupported_combat_foundation", reason)
    return snapshot


def validate_noncombat_foundation(value):
    if type(value) is not dict or set(value) != {
        "snapshot",
        "rng_state",
        "xp",
        "additional_scenes",
    }:
        raise StateMachineError("invalid_noncombat_foundation", "Expected complete initial facts")
    try:
        snapshot = NonCombatSnapshot.model_validate_json(canonical_json(value["snapshot"]))
        RNGState.model_validate_json(canonical_json(value["rng_state"]))
        scenes = [
            SceneState.model_validate_json(canonical_json(scene))
            for scene in value["additional_scenes"]
        ]
        ids = [snapshot.scene_state.scene_id, *(s.scene_id for s in scenes)]
        if (
            snapshot.snapshot_schema_version != "engine-snapshot/2"
            or snapshot.world_version != 0
            or not snapshot.character_states
            or len(ids) != len(set(ids))
            or type(value["xp"]) is not dict
            or set(value["xp"]) != {a.entity_id for a in snapshot.character_states}
            or any(
                type(xp) is not int or not 0 <= xp <= 1_000_000_000 for xp in value["xp"].values()
            )
        ):
            raise ValueError("Invalid schema/version, scenes or XP owners")
    except (ValidationError, ValueError, TypeError) as exc:
        raise StateMachineError(
            "invalid_noncombat_foundation", "Incomplete mechanical facts"
        ) from exc
    if snapshot.effect_states:
        raise StateMachineError(
            "unsupported_noncombat_foundation", "Effect persistence is not admitted"
        )
    return snapshot


def _remap(value, old, new):
    if isinstance(value, dict):
        return {new if k == old else k: _remap(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [_remap(v, old, new) for v in value]
    return new if value == old else value


class CombatRuntime:
    def _initialize_mechanical_scene(self, sid, scene_id, package):
        if package.noncombat_foundation_json is None:
            return
        initial = json.loads(package.noncombat_foundation_json)
        scenes = [initial["snapshot"]["scene_state"], *initial["additional_scenes"]]
        scene = next((s for s in scenes if s["scene_id"] == scene_id), None)
        if scene is not None:
            self._conn.execute(
                "INSERT INTO mechanical_scenes SELECT ?,?,? WHERE NOT EXISTS "
                "(SELECT 1 FROM mechanical_scenes WHERE session_id=? AND scene_id=?)",
                (sid, scene_id, canonical_json(scene), sid, scene_id),
            )

    def _initialize_noncombat(self, row, package):
        initial = _remap(
            json.loads(package.noncombat_foundation_json), row["pc_template_id"], row["pc_actor_id"]
        )
        snapshot = validate_noncombat_foundation(initial)
        if {a.entity_id for a in snapshot.character_states if a.entity_type == "Character"} != {
            row["pc_actor_id"]
        }:
            raise StateMachineError("invalid_noncombat_foundation", "Expected the selected PC only")
        for actor in snapshot.character_states:
            self._store_initial_mechanical_actor(row, actor, xp=initial["xp"][actor.entity_id])
        self._conn.execute(
            "UPDATE sessions SET mechanical_schema_version=? WHERE session_id=?",
            (snapshot.snapshot_schema_version, row["session_id"]),
        )
        self._conn.execute(
            "UPDATE rng_states SET algorithm='stdlib-mt19937',encoding='stdlib-mt19937/1',"
            "state_json=? WHERE session_id=? AND algorithm='unselected'",
            (canonical_json(initial["rng_state"]), row["session_id"]),
        )

    def _store_initial_mechanical_actor(self, row, actor, *, xp=0):
        sid = row["session_id"]
        core = self._conn.execute(
            "SELECT * FROM characters WHERE session_id=? AND actor_id=?", (sid, actor.entity_id)
        ).fetchone()
        if core is not None:
            if (
                (core["current_hp"], core["max_hp"]) != (actor.hp_current, actor.hp_max)
                or json.loads(core["inventory_json"]) != list(actor.carried_item_slugs)
                or json.loads(core["resources_json"])
                or json.loads(core["effects_json"])
            ):
                raise StateMachineError(
                    "invalid_combat_foundation", "Initialization cannot reset core state"
                )
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
                    xp,
                ),
            )
        state = actor.model_dump(
            mode="json", exclude={"hp_current", "hp_max", "carried_item_slugs"}
        )
        self._conn.execute(
            "INSERT INTO mechanical_actors VALUES (?,?,?)",
            (sid, actor.entity_id, canonical_json(state)),
        )

    def _mechanical_actor_data(self, sid):
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

    def _noncombat_snapshot(self, row):
        self._require_noncombat_save(row)
        sid = row["session_id"]
        if row["mechanical_schema_version"] != "engine-snapshot/2":
            raise StateMachineError(
                "unsupported_mechanical_version", "Complete noncombat state was not initialized"
            )
        actors = self._mechanical_actor_data(sid)
        present = [
            a[0]
            for a in self._conn.execute(
                "SELECT actor_id FROM actors WHERE session_id=? AND current_scene_id=? "
                "ORDER BY actor_id",
                (sid, row["current_scene_id"]),
            )
        ]
        scene = self._conn.execute(
            "SELECT state_json FROM mechanical_scenes WHERE session_id=? AND scene_id=?",
            (sid, row["current_scene_id"]),
        ).fetchone()
        try:
            return NonCombatSnapshot.model_validate_json(
                canonical_json(
                    {
                        "snapshot_kind": "non_combat",
                        "snapshot_schema_version": row["mechanical_schema_version"],
                        "session_id": sid,
                        "world_version": row["world_version"],
                        "character_states": [actors[a] for a in present],
                        "effect_states": [],
                        "scene_state": json.loads(scene[0]),
                    }
                )
            )
        except (ValidationError, KeyError, TypeError) as exc:
            raise StateMachineError(
                "invalid_mechanical_state", "Current scene lacks complete mechanical facts"
            ) from exc

    def read_noncombat_snapshot(self, session_id, package, *, context):
        self._verify_dnd_binding(package)
        with self._transaction():
            return self._noncombat_snapshot(self._session(session_id, package, context))

    def _verify_dnd_binding(self, package):
        if package.ruleset_binding.ruleset_id != DND_RULESET:
            raise StateMachineError("unsupported_ruleset", "Expected a D&D evaluation package")
        try:
            verify_ruleset(RulesetBinding(**asdict(package.ruleset_binding)), self._dnd_loader)
        except RulesetBindingError as exc:
            raise StateMachineError(
                "ruleset_binding_mismatch", "Evaluator data/configuration differ"
            ) from exc

    def _prepare_combat(self, row, package):
        self._verify_dnd_binding(package)  # All corpus hashing is outside a write transaction.
        if package.combat_foundation_json is None:
            raise StateMachineError("unsupported_operation", "Package has no reviewed encounter")
        data = json.loads(package.combat_foundation_json)
        original = validate_foundation(data)
        if original.scene_state.scene_id != row["current_scene_id"]:
            raise StateMachineError(
                "invalid_combat_foundation", "Encounter must be in its authored scene"
            )
        if original.combat_state.party_ids != {row["pc_template_id"]}:
            raise StateMachineError(
                "invalid_combat_foundation", "Encounter must use selected PC template"
            )
        data = _remap(data, row["pc_template_id"], row["pc_actor_id"])
        data["snapshot"]["session_id"] = row["session_id"]
        data["snapshot"]["world_version"] = row["world_version"]
        if package.noncombat_foundation_json is not None:
            # Only authored combat components are new. Actor facts and RNG already
            # belong to SQLite, even before the first encounter.
            actors = self._mechanical_actor_data(row["session_id"])
            try:
                data["snapshot"]["character_states"] = [
                    actors[a] for a in data["snapshot"]["combat_state"]["initiative_ids"]
                ]
                scene = self._conn.execute(
                    "SELECT state_json FROM mechanical_scenes WHERE session_id=? AND scene_id=?",
                    (row["session_id"], row["current_scene_id"]),
                ).fetchone()
                data["snapshot"]["scene_state"] = json.loads(scene[0])
            except (KeyError, TypeError) as exc:
                raise StateMachineError(
                    "invalid_mechanical_state", "Encounter dependencies are missing"
                ) from exc
        snapshot = CombatSnapshot.model_validate_json(canonical_json(data["snapshot"]))
        if snapshot.scene_state.scene_id != row["current_scene_id"]:
            raise StateMachineError(
                "invalid_combat_foundation", "Encounter must be in current scene"
            )
        if reason := template_support_failure(snapshot, self._dnd_loader):
            raise StateMachineError("unsupported_combat_foundation", reason)
        return snapshot, RNGState.model_validate_json(canonical_json(data["rng_state"]))

    def _initialize_combat(self, row, package, prepared):
        sid = row["session_id"]
        if self._conn.execute("SELECT 1 FROM combats WHERE session_id=?", (sid,)).fetchone():
            raise StateMachineError("combat_already_initialized", "Encounter cannot be reset")
        snapshot, rng = prepared
        for actor in snapshot.character_states:
            existing = self._conn.execute(
                "SELECT * FROM actors WHERE session_id=? AND actor_id=?", (sid, actor.entity_id)
            ).fetchone()
            if existing is None or existing["current_scene_id"] != row["current_scene_id"]:
                raise StateMachineError(
                    "invalid_combat_foundation", "Encounter actor must already be present"
                )
            is_pc = actor.entity_id == row["pc_actor_id"]
            if is_pc != (actor.entity_type == "Character"):
                raise StateMachineError(
                    "invalid_combat_foundation", "Actor kind differs from Session"
                )
            if package.noncombat_foundation_json is not None:
                continue
            core = self._conn.execute(
                "SELECT * FROM characters WHERE session_id=? AND actor_id=?", (sid, actor.entity_id)
            ).fetchone()
            if core:
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
            else:
                self._conn.execute(
                    "INSERT INTO characters(session_id,actor_id,current_hp,max_hp,resources_json,"
                    "inventory_json,effects_json) VALUES (?,?,?,?,?,?,?)",
                    (
                        sid,
                        actor.entity_id,
                        actor.hp_current,
                        actor.hp_max,
                        "{}",
                        canonical_json(actor.carried_item_slugs),
                        "[]",
                    ),
                )
            state = actor.model_dump(
                mode="json", exclude={"hp_current", "hp_max", "carried_item_slugs"}
            )
            self._conn.execute(
                "INSERT INTO mechanical_actors VALUES (?,?,?)",
                (sid, actor.entity_id, canonical_json(state)),
            )
        if package.noncombat_foundation_json is None:
            self._conn.execute(
                "INSERT INTO mechanical_scenes VALUES (?,?,?)",
                (sid, snapshot.scene_state.scene_id, snapshot.scene_state.model_dump_json()),
            )
        self._conn.execute(
            "UPDATE sessions SET mechanical_schema_version=? WHERE session_id=?",
            (snapshot.snapshot_schema_version, sid),
        )
        self._conn.execute(
            "INSERT INTO combats VALUES (?,?,?,?,?)",
            (
                sid,
                snapshot.combat_state.combat_id,
                snapshot.scene_state.scene_id,
                snapshot.combat_state.model_dump_json(exclude={"event_log"}),
                process_id(),
            ),
        )
        self._conn.execute(
            "UPDATE rng_states SET algorithm='stdlib-mt19937',encoding='stdlib-mt19937/1',"
            "position=0,state_json=? WHERE session_id=? AND algorithm='unselected'",
            (rng.model_dump_json(), sid),
        )
        return [
            self._capture_event(
                sid,
                package,
                "combat.initialized",
                {"scene_id": snapshot.scene_state.scene_id},
                phase="combat.after_initialization",
            )
        ]

    def _combat_snapshot(self, row):
        sid = row["session_id"]
        combat = self._conn.execute("SELECT * FROM combats WHERE session_id=?", (sid,)).fetchone()
        if combat is None:
            raise StateMachineError("combat_not_initialized", "Session has no mechanical encounter")
        state = json.loads(combat["state_json"])
        # Mechanical history comes from committed events, never a second mutable log.
        state["event_log"] = [
            json.loads(e[0])["engine_event"]
            for e in self._conn.execute(
                "SELECT payload_json FROM events WHERE session_id=? "
                "AND event_type='combat.engine_event' ORDER BY event_seq",
                (sid,),
            )
        ]
        actors = self._mechanical_actor_data(sid)
        scene = self._conn.execute(
            "SELECT state_json FROM mechanical_scenes WHERE session_id=? AND scene_id=?",
            (sid, combat["scene_id"]),
        ).fetchone()
        try:
            return CombatSnapshot.model_validate_json(
                canonical_json(
                    {
                        "snapshot_schema_version": row["mechanical_schema_version"]
                        or "engine-snapshot/1",
                        "snapshot_kind": "combat",
                        "session_id": sid,
                        "world_version": row["world_version"],
                        "character_states": [actors[a] for a in state["initiative_ids"]],
                        "effect_states": [],
                        "scene_state": json.loads(scene[0]),
                        "combat_state": state,
                    }
                )
            )
        except (ValidationError, KeyError, TypeError) as exc:
            raise StateMachineError(
                "invalid_mechanical_state", "Stored Engine state is incomplete"
            ) from exc

    def _rng_context(self, row):
        rng = self._conn.execute(
            "SELECT * FROM rng_states WHERE session_id=?", (row["session_id"],)
        ).fetchone()
        if rng["algorithm"] != "stdlib-mt19937" or rng["encoding"] != "stdlib-mt19937/1":
            raise StateMachineError("invalid_rng_state", "Expected pinned Engine RNG encoding")
        try:
            return RNGContext(
                stream_id=rng["state_id"],
                version=rng["rng_version"],
                state=RNGState.model_validate_json(rng["state_json"]),
            )
        except (ValidationError, ValueError) as exc:
            raise StateMachineError("invalid_rng_state", "Invalid stored RNG state") from exc

    def read_combat_snapshot(self, session_id, package, *, context):
        """Trusted audit/read boundary; this does not authorize cross-process continuation."""
        if package.ruleset_binding.evaluator_version != "dnd5e-evaluation/4":
            self._verify_dnd_binding(package)
        with self._transaction():
            row = self._session(session_id, package, context)
            return self._combat_snapshot(row)

    def read_rng_context(self, session_id, package, *, context):
        if package.ruleset_binding.evaluator_version != "dnd5e-evaluation/4":
            self._verify_dnd_binding(package)
        with self._transaction():
            return self._rng_context(self._session(session_id, package, context))

    def _require_noncombat_save(self, row):
        combat = self._conn.execute(
            "SELECT state_json FROM combats WHERE session_id=?", (row["session_id"],)
        ).fetchone()
        if combat and not json.loads(combat[0])["ended"]:
            raise StateMachineError(
                "combat_unrestorable", "MVP does not support active-combat save/resume"
            )
