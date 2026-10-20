"""Pinned history-free evaluator projection and controlled component delta adapter."""

import copy
import json
from dataclasses import asdict

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.evaluation import evaluate
from dnd5e_engine.evaluation_availability import (
    ActionAvailabilityRequest,
    ActionAvailabilityResult,
    query_action_availability,
)
from dnd5e_engine.evaluation_contracts import (
    CheckPayload,
    CombatClosePayload,
    ReadVersion,
    RuleError,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_preflight import template_support_failure
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import RulesetBinding, RulesetBindingError, verify_ruleset
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot, NonCombatSnapshot
from pydantic import ValidationError

from .combat import DND_RULESET, _remap, process_id, validate_foundation
from .dnd_checks import check_changes
from .dnd_closure import closure_changes
from .errors import StateMachineError
from .package import canonical_json
from .session_access import _binding


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
    affected_actors = budget_owners | {request.payload.target_id}

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
        allowed_owners = budget_owners
        if type(op) is delta.ActionBudgetUpdate:
            # Damage can change the target's movement budget (e.g. unconscious),
            # even when a different Actor receives the next turn.
            allowed_owners = affected_actors
        if actor_id is not None and (actor_id not in actors or actor_id not in allowed_owners):
            _invalid("Action/movement delta actor is outside the authorized attack")
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
            if type(op) is delta.TurnUpdate:
                if op.value.ended != op.expected.ended:
                    _invalid("Combat completion is not supported by this attack slice")
                for field in ("monster_turn_start_done", "last_ended_turn", "departed_actor_id"):
                    reference = getattr(op.value, field)
                    if reference is None or reference == getattr(op.expected, field):
                        continue
                    owner = reference if field == "departed_actor_id" else reference[1]
                    if owner not in actors or owner not in affected_actors:
                        _invalid("Changed turn reference is outside the authorized attack")
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
            new_entries = op.value - op.expected
            if any(
                target_id != request.payload.target_id or target_id not in actors
                for target_id, _ in new_entries
            ):
                _invalid("New processed damage must belong to the declared target")
            edit(
                "combat", op.combat_id, "processed_zero_hp_damage_instances", op.expected, op.value
            )
        elif type(op) is delta.DeathLedgerUpdate:
            if (
                not op.expected_dead_ids <= op.dead_ids
                or op.records[: len(op.expected_records)] != op.expected_records
            ):
                _invalid("An attack cannot erase or rewrite death history")
            new_dead_ids = op.dead_ids - op.expected_dead_ids
            new_records = op.records[len(op.expected_records) :]
            if any(
                target_id != request.payload.target_id or target_id not in actors
                for target_id in new_dead_ids
            ):
                _invalid("New deaths must belong to the declared target")
            for record in new_records:
                if record.target_id != request.payload.target_id or record.target_id not in actors:
                    _invalid("New death records must belong to the declared target")
                if (
                    record.killer_id != request.actor_id
                    or record.location_id != snapshot.scene_state.scene_id
                    or record.target_kind != actors[record.target_id]["entity_type"].lower()
                ):
                    _invalid("Death record attribution differs from the authorized attack")
            record_targets = [record.target_id for record in new_records]
            if set(record_targets) != new_dead_ids or len(record_targets) != len(new_dead_ids):
                _invalid("New death identities and records must correspond exactly once")
            edit("combat", op.combat_id, "dead_ids", op.expected_dead_ids, op.dead_ids)
            edit("combat", op.combat_id, "deaths_recorded", op.expected_records, op.records)
        else:
            _invalid("Unrecognized delta operation")
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
        # RE /24 verifies /2 stage coverage against this exact input. Uncovered
        # is a valid explicit limitation, never upgraded to sensory Evidence or
        # complete coverage by SM; existing operation admission remains unchanged.
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
    if result.status != "accepted":
        return result, ()
    if request.operation_kind == "rules.check":
        return result, check_changes(request, result)
    return result, (
        closure_changes(request, result)
        if request.operation_kind == "combat.close"
        else delta_changes(request, result)
    )


class RulesAdapter:
    """Pinned history-free translation; facts and the outer transaction belong to SM."""

    def __init__(self, store, loader, characters, facts, records, world):
        self.store = store
        self.loader = loader
        self.characters = characters
        self.facts = facts
        self.records = records
        self.world = world

    @property
    def _conn(self):
        return self.store.connection

    def session_binding(self, row):
        """Translate SM's durable identity to the pinned RE contract."""
        return RulesetBinding(**asdict(_binding(row)))

    def _pinned_row(self, session_id, package):
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise StateMachineError("session_not_found", "Unknown session")
        self.world.sessions._check_package_pin(row, package)
        return row

    def _verify_dnd_binding(self, package, row=None):
        if package.legacy_audit_only:
            raise StateMachineError(
                "legacy_rules_audit_only", "Old mechanical facts require explicit migration"
            )
        # Only creation has no Session yet. Every live path supplies its row.
        binding = (
            RulesetBinding(**asdict(package.ruleset_binding))
            if row is None
            else self.session_binding(row)
        )
        if binding.ruleset_id != DND_RULESET:
            raise StateMachineError("unsupported_ruleset", "Expected a D&D evaluation package")
        try:
            verify_ruleset(binding, self.loader)
        except RulesetBindingError as exc:
            raise StateMachineError(
                "ruleset_binding_mismatch", "Evaluator data/configuration differ"
            ) from exc

    def _noncombat_snapshot(self, row, package=None):
        self.records.require_noncombat_save(row)
        sid = row["session_id"]
        if row["mechanical_schema_version"] != "engine-snapshot/17":
            raise StateMachineError(
                "unsupported_mechanical_version", "Complete noncombat state was not initialized"
            )
        present = [
            a[0]
            for a in self._conn.execute(
                (
                    "SELECT actor_id FROM actors WHERE session_id=? AND current_scene_id=? "
                    "ORDER BY actor_id"
                ),
                (sid, row["current_scene_id"]),
            )
        ]
        actors = self.characters.rule_actors(sid, set(present))
        scene_data = self.facts.mechanical_scene(sid, row["current_scene_id"])
        if scene_data is not None and package is not None:
            projected, unknown, _ = self.world.objects.lights.project(
                sid, scene_data["scene_id"], package, scene_data["grid"]
            )
            if unknown:
                raise StateMachineError(
                    "unsupported_light_geometry", "Mechanical lighting contains uncertainty"
                )
            scene_data = {**scene_data, "grid": projected}
        try:
            return NonCombatSnapshot.model_validate_json(
                canonical_json(
                    {
                        "snapshot_kind": "non_combat",
                        "snapshot_schema_version": row["mechanical_schema_version"],
                        "session_id": sid,
                        "world_version": row["world_version"],
                        "character_states": [actors[a] for a in present],
                        **self.characters.rule_components(sid, set(present)),
                        "combat_setup": None,
                        "scene_state": scene_data,
                    }
                )
            )
        except (ValidationError, KeyError, TypeError) as exc:
            raise StateMachineError(
                "invalid_mechanical_state", "Current scene lacks complete mechanical facts"
            ) from exc

    def validate_saved_mechanics(self, row):
        """Validate stored stocks without requiring every scene NPC to support RE.

        Resuming exploration is allowed in scenes containing ordinary NPCs with
        no mechanical profile. A later rule request still requires a complete
        local snapshot; recovery must not create those missing profiles.
        """
        if row["mechanical_schema_version"] != "engine-snapshot/17":
            raise StateMachineError(
                "unsupported_mechanical_version", "Old mechanical facts need migration"
            )
        actors = self.characters.rule_actors(row["session_id"])
        if row["pc_actor_id"] not in actors:
            raise StateMachineError("incomplete_session", "Mechanical PC facts are missing")
        try:
            for actor in actors.values():
                CharacterStateV2.model_validate_json(canonical_json(actor))
        except (ValidationError, ValueError, TypeError) as exc:
            raise StateMachineError(
                "invalid_mechanical_state", "Stored Actor facts are invalid"
            ) from exc
        self.characters.rule_components(row["session_id"], set(actors), full_audit=True)

    def _combat_snapshot(self, row):
        sid = row["session_id"]
        if row["mechanical_schema_version"] != "engine-snapshot/17":
            raise StateMachineError(
                "unsupported_mechanical_version", "Old save lacks proven current component facts"
            )
        combat = self._conn.execute("SELECT * FROM combats WHERE session_id=?", (sid,)).fetchone()
        if combat is None:
            raise StateMachineError("combat_not_initialized", "Session has no mechanical encounter")
        state = json.loads(combat["state_json"])
        actors = self.characters.rule_actors(sid, set(state["initiative_ids"]))
        scene = self.facts.mechanical_scene(sid, combat["scene_id"])
        if not state["ended"]:
            state["actor_zone"] = self._current_cells(
                sid, combat["scene_id"], state["initiative_ids"], scene["grid"]
            )
        try:
            return CombatSnapshot.model_validate_json(
                canonical_json(
                    {
                        "snapshot_schema_version": row["mechanical_schema_version"],
                        "snapshot_kind": "combat",
                        "session_id": sid,
                        "world_version": row["world_version"],
                        "character_states": [actors[a] for a in state["initiative_ids"]],
                        **self.characters.rule_components(sid, set(state["initiative_ids"])),
                        "scene_state": scene,
                        "combat_state": state,
                    }
                )
            )
        except (ValidationError, KeyError, TypeError) as exc:
            raise StateMachineError(
                "invalid_mechanical_state", "Stored Engine state is incomplete"
            ) from exc

    def _current_cells(self, sid, scene_id, roster, grid):
        from .grid import cell_in_bounds

        topology = self.world.grid.reader.topology_for_grid(sid, scene_id, grid)
        positions = {}
        for actor_id in roster:
            position = self.characters.position(sid, actor_id)
            if (
                position.scene_id != scene_id
                or not cell_in_bounds(topology, position.cell)
                or not topology.is_valid_cell(position.cell)
            ):
                raise StateMachineError(
                    "invalid_spatial_state", "Current actor position is incomplete"
                )
            positions[actor_id] = position.cell
        if len(set(positions.values())) != len(positions):
            raise StateMachineError("invalid_spatial_state", "Encounter occupancy overlaps")
        return positions

    def _rng_context(self, row):
        return self.world.runtime.rng_context(row)

    def _prepare_combat(self, row, package):
        self._verify_dnd_binding(package, row)
        if package.combat_foundation_json is None:
            raise StateMachineError("unsupported_operation", "Package has no reviewed encounter")
        data = json.loads(package.combat_foundation_json)
        original = validate_foundation(data)
        if original.scene_state.scene_id != row["current_scene_id"]:
            raise StateMachineError(
                "invalid_combat_foundation", "Encounter must be in its authored scene"
            )
        if original.combat_state.party_ids != {row["pc_template_id"]}:
            raise StateMachineError(
                "invalid_combat_foundation", "Encounter must use selected PC template"
            )
        data = _remap(data, row["pc_template_id"], row["pc_actor_id"])
        data["snapshot"]["session_id"] = row["session_id"]
        data["snapshot"]["world_version"] = row["world_version"]
        spatial = self.facts.spatial_scene(row["session_id"], row["current_scene_id"])
        if spatial is not None:
            self.world._spatial_combat_guard(row["session_id"], row["current_scene_id"], package)
            roster = data["snapshot"]["combat_state"]["initiative_ids"]
            present = {
                r[0]
                for r in self._conn.execute(
                    "SELECT actor_id FROM actors WHERE session_id=? AND current_scene_id=?",
                    (row["session_id"], row["current_scene_id"]),
                )
            }
            if present != set(roster):
                raise StateMachineError(
                    "invalid_spatial_state", "Encounter roster must cover present actors"
                )
            data["snapshot"]["scene_state"] = spatial
        if not self.facts.instances.session_value(row["session_id"], "has_noncombat"):
            data["snapshot"]["character_states"] = [
                self.characters.initial_rule_actor(row["session_id"], actor).model_dump(mode="json")
                for actor in CombatSnapshot.model_validate_json(
                    canonical_json(data["snapshot"])
                ).character_states
            ]
        if self.facts.instances.session_value(row["session_id"], "has_noncombat"):
            try:
                roster = data["snapshot"]["combat_state"]["initiative_ids"]
                actors = self.characters.rule_actors(row["session_id"], set(roster))
                data["snapshot"]["character_states"] = [actors[a] for a in roster]
                data["snapshot"].update(
                    self.characters.rule_components(row["session_id"], set(roster))
                )
                data["snapshot"]["scene_state"] = self.facts.mechanical_scene(
                    row["session_id"], row["current_scene_id"]
                )
            except (KeyError, TypeError) as exc:
                raise StateMachineError(
                    "invalid_mechanical_state", "Encounter dependencies are missing"
                ) from exc
        data["snapshot"]["combat_state"]["actor_zone"] = self._current_cells(
            row["session_id"],
            row["current_scene_id"],
            data["snapshot"]["combat_state"]["initiative_ids"],
            data["snapshot"]["scene_state"]["grid"],
        )
        snapshot = CombatSnapshot.model_validate_json(canonical_json(data["snapshot"]))
        if snapshot.scene_state.scene_id != row["current_scene_id"]:
            raise StateMachineError(
                "invalid_combat_foundation", "Encounter must be in current scene"
            )
        if reason := template_support_failure(snapshot, self.loader):
            raise StateMachineError("unsupported_combat_foundation", reason)
        return (snapshot, RNGState.model_validate_json(canonical_json(data["rng_state"])))

    async def _evaluate_dnd(self, request):
        return await evaluate(request, loader=self.loader)

    def _dnd_capability(self, payload, actor_id, snapshot):
        if type(payload) is CheckPayload:
            return "pending_choice_unsupported" if payload.dc is None else None
        if type(payload) is CombatClosePayload:
            return None
        if payload.intent_type != "attack":
            return "intent.capability"
        if payload.weapon_id is None and payload.stat_block_action_id is None:
            return "pending_choice_unsupported"
        if payload.stat_block_action_id is not None:
            slug = snapshot.combat_state.monster_slug_by_entity.get(actor_id)
            monster = self.loader.get_monster(slug) if slug else None
            if monster is None or payload.stat_block_action_id not in {
                a.slug for a in monster.actions
            }:
                return "unsupported_stat_block_action"
        return None

    def _apply_dnd_changes(self, row, changes):
        self.store.require_write()
        sid = row["session_id"]
        for component, identity, field, value in changes:
            if component == "xp":
                self.characters.award_xp(sid, identity, value)
            elif component == "actor" and field == "hp_current":
                self.characters.set_hp(sid, identity, value)
            elif component == "actor":
                self.characters.update_rule_metadata(sid, identity, field, value)
            elif component == "combat":
                if field == "ended" and value:
                    combat = self._conn.execute(
                        "SELECT scene_id,state_json FROM combats WHERE session_id=?", (sid,)
                    ).fetchone()
                    roster = json.loads(combat["state_json"])["initiative_ids"]
                    scene = self.facts.mechanical_scene(sid, combat["scene_id"])
                    cells = self._current_cells(sid, combat["scene_id"], roster, scene["grid"])
                    self.records.freeze_positions(sid, cells)
                self.records.update_rule_state(sid, field, value)
            elif component == "ledger":
                self.records.update_ledger(sid, identity, value)
            else:
                _invalid("Unknown internal component edit")

    def request(self, command, package, snapshot, rng, *, row=None):
        row = row if row is not None else self._pinned_row(command.session_id, package)
        return RuleEvaluationRequest(
            schema_version="engine-evaluation/24",
            session_id=command.session_id,
            command_id=command.command_id,
            operation_kind=command.command_type,
            actor_id=command.actor_id,
            payload=copy.deepcopy(command.payload),
            state_snapshot=snapshot,
            ruleset_binding=self.session_binding(row),
            rng_context=rng,
        )

    def query_availability(
        self, session_id, actor_id, payload, package, snapshot, owner, *, row=None
    ):
        row = row if row is not None else self._pinned_row(session_id, package)
        self._verify_dnd_binding(package, row)
        try:
            request = ActionAvailabilityRequest(
                schema_version="engine-availability/13",
                session_id=session_id,
                operation_kind="combat.intent",
                actor_id=actor_id,
                payload=copy.deepcopy(payload),
                state_snapshot=snapshot,
                ruleset_binding=self.session_binding(row),
            )
            capability = self._dnd_capability(payload, actor_id, snapshot)
            if owner != process_id() and not snapshot.combat_state.ended:
                capability = "combat_unrestorable"
            if capability is not None:
                result = ActionAvailabilityResult(
                    schema_version=request.schema_version,
                    session_id=session_id,
                    actor_id=actor_id,
                    operation_kind=request.operation_kind,
                    snapshot_world_version=snapshot.world_version,
                    ruleset_binding=request.ruleset_binding,
                    status="unknown",
                    reason=RuleError(code=capability, reason="Capability is not supported"),
                )
            else:
                result = query_action_availability(request, loader=self.loader)
        except RulesetBindingError as exc:
            raise StateMachineError(
                "ruleset_binding_mismatch", "Availability evaluator binding differs"
            ) from exc
        except ValidationError as exc:
            raise StateMachineError("rule_schema_error", "Invalid availability schema") from exc
        except Exception as exc:
            raise StateMachineError("rule_query_failed", "Availability query failed") from exc
        try:
            result = ActionAvailabilityResult.model_validate(copy.deepcopy(result))
            if (
                result.session_id != request.session_id
                or result.actor_id != request.actor_id
                or result.operation_kind != request.operation_kind
                or result.schema_version != request.schema_version
                or result.snapshot_world_version != snapshot.world_version
                or result.ruleset_binding != request.ruleset_binding
            ):
                raise ValueError("Availability response differs from request")
        except (ValidationError, ValueError, TypeError) as exc:
            raise StateMachineError("invalid_rule_result", "Invalid availability result") from exc
        # Free-text Engine preflight errors may mention hidden mechanical inputs.
        # Expose a bounded code and a generic status description to the authorized actor.
        return result.model_copy(
            update={
                "reason": RuleError(
                    code=result.reason.code,
                    reason={
                        "available": "Available now; execution revalidates permission and state.",
                        "unavailable": "Unavailable on this snapshot.",
                        "unknown": "This capability cannot currently be determined.",
                    }[result.status],
                )
            }
        )
