"""Real pinned evaluate(), closed delta validation and one SQLite commit boundary."""

import copy
from dataclasses import asdict

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.evaluation import evaluate
from dnd5e_engine.evaluation_contracts import (
    CombatIntentPayload,
    ReadVersion,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_ruleset import RulesetBinding, RulesetBindingError
from dnd5e_engine.evaluation_state import CombatSnapshot
from pydantic import ValidationError

from .combat import process_id
from .commands import CommandReceipt, command_fingerprint, validate_command
from .errors import StateMachineError
from .package import canonical_json


def _invalid(message):
    raise StateMachineError("invalid_rule_result", message)


def _json(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json(v) for v in value]
    return value


def delta_changes(request, result):
    """Check every pre-value and ownership; return only closed component-field edits.

    A temporary candidate validates invariants; it is never persisted as a Snapshot.
    Whole-session OCC covers all Engine dependencies in this candidate API.
    """
    snapshot = request.state_snapshot
    actors = {a.entity_id: a.model_dump(mode="json") for a in snapshot.character_states}
    combat = snapshot.combat_state.model_dump(mode="json")
    changes = []
    seen = set()
    turn = next(
        (
            op.value.current_turn_index
            for op in result.state_delta.operations
            if type(op) is delta.TurnUpdate
        ),
        snapshot.combat_state.current_turn_index,
    )
    if not 0 <= turn < len(snapshot.combat_state.initiative_ids):
        _invalid("Turn delta is outside the encounter")
    budget_owners = {request.actor_id, snapshot.combat_state.initiative_ids[turn]}

    def edit(component, identity, field, expected, value):
        key = (component, identity, field)
        if key in seen:
            _invalid("Duplicate delta field")
        seen.add(key)
        current = actors[identity] if component == "actor" else combat
        # Set-valued schema fields have no serialization-order semantics.
        old = current[field]
        expected = _json(expected)
        if field in ("dead_ids", "processed_zero_hp_damage_instances"):
            old = sorted(old)
            expected = sorted(expected)
        if old != expected:
            _invalid("Delta pre-value differs from authoritative input")
        current[field] = _json(value)
        changes.append((component, identity, field, _json(value)))

    for op in result.state_delta.operations:
        target = getattr(op, "target_id", None)
        actor_id = getattr(op, "actor_id", None)
        if target is not None and (target not in actors or target != request.payload.target_id):
            _invalid("Damage/status delta must belong to the declared target")
        if actor_id is not None and actor_id not in budget_owners:
            _invalid("Action/movement delta actor is outside the encounter")
        if hasattr(op, "combat_id") and op.combat_id != combat["combat_id"]:
            _invalid("Delta belongs to another combat")
        if type(op) is delta.HPDelta:
            if op.amount > 0:
                _invalid("A basic attack cannot heal")
            edit("actor", target, "hp_current", op.expected_hp, op.resulting_hp)
        elif type(op) is delta.TempHPSet:
            if op.temp_hp > op.expected_temp_hp:
                _invalid("A basic attack cannot grant temporary HP")
            edit("actor", target, "temp_hp", op.expected_temp_hp, op.temp_hp)
        elif type(op) in (delta.DeathStateUpdate, delta.ActionBudgetUpdate, delta.TurnUpdate):
            component = "combat" if type(op) is delta.TurnUpdate else "actor"
            identity = combat["combat_id"] if component == "combat" else (target or actor_id)
            for field in type(op.value).model_fields:
                edit(
                    component,
                    identity,
                    field,
                    getattr(op.expected, field),
                    getattr(op.value, field),
                )
        elif type(op) is delta.ConditionsUpdate:
            edit("actor", target, "conditions", op.expected, op.value)
        elif type(op) is delta.DamageAttributionUpdate:
            if op.value not in (None, request.actor_id):
                _invalid("Damage attribution belongs to another actor")
            edit("actor", target, "last_damaged_by", op.expected, op.value)
        elif type(op) is delta.MovementLedgerUpdate:
            field = "movement_ledgers"
            if combat[field][actor_id] != _json(op.expected):
                _invalid("Movement ledger pre-value differs")
            if (field, actor_id) in seen:
                _invalid("Duplicate movement ledger")
            seen.add((field, actor_id))
            combat[field][actor_id] = _json(op.value)
            changes.append(("ledger", actor_id, field, _json(op.value)))
        elif type(op) is delta.DamageSequenceUpdate:
            if op.value < op.expected:
                _invalid("Damage sequence cannot move backwards")
            edit("combat", op.combat_id, "damage_instance_sequence", op.expected, op.value)
        elif type(op) is delta.ProcessedDamageUpdate:
            if not op.expected <= op.value:
                _invalid("An attack cannot erase processed damage history")
            if any(target_id not in actors for target_id, _ in op.value):
                _invalid("Processed damage target outside the encounter")
            edit(
                "combat", op.combat_id, "processed_zero_hp_damage_instances", op.expected, op.value
            )
        elif type(op) is delta.DeathLedgerUpdate:
            if (
                not op.expected_dead_ids <= op.dead_ids
                or op.records[: len(op.expected_records)] != op.expected_records
            ):
                _invalid("An attack cannot erase or rewrite death history")
            if not op.dead_ids <= actors.keys() or any(
                r.target_id not in actors for r in op.records
            ):
                _invalid("Death ledger actor outside the encounter")
            edit("combat", op.combat_id, "dead_ids", op.expected_dead_ids, op.dead_ids)
            edit("combat", op.combat_id, "deaths_recorded", op.expected_records, op.records)
        else:
            _invalid("Unrecognized delta operation")
    combat["event_log"] += [_json(e) for e in result.proposed_events]
    data = snapshot.model_dump(mode="json")
    data.update(
        character_states=[actors[a.entity_id] for a in snapshot.character_states],
        combat_state=combat,
    )
    try:
        CombatSnapshot.model_validate_json(canonical_json(data))
    except ValidationError as exc:
        raise StateMachineError(
            "invalid_rule_result", "Delta violates mechanical invariants"
        ) from exc
    if not changes:
        _invalid("Event-only commit/version policy is not implemented")
    return tuple(changes)


def validate_dnd_result(request, result):
    try:
        result = RuleEvaluationResult.model_validate(copy.deepcopy(result))
        result.verify_request(request)
    except (ValidationError, ValueError, TypeError) as exc:
        raise StateMachineError(
            "invalid_rule_result", "Invalid Engine result envelope/binding/RNG"
        ) from exc
    required = (
        ReadVersion(
            kind="world", ref=request.session_id, version=request.state_snapshot.world_version
        ),
    )
    if result.read_set != required:
        _invalid("Expected the pinned Engine's complete whole-session read fence")
    if result.status == "needs_choice":
        _invalid("Unexpected choice after explicit basic-attack capability admission")
    actors = {a.entity_id for a in request.state_snapshot.character_states}
    for event in result.proposed_events:
        for field in ("actor_id", "target_id", "source_actor_id", "attacker_id", "killer_id"):
            value = getattr(event, field, None)
            if value is not None and value not in actors:
                _invalid("Proposed mechanical event refers to an actor outside the encounter")
    return result, delta_changes(request, result) if result.status == "accepted" else ()


class DndRuntime:
    async def submit_command_async(self, command, package, *, context):
        """Async entry for the same TypedCommand and persistent receipt protocol."""
        validate_command(command)
        if type(command.payload) is not CombatIntentPayload:
            raise StateMachineError(
                "unsupported_operation", "Async entry currently supports combat.intent"
            )
        self.read_session(command.session_id, package, context=context)
        return await self._submit_dnd(
            command, package, context, command_fingerprint(command, package, context)
        )

    async def _evaluate_dnd(self, request):
        return await evaluate(request, loader=self._dnd_loader)

    def _dnd_capability(self, payload, actor_id, snapshot):
        if (
            payload.intent_type == "attack"
            and payload.weapon_id is None
            and payload.stat_block_action_id is None
        ):
            return "pending_choice_unsupported"
        if payload.stat_block_action_id is not None:
            slug = snapshot.combat_state.monster_slug_by_entity.get(actor_id)
            monster = self._dnd_loader.get_monster(slug) if slug else None
            if monster is None or payload.stat_block_action_id not in {
                a.slug for a in monster.actions
            }:
                return "unsupported_stat_block_action"
        return None

    async def _submit_dnd(self, command, package, context, fingerprint):
        with self._transaction():
            row = self._session(command.session_id, package, context)
            self._authorize_command(row, command.actor_id, command.payload, context)
            if replay := self._command_replay(command, fingerprint):
                return replay
            snapshot = self._combat_snapshot(row)
            rng = self._rng_context(row)
            combat = self._conn.execute(
                "SELECT process_id FROM combats WHERE session_id=?", (command.session_id,)
            ).fetchone()
            if combat[0] != process_id() and not snapshot.combat_state.ended:
                raise StateMachineError(
                    "combat_unrestorable", "Interrupted combat requires explicit recovery support"
                )
        request = RuleEvaluationRequest(
            schema_version="engine-evaluation/1",
            session_id=command.session_id,
            command_id=command.command_id,
            operation_kind="combat.intent",
            actor_id=command.actor_id,
            payload=copy.deepcopy(command.payload),
            state_snapshot=snapshot,
            ruleset_binding=RulesetBinding(**asdict(package.ruleset_binding)),
            rng_context=rng,
        )
        capability = self._dnd_capability(command.payload, command.actor_id, snapshot)
        result, changes = None, ()
        if snapshot.world_version == command.expected_world_version:
            self._verify_dnd_binding(package)
            if capability is None:
                try:
                    result = await self._evaluate_dnd(copy.deepcopy(request))
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
            self._verify_dnd_binding(package)
        with self._transaction(write=True):
            row = self._session(command.session_id, package, context)
            self._authorize_command(row, command.actor_id, command.payload, context)
            if replay := self._command_replay(command, fingerprint):
                return replay
            version, seq = row["world_version"], row["event_seq"]
            status, error = "committed", None
            if (
                version != command.expected_world_version
                or self._combat_snapshot(row) != snapshot
                or self._rng_context(row) != rng
            ):
                status, error = "conflict", "version_conflict"
            elif capability is not None:
                status, error = "rejected", capability
            elif result.status != "accepted":
                status, error = "rejected", result.error.code
            else:
                result, changes = validate_dnd_result(request, result)
                self._conn.execute("SAVEPOINT dnd_effects")
                try:
                    self._apply_dnd_changes(row, changes)
                    events = [
                        (
                            "combat.engine_event",
                            {
                                "combat_id": snapshot.combat_state.combat_id,
                                "scene_id": snapshot.scene_state.scene_id,
                                "engine_event": event.model_dump(mode="json"),
                            },
                        )
                        for event in result.proposed_events
                    ]
                    events = self._expand_events(row, package, events, command.command_id)
                    version += 1
                    transition = result.rng_transition
                    self._conn.execute(
                        "UPDATE sessions SET world_version=? WHERE session_id=?",
                        (version, command.session_id),
                    )
                    self._conn.execute(
                        "UPDATE rng_states SET state_json=?,rng_version=? WHERE session_id=?",
                        (
                            transition.next_state.model_dump_json(),
                            rng.version + int(transition.state_changed),
                            command.session_id,
                        ),
                    )
                    self._append_events(
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
            self._conn.execute(
                "INSERT INTO command_receipts VALUES (?,?,?,?,?)",
                (
                    command.session_id,
                    command.command_id,
                    context.principal_id,
                    fingerprint,
                    canonical_json(asdict(receipt)),
                ),
            )
        return receipt

    def _apply_dnd_changes(self, row, changes):
        sid = row["session_id"]
        for component, identity, field, value in changes:
            if component == "actor" and field == "hp_current":
                self._conn.execute(
                    "UPDATE characters SET current_hp=? WHERE session_id=? AND actor_id=?",
                    (value, sid, identity),
                )
            elif component == "actor":
                self._conn.execute(
                    "UPDATE mechanical_actors SET state_json=json_set(state_json,?,json(?)) "
                    "WHERE session_id=? AND actor_id=?",
                    ("$." + field, canonical_json(value), sid, identity),
                )
            elif component == "combat":
                self._conn.execute(
                    "UPDATE combats SET state_json=json_set(state_json,?,json(?)) "
                    "WHERE session_id=?",
                    ("$." + field, canonical_json(value), sid),
                )
            elif component == "ledger":
                # Quote the JSON object key: Actor IDs can contain punctuation.
                self._conn.execute(
                    "UPDATE combats SET state_json=json_set(state_json,?,json(?)) "
                    "WHERE session_id=?",
                    ("$.movement_ledgers." + canonical_json(identity), canonical_json(value), sid),
                )
            else:
                _invalid("Unknown internal component edit")
