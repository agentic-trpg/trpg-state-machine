"""Shared prepare/evaluate/commit path for checks, combat and object checks."""

import copy
from dataclasses import asdict, replace

from dnd5e_engine.evaluation_contracts import CheckPayload
from dnd5e_engine.evaluation_openable import OpenableCheckRequest, evaluate_openable
from dnd5e_engine.evaluation_ruleset import EVALUATOR_VERSION, RulesetBindingError
from pydantic import ValidationError

from ._execution import _WorldEffectKind, _WorldEffectOrigin
from .combat import process_id
from .dnd_checks import world_effects_ready
from .errors import StateMachineError
from .rules_adapter import validate_dnd_result
from .world_commands import OPENABLE_CHECK_OPERATIONS, CheckMechanism, PickLock


class RuleExecution:
    def __init__(self, owner):
        self.runtime = owner.runtime
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
        command = self.runtime.prepare_timed_command(command, package, context, fingerprint)
        object_check = type(command.payload) in OPENABLE_CHECK_OPERATIONS
        mechanism_check = type(command.payload) is CheckMechanism
        domain_check = object_check or mechanism_check
        check_service = (
            self.world.objects.mechanisms if mechanism_check else self.world.objects.openable
        )
        plan, prepare_error, rule_command = None, None, command

        def build_snapshot(value):
            if domain_check and self.world._active_combat(command.session_id):
                # Preserve a durable rejected receipt without pretending a combat
                # snapshot is a noncombat check or relaxing any capability guard.
                return self.rules._combat_snapshot(value)
            if type(command.payload) is CheckPayload or domain_check:
                return self.rules._noncombat_snapshot(value, package)
            return self.rules._combat_snapshot(value)

        with self.store.transaction():
            row = self.sessions._session(command.session_id, package, context)
            self.sessions._authorize_command(row, command.actor_id, command.payload, context)
            self.runtime.authorize_time_wait_reply(row, package, command, context)
            self.sessions._npc_handoff_command_guard(command)
            if replay := self.runtime._command_replay(command, fingerprint):
                return replay
            self.sessions._npc_combat_location_guard(command)
            if row["evaluator_version"] != EVALUATOR_VERSION:
                raise StateMachineError(
                    "ruleset_binding_mismatch", "Pinned evaluator version is not installed"
                )
            snapshot = build_snapshot(row)
            if domain_check:
                try:
                    plan = check_service.prepare_check(
                        row, command.actor_id, command.payload, package
                    )
                    if plan[0] is not None:
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
            if type(command.payload) is not CheckPayload and not domain_check:
                combat = self._conn.execute(
                    "SELECT process_id FROM combats WHERE session_id=?", (command.session_id,)
                ).fetchone()
                if combat[0] != process_id() and not snapshot.combat_state.ended:
                    raise StateMachineError(
                        "combat_unrestorable",
                        "Interrupted combat requires explicit recovery support",
                    )
        noop = object_check and plan is not None and plan[5]
        unlocked_pick = noop and type(command.payload) is PickLock
        if unlocked_pick:
            request, capability = None, None
        elif domain_check and prepare_error is not None:
            request = None
            capability = prepare_error
        else:
            request = self.rules.request(rule_command, package, snapshot, rng, row=row)
            capability = self.rules._dnd_capability(
                rule_command.payload, command.actor_id, snapshot
            )
        spec = (
            OpenableCheckRequest(
                schema_version="engine-openable/1",
                request=request,
                operation=plan[1],
                tool_instance_id=command.payload.tool_instance_id,
                required_tool=plan[2],
                leverage_applicable=plan[3],
            )
            if object_check and prepare_error is None and not unlocked_pick
            else None
        )
        if capability is None and not world_ready:
            capability = "world_precondition_failed"
        result, changes = None, ()
        if snapshot.world_version == command.expected_world_version:
            self.rules._verify_dnd_binding(package, row)
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
            self.rules._verify_dnd_binding(package, row)
        with self.store.transaction(write=True):
            row = self.sessions._session(command.session_id, package, context)
            self.sessions._authorize_command(row, command.actor_id, command.payload, context)
            self.runtime.authorize_time_wait_reply(row, package, command, context)
            self.sessions._npc_handoff_command_guard(command)
            if replay := self.runtime._command_replay(command, fingerprint):
                return replay
            self.sessions._npc_combat_location_guard(command)
            version = row["world_version"]
            status, error = "committed", None
            if (
                self.runtime.version_conflict(row, command)
                or build_snapshot(row) != snapshot
                or self.rules._rng_context(row) != rng
            ):
                status, error = "conflict", "version_conflict"
            elif capability is not None:
                status, error = "rejected", capability
            elif noop:
                try:
                    if command.time_wait is not None:
                        self.runtime.time.waits.validate(row, package, command)
                    current_plan = check_service.prepare_check(
                        row, command.actor_id, command.payload, package
                    )
                    if current_plan != plan:
                        status, error = "conflict", "version_conflict"
                except StateMachineError as exc:
                    status, error = "rejected", exc.code
            elif not world_effects_ready(self.world, row, command):
                status, error = "rejected", "world_precondition_failed"
            elif result.status != "accepted":
                status, error = "rejected", result.error.code
            else:
                self.sessions._npc_combat_target_guard(command, package, build_snapshot(row))
                result, changes = validate_dnd_result(request, result)
                self._conn.execute("SAVEPOINT dnd_effects")
                try:
                    if command.time_wait is not None:
                        self.runtime.time.waits.validate(row, package, command)
                    if domain_check:
                        current_plan = check_service.prepare_check(
                            row, command.actor_id, command.payload, package
                        )
                        if current_plan != plan:
                            raise StateMachineError(
                                "version_conflict", "Object check dependencies changed"
                            )
                    previous_round = (
                        snapshot.combat_state.round_number
                        if hasattr(snapshot, "combat_state")
                        else None
                    )
                    self.rules._apply_dnd_changes(row, changes)
                    # Failure consequences and automatic hooks both consume the
                    # original rule result's candidate RNG successor. Never
                    # overwrite a mechanism successor with this earlier state.
                    self.runtime.advance_rule_rng(command.session_id, result.rng_transition, rng)
                    events = [
                        self.capture._capture_event(
                            command.session_id,
                            package,
                            "rules.check_rolled"
                            if type(command.payload) is CheckPayload or domain_check
                            else "combat.engine_event",
                            {
                                **(
                                    {
                                        "adjudication": {
                                            "principal_id": context.principal_id,
                                            "payload": rule_command.payload.model_dump(mode="json"),
                                            **(
                                                {
                                                    (
                                                        "mechanism_operation"
                                                        if mechanism_check
                                                        else "openable_operation"
                                                    ): {
                                                        "command_type": command.command_type,
                                                        "payload": asdict(command.payload),
                                                    }
                                                }
                                                if domain_check
                                                else {}
                                            ),
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
                                    if type(command.payload) is CheckPayload or domain_check
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
                    if mechanism_check:
                        events.extend(
                            self.world.objects.mechanisms.service(
                                row,
                                command.actor_id,
                                command.payload,
                                package,
                                succeeded=result.proposed_events[0].succeeded,
                                command_id=command.command_id,
                            )
                        )
                    elif object_check:
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
                    events.extend(
                        self.runtime.finish_time_wait_reply(row, package, command, events)
                    )
                    events = self.effects._expand_events(row, package, events, command.command_id)
                    events.extend(
                        self.runtime.coordinate_combat_time(
                            row, package, command.command_id, previous_round, len(events)
                        )
                    )
                    version = self.runtime.stage_outcome(command, row, package, events)
                except StateMachineError as exc:
                    if exc.code not in {
                        "event_cascade_limit",
                        "door_locked",
                        "invalid_lock_state",
                        "object_jammed",
                        "object_barred",
                        "object_destroyed",
                        "passage_unavailable",
                        "invalid_spatial_state",
                        "object_obstructed",
                        "position_conflict",
                        "version_conflict",
                        "mechanism_target_unavailable",
                        "unsupported_mechanism_target",
                        "mechanism_state_conflict",
                        "mechanism_exhausted",
                        "mechanism_broken",
                        "mechanism_spent",
                        "mechanism_unprepared",
                        "mechanism_blocked",
                        "mechanism_disarmed",
                        "mechanism_frequency_limited",
                        "unsupported_mechanism_effect",
                        "unsupported_mechanism_effect_lifecycle",
                        "unsupported_mechanism_effect_delta",
                        "invalid_mechanism_targets",
                        "mechanism_rule_rejected",
                        "mechanism_needs_choice",
                    }:
                        # Malformed rule results/storage failures remain faults: the
                        # outer transaction rolls back without persisting a refusal.
                        raise
                    self._conn.execute("ROLLBACK TO dnd_effects")
                    self.world.store._scope.read_memo.clear()
                    status = (
                        "conflict"
                        if exc.code == "version_conflict"
                        else "pending_choice"
                        if exc.code == "mechanism_needs_choice"
                        else "rejected"
                    )
                    error, version = exc.code, row["world_version"]
                finally:
                    self._conn.execute("RELEASE dnd_effects")
            if noop and status == "committed" and command.time_wait is not None:
                with self.store.savepoint():
                    events = self.runtime.finish_time_wait_reply(row, package, command)
                    events = self.effects._expand_events(row, package, events, command.command_id)
                    version = self.runtime.stage_outcome(command, row, package, events)
            receipt = self.runtime.finish(
                command, context, fingerprint, row, status, version, error
            )
        self.runtime.refresh_time_drivers()
        return receipt
