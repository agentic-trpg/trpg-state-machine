"""Transactionally bind one same-process NPC attack to its PC defender's answer."""

from dataclasses import replace

from dnd5e_engine.evaluation_contracts import CombatIntentPayload

from .choice_models import PendingChoice
from .combat import process_id
from .commands import TypedCommand, receipt_from_json
from .errors import StateMachineError


def current_window(connection, session_id):
    """Read the keyed Shield checkpoint for this Session, if present."""
    return connection.execute(
        "SELECT * FROM pending_shield_windows WHERE session_id=?", (session_id,)
    ).fetchone()


def guard_shield_window(connection, session):
    """Preserve the decision boundary while the process still owns its input version."""
    pending = current_window(connection, session["session_id"])
    if (
        pending is not None
        and pending["process_id"] == process_id()
        and pending["input_world_version"] == session["world_version"]
    ):
        raise StateMachineError("combat_choice_pending", "Resolve the current combat choice")


class ShieldWindows:
    """Keep the attack identity and private hit token in the existing short transaction."""

    def __init__(self, store, runtime, sessions):
        """Bind the existing command ledger and actor authorization."""
        self.store = store
        self.runtime = runtime
        self.sessions = sessions

    def create(self, command, token):
        """Insert the one pause and prepare its defender-facing prompt."""
        self.store.require_write()
        sid = command.session_id
        connection = self.store.connection
        connection.execute("DELETE FROM pending_shield_windows WHERE session_id=?", (sid,))
        connection.execute(
            "INSERT INTO pending_shield_windows "
            "(session_id,source_command_id,attacker_id,defender_id,payload_json,"
            "input_world_version,checkpoint_token,process_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                sid,
                command.command_id,
                command.actor_id,
                command.payload.target_id,
                command.payload.model_dump_json(),
                command.expected_world_version,
                token,
                process_id(),
            ),
        )
        return PendingChoice(
            command.command_id,
            command.payload.target_id,
            "combat.shield_reaction",
            ("accept", "decline"),
            None,
        )

    def for_response(self, response):
        """Route this response using its original prompt, even after that window closed."""
        receipt = self.store.connection.execute(
            "SELECT receipt_json FROM command_receipts WHERE session_id=? AND command_id=?",
            (response.session_id, response.payload.pending_operation_id),
        ).fetchone()
        if receipt is None:
            return None
        prompt = receipt_from_json(receipt[0]).pending_choice
        return prompt if prompt and prompt.choice_kind == "combat.shield_reaction" else None

    def check(self, response, fingerprint, session, context, prompt):
        """Authorize the exact defender, then check replay, process and fixed version."""
        self.sessions._authorize_command(session, response.actor_id, response.payload, context)
        self.sessions._npc_handoff_command_guard(response)
        if response.actor_id != prompt.authorized_responder:
            raise StateMachineError("unauthorized", "Choice belongs to another actor")
        if replay := self.runtime._command_replay(response, fingerprint):
            return replay, None, None
        pending = current_window(self.store.connection, response.session_id)
        if pending is None or pending["source_command_id"] != prompt.pending_operation_id:
            return None, None, "choice_closed"
        if pending["defender_id"] != prompt.authorized_responder:
            raise StateMachineError("storage_error", "Shield checkpoint differs from its prompt")
        if pending["process_id"] != process_id():
            return None, None, "combat_unrestorable"
        if self.runtime.version_conflict(session, response):
            return None, None, "version_conflict"
        if pending["input_world_version"] != session["world_version"]:
            return None, pending, "choice_stale"
        return None, pending, None

    def resume(self, pending, response):
        """Bind the privileged continuation to the original command and receipt identity."""
        original = TypedCommand(
            pending["session_id"],
            pending["source_command_id"],
            pending["attacker_id"],
            "combat.intent",
            CombatIntentPayload.model_validate_json(pending["payload_json"]),
            pending["input_world_version"],
        )
        if original.payload.target_id != pending["defender_id"]:
            raise StateMachineError("storage_error", "Shield checkpoint has a different target")
        return original, replace(original, command_id=response.command_id)

    def source_is_authorized(self, original):
        """Distinguish a revoked handoff grant from malformed provenance and faults."""
        try:
            self.sessions._npc_handoff_command_guard(original)
        except StateMachineError as error:
            if error.code != "unauthorized":
                raise
            return False
        return True

    def close(self, response):
        """Consume the pending metadata together with the committed answer and attack."""
        self.store.require_write()
        changed = self.store.connection.execute(
            "DELETE FROM pending_shield_windows WHERE session_id=? AND source_command_id=?",
            (response.session_id, response.payload.pending_operation_id),
        )
        if changed.rowcount != 1:
            raise StateMachineError("version_conflict", "Shield window changed")
