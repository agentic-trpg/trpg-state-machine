"""Persist failed standalone D20 evidence and adopt a holder's Bardic choice."""

import json

from dnd5e_engine.evaluation_bardic_check import (
    BARDIC_INSPIRATION_SOURCE,
    check_bardic_fingerprint,
    resolve_bardic_check_choice,
)
from dnd5e_engine.evaluation_contracts import CheckPayload, SavePayload
from dnd5e_engine.evaluation_ruleset import RulesetBindingError
from dnd5e_engine.evaluation_saves import resolve_bardic_save_choice, save_bardic_fingerprint
from dnd5e_engine.events import CheckRolled, RollDieModifier, SaveRolled

from .choice_models import PendingChoice
from .commands import TypedCommand
from .errors import StateMachineError
from .ongoing_effects import BARDIC_INSPIRATION_EFFECT_ID, BardicInspirationGrant
from .package import canonical_json
from .rules_adapter import validate_dnd_result
from .shield_window import guard_shield_window

_KIND = "rules.bardic_inspiration"
BARDIC_CHECK_PENDING_EVENT = "rules.bardic_check_pending"
_OPTIONS = ("accept", "decline")


def _read_checkpoint(connection, session_id, source_command_id):
    """Read one exact checkpoint within the caller's authorized Session scope."""
    return connection.execute(
        "SELECT session_id,source_command_id,actor_id,source_event_seq,facts_fingerprint,"
        "grant_origin,grant_id,die_sides,expires_tick,status FROM pending_bardic_checks "
        "WHERE session_id=? AND source_command_id=?",
        (session_id, source_command_id),
    ).fetchone()


def _original_evidence(connection, pending, receipt, event):
    """Associate private evidence with its neutral event and immutable receipt."""
    if (
        pending is None
        or receipt is None
        or receipt.status != "pending_choice"
        or receipt.actor_id != pending["actor_id"]
        or receipt.pending_choice is None
        or receipt.pending_choice.choice_kind != _KIND
        or receipt.pending_choice.pending_operation_id != pending["source_command_id"]
        or receipt.pending_choice.authorized_responder != pending["actor_id"]
        or receipt.pending_choice.expires_at_tick != pending["expires_tick"]
        or event.event_seq != pending["source_event_seq"]
        or event.event_seq not in receipt.event_seqs
        or event.source_command_id != pending["source_command_id"]
        or event.event_type != BARDIC_CHECK_PENDING_EVENT
    ):
        raise StateMachineError("storage_error", "Original Bardic check evidence differs")
    data = connection.execute(
        "SELECT original_evidence_json FROM pending_bardic_checks "
        "WHERE session_id=? AND source_command_id=?",
        (receipt.session_id, receipt.command_id),
    ).fetchone()
    try:
        evidence = json.loads(data[0])
        if not isinstance(evidence.get("engine_event"), dict):
            raise ValueError("Missing committed roll")
    except (AttributeError, TypeError, ValueError) as exc:
        raise StateMachineError("storage_error", "Malformed Bardic check evidence") from exc
    return evidence


def pending_bardic_roll(connection, receipt, event):
    """Return the original roll for the caller's actor-authorized dice projection."""
    pending = _read_checkpoint(connection, receipt.session_id, receipt.command_id)
    evidence = _original_evidence(connection, pending, receipt, event)
    return evidence["engine_event"]


class PendingBardicChecks:
    """Coordinate check and standalone save choices through Effects and receipts.

    A prompt records its original roll privately behind a neutral audit event. The response
    authenticates the holder and uses its own OCC version. It compares the
    roll's bounded dependencies and exact grant against the saved checkpoint,
    then commits a result calculated from the old roll and the current RNG.
    """

    def __init__(self, execution):
        """Bind the cooperating services used by the normal rule command path."""
        self.store = execution.store
        self.runtime = execution.runtime
        self.sessions = execution.sessions
        self.rules = execution.rules
        self.world = execution.world
        self.capture = execution.capture
        self.persistence = execution.persistence
        self.effects = execution.effects
        self.grants = self.world.characters.effects

    def for_response(self, response):
        """Find a window by the exact original command identity in this Session."""
        return _read_checkpoint(
            self.store.connection, response.session_id, response.payload.pending_operation_id
        )

    def fingerprint(self, request):
        """Use RE's draw-free rule digest; unsupported rolls open no window."""
        try:
            if request.operation_kind == "rules.save":
                return save_bardic_fingerprint(request, self.rules.loader)
            return check_bardic_fingerprint(request, self.rules.loader)
        except RulesetBindingError as exc:
            raise StateMachineError(
                "ruleset_binding_mismatch", "Evaluator binding differs"
            ) from exc
        except ValueError:
            return None

    def candidate(self, command, request, result):
        """Read a failed eligible roll's held grant before hashing its dependencies."""
        is_save = command.command_type == "rules.save" and type(command.payload) is SavePayload
        if (
            command.command_type not in {"rules.check", "rules.save"}
            or command.check_world_effects is not None
            or command.time_wait is not None
            or (not is_save and type(command.payload) is not CheckPayload)
            or command.payload.dc is None
            or (not is_save and command.payload.target_id is not None)
            or (is_save and command.payload.target_id != command.actor_id)
            or result is None
            or result.status != "accepted"
            or len(result.proposed_events) != 1
        ):
            return None
        event = result.proposed_events[0]
        if (
            type(event) is not (SaveRolled if is_save else CheckRolled)
            or event.succeeded is not False
        ):
            return None
        if is_save and (event.roll_detail is None or event.roll_detail.status != "rolled"):
            return None
        with self.store.transaction():
            grant = self._held_grant(command.session_id, command.actor_id)
        if grant is None:
            return None
        digest = self.fingerprint(request)
        return (grant, digest) if digest is not None else None

    def _held_grant(self, session_id, actor_id):
        """Read the exact grant against its Session's committed tick."""
        tick = self.world.facts.time_state(session_id)[1]
        return self.grants.held_bardic_inspiration(session_id, actor_id, tick)

    def open_if_held(self, row, package, command, context, request, result, candidate):
        """Stage the original roll and a prompt atomically if the PC holds a timed grant."""
        if command.actor_id != row["pc_actor_id"]:
            return None
        sid = command.session_id
        grant, facts_fingerprint = candidate
        if self._held_grant(sid, command.actor_id) != grant:
            return None
        result, _ = validate_dnd_result(request, result, loader=self.rules.loader)
        evidence = {
            "adjudication": {
                "principal_id": context.principal_id,
                "payload": command.payload.model_dump(mode="json"),
            },
            "scene_id": request.state_snapshot.scene_state.scene_id,
            "engine_event": result.proposed_events[0].model_dump(mode="json"),
        }
        event = self.capture._capture_event(
            sid,
            package,
            BARDIC_CHECK_PENDING_EVENT,
            {},
            phase="rules.save_awaiting_bardic_choice"
            if type(command.payload) is SavePayload
            else "rules.check_awaiting_bardic_choice",
        )
        self.runtime.advance_rule_rng(sid, result.rng_transition, request.rng_context)
        version = self.runtime.stage_outcome(command, row, package, [event])
        self.store.connection.execute(
            "INSERT INTO pending_bardic_checks(session_id,source_command_id,actor_id,"
            "source_event_seq,original_evidence_json,facts_fingerprint,grant_origin,grant_id,die_sides,"
            "expires_tick,status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'pending')",
            (
                sid,
                command.command_id,
                command.actor_id,
                row["event_seq"] + 1,
                canonical_json(evidence),
                facts_fingerprint,
                grant.origin,
                grant.grant_id,
                grant.die_sides,
                grant.expires_tick,
            ),
        )
        return PendingChoice(
            command.command_id, command.actor_id, _KIND, _OPTIONS, grant.expires_tick
        ), version

    def _admit_response(self, response, package, context, fingerprint):
        """Authorize the holder and replay before validating the current reply version."""
        row = self.sessions._session(response.session_id, package, context)
        self.sessions._authorize_command(row, response.actor_id, response.payload, context)
        self.sessions._npc_handoff_command_guard(response)
        pending = self.for_response(response)
        if pending is None:
            raise StateMachineError("storage_error", "Bardic check checkpoint is missing")
        if pending["actor_id"] != response.actor_id:
            raise StateMachineError("unauthorized", "Choice belongs to another actor")
        replay = self.runtime._command_replay(response, fingerprint)
        error = None
        if replay is None:
            if pending["status"] != "pending":
                error = "choice_closed"
            elif self.runtime.version_conflict(row, response):
                error = "version_conflict"
            elif self.world.facts.time_state(response.session_id)[1] >= pending["expires_tick"]:
                error = "choice_expired"
            elif response.actor_id != row["pc_actor_id"] or self.world._active_combat(
                response.session_id
            ):
                error = "choice_stale"
            else:
                guard_shield_window(self.store.connection, row)
                self.rules._verify_dnd_binding(package, row)
        return row, pending, replay, error

    def _original(self, pending, row):
        """Restore private roll evidence bound to the originating receipt and marker."""
        sid, source_id, seq = (
            pending["session_id"],
            pending["source_command_id"],
            pending["source_event_seq"],
        )
        receipt = self.runtime.receipt(sid, source_id)
        event = self.persistence._read_event(sid, seq)
        evidence = _original_evidence(self.store.connection, pending, receipt, event)
        try:
            event_type = evidence["engine_event"]["type"]
            if event_type not in {"check_rolled", "save_rolled"}:
                raise ValueError("Unknown Bardic roll kind")
            payload_type = SavePayload if event_type == "save_rolled" else CheckPayload
            roll_type = SaveRolled if event_type == "save_rolled" else CheckRolled
            payload = payload_type.model_validate_json(
                canonical_json(evidence["adjudication"]["payload"])
            )
            roll = roll_type.model_validate_json(canonical_json(evidence["engine_event"]))
            if event_type == "save_rolled" and (
                payload.target_id != pending["actor_id"]
                or roll.target_id != pending["actor_id"]
                or (roll.ability, roll.dc) != (payload.ability, payload.dc)
            ):
                raise ValueError("Save identity differs from saved choice")
        except (KeyError, ValueError) as exc:
            raise StateMachineError("storage_error", "Malformed Bardic check evidence") from exc
        command = TypedCommand(
            sid,
            source_id,
            pending["actor_id"],
            "rules.save" if event_type == "save_rolled" else "rules.check",
            payload,
            row["world_version"],
        )
        return command, roll, evidence

    def _current_rng(self, row, package, source, pending):
        """Compare the original rule dependency digest and exact active grant."""
        sid, holder = source.session_id, source.actor_id
        saved_grant = BardicInspirationGrant(
            pending["grant_origin"],
            pending["grant_id"],
            pending["die_sides"],
            pending["expires_tick"],
        )
        if self._held_grant(sid, holder) != saved_grant:
            return None
        rng = self.rules._rng_context(row)
        snapshot = self.rules._noncombat_snapshot(row, actor_ids=(holder,))
        request = self.rules.request(source, package, snapshot, rng, row=row)
        if source.command_type == "rules.check":
            _, facts = self.rules.prepare_check_sight(request, package)
            if facts:
                request = request.model_copy(update={"creature_sight_facts": facts})
        return rng if self.fingerprint(request) == pending["facts_fingerprint"] else None

    def _close(self, response, status):
        """Bind one terminal response under the same transaction as its receipt."""
        changed = self.store.connection.execute(
            "UPDATE pending_bardic_checks SET status=?,answer_command_id=? "
            "WHERE session_id=? AND source_command_id=? AND status='pending'",
            (
                status,
                response.command_id,
                response.session_id,
                response.payload.pending_operation_id,
            ),
        )
        if changed.rowcount != 1:
            raise StateMachineError("storage_error", "Bardic check could not be closed")

    def respond(self, response, package, context, fingerprint):
        """Resolve outside the write lock, then adopt under current reply guards."""
        source = original = evidence = rng = None
        with self.store.transaction():
            row, pending, replay, error = self._admit_response(
                response, package, context, fingerprint
            )
            if replay is not None:
                return replay
            if error is None:
                source, original, evidence = self._original(pending, row)
                rng = self._current_rng(row, package, source, pending)
        use = response.payload.selected_option_id == "accept"
        if rng is not None:
            try:
                resolver = (
                    resolve_bardic_save_choice
                    if source.command_type == "rules.save"
                    else resolve_bardic_check_choice
                )
                final, transition = resolver(original, pending["die_sides"], rng, use=use)
            except ValueError as exc:
                raise StateMachineError(
                    "invalid_rule_result", "Bardic continuation failed"
                ) from exc
        with self.store.transaction(write=True):
            row, pending, replay, current_error = self._admit_response(
                response, package, context, fingerprint
            )
            if replay is not None:
                return replay
            error = current_error or ("version_conflict" if source is None else None)
            if error is None:
                current_rng = self._current_rng(row, package, source, pending)
                if current_rng is None:
                    error = "choice_stale"
                elif current_rng != rng:
                    error = "version_conflict"
            if error is not None:
                if error in {"choice_expired", "choice_stale"}:
                    self._close(response, error.removeprefix("choice_"))
                status = "conflict" if error == "version_conflict" else "rejected"
                return self.runtime.finish(
                    response, context, fingerprint, row, status, row["world_version"], error
                )
            sid = response.session_id
            if use:
                modifiers = final.roll_detail.modifiers if final.roll_detail is not None else []
                bonus = modifiers[-1] if modifiers else None
                if (
                    type(bonus) is not RollDieModifier
                    or bonus.source != BARDIC_INSPIRATION_SOURCE
                    or bonus.sides != pending["die_sides"]
                    or bonus.face != bonus.value
                    or (isinstance(final, CheckRolled) and bonus.face != final.inspiration_bonus)
                    or not transition.state_changed
                ):
                    raise StateMachineError("invalid_rule_result", "Bardic spend evidence differs")
                self.runtime.advance_rule_rng(sid, transition, rng)
                self.grants.remove_effect(
                    sid, response.actor_id, BARDIC_INSPIRATION_EFFECT_ID, pending["grant_origin"]
                )
            elif final != original or transition.state_changed:
                raise StateMachineError(
                    "invalid_rule_result", "Declining changed the original roll"
                )
            event = self.capture._capture_event(
                sid,
                package,
                "rules.save_rolled"
                if source.command_type == "rules.save"
                else "rules.check_rolled",
                {
                    **evidence,
                    "engine_event": final.model_dump(mode="json"),
                    "original_command_id": source.command_id,
                    "bardic_inspiration": response.payload.selected_option_id,
                },
                phase="rules.after_bardic_choice",
                unresolved=True,
            )
            events = self.effects._expand_events(row, package, [event], response.command_id)
            version = self.runtime.stage_outcome(response, row, package, events)
            self._close(response, "accepted" if use else "declined")
            receipt = self.runtime.finish(
                response, context, fingerprint, row, "committed", version, None
            )
        self.runtime.refresh_time_drivers()
        return receipt
