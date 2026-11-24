"""Versioned local command boundary; authentication belongs to the caller adapter."""

import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, replace

from dnd5e_engine.evaluation_bardic_grant import ExplorationFeaturePayload
from dnd5e_engine.evaluation_contracts import (
    MAX_EXPLORATION_SPELL_TARGETS,
    CheckPayload,
    CombatClosePayload,
    CombatIntentPayload,
    ExplorationEnvironmentalSpellCastPayload,
    ExplorationItemUsePayload,
    ExplorationMultiSpellCastPayload,
    ExplorationSpellCastPayload,
    ItemUsePayload,
    SavePayload,
)
from pydantic import ValidationError

from ._operations import (
    DismissWorldSpell,
    DropItem,
    DropWorldConcentration,
    EnterScene,
    LockDoor,
    MoveActor,
    ObjectState,
    PickupItem,
    PlaceObject,
    SetObjectState,
    SetSceneFlag,
    StepActor,
    TraversePassage,
    UnlockDoor,
)
from .choice_models import (
    ChoiceFinalizeSpellOperationPayload,
    ChoiceRespondOperationPayload,
    PendingChoice,
)
from .combat import InitializeCombat
from .environment_commands import (
    ENVIRONMENT_COMMANDS,
    ENVIRONMENT_OPERATIONS,
    BatchEnvironment,
    EnvironmentMutation,
    environment_operation_from_data,
    validate_environment_operation,
)
from .errors import StateMachineError
from .game_time import AdvanceGameTime, sync_npc_schedules
from .mechanics import RuleOperation
from .navigation_models import (
    NAVIGATION_OPERATIONS,
    AdvanceNavigation,
    CancelNavigation,
    ResumeNavigation,
    StartNavigation,
)
from .rest_activity import HitDiceSpend, LongRest, ShortRest, validate_short_rest
from .spell_cast_activity import FinishSpellCast, StartSpellCast, validate_spell_cast_activity
from .time_coordination import AdvanceSessionTime, SetTimeRunning, validate_time_operation
from .time_waits import TimeWaitReference, validate_wait_reference
from .world_commands import (
    OPENABLE_CHECK_OPERATIONS,
    WORLD_CAPABILITY_COMMANDS,
    ActivateMechanism,
    AimLight,
    BarObject,
    CheckMechanism,
    ClearJam,
    EquipArmor,
    EquipShield,
    EquipWeapon,
    EquipWearable,
    ForceOpen,
    LockObject,
    PickLock,
    PutItem,
    RepositionObject,
    ServiceMechanism,
    SetLightEnabled,
    SpendItemCharge,
    SwitchLightMode,
    TakeItem,
    UnbarObject,
    UnequipArmor,
    UnequipShield,
    UnequipWeapon,
    UnequipWearable,
    UnlockObject,
)

OPERATIONS = {
    "world.unlock_door": UnlockDoor,
    "world.lock_door": LockDoor,
    "world.pickup_item": PickupItem,
    "world.drop_item": DropItem,
    "activity.start_navigation": StartNavigation,
    "activity.advance_navigation": AdvanceNavigation,
    "activity.resume_navigation": ResumeNavigation,
    "activity.cancel_navigation": CancelNavigation,
    "activity.long_rest": LongRest,
    "activity.short_rest": ShortRest,
    "activity.start_spell_cast": StartSpellCast,
    "activity.finish_spell_cast": FinishSpellCast,
    "world.step_actor": StepActor,
    "world.traverse_passage": TraversePassage,
    "world.place_object": PlaceObject,
    "world.advance_time": AdvanceGameTime,
    "session.advance_time": AdvanceSessionTime,
    "session.set_time_running": SetTimeRunning,
    "rules.check": CheckPayload,
    "rules.save": SavePayload,
    "combat.close": CombatClosePayload,
    "combat.intent": CombatIntentPayload,
    "combat.item": ItemUsePayload,
    "world.use_item": ExplorationItemUsePayload,
    "world.cast_spell": ExplorationSpellCastPayload,
    "world.use_feature": ExplorationFeaturePayload,
    "world.cast_spell_many": ExplorationMultiSpellCastPayload,
    "choice.respond": ChoiceRespondOperationPayload,
    "choice.finalize_spell": ChoiceFinalizeSpellOperationPayload,
    "world.drop_concentration": DropWorldConcentration,
    "world.dismiss_spell": DismissWorldSpell,
    "combat.initialize": InitializeCombat,
    "rule_operation": RuleOperation,
    "enter_scene": EnterScene,
    "set_scene_flag": SetSceneFlag,
    "set_object_state": SetObjectState,
    "move_actor": MoveActor,
}

OPERATIONS.update(WORLD_CAPABILITY_COMMANDS)
OPERATIONS.update(ENVIRONMENT_COMMANDS)


@dataclass(frozen=True, slots=True)
class CheckWorldEffects:
    """Local conditional-world-effects/1, fixed before any rule evaluation."""

    source_ref: str
    preconditions: tuple[SetSceneFlag | SetObjectState, ...] = ()
    on_success: tuple[SetSceneFlag | SetObjectState, ...] = ()
    on_failure: tuple[SetSceneFlag | SetObjectState, ...] = ()


@dataclass(frozen=True, slots=True)
class TypedCommand:
    """Carry command identity, expected world version, actor and a closed operation
    payload.
    """

    session_id: str
    command_id: str
    actor_id: str
    command_type: str
    payload: (
        EnvironmentMutation
        | BatchEnvironment
        | PickLock
        | ForceOpen
        | ClearJam
        | ActivateMechanism
        | ServiceMechanism
        | CheckMechanism
        | SetLightEnabled
        | SwitchLightMode
        | AimLight
        | RepositionObject
        | PutItem
        | TakeItem
        | SpendItemCharge
        | EquipArmor
        | UnequipArmor
        | EquipShield
        | UnequipShield
        | EquipWeapon
        | UnequipWeapon
        | EquipWearable
        | UnequipWearable
        | LockObject
        | UnlockObject
        | BarObject
        | UnbarObject
        | EnterScene
        | SetSceneFlag
        | SetObjectState
        | MoveActor
        | StepActor
        | TraversePassage
        | PlaceObject
        | RuleOperation
        | InitializeCombat
        | CombatIntentPayload
        | ItemUsePayload
        | ExplorationItemUsePayload
        | ExplorationEnvironmentalSpellCastPayload
        | ExplorationSpellCastPayload
        | ExplorationFeaturePayload
        | ExplorationMultiSpellCastPayload
        | ChoiceRespondOperationPayload
        | ChoiceFinalizeSpellOperationPayload
        | LongRest
        | ShortRest
        | StartSpellCast
        | FinishSpellCast
        | CombatClosePayload
        | CheckPayload
        | SavePayload
        | AdvanceGameTime
        | AdvanceSessionTime
        | SetTimeRunning
        | StartNavigation
        | AdvanceNavigation
        | ResumeNavigation
        | CancelNavigation
        | PickupItem
        | DropItem
        | DropWorldConcentration
        | DismissWorldSpell
        | UnlockDoor
        | LockDoor
    )
    expected_world_version: int
    schema_version: str = "sm-command/1"
    check_world_effects: CheckWorldEffects | None = None
    time_wait: TimeWaitReference | None = None


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """Represent the durable command outcome used for idempotent retries."""

    session_id: str
    command_id: str
    actor_id: str
    status: str
    world_version: int
    event_seqs: tuple[int, ...]
    error_code: str | None = None
    pending_choice: PendingChoice | None = None
    pending_choices: tuple[PendingChoice, ...] | None = None


def receipt_from_json(raw: str) -> CommandReceipt:
    """Decode a durable JSON receipt and restore event sequences as tuples."""
    data = json.loads(raw)
    data["event_seqs"] = tuple(data["event_seqs"])
    if data.get("pending_choices") is not None:
        data["pending_choices"] = tuple(
            _pending_choice_from_json_fields(fields) for fields in data["pending_choices"]
        )
    if data.get("pending_choice") is not None:
        data["pending_choice"] = _pending_choice_from_json_fields(data["pending_choice"])
    return CommandReceipt(**data)


def _pending_choice_from_json_fields(fields: dict) -> PendingChoice:
    """Restore the ordered option IDs of a persisted prompt."""
    return PendingChoice(**{**fields, "allowed_options": tuple(fields["allowed_options"])})


def validate_command(command: TypedCommand) -> None:
    """Validate the command envelope and its closed supported payload shape.

    Check identity strings, expected versions, command/payload tag agreement, schema
    markers and optional wait references, delegating domain payload validation as
    needed. Raise structured validation errors; shape validity alone grants no actor
    authority, world precondition or permission to commit.
    """
    if type(command) is not TypedCommand or command.schema_version != "sm-command/1":
        raise StateMachineError("invalid_command", "Expected sm-command/1")
    if any(
        type(v) is not str or not v.strip() or len(v) > 256
        for v in (command.session_id, command.command_id, command.actor_id, command.command_type)
    ):
        raise StateMachineError("invalid_command", "Command identities must be bounded strings")
    if type(command.expected_world_version) is not int or command.expected_world_version < 0:
        raise StateMachineError("invalid_operation", "Expected a nonnegative integer version")
    if command.time_wait is not None:
        validate_wait_reference(command.time_wait)
        if type(command.payload) in (
            AdvanceGameTime,
            AdvanceSessionTime,
            SetTimeRunning,
            RuleOperation,
            ExplorationItemUsePayload,
            ExplorationEnvironmentalSpellCastPayload,
            ExplorationSpellCastPayload,
            ExplorationFeaturePayload,
            ExplorationMultiSpellCastPayload,
            ChoiceRespondOperationPayload,
            ChoiceFinalizeSpellOperationPayload,
            LongRest,
            ShortRest,
            StartSpellCast,
        ):
            raise StateMachineError(
                "invalid_time_wait", "This operation cannot be a domain wait reply"
            )
    effects = command.check_world_effects
    if effects is not None:
        if (
            command.command_type != "rules.check"
            or type(effects) is not CheckWorldEffects
            or type(effects.source_ref) is not str
            or not re.fullmatch(r"gm\.[a-zA-Z0-9_.-]{1,120}", effects.source_ref)
        ):
            raise StateMachineError(
                "invalid_command", "Expected a fixed trusted GM effect proposal"
            )
        for operations in (effects.preconditions, effects.on_success, effects.on_failure):
            if type(operations) is not tuple or len(operations) > 16:
                raise StateMachineError("invalid_command", "At most sixteen typed world operations")
            for operation in operations:
                if type(operation) not in (SetSceneFlag, SetObjectState):
                    raise StateMachineError("invalid_command", "Conditional effects are world-only")
                if any(
                    type(v) is not str or not v or len(v) > 256
                    for k, v in asdict(operation).items()
                    if k not in ("value", "state")
                ):
                    raise StateMachineError("invalid_command", "Invalid world operation reference")
                try:
                    for value in asdict(operation).values():
                        if isinstance(value, str):
                            value.encode("utf-8", errors="strict")
                except UnicodeEncodeError as exc:
                    raise StateMachineError("invalid_command", "Invalid Unicode reference") from exc
                if (type(operation) is SetSceneFlag and type(operation.value) is not bool) or (
                    type(operation) is SetObjectState and type(operation.state) is not ObjectState
                ):
                    raise StateMachineError(
                        "invalid_command", "Invalid typed world operation value"
                    )
    operation_type = OPERATIONS.get(command.command_type)
    if (
        command.command_type == "world.cast_spell"
        and type(command.payload) is ExplorationEnvironmentalSpellCastPayload
    ):
        operation_type = ExplorationEnvironmentalSpellCastPayload
    if operation_type is None or type(command.payload) is not operation_type:
        raise StateMachineError("unsupported_operation", "Unknown command or payload type")
    if operation_type is ChoiceRespondOperationPayload:
        payload = command.payload
        if (
            type(payload.pending_operation_id) is not str
            or not payload.pending_operation_id.strip()
            or len(payload.pending_operation_id) > 256
            or type(payload.selected_option_id) is not str
            or payload.selected_option_id not in ("accept", "decline")
        ):
            raise StateMachineError("invalid_operation", "Expected a bounded choice ID and option")
        return
    if operation_type is ChoiceFinalizeSpellOperationPayload:
        payload = command.payload
        if (
            type(payload.pending_operation_id) is not str
            or not payload.pending_operation_id.strip()
            or len(payload.pending_operation_id) > 256
            or type(payload.target_ids) is not tuple
            or not 1 <= len(payload.target_ids) <= MAX_EXPLORATION_SPELL_TARGETS
            or any(
                type(value) is not str or not value.strip() or len(value) > 256
                for value in payload.target_ids
            )
            or len(set(payload.target_ids)) != len(payload.target_ids)
        ):
            raise StateMachineError(
                "invalid_operation", "Expected a choice ID and distinct targets"
            )
        return
    if operation_type is LongRest:
        return
    if operation_type is DropWorldConcentration:
        return
    if operation_type is DismissWorldSpell:
        if any(
            type(value) is not str or not value.strip() or len(value) > 1024
            for value in asdict(command.payload).values()
        ):
            raise StateMachineError("invalid_operation", "Expected an exact spell application")
        return
    if operation_type in (StartSpellCast, FinishSpellCast):
        if operation_type is FinishSpellCast and command.time_wait is None:
            raise StateMachineError(
                "invalid_time_wait", "Casting completion needs its deadline wait"
            )
        validate_spell_cast_activity(command.payload)
        return
    if operation_type is ShortRest:
        validate_short_rest(command.payload)
        return
    if type(command.payload) in ENVIRONMENT_OPERATIONS:
        validate_environment_operation(command.payload)
        return
    if type(command.payload) in NAVIGATION_OPERATIONS:
        payload = command.payload
        if (
            payload.schema_version != "navigation-activity/1"
            or type(payload.activity_id) is not str
            or not re.fullmatch(r"activity\.[a-zA-Z0-9_.-]{1,119}", payload.activity_id)
        ):
            raise StateMachineError(
                "invalid_operation", "Expected a bounded navigation activity identity"
            )
        if type(payload) is StartNavigation:
            if any(
                type(v) is not str or not v or len(v) > 128
                for v in (payload.target_scene_id, payload.target_cell)
            ):
                raise StateMachineError("invalid_operation", "Expected explicit Scene and Cell")
        elif (
            type(payload.expected_activity_version) is not int
            or payload.expected_activity_version < 1
        ):
            raise StateMachineError("invalid_operation", "Expected a positive activity version")
        return
    if type(command.payload) is EquipWeapon and command.payload.grip not in (
        "auto",
        "one_handed",
        "two_handed",
    ):
        raise StateMachineError("invalid_operation", "Weapon grip must be one or two handed")
    local_version = {
        StepActor: "actor-step/1",
        TraversePassage: "passage-traversal/1",
        PlaceObject: "object-placement/1",
        PickupItem: "item-transfer/1",
        DropItem: "item-transfer/1",
        UnlockDoor: "door-lock/1",
        LockDoor: "door-lock/1",
    }.get(type(command.payload))
    if local_version and command.payload.schema_version != local_version:
        raise StateMachineError("invalid_operation", "Unsupported local spatial operation version")
    if type(command.payload) in (AdvanceSessionTime, SetTimeRunning):
        validate_time_operation(command.payload)
        return
    if type(command.payload) is AdvanceGameTime:
        if any(
            type(v) is not int or not 0 <= v <= 2147483647 for v in asdict(command.payload).values()
        ):
            raise StateMachineError("invalid_operation", "Expected bounded integer game ticks")
        if command.payload.next_tick < command.payload.expected_tick:
            raise StateMachineError("invalid_operation", "Game time cannot move backwards")
        return
    if type(command.payload) in (
        CombatIntentPayload,
        CombatClosePayload,
        CheckPayload,
        SavePayload,
        ItemUsePayload,
        ExplorationItemUsePayload,
        ExplorationEnvironmentalSpellCastPayload,
        ExplorationSpellCastPayload,
        ExplorationFeaturePayload,
        ExplorationMultiSpellCastPayload,
    ):
        try:
            type(command.payload).model_validate(deepcopy(command.payload))
        except ValidationError as exc:
            raise StateMachineError("invalid_command", "Invalid Engine intent schema") from exc
        if isinstance(command.payload, CombatIntentPayload) and command.payload.source_id not in (
            None,
            command.actor_id,
        ):
            raise StateMachineError("invalid_actor", "Intent source differs from trusted actor")
        if (
            type(command.payload) is ItemUsePayload
            and command.payload.target_id != command.actor_id
        ):
            raise StateMachineError("invalid_target", "Drinking a potion requires self target")
        if (
            isinstance(command.payload, CheckPayload)
            and command.payload.actor_id != command.actor_id
        ):
            raise StateMachineError("invalid_actor", "Check subject differs from trusted actor")
        if type(command.payload) is SavePayload and command.payload.target_id != command.actor_id:
            raise StateMachineError("invalid_actor", "Save subject differs from trusted actor")
        return
    if type(command.payload) is RuleOperation:
        operation = command.payload
        if operation.difficulty is not None and (
            type(operation.difficulty) is not int or not 1 <= operation.difficulty <= 7
        ):
            raise StateMachineError(
                "invalid_operation", "Synthetic difficulty must be 1..7 or None"
            )
        fields = asdict(operation)
        fields.pop("difficulty")
    else:
        fields = asdict(command.payload)
    for key, value in fields.items():
        if type(command.payload) is RepositionObject and key in (
            "expected_orientation",
            "to_orientation",
        ):
            if value is not None and value not in ("north", "east", "south", "west"):
                raise StateMachineError("invalid_operation", "Invalid object orientation")
        elif type(command.payload) is AimLight and key in ("expected_direction", "direction"):
            if value not in ("north", "east", "south", "west"):
                raise StateMachineError("invalid_operation", "Invalid light direction")
        elif key in ("value", "enabled"):
            if type(value) is not bool:
                raise StateMachineError("invalid_operation", "Flag must be boolean")
        elif (
            key == "tool_instance_id"
            and value is None
            and type(command.payload) in (ForceOpen, ClearJam, ServiceMechanism, CheckMechanism)
        ):
            continue
        elif not isinstance(value, str) or not value or len(value) > 256:
            raise StateMachineError("invalid_operation", "Invalid operation field")


def command_fingerprint(command, package, context):
    """Compatibility helper; live execution fingerprints the admitted Session pin."""
    from .session_runtime import _fingerprint

    return _fingerprint(command, package.package_digest, package.ruleset_binding, context)


class CommandRuntime:
    """Coordinate authorized command dispatch, savepoints and durable outcomes."""

    def _authorize_command(self, row, actor_id, payload, context):
        """Delegate actor and payload authorization to SessionAccess."""
        return self._sessions._authorize_command(row, actor_id, payload, context)

    def _world_operation_capability(self, row, payload):
        """Check whether SessionAccess admits the requested world operation."""
        return self._sessions._world_operation_capability(row, payload)

    def _command_replay(self, command, fingerprint):
        """Look up an idempotent replay using command identity and fingerprint."""
        return self._session_runtime._command_replay(command, fingerprint)

    def _respond_action_choice(self, pending, response, package, context, fingerprint):
        """Resolve a live mechanism save from its original roll, then commit once."""
        from dnd5e_engine.evaluation_rng import RNGContext, RNGTransition
        from dnd5e_engine.evaluation_saves import resolve_bardic_save_choice

        with self._transaction():
            row = self._session(response.session_id, package, context)
            self._authorize_command(row, response.actor_id, response.payload, context)
            if replay := self._command_replay(response, fingerprint):
                self._discard_pending_action_choice(response.session_id, expected=pending)
                return replay
            if response.actor_id != pending.command.actor_id:
                raise StateMachineError("unauthorized", "Action choice belongs to another actor")
            if self._session_runtime.version_conflict(row, response):
                self._discard_pending_action_choice(response.session_id, expected=pending)
                raise StateMachineError("version_conflict", "Choice reply version changed")
            if self._world_facts.time_state(response.session_id)[1] >= pending.grant.expires_tick:
                self._discard_pending_action_choice(response.session_id, expected=pending)
                raise StateMachineError("choice_expired", "Bardic grant expired")
            grant = self._world.characters.effects.held_bardic_inspiration(
                response.session_id,
                response.actor_id,
                self._world_facts.time_state(response.session_id)[1],
            )
            if grant != pending.grant:
                self._discard_pending_action_choice(response.session_id, expected=pending)
                raise StateMachineError("choice_stale", "Bardic grant changed")
            oid, operation_id = pending.waiting_key
            gate = (
                self._objects.mechanisms.config(response.session_id, oid)
                .operation(operation_id)
                .save_gate
            )
            request, result, _ = pending.prepared[pending.waiting_key]
            current = self._objects.mechanisms._save_request(
                row, package, response.actor_id, gate, pending.command.command_id
            )
            if current != request:
                self._discard_pending_action_choice(response.session_id, expected=pending)
                raise StateMachineError("choice_stale", "Action save facts changed")
        original = result.proposed_events[0]
        after_original = RNGContext(
            stream_id=request.rng_context.stream_id,
            version=request.rng_context.version + int(result.rng_transition.state_changed),
            state=result.rng_transition.next_state,
        )
        use = response.payload.selected_option_id == "accept"
        final, bonus = resolve_bardic_save_choice(
            original, pending.grant.die_sides, after_original, use=use
        )
        combined = RNGTransition(
            stream_id=request.rng_context.stream_id,
            input_version=request.rng_context.version,
            input_state=request.rng_context.state,
            next_state=bonus.next_state,
            state_changed=request.rng_context.state != bonus.next_state,
        )
        prepared = dict(pending.prepared)
        prepared[pending.waiting_key] = (
            request,
            result.model_copy(update={"proposed_events": (final,), "rng_transition": combined}),
            pending.grant if use else None,
        )
        remaining = [item for item in pending.requests if (item[0], item[1]) not in prepared]
        if remaining:
            next_rng = RNGContext(
                stream_id=after_original.stream_id,
                version=after_original.version + int(bonus.state_changed),
                state=bonus.next_state,
            )
            rest, _, _ = self._objects.mechanisms.evaluate_direct_saves(
                remaining, first_rng=next_rng, action_choice=True
            )
            prepared.update(rest)
        resumed = replace(pending, prepared=prepared)
        return self.submit_command(
            resumed.command,
            package,
            context=context,
            _action_resume=(resumed, response, fingerprint),
        )

    def submit_command(
        self, command: TypedCommand, package, *, context, _action_resume=None
    ) -> CommandReceipt:
        """Authorize and submit a command, returning its durable idempotent outcome.

        Route actual rules operations to the async evaluation coordinator and synthetic
        checks to their isolated path. Other domain effects run under a savepoint;
        ordinary domain refusal rolls back effects before recording a
        rejected/conflict/choice receipt. Storage, binding and invalid rule-result
        failures propagate. Successful return follows Store commit/publication; a
        running event loop requires the async entrypoint for rule commands.
        """
        validate_command(command)
        with self._transaction():
            admitted = self._session(command.session_id, package, context)
            self._session_runtime.guard_time_owner(command.session_id)
            self._session_runtime.admit(admitted)
            fingerprint = self._session_runtime.fingerprint(command, admitted, context)
        if _action_resume is None:
            pending = self._pending_action_choices.get(command.session_id)
            if pending is not None:
                if command.command_id == pending.command.command_id:
                    if fingerprint != pending.fingerprint:
                        raise StateMachineError(
                            "idempotency_conflict", "Pending action key has different content"
                        )
                    with self._transaction():
                        if replay := self._command_replay(command, fingerprint):
                            self._discard_pending_action_choice(
                                command.session_id, expected=pending
                            )
                            return replay
                    return pending.receipt
                if (
                    type(command.payload) is ChoiceRespondOperationPayload
                    and command.payload.pending_operation_id == pending.command.command_id
                ):
                    from .action_pending import PendingFaerieChoice

                    if isinstance(pending, PendingFaerieChoice):
                        import asyncio

                        try:
                            asyncio.get_running_loop()
                        except RuntimeError:
                            return asyncio.run(
                                self._submit_dnd(command, package, context, fingerprint)
                            )
                        raise StateMachineError(
                            "async_context_required", "Use await submit_command_async"
                        )
                    return self._respond_action_choice(
                        pending, command, package, context, fingerprint
                    )
                raise StateMachineError("action_choice_pending", "Resolve the current action")
            self._guard_pending_action_owner(command.session_id)
            if type(command.payload) is ChoiceRespondOperationPayload:
                with self._transaction():
                    row = self._session(command.session_id, package, context)
                    self._authorize_command(row, command.actor_id, command.payload, context)
                    if replay := self._command_replay(command, fingerprint):
                        return replay
        if package.legacy_audit_only:
            raise StateMachineError(
                "legacy_rules_audit_only", "Old package permits audit reads only"
            )
        if type(command.payload) in (
            CombatIntentPayload,
            ItemUsePayload,
            ExplorationItemUsePayload,
            ExplorationEnvironmentalSpellCastPayload,
            ExplorationSpellCastPayload,
            ExplorationFeaturePayload,
            ExplorationMultiSpellCastPayload,
            ChoiceRespondOperationPayload,
            ChoiceFinalizeSpellOperationPayload,
            LongRest,
            ShortRest,
            StartSpellCast,
            FinishSpellCast,
            CombatClosePayload,
            CheckPayload,
            SavePayload,
            *OPENABLE_CHECK_OPERATIONS,
            CheckMechanism,
        ):
            import asyncio

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(self._submit_dnd(command, package, context, fingerprint))
            raise StateMachineError("async_context_required", "Use await submit_command_async")
        if type(command.payload) is RuleOperation:
            return self._submit_mechanical(command, package, context, fingerprint)
        command = self._session_runtime.prepare_timed_command(
            command, package, context, fingerprint
        )
        prepared = None
        mechanism_saves = _action_resume[0].prepared if _action_resume is not None else {}
        requests = ()
        grant = None
        if _action_resume is None and type(command.payload) in (SetLightEnabled, SetObjectState):
            with self._transaction():
                row = self._session(command.session_id, package, context)
                self._authorize_command(row, command.actor_id, command.payload, context)
                if not self._command_replay(
                    command, fingerprint
                ) and not self._session_runtime.version_conflict(row, command):
                    try:
                        requests = self._objects.mechanisms.prepare_direct_saves(
                            row, command.actor_id, command.payload, package, command.command_id
                        )
                        tick = self._world_facts.time_state(command.session_id)[1]
                        grant = self._world.characters.effects.held_bardic_inspiration(
                            command.session_id, command.actor_id, tick
                        )
                    except StateMachineError:
                        requests = ()  # The ordinary write path records the rejection.
                else:
                    requests = ()
            mechanism_saves, waiting, _ = self._objects.mechanisms.evaluate_direct_saves(
                requests, stop_on_failure=grant is not None, action_choice=grant is not None
            )
            if waiting is not None:
                from .action_pending import PendingActionChoice

                choice = PendingChoice(
                    command.command_id,
                    command.actor_id,
                    "rules.bardic_inspiration",
                    ("accept", "decline"),
                    grant.expires_tick,
                )
                receipt = CommandReceipt(
                    command.session_id,
                    command.command_id,
                    command.actor_id,
                    "pending_choice",
                    command.expected_world_version,
                    (),
                    pending_choice=choice,
                )
                candidate = PendingActionChoice(
                    command, fingerprint, requests, mechanism_saves, waiting, grant, receipt
                )
                return self._register_pending_action_choice(
                    candidate, package, context, requests[0][2].rng_context
                )
        if type(command.payload) is InitializeCombat:
            with self._transaction():
                row = self._session(command.session_id, package, context)
                self._authorize_command(row, command.actor_id, command.payload, context)
                if replay := self._command_replay(command, fingerprint):
                    return replay
                prepared = self._prepare_combat(dict(row), package)
        with self._transaction(write=True):
            row = self._session(command.session_id, package, context)
            self._authorize_command(row, command.actor_id, command.payload, context)
            self._session_runtime.authorize_time_wait_reply(row, package, command, context)
            self._npc_handoff_command_guard(command)
            replay = self._command_replay(command, fingerprint)
            if replay is not None:
                if _action_resume is not None:
                    _, response, response_fingerprint = _action_resume
                    answer_replay = self._command_replay(response, response_fingerprint)
                    if answer_replay is None:
                        raise StateMachineError(
                            "choice_closed", "Original action already completed"
                        )
                    self._discard_pending_action_choice(command.session_id)
                    return answer_replay
                return replay
            self._guard_pending_action_owner(
                command.session_id,
                resume_id=command.command_id if _action_resume is not None else None,
            )
            version = row["world_version"]
            error = None
            status = "committed"
            if _action_resume is None and self._session_runtime.version_conflict(row, command):
                status, error = "conflict", "version_conflict"
            else:
                self._conn.execute("SAVEPOINT command_effects")
                try:
                    if command.time_wait is not None:
                        self._session_runtime.time.waits.validate(row, package, command)
                    if type(command.payload) in NAVIGATION_OPERATIONS:
                        events = self._activities.operate(
                            row,
                            package,
                            command.payload,
                            actor_id=command.actor_id,
                            command_id=command.command_id,
                        )
                    else:
                        events = (
                            self._initialize_combat(row, package, prepared)
                            if isinstance(command.payload, InitializeCombat)
                            else self._operate(
                                row,
                                package,
                                command.payload,
                                actor_id=command.actor_id,
                                source_command_id=command.command_id,
                            )
                        )
                    events.extend(
                        self._session_runtime.finish_time_wait_reply(row, package, command, events)
                    )
                    if type(command.payload) not in (
                        AdvanceGameTime,
                        AdvanceSessionTime,
                        SetTimeRunning,
                    ):
                        token = self._objects.mechanisms._prepared_saves.set(mechanism_saves)
                        source_token = self._objects.mechanisms._source_operation.set(
                            command.payload
                        )
                        try:
                            events = self._expand_events(row, package, events, command.command_id)
                        finally:
                            self._objects.mechanisms._source_operation.reset(source_token)
                            self._objects.mechanisms._prepared_saves.reset(token)
                    if type(command.payload) is not InitializeCombat:
                        events.extend(
                            sync_npc_schedules(
                                self, command.session_id, command.command_id, package
                            )
                        )
                        if len(events) > 32:
                            raise StateMachineError(
                                "event_cascade_limit", "Bounded schedule cascade exceeded"
                            )
                    if events:
                        version = self._session_runtime.stage_outcome(command, row, package, events)
                except StateMachineError as exc:
                    if exc.code in {
                        "storage_error",
                        "session_reconciliation_required",
                        "invalid_rule_result",
                        "rule_evaluation_failed",
                        "rule_schema_error",
                        "ruleset_binding_mismatch",
                        "invalid_time_wait_result",
                    }:
                        raise
                    self._conn.execute("ROLLBACK TO command_effects")
                    self._store._scope.read_memo.clear()
                    version = row["world_version"]
                    status = (
                        "pending_choice"
                        if exc.code == "mechanism_needs_choice"
                        else "conflict"
                        if exc.code == "version_conflict"
                        else "rejected"
                    )
                    error = exc.code
                finally:
                    self._conn.execute("RELEASE command_effects")
            receipt = self._session_runtime.finish(
                command, context, fingerprint, row, status, version, error
            )
            if _action_resume is not None:
                _, response, response_fingerprint = _action_resume
                self._authorize_command(row, response.actor_id, response.payload, context)
                response_receipt = self._session_runtime.finish(
                    response,
                    context,
                    response_fingerprint,
                    row,
                    status,
                    version,
                    error,
                )
        self._session_runtime.refresh_time_drivers()
        if _action_resume is not None:
            self._discard_pending_action_choice(command.session_id)
            return response_receipt
        return receipt

    def get_command_receipt(self, session_id, command_id, package, *, context):
        """Authorize a durable receipt or this runtime's provisional action prompt."""
        with self._transaction():
            row = self._session(session_id, package, context)
            pending = self._pending_action_choices.get(session_id)
            if pending is not None and pending.command.command_id != command_id:
                pending = None
            if pending is not None:
                if pending.receipt.pending_choice.authorized_responder != row["pc_actor_id"]:
                    raise StateMachineError("unauthorized", "Action choice holder changed")
                return pending.receipt
            receipt = self._session_runtime.receipt(session_id, command_id)
            if receipt is None:
                return None
            self._authorize_receipt_actor(row, receipt, context)
            return receipt

    @staticmethod
    def _authorize_receipt_actor(row, receipt, context):
        """Enforce the current PC or controlled-NPC grant on a persisted receipt."""
        if receipt.actor_id != row["pc_actor_id"] and (
            receipt.actor_id not in context.controlled_npc_ids
        ):
            raise StateMachineError("unauthorized", "Actor control grant has been revoked")

    def get_command_rolls(self, session_id, command_id, package, *, context):
        """Project committed dice or authorized live action-choice dice evidence.

        Return None for an unknown command, otherwise a JSON-ready mapping with
        receipt status and rolls in committed event order. A roll_detail of None
        means that the stored event lacks detailed dice evidence, whether from an
        older event or a path without that evidence. Rest hit-die values are
        adjusted per-die healing, while Cure Wounds dice are raw faces. An empty
        rolls list means the receipt has no supported dice event. This read never evaluates rules or
        RNG and only follows event identities in the durable receipt. The selected
        PC's neutral Bardic marker links to private original dice evidence.
        """
        from .pending_bardic_checks import BARDIC_CHECK_PENDING_EVENT, pending_bardic_roll

        with self._transaction():
            row = self._session(session_id, package, context)
            pending = self._pending_action_choices.get(session_id)
            if pending is not None and pending.command.command_id != command_id:
                pending = None
            if pending is not None:
                from .action_pending import PendingFaerieChoice

                if pending.receipt.pending_choice.authorized_responder != row["pc_actor_id"]:
                    raise StateMachineError("unauthorized", "Action choice holder changed")
                rolls = []
                events = (
                    tuple(step.original for step in pending.progress.steps)
                    if isinstance(pending, PendingFaerieChoice)
                    else tuple(
                        event
                        for _, result, _ in pending.prepared.values()
                        for event in result.proposed_events
                    )
                )
                for event in events:
                    if event.type != "save_rolled":
                        continue
                    data = event.model_dump(mode="json")
                    rolls.append(
                        {
                            "event_seq": None,
                            "type": data["type"],
                            **{
                                key: data[key]
                                for key in (
                                    "target_id",
                                    "ability",
                                    "dc",
                                    "advantage",
                                    "natural",
                                    "modifier",
                                    "roll_total",
                                    "succeeded",
                                    "sources",
                                )
                                if key in data
                            },
                            "roll_detail": data.get("roll_detail"),
                        }
                    )
                return {
                    "session_id": session_id,
                    "command_id": command_id,
                    "status": "pending_choice",
                    "rolls": rolls,
                }
            receipt = self._session_runtime.receipt(session_id, command_id)
            if receipt is None:
                return None
            self._authorize_receipt_actor(row, receipt, context)
            rolls = []
            for seq in receipt.event_seqs:
                event = self._read_event(session_id, seq)
                if event.source_command_id != command_id:
                    raise StateMachineError(
                        "invalid_event", "Receipt event has another command source"
                    )
                if event.event_type == BARDIC_CHECK_PENDING_EVENT:
                    if receipt.actor_id != row["pc_actor_id"]:
                        raise StateMachineError("unauthorized", "Bardic choice holder changed")
                    engine = pending_bardic_roll(self._conn, receipt, event)
                else:
                    engine = event.payload.get("engine_event")
                if not isinstance(engine, dict) or engine.get("type") not in (
                    "check_rolled",
                    "save_rolled",
                    "rest_resolved",
                    "healing_applied",
                ):
                    continue
                if engine["type"] == "rest_resolved":
                    if engine.get("hit_dice"):
                        rolls.append(
                            {
                                "event_seq": seq,
                                "type": "hit_dice_healing",
                                "actor_id": engine["actor_id"],
                                "hit_dice": engine["hit_dice"],
                                "hp_regained": engine["hp_regained"],
                            }
                        )
                    continue
                if engine["type"] == "healing_applied":
                    if engine.get("roll_detail") is not None:
                        rolls.append(
                            {
                                "event_seq": seq,
                                "type": "healing_applied",
                                "target_id": engine["target_id"],
                                "amount": engine["amount"],
                                "hp_gained": event.payload.get("hp_gained"),
                                "roll_detail": engine["roll_detail"],
                            }
                        )
                    continue
                rolls.append(
                    {
                        "event_seq": seq,
                        "type": engine["type"],
                        **{
                            field: engine[field]
                            for field in (
                                "actor_id",
                                "target_id",
                                "ability",
                                "dc",
                                "advantage",
                                "natural",
                                "modifier",
                                "roll_total",
                                "succeeded",
                                "sources",
                            )
                            if field in engine
                        },
                        "roll_detail": engine.get("roll_detail"),
                    }
                )
            return {
                "session_id": session_id,
                "command_id": command_id,
                "status": receipt.status,
                "rolls": rolls,
            }


def world_command_from_data(data):
    """Closed local data adapter. Context/capabilities never come from these fields."""
    if type(data) is not dict or set(data) - {"time_wait"} != {
        "session_id",
        "command_id",
        "actor_id",
        "command_type",
        "payload",
        "expected_world_version",
        "schema_version",
    }:
        raise StateMachineError("invalid_command", "Expected the complete local command envelope")
    kind = data["command_type"]
    if type(kind) is not str or kind not in (
        *WORLD_CAPABILITY_COMMANDS,
        *ENVIRONMENT_COMMANDS,
        "world.step_actor",
        "world.advance_time",
        "session.advance_time",
        "session.set_time_running",
        "world.traverse_passage",
        "world.place_object",
        "world.unlock_door",
        "world.lock_door",
        "world.pickup_item",
        "world.drop_item",
        "world.use_item",
        "world.equip_wearable",
        "world.unequip_wearable",
        "rules.save",
        "world.cast_spell",
        "world.use_feature",
        "world.cast_spell_many",
        "choice.respond",
        "choice.finalize_spell",
        "world.drop_concentration",
        "world.dismiss_spell",
        "enter_scene",
        "move_actor",
        "set_object_state",
        "activity.start_navigation",
        "activity.advance_navigation",
        "activity.resume_navigation",
        "activity.cancel_navigation",
        "activity.long_rest",
        "activity.short_rest",
        "activity.start_spell_cast",
        "activity.finish_spell_cast",
    ):
        raise StateMachineError("unsupported_operation", "Unregistered world operation")
    if type(data["payload"]) is not dict:
        raise StateMachineError("invalid_command", "Expected a typed payload mapping")
    try:
        payload = data["payload"]
        if kind == "set_object_state":
            payload = {**payload, "state": ObjectState(payload["state"])}
        if kind in ("choice.finalize_spell", "world.cast_spell_many") or (
            kind == "world.cast_spell" and "target_ids" in payload
        ):
            targets = payload.get("target_ids")
            if type(targets) not in (list, tuple):
                raise ValueError("Expected ordered spell targets")
            payload = {**payload, "target_ids": tuple(targets)}
        if kind == "world.use_item":
            area = payload.get("area_target")
            if type(area) is dict and type(area.get("target_ids")) is list:
                payload = {
                    **payload,
                    "area_target": {**area, "target_ids": tuple(area["target_ids"])},
                }
        if kind == "activity.short_rest" and "hit_dice" in payload:
            selections = payload["hit_dice"]
            if type(selections) not in (list, tuple):
                raise ValueError("Expected ordered Hit Dice selections")
            payload = {**payload, "hit_dice": tuple(HitDiceSpend(**spend) for spend in selections)}
        operation = (
            environment_operation_from_data(kind, payload)
            if kind in ENVIRONMENT_COMMANDS
            else ExplorationEnvironmentalSpellCastPayload(**payload)
            if kind == "world.cast_spell" and "target" in payload
            else SavePayload.model_validate_json(json.dumps(payload))
            if kind == "rules.save"
            else OPERATIONS[kind](**payload)
        )
        ref = data.get("time_wait")
        if ref is not None:
            if type(ref) is not dict or set(ref) != {"wait_id", "version"}:
                raise StateMachineError("invalid_time_wait", "Expected a closed wait reference")
            ref = TimeWaitReference(**ref)
        command = TypedCommand(**{**data, "payload": operation, "time_wait": ref})
    except (TypeError, ValueError, KeyError) as exc:
        raise StateMachineError("invalid_command", "Unknown or missing payload fields") from exc
    validate_command(command)
    return command
