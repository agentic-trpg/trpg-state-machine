"""Shared prepare/evaluate/commit path for combat and exploration rules."""

import copy
from dataclasses import asdict, replace

from dnd5e_engine.evaluation_contracts import (
    CheckPayload,
    ExplorationItemUsePayload,
    ExplorationSpellCastPayload,
    ExplorationSpellPositions,
    ItemUsePayload,
    RestPayload,
)
from dnd5e_engine.evaluation_openable import OpenableCheckRequest, evaluate_openable
from dnd5e_engine.evaluation_resources import HitDieSpend
from dnd5e_engine.evaluation_ruleset import EVALUATOR_VERSION, RulesetBindingError
from dnd5e_engine.evaluation_speed_effects import spell_attack_has_speed_effect
from pydantic import ValidationError

from ._execution import _WorldEffectKind, _WorldEffectOrigin
from .combat import process_id
from .dnd_checks import world_effects_ready
from .errors import StateMachineError
from .rest_activity import LongRest, ShortRest, advance_rest, prepare_rest
from .rules_adapter import UnsupportedEffectConsequence, validate_dnd_result
from .spell_adoption import UnsupportedSpellConsequence
from .time_binding import TimeBinding
from .world_commands import OPENABLE_CHECK_OPERATIONS, CheckMechanism, PickLock


class RuleExecution:
    """Coordinate fixed-input evaluation outside SQL and guarded transactional adoption."""

    def __init__(self, owner):
        """Bind rule submission runtime and cooperating domain interfaces."""
        self.runtime = owner.runtime
        self.store = owner.store
        self.sessions = owner.sessions
        self.rules = owner.rules
        self.world = owner.world
        self.capture = owner.capture
        self.effects = owner.effects
        self.persistence = owner.persistence
        self.activities = owner.activities

    @property
    def _conn(self):
        """Return the shared rule-execution connection."""
        return self.store.connection

    def _spell_positions(self, row, command):
        """Read only the named participants and require placement in the current scene."""
        source_id, target_id = command.actor_id, command.payload.target_id
        ids = tuple(dict.fromkeys((source_id, target_id)))
        placements = [self.world.facts.carrier_position(row["session_id"], actor) for actor in ids]
        if any(place is None or place[0] != row["current_scene_id"] for place in placements):
            return "spell.target", ids, None
        if source_id == target_id:
            return None, ids, None
        source, target = placements
        if source[1] is None or target[1] is None:
            return "spell.target", ids, None
        return None, ids, ExplorationSpellPositions(source_cell=source[1], target_cell=target[1])

    async def submit(self, command, package, context, fingerprint):
        """Evaluate fixed rules inputs, then revalidate and adopt their guarded result.

        Preparation authorizes the actor, bindings, target and optional domain check in
        a read scope; RE evaluation runs outside the write transaction. Adoption
        rechecks authorization, replay identity, world version, snapshot/RNG and domain
        prerequisites. Allowed Delta, RNG, events and receipt share the existing write
        scope. Domain failure uses the effects savepoint; transaction/publication
        failures remain governed by the Store rather than being converted to an ordinary
        rules rejection. Exploration items and spells use the committed exploration tick.
        """
        exploration_item = type(command.payload) is ExplorationItemUsePayload
        exploration_spell = type(command.payload) is ExplorationSpellCastPayload
        exploration_rule = exploration_item or exploration_spell
        rest = type(command.payload) in (LongRest, ShortRest)
        long_rest = type(command.payload) is LongRest
        if not (exploration_rule or rest):
            command = self.runtime.prepare_timed_command(command, package, context, fingerprint)
        object_check = type(command.payload) in OPENABLE_CHECK_OPERATIONS
        mechanism_check = type(command.payload) is CheckMechanism
        domain_check = object_check or mechanism_check
        check_service = (
            self.world.objects.mechanisms if mechanism_check else self.world.objects.openable
        )
        plan, prepare_error, rule_command = None, None, command
        initial_exploration_error = None
        rest_plan = None
        spell_actor_ids, spell_positions = None, None

        def exploration_error(value):
            """Check mode and protocol, deferring use while saved time work is unfinished."""
            if self.world._active_combat(command.session_id):
                return "unsupported_combat_world_operation"
            if value["mechanical_schema_version"] != "engine-snapshot/20":
                return "unsupported_mechanical_version"
            if exploration_spell and not isinstance(
                self.world.facts.time_state(command.session_id)[0], TimeBinding
            ):
                return "unsupported_time_protocol"
            if self.runtime.time is not None:
                progress = self.runtime.time.progress(command.session_id)
                if progress is not None and progress["status"] != "completed":
                    raise StateMachineError(
                        "time_advance_pending", "Finish the saved time work first"
                    )
            return None

        def build_snapshot(value):
            """Build the required mechanical snapshot for the command and current combat
            state.
            """
            if domain_check and self.world._active_combat(command.session_id):
                # Preserve a durable rejected receipt without pretending a combat
                # snapshot is a noncombat check or relaxing any capability guard.
                return self.rules._combat_snapshot(value)
            if rest:
                return self.rules._noncombat_snapshot(value, actor_ids=(command.actor_id,))
            if exploration_spell:
                # Touch delivery depends on explicit placement and the mechanical grid.
                return self.rules._noncombat_snapshot(value, actor_ids=spell_actor_ids)
            if type(command.payload) is CheckPayload or domain_check or exploration_item:
                return self.rules._noncombat_snapshot(value, package)
            intent_type = getattr(command.payload, "intent_type", None)
            projects_walk = intent_type == "move" or (
                intent_type == "cast_spell"
                and command.payload.spell_id is not None
                and spell_attack_has_speed_effect(self.rules.loader, command.payload.spell_id)
            )
            return self.rules._combat_snapshot(
                value, for_pass=intent_type == "pass", for_walk=projects_walk
            )

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
            if exploration_rule:
                initial_exploration_error = exploration_error(row)
                prepare_error = initial_exploration_error
            if rest:
                rest_plan, prepare_error = prepare_rest(
                    row, command.actor_id, self.world, self.activities, command.payload
                )
                if prepare_error is None:
                    rule_command = replace(
                        command,
                        command_type="rules.rest",
                        payload=RestPayload(
                            kind="rules.rest",
                            target_id=command.actor_id,
                            rest_type="long" if long_rest else "short",
                            hit_dice=()
                            if long_rest
                            else tuple(
                                HitDieSpend(pool_id=spend.pool_id, amount=spend.amount)
                                for spend in command.payload.hit_dice
                            ),
                        ),
                    )
            if exploration_spell and prepare_error is None:
                prepare_error, spell_actor_ids, spell_positions = self._spell_positions(
                    row, command
                )
            snapshot = None if prepare_error else build_snapshot(row)
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
            if (
                type(command.payload) is not CheckPayload
                and not domain_check
                and not (exploration_rule or rest)
            ):
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
        elif prepare_error is not None:
            request = None
            capability = prepare_error
        else:
            request = self.rules.request(
                rule_command, package, snapshot, rng, row=row, spell_positions=spell_positions
            )
            capability = (
                None
                if rest
                else self.rules._dnd_capability(rule_command.payload, command.actor_id, snapshot)
            )
            payload = rule_command.payload
            if capability is None and getattr(payload, "intent_type", None) == "cast_spell":
                casting = snapshot.casting_state
                caster = (
                    next(
                        (entry for entry in casting.actors if entry.actor_id == command.actor_id),
                        None,
                    )
                    if casting is not None
                    else None
                )
                source = (
                    next(
                        (
                            source
                            for source in caster.sources
                            if source.source_id == payload.spell_source_id
                        ),
                        None,
                    )
                    if caster is not None
                    else None
                )
                selected = (
                    source.prepared_spells
                    if source is not None and payload.slot_level not in (None, 0)
                    else source.known_spells
                    if source is not None
                    else ()
                )
                if payload.spell_id not in selected:
                    capability = "spell.source"
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
        input_version = row["world_version"] if snapshot is None else snapshot.world_version
        if input_version == command.expected_world_version:
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
                try:
                    result, changes = validate_dnd_result(
                        request,
                        result,
                        loader=self.rules.loader,
                    )
                except UnsupportedSpellConsequence:
                    capability = "spell.capability"
                except UnsupportedEffectConsequence:
                    capability = "effect.capability"
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
            current_exploration_error = exploration_error(row) if exploration_rule else None
            changed_rest_plan = rest and prepare_rest(
                row, command.actor_id, self.world, self.activities, command.payload
            ) != (rest_plan, prepare_error)
            if (
                self.runtime.version_conflict(row, command)
                or changed_rest_plan
                or (exploration_rule and current_exploration_error != initial_exploration_error)
                or (snapshot is not None and build_snapshot(row) != snapshot)
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
                if not (exploration_spell or rest):
                    self.sessions._npc_combat_target_guard(command, package, build_snapshot(row))
                result, changes = validate_dnd_result(
                    request,
                    result,
                    loader=self.rules.loader,
                )
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
                    time_events = []
                    if rest:
                        time_events = advance_rest(
                            row, package, command.command_id, self.runtime.time, rest_plan
                        )
                        sid = command.session_id
                        start, target, place = rest_plan
                        if (
                            self.world._active_combat(sid)
                            or self.world.facts.time_state(sid)[1] != target
                            or self.world.facts.carrier_position(sid, command.actor_id) != place
                            or self.activities.active(sid, command.actor_id) is not None
                            or build_snapshot(row) != snapshot
                            or self.rules._rng_context(row) != rng
                        ):
                            raise StateMachineError(
                                "rest_interrupted", "Time work changed the rest's dependencies"
                            )
                        if long_rest:
                            self.activities.complete_long_rest(
                                sid, command.actor_id, place[0], command.command_id, start, target
                            )
                    self.rules._apply_dnd_changes(row, changes, command_id=command.command_id)
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
                            else "world.item_used"
                            if exploration_item
                            else "world.spell_cast"
                            if exploration_spell
                            else command.command_type
                            if rest
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
                                    else {"item_instance_id": command.payload.instance_id}
                                    if exploration_item
                                    else {"spell_id": command.payload.spell_id}
                                    if exploration_spell
                                    else {
                                        "actor_id": command.actor_id,
                                        "started_tick": rest_plan[0],
                                        "completed_tick": rest_plan[1],
                                    }
                                    if rest
                                    else {
                                        "combat_id": snapshot.combat_state.combat_id,
                                        **(
                                            {"item_instance_id": command.payload.instance_id}
                                            if type(command.payload) is ItemUsePayload
                                            else {}
                                        ),
                                    }
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
                    outcome_row = (
                        {**dict(row), "event_seq": row["event_seq"] + len(time_events)}
                        if time_events
                        else row
                    )
                    events = time_events + self.effects._expand_events(
                        outcome_row, package, events, command.command_id
                    )
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
                        "movement.capability",
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
                        "unsupported_item_binding",
                        "equipped_item_unsupported",
                        "rest_time_unsettled",
                        "rest_interrupted",
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
