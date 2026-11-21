"""Persist per-target exploration spell choices alongside the command ledger."""

from dataclasses import replace

from dnd5e_engine.evaluation_contracts import (
    ExplorationMultiSpellCastPayload,
    ExplorationMultiSpellPositions,
    ExplorationMultiSpellTarget,
)

from .choice_models import ChoiceRespondOperationPayload, PendingChoice
from .commands import TypedCommand
from .errors import StateMachineError
from .time_binding import MAX_TICK, deadline_after_seconds

_OPTIONS = ("accept", "decline")
_LIFETIME_SECONDS = 60


class PendingMultiSpellChoices:
    """Keep cast inputs and replies keyed in Store, without advancing mechanics.

    RuleExecution owns fresh evaluation and final adoption. The terminal update
    joins that adoption and its receipt within the existing write transaction.
    """

    def __init__(self, store, runtime, sessions, facts):
        """Share the existing Store, command authorization and game clock."""
        self.store = store
        self.runtime = runtime
        self.sessions = sessions
        self.facts = facts

    def _cast(self, session_id, operation_id):
        """Read one pending cast by the session and immutable original command key."""
        return self.store.connection.execute(
            "SELECT * FROM pending_multi_spell_casts WHERE session_id=? AND source_command_id=?",
            (session_id, operation_id),
        ).fetchone()

    def _target(self, session_id, operation_id, responder_id):
        """Read one named responder without loading the other targets' replies."""
        return self.store.connection.execute(
            "SELECT decision FROM pending_multi_spell_targets "
            "WHERE session_id=? AND source_command_id=? AND responder_id=?",
            (session_id, operation_id, responder_id),
        ).fetchone()

    def for_response(self, response):
        """Find a response's operation in its caller's current transaction."""
        return self._cast(response.session_id, response.payload.pending_operation_id)

    def positions(self, session, command, *, verified_target_ids=()):
        """Build the scoped placements from current facts and trusted accepted replies."""
        actor_ids = tuple(dict.fromkeys((command.actor_id, *command.payload.target_ids)))
        placements = {}
        for actor_id in actor_ids:
            place = self.facts.carrier_position(command.session_id, actor_id)
            if place is None or place[0] != session["current_scene_id"] or place[1] is None:
                return "spell.target", actor_ids, None
            placements[actor_id] = place[1]
        return (
            None,
            actor_ids,
            ExplorationMultiSpellPositions(
                source_cell=placements[command.actor_id],
                targets=tuple(
                    ExplorationMultiSpellTarget(
                        target_id=target_id,
                        cell_id=placements[target_id],
                        willing=target_id == command.actor_id or target_id in verified_target_ids,
                    )
                    for target_id in command.payload.target_ids
                ),
            ),
        )

    @staticmethod
    def _prompt(operation_id, responder_id, deadline):
        """Return a single target's options and deadline."""
        return PendingChoice(
            operation_id, responder_id, "spell.targets_willingness", _OPTIONS, deadline
        )

    def create(self, command, unresolved_target_ids):
        """Save the original cast and each unresolved target with its source receipt."""
        self.store.require_write()
        connection = self.store.connection
        binding, tick = self.facts.time_state(command.session_id)
        if tick > MAX_TICK - _LIFETIME_SECONDS:
            return (), "choice_window_unavailable"
        deadline = deadline_after_seconds(binding, tick, _LIFETIME_SECONDS)
        connection.execute(
            "INSERT INTO pending_multi_spell_casts "
            "(session_id,source_command_id,actor_id,payload_json,input_world_version,"
            "created_tick,expires_tick,status) VALUES (?,?,?,?,?,?,?,'pending')",
            (
                command.session_id,
                command.command_id,
                command.actor_id,
                command.payload.model_dump_json(),
                command.expected_world_version,
                tick,
                deadline,
            ),
        )
        for target_id in unresolved_target_ids:
            connection.execute(
                "INSERT INTO pending_multi_spell_targets "
                "(session_id,source_command_id,responder_id) VALUES (?,?,?)",
                (command.session_id, command.command_id, target_id),
            )
        return (
            tuple(
                self._prompt(command.command_id, target_id, deadline)
                for target_id in unresolved_target_ids
            ),
            None,
        )

    def read_prompt(self, session, operation_id, responder_id, context):
        """Return a current prompt only to its currently authorized responder."""
        response = ChoiceRespondOperationPayload(operation_id, "accept")
        self.sessions._authorize_command(session, responder_id, response, context)
        sid = session["session_id"]
        pending = self._cast(sid, operation_id)
        if pending is None or self._closed_reason(pending):
            return None
        target = self._target(sid, operation_id, responder_id)
        if target is None or target["decision"] is not None:
            return None
        return self._prompt(operation_id, responder_id, pending["expires_tick"])

    def _closed_reason(self, pending):
        """Compare the persisted deadline to the current game tick."""
        if pending["status"] != "pending":
            return "choice_closed"
        if self.facts.time_state(pending["session_id"])[1] >= pending["expires_tick"]:
            return "choice_expired"
        return None

    def respond(self, response, fingerprint, session, context, pending):
        """Commit one authorized target's receipt without evaluating the cast."""
        self.store.require_write()
        self.sessions._authorize_command(session, response.actor_id, response.payload, context)
        self.sessions._npc_handoff_command_guard(response)
        operation_id = response.payload.pending_operation_id
        target = self._target(response.session_id, operation_id, response.actor_id)
        if target is None:
            raise StateMachineError("unauthorized", "Choice belongs to another actor")
        replay = self.runtime._command_replay(response, fingerprint)
        if replay is not None:
            return replay
        error = self._closed_reason(pending)
        if error is None and target["decision"] is not None:
            error = "choice_already_answered"
        if error is None and self.runtime.version_conflict(session, response):
            error = "version_conflict"
        if error is None:
            self.store.connection.execute(
                "UPDATE pending_multi_spell_targets SET decision=?,answer_command_id=? "
                "WHERE session_id=? AND source_command_id=? "
                "AND responder_id=? AND decision IS NULL",
                (
                    response.payload.selected_option_id,
                    response.command_id,
                    response.session_id,
                    operation_id,
                    response.actor_id,
                ),
            )
        elif error == "choice_expired":
            self.finish_finalize(response, "expired")
        return self._receipt(response, fingerprint, session, context, error)

    def prepare_finalize(self, confirmation, fingerprint, session, context):
        """Authorize the caster and select only original targets with valid consent.

        This performs keyed reads in the caller's read or write scope. Repeating
        it before adoption closes the gap between evaluation and committing.
        """
        self.sessions._authorize_command(
            session, confirmation.actor_id, confirmation.payload, context
        )
        operation_id = confirmation.payload.pending_operation_id
        pending = self._cast(confirmation.session_id, operation_id)
        if pending is not None and confirmation.actor_id != pending["actor_id"]:
            raise StateMachineError("unauthorized", "Only the original caster can finalize")
        replay = self.runtime._command_replay(confirmation, fingerprint)
        if replay is not None:
            return replay, None, None
        if pending is None:
            return None, None, "choice_not_found"
        error = self._closed_reason(pending)
        if error is not None:
            return None, None, error
        if self.runtime.version_conflict(session, confirmation):
            return None, None, "version_conflict"

        original = TypedCommand(
            confirmation.session_id,
            operation_id,
            pending["actor_id"],
            "world.cast_spell_many",
            ExplorationMultiSpellCastPayload.model_validate_json(pending["payload_json"]),
            pending["input_world_version"],
        )
        self.sessions._authorize_command(session, original.actor_id, original.payload, context)
        original_fingerprint = self.runtime.fingerprint(original, session, context)
        receipt = self.runtime._command_replay(original, original_fingerprint)
        if receipt is None or receipt.status != "pending_choice":
            raise StateMachineError("storage_error", "Pending spell has no original choice receipt")

        selected = set(confirmation.payload.target_ids)
        if not selected.issubset(original.payload.target_ids):
            return None, None, "choice_target_not_selected"
        ordered = tuple(
            target_id for target_id in original.payload.target_ids if target_id in selected
        )
        for target_id in ordered:
            if target_id == original.actor_id:
                continue
            target = self._target(confirmation.session_id, operation_id, target_id)
            if target is None or target["decision"] != "accept":
                return None, None, "choice_target_not_willing"
        return (
            None,
            replace(
                original,
                command_id=confirmation.command_id,
                expected_world_version=confirmation.expected_world_version,
                payload=original.payload.model_copy(update={"target_ids": ordered}),
            ),
            None,
        )

    def reject_finalize(self, confirmation, fingerprint, session, context, error):
        """Record a failed confirmation without closing a live retryable choice."""
        if error == "choice_expired":
            self.finish_finalize(confirmation, "expired")
        return self._receipt(confirmation, fingerprint, session, context, error)

    def finish_finalize(self, confirmation, status):
        """Close the group with its terminal receipt in the caller's write scope."""
        self.store.require_write()
        updated = self.store.connection.execute(
            "UPDATE pending_multi_spell_casts SET status=?,final_command_id=? "
            "WHERE session_id=? AND source_command_id=? AND status='pending'",
            (
                status,
                confirmation.command_id,
                confirmation.session_id,
                confirmation.payload.pending_operation_id,
            ),
        )
        if updated.rowcount != 1:
            raise StateMachineError("storage_error", "Pending spell could not be closed")

    def _receipt(self, command, fingerprint, session, context, error):
        """Record a metadata-only operation at the existing world version."""
        status = "committed"
        if error == "version_conflict":
            status = "conflict"
        elif error is not None:
            status = "rejected"
        return self.runtime.finish(
            command, context, fingerprint, session, status, session["world_version"], error
        )
