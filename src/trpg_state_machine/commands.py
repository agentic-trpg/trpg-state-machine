"""Versioned local command boundary; authentication belongs to the caller adapter."""

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass

from dnd5e_engine.evaluation_contracts import CheckPayload, CombatClosePayload, CombatIntentPayload
from pydantic import ValidationError

from ._operations import (
    DropItem,
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
from .combat import InitializeCombat
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
from .package import canonical_json
from .world_commands import (
    OPENABLE_CHECK_OPERATIONS,
    WORLD_CAPABILITY_COMMANDS,
    ActivateMechanism,
    BarObject,
    ClearJam,
    ForceOpen,
    LockObject,
    PickLock,
    PutItem,
    RepositionObject,
    SetLightEnabled,
    SwitchLightMode,
    TakeItem,
    UnbarObject,
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
    "world.step_actor": StepActor,
    "world.traverse_passage": TraversePassage,
    "world.place_object": PlaceObject,
    "world.advance_time": AdvanceGameTime,
    "rules.check": CheckPayload,
    "combat.close": CombatClosePayload,
    "combat.intent": CombatIntentPayload,
    "combat.initialize": InitializeCombat,
    "rule_operation": RuleOperation,
    "enter_scene": EnterScene,
    "set_scene_flag": SetSceneFlag,
    "set_object_state": SetObjectState,
    "move_actor": MoveActor,
}

OPERATIONS.update(WORLD_CAPABILITY_COMMANDS)


@dataclass(frozen=True, slots=True)
class CheckWorldEffects:
    """Local conditional-world-effects/1, fixed before any rule evaluation."""

    source_ref: str
    preconditions: tuple[SetSceneFlag | SetObjectState, ...] = ()
    on_success: tuple[SetSceneFlag | SetObjectState, ...] = ()
    on_failure: tuple[SetSceneFlag | SetObjectState, ...] = ()


@dataclass(frozen=True, slots=True)
class TypedCommand:
    session_id: str
    command_id: str
    actor_id: str
    command_type: str
    payload: (
        PickLock
        | ForceOpen
        | ClearJam
        | ActivateMechanism
        | SetLightEnabled
        | SwitchLightMode
        | RepositionObject
        | PutItem
        | TakeItem
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
        | CombatClosePayload
        | CheckPayload
        | AdvanceGameTime
        | StartNavigation
        | AdvanceNavigation
        | ResumeNavigation
        | CancelNavigation
        | PickupItem
        | DropItem
        | UnlockDoor
        | LockDoor
    )
    expected_world_version: int
    schema_version: str = "sm-command/1"
    check_world_effects: CheckWorldEffects | None = None


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    session_id: str
    command_id: str
    actor_id: str
    status: str
    world_version: int
    event_seqs: tuple[int, ...]
    error_code: str | None = None


def receipt_from_json(raw: str) -> CommandReceipt:
    data = json.loads(raw)
    data["event_seqs"] = tuple(data["event_seqs"])
    return CommandReceipt(**data)


def validate_command(command: TypedCommand) -> None:
    if type(command) is not TypedCommand or command.schema_version != "sm-command/1":
        raise StateMachineError("invalid_command", "Expected sm-command/1")
    if any(
        type(v) is not str or not v.strip() or len(v) > 256
        for v in (command.session_id, command.command_id, command.actor_id, command.command_type)
    ):
        raise StateMachineError("invalid_command", "Command identities must be bounded strings")
    if type(command.expected_world_version) is not int or command.expected_world_version < 0:
        raise StateMachineError("invalid_operation", "Expected a nonnegative integer version")
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
    if operation_type is None or type(command.payload) is not operation_type:
        raise StateMachineError("unsupported_operation", "Unknown command or payload type")
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
    if type(command.payload) is AdvanceGameTime:
        if any(
            type(v) is not int or not 0 <= v <= 2147483647 for v in asdict(command.payload).values()
        ):
            raise StateMachineError("invalid_operation", "Expected bounded abstract game ticks")
        if command.payload.next_tick < command.payload.expected_tick:
            raise StateMachineError("invalid_operation", "Game time cannot move backwards")
        return
    if type(command.payload) in (CombatIntentPayload, CombatClosePayload, CheckPayload):
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
            isinstance(command.payload, CheckPayload)
            and command.payload.actor_id != command.actor_id
        ):
            raise StateMachineError("invalid_actor", "Check subject differs from trusted actor")
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
        if key in ("value", "enabled"):
            if type(value) is not bool:
                raise StateMachineError("invalid_operation", "Flag must be boolean")
        elif (
            key == "tool_instance_id"
            and value is None
            and type(command.payload) in (ForceOpen, ClearJam)
        ):
            continue
        elif not isinstance(value, str) or not value or len(value) > 256:
            raise StateMachineError("invalid_operation", "Invalid operation field")


def command_fingerprint(command, package, context):
    data = asdict(command)
    if command.check_world_effects is None:
        # Preserve canonical fingerprints of all pre-SM21 stored commands.
        data.pop("check_world_effects")
    if type(command.payload) in (CombatIntentPayload, CombatClosePayload, CheckPayload):
        data["payload"] = command.payload.model_dump(mode="json")
    return hashlib.sha256(
        canonical_json(
            {
                "command": data,
                "principal": context.principal_id,
                "player": context.player_id,
                "package": package.package_digest,
                "binding": asdict(package.ruleset_binding),
            }
        ).encode()
    ).hexdigest()


class CommandRuntime:
    def _authorize_command(self, row, actor_id, payload, context):
        return self._sessions._authorize_command(row, actor_id, payload, context)

    def _world_operation_capability(self, row, payload):
        return self._sessions._world_operation_capability(row, payload)

    def _command_replay(self, command, fingerprint):
        return self._sessions._command_replay(command, fingerprint)

    def submit_command(self, command: TypedCommand, package, *, context) -> CommandReceipt:
        validate_command(command)
        self.read_session(command.session_id, package, context=context)
        if package.legacy_audit_only:
            raise StateMachineError(
                "legacy_rules_audit_only", "Old package permits audit reads only"
            )
        fingerprint = command_fingerprint(command, package, context)
        if type(command.payload) in (
            CombatIntentPayload,
            CombatClosePayload,
            CheckPayload,
            *OPENABLE_CHECK_OPERATIONS,
        ):
            import asyncio

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(self._submit_dnd(command, package, context, fingerprint))
            raise StateMachineError("async_context_required", "Use await submit_command_async")
        if type(command.payload) is RuleOperation:
            return self._submit_mechanical(command, package, context, fingerprint)
        prepared = None
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
            self._npc_handoff_command_guard(command)
            replay = self._command_replay(command, fingerprint)
            if replay is not None:
                return replay
            version = row["world_version"]
            seq = row["event_seq"]
            error = None
            status = "committed"
            if version != command.expected_world_version:
                status, error = "conflict", "version_conflict"
            else:
                self._conn.execute("SAVEPOINT command_effects")
                try:
                    if package.portable_items:
                        self._characters.rule_components(
                            command.session_id,
                            set(self._characters.rule_actors(command.session_id)),
                        )
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
                    events = self._expand_events(row, package, events, command.command_id)
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
                        if package.portable_items:
                            self._characters.sync_world_inventory(command.session_id)
                        self._assert_spatial_consistency(command.session_id, package)
                        version += 1
                        self._sessions.set_world_version(command.session_id, version)
                        self._append_events(
                            command.session_id,
                            version,
                            events,
                            source_command_id=command.command_id,
                            package=package,
                        )
                except StateMachineError as exc:
                    if exc.code == "storage_error":
                        raise
                    self._conn.execute("ROLLBACK TO command_effects")
                    version = row["world_version"]
                    status, error = "rejected", exc.code
                finally:
                    self._conn.execute("RELEASE command_effects")
            end_seq = self._conn.execute(
                "SELECT event_seq FROM sessions WHERE session_id = ?", (command.session_id,)
            ).fetchone()[0]
            receipt = CommandReceipt(
                command.session_id,
                command.command_id,
                command.actor_id,
                status,
                version,
                tuple(range(seq + 1, end_seq + 1)),
                error,
            )
            self._sessions.record_receipt(command, context, fingerprint, receipt)
        return receipt

    def get_command_receipt(self, session_id, command_id, package, *, context):
        with self._transaction():
            row = self._session(session_id, package, context)
            stored = self._conn.execute(
                "SELECT receipt_json FROM command_receipts WHERE session_id=? AND command_id=?",
                (session_id, command_id),
            ).fetchone()
            if stored is None:
                return None
            receipt = receipt_from_json(stored[0])
            if receipt.actor_id != row["pc_actor_id"] and (
                receipt.actor_id not in context.controlled_npc_ids
            ):
                raise StateMachineError("unauthorized", "Actor control grant has been revoked")
            return receipt


def world_command_from_data(data):
    """Closed local data adapter. Context/capabilities never come from these fields."""
    if type(data) is not dict or set(data) != {
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
        "world.step_actor",
        "world.traverse_passage",
        "world.place_object",
        "world.unlock_door",
        "world.lock_door",
        "world.pickup_item",
        "world.drop_item",
        "enter_scene",
        "move_actor",
        "set_object_state",
        "activity.start_navigation",
        "activity.advance_navigation",
        "activity.resume_navigation",
        "activity.cancel_navigation",
    ):
        raise StateMachineError("unsupported_operation", "Unregistered world operation")
    if type(data["payload"]) is not dict:
        raise StateMachineError("invalid_command", "Expected a typed payload mapping")
    try:
        payload = data["payload"]
        if kind == "set_object_state":
            payload = {**payload, "state": ObjectState(payload["state"])}
        command = TypedCommand(**{**data, "payload": OPERATIONS[kind](**payload)})
    except (TypeError, ValueError, KeyError) as exc:
        raise StateMachineError("invalid_command", "Unknown or missing payload fields") from exc
    validate_command(command)
    return command
