"""Real pinned evaluate(), closed delta validation and one SQLite commit boundary."""

import json

from dnd5e_engine.evaluation_contracts import (
    CheckPayload,
    CombatClosePayload,
    CombatIntentPayload,
)

from .combat import (
    _remap,
    process_id,
    validate_noncombat_foundation,
)
from .commands import command_fingerprint, validate_command
from .errors import StateMachineError
from .package import canonical_json


class CombatHandler:
    """Current authored encounter/check/attack/closure orchestration, no formulas."""

    def __init__(
        self,
        store,
        sessions,
        characters,
        facts,
        records,
        world,
        rules,
        capture,
        effects,
        persistence,
    ):
        self.store = store
        self.sessions = sessions
        self.characters = characters
        self.facts = facts
        self.records = records
        self.world = world
        self.rules = rules
        self.capture = capture
        self.effects = effects
        self.persistence = persistence

    @property
    def _conn(self):
        return self.store.connection

    def _initialize_noncombat(self, row, package):
        self.store.require_write()
        initial = _remap(
            json.loads(package.noncombat_foundation_json), row["pc_template_id"], row["pc_actor_id"]
        )
        snapshot = validate_noncombat_foundation(initial)
        if {a.entity_id for a in snapshot.character_states if a.entity_type == "Character"} != {
            row["pc_actor_id"]
        }:
            raise StateMachineError("invalid_noncombat_foundation", "Expected the selected PC only")
        for actor in snapshot.character_states:
            self.characters.initialize_rule_actor(row, actor, xp=initial["xp"][actor.entity_id])
        self.characters.initialize_rule_components(row["session_id"], snapshot)
        self.sessions.set_mechanical_schema(row["session_id"], "engine-snapshot/17")
        self.sessions.select_rule_rng(row["session_id"], canonical_json(initial["rng_state"]))

    def _initialize_combat(self, row, package, prepared):
        self.store.require_write()
        sid = row["session_id"]
        if self._conn.execute("SELECT 1 FROM combats WHERE session_id=?", (sid,)).fetchone():
            raise StateMachineError("combat_already_initialized", "Encounter cannot be reset")
        snapshot, rng = prepared
        spatial = self.facts.spatial_scene(sid, row["current_scene_id"])
        if snapshot.world_version != row["world_version"]:
            raise StateMachineError("version_conflict", "Combat preparation is stale")
        if spatial is not None:
            self.world._spatial_combat_guard(sid, row["current_scene_id"], package)
        if snapshot.combat_state.actor_zone != self.rules._current_cells(
            sid,
            row["current_scene_id"],
            snapshot.combat_state.initiative_ids,
            snapshot.scene_state.grid.model_dump(),
        ):
            raise StateMachineError(
                "version_conflict", "Combat positions changed during preparation"
            )
        current_actors = (
            self.characters.rule_actors(sid)
            if package.noncombat_foundation_json is not None
            else {}
        )
        for actor in snapshot.character_states:
            if current_actors and current_actors.get(actor.entity_id) != actor.model_dump(
                mode="json"
            ):
                raise StateMachineError(
                    "version_conflict", "Actor facts changed during combat preparation"
                )
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
            self.characters.initialize_rule_actor(row, actor, encounter=True)
        if package.noncombat_foundation_json is None and spatial is None:
            self.facts.initialize_rule_scene(sid, snapshot.scene_state)
        if package.noncombat_foundation_json is None:
            self.characters.initialize_rule_components(sid, snapshot)
        elif self.characters.rule_components(
            sid, {a.entity_id for a in snapshot.character_states}
        ) != snapshot.model_dump(
            mode="json",
            include={"inventory_state", "resource_state", "casting_state", "effect_states"},
        ):
            raise StateMachineError(
                "version_conflict", "Component facts changed during preparation"
            )
        self.sessions.set_mechanical_schema(row["session_id"], snapshot.snapshot_schema_version)
        self.records.initialize(sid, snapshot, spatial=spatial is not None, process_id=process_id())
        self.sessions.select_rule_rng(sid, rng.model_dump_json(), reset_position=True)
        return [
            self.capture._capture_event(
                sid,
                package,
                "combat.initialized",
                {"scene_id": snapshot.scene_state.scene_id},
                phase="combat.after_initialization",
            )
        ]

    def read_noncombat_snapshot(self, session_id, package, *, context):
        self.rules._verify_dnd_binding(package)
        with self.store.transaction():
            return self.rules._noncombat_snapshot(
                self.sessions._session(session_id, package, context), package
            )

    def read_combat_snapshot(self, session_id, package, *, context):
        """Trusted audit/read boundary; this does not authorize cross-process continuation."""
        if not package.legacy_audit_only:
            self.rules._verify_dnd_binding(package)
        with self.store.transaction():
            row = self.sessions._session(session_id, package, context)
            return self.rules._combat_snapshot(row)

    def read_rng_context(self, session_id, package, *, context):
        if not package.legacy_audit_only:
            self.rules._verify_dnd_binding(package)
        with self.store.transaction():
            return self.rules._rng_context(self.sessions._session(session_id, package, context))

    async def submit_command_async(self, command, package, *, context):
        """Async entry for the same TypedCommand and persistent receipt protocol."""
        validate_command(command)
        if package.legacy_audit_only:
            raise StateMachineError(
                "legacy_rules_audit_only", "Old package permits audit reads only"
            )
        from .world_commands import OPENABLE_CHECK_OPERATIONS

        if type(command.payload) not in (
            CombatIntentPayload,
            CombatClosePayload,
            CheckPayload,
            *OPENABLE_CHECK_OPERATIONS,
        ):
            raise StateMachineError(
                "unsupported_operation", "Expected an admitted real Engine command"
            )
        with self.store.transaction():
            self.sessions._session(command.session_id, package, context)
        return await self._submit_dnd(
            command, package, context, command_fingerprint(command, package, context)
        )

    async def _submit_dnd(self, command, package, context, fingerprint):
        from .rule_execution import RuleExecution

        return await RuleExecution(self).submit(command, package, context, fingerprint)

    def query_action_availability(self, session_id, actor_id, payload, package, *, context):
        if type(payload) is not CombatIntentPayload or type(actor_id) is not str or not actor_id:
            raise StateMachineError("invalid_query", "Expected a typed intent and Actor identity")
        with self.store.transaction():
            row = self.sessions._session(session_id, package, context)
            self.sessions._authorize_command(row, actor_id, payload, context)
            snapshot = self.rules._combat_snapshot(row)
            owner = self._conn.execute(
                "SELECT process_id FROM combats WHERE session_id=?", (session_id,)
            ).fetchone()[0]
        return self.rules.query_availability(
            session_id, actor_id, payload, package, snapshot, owner
        )
