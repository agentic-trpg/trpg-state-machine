"""Real pinned evaluate(), closed delta validation and one SQLite commit boundary."""

import copy
import json
from dataclasses import asdict

from dnd5e_engine.evaluation_contracts import (
    CheckPayload,
    CombatClosePayload,
    CombatIntentPayload,
)
from dnd5e_engine.evaluation_ruleset import (
    EVALUATOR_VERSION,
    RulesetBindingError,
)
from pydantic import ValidationError

from ._execution import _WorldEffectKind, _WorldEffectOrigin
from .combat import (
    _remap,
    process_id,
    validate_noncombat_foundation,
)
from .commands import CommandReceipt, command_fingerprint, validate_command
from .dnd_checks import world_effects_ready
from .errors import StateMachineError
from .package import canonical_json
from .rules_adapter import validate_dnd_result


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
        self.sessions.set_mechanical_schema(row["session_id"], "engine-snapshot/2")
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
            if snapshot.combat_state.actor_zone != self.world._current_actor_cells(
                sid, row["current_scene_id"], snapshot.combat_state.initiative_ids
            ):
                raise StateMachineError(
                    "version_conflict", "Combat positions changed during preparation"
                )
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
            self.characters.initialize_rule_actor(row, actor, encounter=True)
        if package.noncombat_foundation_json is None and spatial is None:
            self.facts.initialize_rule_scene(sid, snapshot.scene_state)
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
                self.sessions._session(session_id, package, context)
            )

    def read_combat_snapshot(self, session_id, package, *, context):
        """Trusted audit/read boundary; this does not authorize cross-process continuation."""
        if package.ruleset_binding.evaluator_version != "dnd5e-evaluation/4":
            self.rules._verify_dnd_binding(package)
        with self.store.transaction():
            row = self.sessions._session(session_id, package, context)
            return self.rules._combat_snapshot(row)

    def read_rng_context(self, session_id, package, *, context):
        if package.ruleset_binding.evaluator_version != "dnd5e-evaluation/4":
            self.rules._verify_dnd_binding(package)
        with self.store.transaction():
            return self.rules._rng_context(self.sessions._session(session_id, package, context))

    async def submit_command_async(self, command, package, *, context):
        """Async entry for the same TypedCommand and persistent receipt protocol."""
        validate_command(command)
        if type(command.payload) not in (CombatIntentPayload, CombatClosePayload, CheckPayload):
            raise StateMachineError(
                "unsupported_operation", "Expected an admitted real Engine command"
            )
        with self.store.transaction():
            self.sessions._session(command.session_id, package, context)
        return await self._submit_dnd(
            command, package, context, command_fingerprint(command, package, context)
        )

    async def _submit_dnd(self, command, package, context, fingerprint):
        build_snapshot = (
            self.rules._noncombat_snapshot
            if type(command.payload) is CheckPayload
            else self.rules._combat_snapshot
        )
        with self.store.transaction():
            row = self.sessions._session(command.session_id, package, context)
            self.sessions._authorize_command(row, command.actor_id, command.payload, context)
            self.sessions._npc_handoff_command_guard(command)
            if replay := self.sessions._command_replay(command, fingerprint):
                return replay
            if row["evaluator_version"] != EVALUATOR_VERSION:
                raise StateMachineError(
                    "ruleset_binding_mismatch", "Pinned evaluator version is not installed"
                )
            snapshot = build_snapshot(row)
            self.sessions._npc_combat_target_guard(command, package, snapshot)
            world_ready = (
                world_effects_ready(self.world, row, command)
                if row["world_version"] == command.expected_world_version
                else True
            )
            rng = self.rules._rng_context(row)
            if type(command.payload) is not CheckPayload:
                combat = self._conn.execute(
                    "SELECT process_id FROM combats WHERE session_id=?", (command.session_id,)
                ).fetchone()
                if combat[0] != process_id() and not snapshot.combat_state.ended:
                    raise StateMachineError(
                        "combat_unrestorable",
                        "Interrupted combat requires explicit recovery support",
                    )
        request = self.rules.request(command, package, snapshot, rng)
        capability = self.rules._dnd_capability(command.payload, command.actor_id, snapshot)
        if capability is None and not world_ready:
            capability = "world_precondition_failed"
        result, changes = None, ()
        if snapshot.world_version == command.expected_world_version:
            self.rules._verify_dnd_binding(package)
            if capability is None:
                try:
                    result = await self.rules._evaluate_dnd(copy.deepcopy(request))
                except RulesetBindingError as exc:
                    raise StateMachineError(
                        "ruleset_binding_mismatch", "Evaluator binding differs"
                    ) from exc
                except ValidationError as exc:
                    raise StateMachineError(
                        "rule_schema_error", "Evaluator schema failure"
                    ) from exc
                except Exception as exc:
                    raise StateMachineError(
                        "rule_evaluation_failed",
                        "Evaluator failed; original command may be retried",
                    ) from exc
                result, changes = validate_dnd_result(request, result)
            self.rules._verify_dnd_binding(package)
        with self.store.transaction(write=True):
            row = self.sessions._session(command.session_id, package, context)
            self.sessions._authorize_command(row, command.actor_id, command.payload, context)
            self.sessions._npc_handoff_command_guard(command)
            if replay := self.sessions._command_replay(command, fingerprint):
                return replay
            version, seq = row["world_version"], row["event_seq"]
            status, error = "committed", None
            if (
                version != command.expected_world_version
                or build_snapshot(row) != snapshot
                or self.rules._rng_context(row) != rng
            ):
                status, error = "conflict", "version_conflict"
            elif capability is not None:
                status, error = "rejected", capability
            elif not world_effects_ready(self.world, row, command):
                status, error = "rejected", "world_precondition_failed"
            elif result.status != "accepted":
                status, error = "rejected", result.error.code
            else:
                self.sessions._npc_combat_target_guard(command, package, build_snapshot(row))
                result, changes = validate_dnd_result(request, result)
                self._conn.execute("SAVEPOINT dnd_effects")
                try:
                    self.rules._apply_dnd_changes(row, changes)
                    events = [
                        self.capture._capture_event(
                            command.session_id,
                            package,
                            "rules.check_rolled"
                            if type(command.payload) is CheckPayload
                            else "combat.engine_event",
                            {
                                **(
                                    {
                                        "adjudication": {
                                            "principal_id": context.principal_id,
                                            "payload": command.payload.model_dump(mode="json"),
                                            **(
                                                {
                                                    "world_effects": asdict(
                                                        command.check_world_effects
                                                    )
                                                }
                                                if command.check_world_effects
                                                else {}
                                            ),
                                        }
                                    }
                                    if type(command.payload) is CheckPayload
                                    else {"combat_id": snapshot.combat_state.combat_id}
                                ),
                                "scene_id": snapshot.scene_state.scene_id,
                                "engine_event": event.model_dump(mode="json"),
                            },
                            phase="engine.intermediate_phase_unavailable",
                            unresolved=True,
                        )
                        for event in result.proposed_events
                    ]
                    if command.check_world_effects is not None:
                        # check_changes already validated the sole official CheckRolled.
                        # accepted is NOT the success predicate; the Engine owns succeeded.
                        branch = (
                            command.check_world_effects.on_success
                            if result.proposed_events[0].succeeded
                            else command.check_world_effects.on_failure
                        )
                        for operation in branch:
                            events.extend(
                                self.world._operate(
                                    row,
                                    package,
                                    operation,
                                    effect_origin=_WorldEffectOrigin(
                                        _WorldEffectKind.CHECK, command.command_id
                                    ),
                                )
                            )
                    events = self.effects._expand_events(row, package, events, command.command_id)
                    version += 1
                    transition = result.rng_transition
                    self.sessions.set_world_version(command.session_id, version)
                    self.sessions.advance_rule_rng(command.session_id, transition, rng)
                    self.persistence._append_events(
                        command.session_id,
                        version,
                        events,
                        source_command_id=command.command_id,
                        package=package,
                    )
                except StateMachineError as exc:
                    if exc.code != "event_cascade_limit":
                        raise
                    self._conn.execute("ROLLBACK TO dnd_effects")
                    status, error, version = "rejected", exc.code, row["world_version"]
                finally:
                    self._conn.execute("RELEASE dnd_effects")
            end = self._conn.execute(
                "SELECT event_seq FROM sessions WHERE session_id=?", (command.session_id,)
            ).fetchone()[0]
            receipt = CommandReceipt(
                command.session_id,
                command.command_id,
                command.actor_id,
                status,
                version,
                tuple(range(seq + 1, end + 1)),
                error,
            )
            self.sessions.record_receipt(command, context, fingerprint, receipt)
        return receipt

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
