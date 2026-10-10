"""Versioned local command boundary; authentication belongs to the caller adapter."""

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass

from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from pydantic import ValidationError

from ._operations import EnterScene, MoveActor, SetObjectState, SetSceneFlag
from .combat import InitializeCombat
from .errors import StateMachineError
from .mechanics import RuleOperation
from .package import canonical_json

OPERATIONS = {
    "combat.intent": CombatIntentPayload,
    "combat.initialize": InitializeCombat,
    "rule_operation": RuleOperation,
    "enter_scene": EnterScene,
    "set_scene_flag": SetSceneFlag,
    "set_object_state": SetObjectState,
    "move_actor": MoveActor,
}


@dataclass(frozen=True, slots=True)
class TypedCommand:
    session_id: str
    command_id: str
    actor_id: str
    command_type: str
    payload: (
        EnterScene
        | SetSceneFlag
        | SetObjectState
        | MoveActor
        | RuleOperation
        | InitializeCombat
        | CombatIntentPayload
    )
    expected_world_version: int
    schema_version: str = "sm-command/1"


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
    operation_type = OPERATIONS.get(command.command_type)
    if operation_type is None or type(command.payload) is not operation_type:
        raise StateMachineError("unsupported_operation", "Unknown command or payload type")
    if type(command.payload) is CombatIntentPayload:
        try:
            CombatIntentPayload.model_validate(deepcopy(command.payload))
        except ValidationError as exc:
            raise StateMachineError("invalid_command", "Invalid Engine intent schema") from exc
        if command.payload.source_id not in (None, command.actor_id):
            raise StateMachineError("invalid_actor", "Intent source differs from trusted actor")
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
        if key == "value":
            if type(value) is not bool:
                raise StateMachineError("invalid_operation", "Flag must be boolean")
        elif not isinstance(value, str) or not value or len(value) > 256:
            raise StateMachineError("invalid_operation", "Invalid operation field")


def command_fingerprint(command, package, context):
    data = asdict(command)
    if type(command.payload) is CombatIntentPayload:
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
        if isinstance(payload, MoveActor) or (
            isinstance(payload, CombatIntentPayload) and actor_id != row["pc_actor_id"]
        ):
            if isinstance(payload, MoveActor) and actor_id != payload.actor_id:
                raise StateMachineError("invalid_actor", "Envelope and payload actors differ")
            actor = self._conn.execute(
                "SELECT kind FROM actors WHERE session_id=? AND actor_id=?",
                (row["session_id"], actor_id),
            ).fetchone()
            if actor is None or actor[0] != "npc":
                raise StateMachineError("invalid_target", "Expected an existing NPC")
            if actor_id not in context.controlled_npc_ids:
                raise StateMachineError("unauthorized", "Trusted NPC control grant required")
        elif actor_id != row["pc_actor_id"]:
            raise StateMachineError("invalid_actor", "Command requires the selected PC")

    def _world_operation_capability(self, row, payload):
        if isinstance(payload, (EnterScene, MoveActor, SetObjectState)):
            combat = self._conn.execute(
                "SELECT state_json FROM combats WHERE session_id=?", (row["session_id"],)
            ).fetchone()
            if combat and not json.loads(combat[0])["ended"]:
                raise StateMachineError(
                    "unsupported_combat_world_operation",
                    "This world operation has no represented mechanical Delta during combat",
                )

    def _command_replay(self, command, fingerprint):
        stored = self._conn.execute(
            "SELECT fingerprint, receipt_json FROM command_receipts "
            "WHERE session_id = ? AND command_id = ?",
            (command.session_id, command.command_id),
        ).fetchone()
        if stored is None:
            return None
        if stored["fingerprint"] != fingerprint:
            raise StateMachineError("idempotency_conflict", "Command key has different content")
        return receipt_from_json(stored["receipt_json"])

    def submit_command(self, command: TypedCommand, package, *, context) -> CommandReceipt:
        validate_command(command)
        self.read_session(command.session_id, package, context=context)
        fingerprint = command_fingerprint(command, package, context)
        if type(command.payload) is CombatIntentPayload:
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
                prepared_row = dict(row)
            prepared = self._prepare_combat(prepared_row, package)
        with self._transaction(write=True):
            row = self._session(command.session_id, package, context)
            self._authorize_command(row, command.actor_id, command.payload, context)
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
                    events = (
                        self._initialize_combat(row, package, prepared)
                        if isinstance(command.payload, InitializeCombat)
                        else self._operate(row, package, command.payload)
                    )
                    events = self._expand_events(row, package, events, command.command_id)
                    if events:
                        version += 1
                        self._conn.execute(
                            "UPDATE sessions SET world_version = ? WHERE session_id = ?",
                            (version, command.session_id),
                        )
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
            self._conn.execute(
                "INSERT INTO command_receipts VALUES (?, ?, ?, ?, ?)",
                (
                    command.session_id,
                    command.command_id,
                    context.principal_id,
                    fingerprint,
                    canonical_json(asdict(receipt)),
                ),
            )
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
