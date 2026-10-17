"""Shared prepare/evaluate/commit path for checks, combat and object checks."""

import copy
from dataclasses import asdict, replace

from dnd5e_engine.evaluation_contracts import CheckPayload
from dnd5e_engine.evaluation_openable import OpenableCheckRequest, evaluate_openable
from dnd5e_engine.evaluation_ruleset import EVALUATOR_VERSION, RulesetBindingError
from pydantic import ValidationError

from ._execution import _WorldEffectKind, _WorldEffectOrigin
from .combat import process_id
from .commands import CommandReceipt
from .dnd_checks import world_effects_ready
from .errors import StateMachineError
from .rules_adapter import validate_dnd_result
from .world_commands import OPENABLE_CHECK_OPERATIONS


class RuleExecution:
    def __init__(self, owner):
        self.store = owner.store
        self.sessions = owner.sessions
        self.rules = owner.rules
        self.world = owner.world
        self.capture = owner.capture
        self.effects = owner.effects
        self.persistence = owner.persistence

    @property
    def _conn(self):
        return self.store.connection

    async def submit(self, command, package, context, fingerprint):
        object_check = type(command.payload) in OPENABLE_CHECK_OPERATIONS
        plan, prepare_error, rule_command = None, None, command

        def build_snapshot(value):
            if object_check and self.world._active_combat(command.session_id):
                # Preserve a durable rejected receipt without pretending a combat
                # snapshot is a noncombat check or relaxing any capability guard.
                return self.rules._combat_snapshot(value)
            if type(command.payload) is CheckPayload or object_check:
                return self.rules._noncombat_snapshot(value, package)
            return self.rules._combat_snapshot(value)

        with self.store.transaction():
            row = self.sessions._session(command.session_id, package, context)
            self.sessions._authorize_command(row, command.actor_id, command.payload, context)
            self.sessions._npc_handoff_command_guard(command)
            if replay := self.sessions._command_replay(command, fingerprint):
                return replay
            self.sessions._npc_combat_location_guard(command)
            if row["evaluator_version"] != EVALUATOR_VERSION:
                raise StateMachineError(
                    "ruleset_binding_mismatch", "Pinned evaluator version is not installed"
                )
            snapshot = build_snapshot(row)
            if object_check:
                try:
                    plan = self.world.objects.openable.prepare_check(
                        row, command.actor_id, command.payload, package
                    )
                    rule_command = replace(command, command_type="rules.check", payload=plan[0])
                except StateMachineError as exc:
                    prepare_error = exc.code

            self.sessions._npc_combat_target_guard(command, package, snapshot)
            world_ready = (
                world_effects_ready(self.world, row, command)
                if row["world_version"] == command.expected_world_version
                else True
            )
            rng = self.rules._rng_context(row)
            if type(command.payload) is not CheckPayload and not object_check:
                combat = self._conn.execute(
                    "SELECT process_id FROM combats WHERE session_id=?", (command.session_id,)
                ).fetchone()
                if combat[0] != process_id() and not snapshot.combat_state.ended:
                    raise StateMachineError(
                        "combat_unrestorable",
                        "Interrupted combat requires explicit recovery support",
                    )
        if object_check and prepare_error is not None:
            request = None
            capability = prepare_error
        else:
            request = self.rules.request(rule_command, package, snapshot, rng)
            capability = self.rules._dnd_capability(
                rule_command.payload, command.actor_id, snapshot
            )
        noop = object_check and plan is not None and plan[5]
        spec = (
            OpenableCheckRequest(
                schema_version="engine-openable/1",
                request=request,
                operation=plan[1],
                tool_instance_id=command.payload.tool_instance_id,
                required_tool=plan[2],
                leverage_applicable=plan[3],
            )
            if object_check and prepare_error is None
            else None
        )
        if capability is None and not world_ready:
            capability = "world_precondition_failed"
        result, changes = None, ()
        if snapshot.world_version == command.expected_world_version:
            self.rules._verify_dnd_binding(package)
            if capability is None and not noop:
                try:
                    result = (
                        await evaluate_openable(spec, loader=self.rules.loader)
                        if object_check
                        else await self.rules._evaluate_dnd(copy.deepcopy(request))
                    )
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
            self.sessions._npc_combat_location_guard(command)
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
            elif noop:
                pass
            elif not world_effects_ready(self.world, row, command):
                status, error = "rejected", "world_precondition_failed"
            elif result.status != "accepted":
                status, error = "rejected", result.error.code
            else:
                self.sessions._npc_combat_target_guard(command, package, build_snapshot(row))
                result, changes = validate_dnd_result(request, result)
                self._conn.execute("SAVEPOINT dnd_effects")
                try:
                    if object_check:
                        current_plan = self.world.objects.openable.prepare_check(
                            row, command.actor_id, command.payload, package
                        )
                        if current_plan != plan:
                            raise StateMachineError(
                                "version_conflict", "Object check dependencies changed"
                            )
                    self.rules._apply_dnd_changes(row, changes)
                    events = [
                        self.capture._capture_event(
                            command.session_id,
                            package,
                            "rules.check_rolled"
                            if type(command.payload) is CheckPayload or object_check
                            else "combat.engine_event",
                            {
                                **(
                                    {
                                        "adjudication": {
                                            "principal_id": context.principal_id,
                                            "payload": rule_command.payload.model_dump(mode="json"),
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
                                    if type(command.payload) is CheckPayload or object_check
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
                    if object_check:
                        events.extend(
                            self.world.objects.openable.apply_check(
                                row,
                                command.actor_id,
                                command.payload,
                                package,
                                plan,
                                succeeded=result.proposed_events[0].succeeded,
                            )
                        )
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
                    self.world._assert_spatial_consistency(command.session_id, package)
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
