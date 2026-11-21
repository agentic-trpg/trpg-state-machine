"""Save and resolve exploration spell prompts in RuleExecution's command transaction."""

from dataclasses import replace

from dnd5e_engine.evaluation_contracts import ExplorationSpellCastPayload

from .choice_models import PendingChoice
from .commands import TypedCommand
from .errors import StateMachineError
from .time_binding import MAX_TICK, deadline_after_seconds

_CHOICES = ("accept", "decline")
_LIFETIME_SECONDS = 60


class PendingSpellChoices:
    """Keep immutable cast inputs and terminal response identity in keyed SQLite rows.

    RuleExecution owns prepare/evaluate/adopt. The command ledger holds the immutable
    prompt receipt and binds the saved command to its authenticated owner and rules.
    Pending metadata uses the Store's shared keyed reads with no separate
    runtime representation or mechanical projection.
    """

    def __init__(self, store, runtime, sessions, facts):
        """Use the existing command transaction and its Session clock."""
        self.store = store
        self.runtime = runtime
        self.sessions = sessions
        self.facts = facts

    def create(self, command):
        """Create a response window for the cast's target under the shared write lock."""
        self.store.require_write()
        connection = self.store.connection
        target = command.payload.target_id
        actor = connection.execute(
            "SELECT kind FROM actors WHERE session_id=? AND actor_id=?",
            (command.session_id, target),
        ).fetchone()
        if actor is None or actor["kind"] != "npc":
            return None, "spell.target"
        binding, tick = self.facts.time_state(command.session_id)
        if tick > MAX_TICK - _LIFETIME_SECONDS:
            return None, "choice_window_unavailable"
        deadline = deadline_after_seconds(binding, tick, _LIFETIME_SECONDS)
        connection.execute(
            "INSERT INTO pending_spell_choices "
            "(session_id,source_command_id,actor_id,responder_id,payload_json,"
            "input_world_version,created_tick,expires_tick,status) "
            "VALUES (?,?,?,?,?,?,?,?,'pending')",
            (
                command.session_id,
                command.command_id,
                command.actor_id,
                target,
                command.payload.model_dump_json(),
                command.expected_world_version,
                tick,
                deadline,
            ),
        )
        return PendingChoice(
            command.command_id, target, "spell.target_willingness", _CHOICES, deadline
        ), None

    def check(self, response, fingerprint, session, context):
        """Authorize an exact response and return a replay, row, or terminal refusal.

        The receipt is checked under the same authorization as a new response. Clock
        expiry is observed lazily. Version conflicts leave a live operation available
        to a new response with the current expected version.
        """
        self.sessions._authorize_command(session, response.actor_id, response.payload, context)
        self.sessions._npc_handoff_command_guard(response)
        pending = self.store.connection.execute(
            "SELECT * FROM pending_spell_choices WHERE session_id=? AND source_command_id=?",
            (response.session_id, response.payload.pending_operation_id),
        ).fetchone()
        if pending is not None and response.actor_id != pending["responder_id"]:
            raise StateMachineError("unauthorized", "Choice belongs to another actor")
        replay = self.runtime._command_replay(response, fingerprint)
        if replay is not None:
            return replay, None, None
        if pending is None:
            return None, None, "choice_not_found"
        if pending["status"] != "pending":
            return None, None, "choice_closed"
        if self.facts.time_state(response.session_id)[1] >= pending["expires_tick"]:
            return None, pending, "choice_expired"
        if self.runtime.version_conflict(session, response):
            return None, pending, "version_conflict"
        return None, pending, None

    def resume(self, pending, response, session, context):
        """Authenticate the saved cast before using the response's execution identity."""
        command = TypedCommand(
            pending["session_id"],
            pending["source_command_id"],
            pending["actor_id"],
            "world.cast_spell",
            ExplorationSpellCastPayload.model_validate_json(pending["payload_json"]),
            pending["input_world_version"],
        )
        self.sessions._authorize_command(session, command.actor_id, command.payload, context)
        fingerprint = self.runtime.fingerprint(command, session, context)
        receipt = self.runtime._command_replay(command, fingerprint)
        if receipt is None or receipt.status != "pending_choice":
            raise StateMachineError("storage_error", "Pending spell has no original choice receipt")
        if command.payload.target_id != pending["responder_id"]:
            raise StateMachineError("storage_error", "Pending spell responder differs from target")
        return replace(
            command,
            command_id=response.command_id,
            expected_world_version=response.expected_world_version,
        )

    def close(self, response, status):
        """Associate one terminal response with the prompt in the same write scope."""
        self.store.require_write()
        updated = self.store.connection.execute(
            "UPDATE pending_spell_choices SET status=?,answer_command_id=? "
            "WHERE session_id=? AND source_command_id=? AND status='pending'",
            (
                status,
                response.command_id,
                response.session_id,
                response.payload.pending_operation_id,
            ),
        )
        if updated.rowcount != 1:
            raise StateMachineError("storage_error", "Pending spell could not be closed")
