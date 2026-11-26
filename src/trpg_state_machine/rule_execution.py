"""Shared prepare/evaluate/commit path for combat and exploration rules."""

import copy
from dataclasses import asdict, replace

from dnd5e_engine.evaluation_bardic_grant import (
    BardicGrantRequest,
    ExplorationFeaturePayload,
    plan_bardic_grant,
)
from dnd5e_engine.evaluation_charged_item import (
    exploration_charged_cast_profile,
    is_healing_staff,
)
from dnd5e_engine.evaluation_condition_choice import condition_choice_profile
from dnd5e_engine.evaluation_contracts import (
    CheckPayload,
    CombatClosePayload,
    CombatIntentPayload,
    ConfiguredCloseFact,
    ExplorationCastActivityPayload,
    ExplorationEnvironmentalSpellCastPayload,
    ExplorationItemAreaFacts,
    ExplorationItemUsePayload,
    ExplorationMultiSpellCastPayload,
    ExplorationMultiSpellPositions,
    ExplorationMultiSpellTarget,
    ExplorationSpellCastPayload,
    ExplorationSpellPositions,
    IndependentConditionSelection,
    ItemUsePayload,
    RestPayload,
    SavePayload,
)
from dnd5e_engine.evaluation_delta import ConditionsUpdate, TurnUpdate, WorldRestEffectEnd
from dnd5e_engine.evaluation_environmental_contracts import (
    EnvironmentalCreatureTarget,
    EnvironmentalMagicEffectTarget,
    WorldEnvironmentalEffectCreate,
    WorldEnvironmentalEffectEnd,
    WorldVisualEffectCreate,
)
from dnd5e_engine.evaluation_environmental_spells import (
    evaluate_bound_faerie_step,
    verify_bound_faerie_result,
)
from dnd5e_engine.evaluation_exploration_healing import exploration_healing_requires_sight
from dnd5e_engine.evaluation_identify import IDENTIFY_SPELL_ID
from dnd5e_engine.evaluation_openable import OpenableCheckRequest, evaluate_openable
from dnd5e_engine.evaluation_resources import HitDieSpend
from dnd5e_engine.evaluation_ruleset import EVALUATOR_VERSION, RulesetBindingError
from dnd5e_engine.evaluation_shield_window import answer_shield, evaluate_for_shield
from dnd5e_engine.evaluation_speed_effects import spell_attack_has_speed_effect
from dnd5e_engine.evaluation_spell_termination import WorldSpellEnd
from dnd5e_engine.evaluation_ward_spells import (
    evaluate_bound_bane_step,
    prepare_ward_spell,
    verify_bound_bane_result,
)
from dnd5e_engine.events import ObjectIdentified
from dnd5e_engine.spellcasting import spell_range_ft
from pydantic import ValidationError

from ._execution import _WorldEffectKind, _WorldEffectOrigin
from .action_pending import PendingPickSaveChoice, resolve_action_save_choice
from .choice_models import (
    ChoiceFinalizeSpellOperationPayload,
    ChoiceRespondOperationPayload,
    PendingChoice,
)
from .combat import DistanceWithdrawal, EncounterRegion, process_id
from .combat_withdrawal import (
    CombatWithdraw,
    boundary_blockers,
    check_withdrawal,
    commit_withdrawal,
    confine_walk,
    crosses_boundary,
    distance_withdrawal_origin,
    pass_snapshot_after_walk,
    prepare_withdrawal,
    require_no_unmasked_opportunity,
    require_walk_bystander_clearance,
    walking_position,
    withdrawal_continues,
)
from .commands import CommandReceipt
from .dnd_checks import world_effects_ready
from .effect_hp_adoption import apply_world_spell_ends
from .errors import StateMachineError
from .models import SpellConditionOption, SpellConditionOptions
from .pending_bardic_checks import PendingBardicChecks
from .pending_multi_spell_choices import PendingMultiSpellChoices
from .pending_spell_choices import PendingSpellChoices
from .perception import visible_actor_in_range
from .rest_activity import (
    LongRest,
    ShortRest,
    advance_rest,
    prepare_attunement,
    prepare_rest,
    prepare_rest_time_expiries,
    rest_snapshot_after_time,
)
from .rule_sight import read_creature_sight
from .rules_adapter import UnsupportedEffectConsequence, validate_dnd_result
from .shield_adoption import UnsupportedShieldKnockout, UnsupportedShieldTerminal
from .shield_window import ShieldWindows, guard_shield_window
from .spatial import SpatialPosition
from .spell_adoption import UnsupportedSpellConsequence
from .spell_cast_activity import (
    EXPLORATION_RULES_SNAPSHOT_VERSION,
    FinishSpellCast,
    StartSpellCast,
)
from .time_binding import TimeBinding, deadline_after_seconds
from .world_commands import OPENABLE_CHECK_OPERATIONS, CheckMechanism, PickLock
from .world_mechanisms import _PickFailureSaveCandidate


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
        self.spell_choices = PendingSpellChoices(
            self.store, self.runtime, self.sessions, self.world.facts
        )
        self.multi_spell_choices = PendingMultiSpellChoices(
            self.store, self.runtime, self.sessions, self.world.facts
        )
        self.shield_windows = ShieldWindows(self.store, self.runtime, self.sessions)
        self.bardic_checks = PendingBardicChecks(self)
        self.action_choices = getattr(owner, "action_choice_runtime", None)

    @property
    def _conn(self):
        """Return the shared rule-execution connection."""
        return self.store.connection

    def _spell_positions(self, row, command, package, *, willing_target_id=None, sight_range=None):
        """Project fixed participant placement, plus current authorized sight when required."""
        sid, scene = row["session_id"], row["current_scene_id"]
        source_id, target_id = command.actor_id, command.payload.target_id
        ids = tuple(dict.fromkeys((source_id, target_id)))
        placements = [self.world.facts.carrier_position(sid, actor) for actor in ids]
        target_error = "target_invalid" if sight_range is not None else "spell.target"
        if any(place is None or place[0] != scene for place in placements):
            return target_error, ids, None
        if sight_range is None and source_id == target_id:
            return None, ids, None
        source, target = placements[0], placements[-1]
        if source[1] is None or target[1] is None:
            return target_error, ids, None
        if sight_range is not None and (
            not visible_actor_in_range(
                self.world, sid, source_id, target_id, target[1], package, range_ft=sight_range
            )
            or self._conn.execute(
                "SELECT 1 FROM mechanical_actors WHERE session_id=? AND actor_id=?",
                (sid, target_id),
            ).fetchone()
            is None
        ):
            return target_error, ids, None
        positions = ExplorationSpellPositions(
            source_cell=source[1],
            target_cell=target[1],
            willing_target_id=willing_target_id,
            visible_target_id=target_id if sight_range is not None else None,
        )
        return None, ids, positions

    def _ward_positions(self, row, command, package, needs):
        """Read the exact actor cells and sight relationships declared by RE."""
        sid = command.session_id
        actor_ids = needs.actor_ids
        placements = {}
        for actor_id in actor_ids:
            place = self.world.facts.carrier_position(sid, actor_id)
            if place is None or place[0] != row["current_scene_id"] or place[1] is None:
                return "spell.target", actor_ids, None
            placements[actor_id] = place[1]
        sight_ids = frozenset(needs.sight_target_ids)
        return (
            None,
            actor_ids,
            ExplorationMultiSpellPositions(
                source_cell=placements[command.actor_id],
                targets=tuple(
                    ExplorationMultiSpellTarget(
                        target_id=target_id,
                        cell_id=placements[target_id],
                        willing=False,
                        visible=(
                            target_id == command.actor_id
                            or visible_actor_in_range(
                                self.world,
                                sid,
                                command.actor_id,
                                target_id,
                                placements[target_id],
                                package,
                                range_ft=needs.range_ft,
                            )
                        )
                        if target_id in sight_ids
                        else False,
                    )
                    for target_id in command.payload.target_ids
                ),
            ),
        )

    def _spell_sight_range(self, command):
        """Return the bound range only for the supported visible-creature spell."""
        payload = command.payload
        if type(payload) is not ExplorationSpellCastPayload or payload.spell_id != "healing-word":
            return None
        spell = self.rules.loader.get_spell(payload.spell_id)
        return (
            spell_range_ft(spell)
            if spell is not None and exploration_healing_requires_sight(spell)
            else None
        )

    async def _prepare_withdrawal_turn(
        self, row, package, context, command, region, passage, request, result, changes
    ):
        """Evaluate the leaving actor's normal handoff outside the shared write lock."""
        position = walking_position(changes)
        check_withdrawal(request.state_snapshot, region, position, passage, result)
        if not withdrawal_continues(request.state_snapshot, command.actor_id):
            return None
        with self.store.transaction():
            current = self.sessions._session(command.session_id, package, context)
            if current["world_version"] != row["world_version"]:
                return None
            pass_base = self.rules._combat_snapshot(current, for_pass=True)
        turn_input = pass_snapshot_after_walk(pass_base, result, position)
        return await self._evaluate_withdrawal_turn(
            row, package, command, turn_input, request.rng_context
        )

    async def _evaluate_withdrawal_turn(self, row, package, command, turn_input, rng):
        """Use the ordinary stateless Pass contract for the remaining encounter."""
        turn_payload = CombatIntentPayload(intent_type="pass")
        turn_request = self.rules.request(
            replace(command, command_type="combat.intent", payload=turn_payload),
            package,
            turn_input,
            rng,
            row=row,
        )
        turn_result = await self.rules._evaluate_dnd(copy.deepcopy(turn_request))
        turn_result, turn_changes = validate_dnd_result(
            turn_request, turn_result, loader=self.rules.loader
        )
        if turn_result.status != "accepted":
            raise StateMachineError(turn_result.error.code, "Withdrawal handoff was refused")
        turn = next(
            operation
            for operation in turn_result.state_delta.operations
            if type(operation) is TurnUpdate
        )
        next_actor = turn_input.combat_state.initiative_ids[turn.value.current_turn_index]
        return turn_input, turn_result, turn_changes, next_actor

    def _adopt_withdrawal_turn(self, row, package, command, turn, rng):
        """Apply the verified turn and capture its events for the original receipt."""
        if turn is None:
            return None, []
        turn_input, turn_result, turn_changes, next_actor = turn
        self.rules._apply_dnd_changes(
            row,
            turn_changes,
            command_id=command.command_id,
            snapshot=turn_input,
            turn_starts=(next_actor,),
        )
        self.runtime.advance_rule_rng(command.session_id, turn_result.rng_transition, rng)
        return next_actor, [
            self.capture._capture_event(
                command.session_id,
                package,
                "combat.engine_event",
                {
                    "combat_id": turn_input.combat_state.combat_id,
                    "scene_id": turn_input.scene_state.scene_id,
                    "engine_event": event.model_dump(mode="json"),
                },
                phase="engine.intermediate_phase_unavailable",
                unresolved=True,
            )
            for event in turn_result.proposed_events
            if event.type != "intent_submitted"
        ]

    async def _submit_distance_withdrawal(self, command, package, context, fingerprint):
        """Commit a no-move exit, with an ordinary RE handoff only when combat continues."""
        sid = command.session_id

        def authorized_row():
            """Share authorization and pending-command fences across both scopes."""
            row = self.sessions._session(sid, package, context)
            self.sessions._authorize_command(row, command.actor_id, command.payload, context)
            self.sessions._npc_handoff_command_guard(command)
            self.runtime.authorize_time_wait_reply(row, package, command, context)
            if self.action_choices is not None:
                self.action_choices._guard_pending_action_owner(sid)
            return row

        status, error = "committed", None
        snapshot, policy, turn = None, None, None
        with self.store.transaction():
            row = authorized_row()
            if replay := self.runtime._command_replay(command, fingerprint):
                return replay
            guard_shield_window(self._conn, row)
            self.rules._verify_dnd_binding(package, row)
            rng = self.rules._rng_context(row)
            if self.runtime.version_conflict(row, command):
                status, error = "conflict", "version_conflict"
            else:
                policy = self.rules.records.active_withdrawal(sid)
                if not isinstance(policy, DistanceWithdrawal):
                    status, error = "rejected", "combat.withdrawal_unavailable"
                else:
                    snapshot = self.rules._combat_snapshot(row, for_pass=True)
                    owner = self._conn.execute(
                        "SELECT process_id FROM combats WHERE session_id=?", (sid,)
                    ).fetchone()[0]
                    if owner != process_id():
                        raise StateMachineError(
                            "combat_unrestorable",
                            "Interrupted combat requires explicit recovery support",
                        )
                    try:
                        origin = distance_withdrawal_origin(
                            self.world, row, snapshot, command.actor_id, command.payload, policy
                        )
                    except StateMachineError as exc:
                        status, error = "rejected", exc.code
        if status == "committed" and withdrawal_continues(snapshot, command.actor_id):
            try:
                turn = await self._evaluate_withdrawal_turn(row, package, command, snapshot, rng)
            except StateMachineError as exc:
                if exc.code == "invalid_rule_result":
                    raise
                status, error = "rejected", exc.code

        with self.store.transaction(write=True):
            row = authorized_row()
            if replay := self.runtime._command_replay(command, fingerprint):
                return replay
            guard_shield_window(self._conn, row)
            version = row["world_version"]
            if (
                self.runtime.version_conflict(row, command)
                or self.rules._rng_context(row) != rng
                or (
                    snapshot is not None
                    and (
                        self.rules.records.active_withdrawal(sid) != policy
                        or self.rules._combat_snapshot(row, for_pass=True) != snapshot
                    )
                )
            ):
                status, error = "conflict", "version_conflict"
            elif status == "committed":
                try:
                    with self.store.savepoint():
                        next_actor, turn_events = self._adopt_withdrawal_turn(
                            row, package, command, turn, rng
                        )
                        events = commit_withdrawal(
                            self.world,
                            self.rules.records,
                            row,
                            package,
                            snapshot,
                            command.actor_id,
                            origin,
                            next_actor,
                        )
                        events.extend(turn_events)
                        events.extend(
                            self.runtime.finish_time_wait_reply(row, package, command, events)
                        )
                        events = self.effects._expand_events(
                            row, package, events, command.command_id
                        )
                        events.extend(
                            self.runtime.coordinate_combat_time(
                                row,
                                package,
                                command.command_id,
                                snapshot.combat_state.round_number,
                                len(events),
                            )
                        )
                        version = self.runtime.stage_outcome(command, row, package, events)
                except StateMachineError as exc:
                    if exc.code not in {
                        "version_conflict",
                        "combat.region_capability",
                        "event_cascade_limit",
                    }:
                        raise
                    status = "conflict" if exc.code == "version_conflict" else "rejected"
                    error = exc.code
            return self.runtime.finish(command, context, fingerprint, row, status, version, error)

    def _exploration_error(
        self, row, command, *, exploration_spell, casting_finish=False, casting_activity=False
    ):
        """Require the exploration mode, time boundary and compatible caster activity."""
        sid = command.session_id
        if self.world._active_combat(sid):
            return "unsupported_combat_world_operation"
        if row["mechanical_schema_version"] != EXPLORATION_RULES_SNAPSHOT_VERSION:
            return "unsupported_mechanical_version"
        if exploration_spell and not isinstance(self.world.facts.time_state(sid)[0], TimeBinding):
            return "unsupported_time_protocol"
        if self.runtime.time is not None and not casting_finish:
            progress = self.runtime.time.progress(sid)
            if progress is not None and progress["status"] != "completed":
                raise StateMachineError("time_advance_pending", "Finish the saved time work first")
        if not casting_activity and self.world.spell_casts.active(sid, command.actor_id):
            return "activity_conflict"
        return None

    def _independent_condition(self, command):
        """Resolve an opaque ID inside the selected target's independent conditions."""
        payload = command.payload
        index = self.world.characters.condition_index(
            command.session_id, payload.target_id, payload.selected_condition_id
        )
        if index is None:
            return None
        return IndependentConditionSelection(
            target_id=payload.target_id, condition_id=payload.selected_condition_id, index=index
        )

    async def query_spell_condition_choices(self, command, package, context):
        """Return only the caster's legal conditions from a fixed read, with no commit."""
        from .commands import validate_command

        validate_command(command)
        payload = command.payload
        if (
            type(payload) is not ExplorationSpellCastPayload
            or payload.has_condition_choice
            or payload.material_instance_id is not None
        ):
            raise StateMachineError("invalid_query", "Expected a creature cast without a selection")
        with self.store.transaction():
            sid = command.session_id
            row = self.sessions._session(sid, package, context)
            self.sessions._authorize_command(row, command.actor_id, payload, context)
            self.rules._verify_dnd_binding(package, row)
            empty = SpellConditionOptions(row["world_version"])
            if self._exploration_error(
                row, command, exploration_spell=True
            ) or not world_effects_ready(self.world, row, command):
                return empty
            spell = self.rules.loader.get_spell(payload.spell_id)
            profile = condition_choice_profile(spell) if spell is not None else None
            error, actors, positions = self._spell_positions(row, command, package)
            if profile is None or error is not None:
                return empty
            options = tuple(
                (index, SpellConditionOption(condition_id, condition))
                for index, (condition_id, condition) in enumerate(
                    self.world.characters.condition_options(sid, payload.target_id)
                )
                if condition in profile[1]
            )
            if not options:
                return empty
            # All options share the same delivery, source and payment admission. One
            # full stateless proposal proves those checks without publishing its Delta.
            index, first = options[0]
            probe = replace(
                command,
                payload=payload.model_copy(update={"selected_condition_id": first.condition_id}),
            )
            request = self.rules.request(
                probe,
                package,
                self.with_current_verbal_availability(
                    self.rules._noncombat_snapshot(row, actor_ids=actors),
                    row,
                    package,
                    command.actor_id,
                ),
                self.rules._rng_context(row),
                row=row,
                spell_positions=positions,
                spell_end_applications=self.rules.spell_end_applications(sid, command.actor_id),
                independent_condition=IndependentConditionSelection(
                    target_id=payload.target_id, condition_id=first.condition_id, index=index
                ),
            )
        result = await self.rules._evaluate_dnd(request)
        if result.status != "accepted":
            return empty
        return SpellConditionOptions(empty.world_version, tuple(option for _, option in options))

    def _charged_item_positions(self, row, package, command, entry):
        """Freeze the visible area center or an ordinary creature target."""
        area = command.payload.area_target
        if area is None:
            error, actors, positions = self._spell_positions(row, command, package)
            return error, actors, positions, None
        sid, scene_id = row["session_id"], row["current_scene_id"]
        actors = tuple(dict.fromkeys((command.actor_id, *area.target_ids)))
        loader = self.rules.loader
        item = loader.get_item(entry["item_slug"])
        profile = (
            exploration_charged_cast_profile(item, loader, activity_id=command.payload.activity_id)
            if item is not None
            else None
        )
        if profile is None or profile.spell.slug != "mass-cure-wounds":
            return "item.capability", actors, None, None
        cells = {}
        for target_id in actors:
            placement = self.world.facts.carrier_position(sid, target_id)
            if placement is None or placement[0] != scene_id or placement[1] is None:
                return "target_invalid", actors, None, None
            cells[target_id] = placement[1]
        source = cells[command.actor_id]
        targets = {target_id: cells[target_id] for target_id in area.target_ids}
        if not self.world.magic.healing_sphere_reaches(
            sid, scene_id, package, profile.spell, source, area.center_cell, tuple(targets.values())
        ):
            return "target_invalid", actors, None, None
        try:
            self.world._visible_target(sid, command.actor_id, area.center_cell, package)
        except StateMachineError as exc:
            if exc.code in ("unsupported_target_visibility", "invalid_spatial_state"):
                return "target_invalid", actors, None, None
            raise
        facts = ExplorationItemAreaFacts(
            source_cell=source, visible_center_cell=area.center_cell, target_cells=targets
        )
        return None, actors, None, facts

    def _prepare_cast_activity(self, row, command, package):
        """Resolve inputs for the timed cast whose snapshot checks current components."""
        sid, actor = row["session_id"], command.actor_id
        operation = command.payload
        domain = self.world.spell_casts
        position = self.world.facts.carrier_position(sid, actor)
        if position is None or position[0] != row["current_scene_id"]:
            return None, None, "spell.target"
        record = domain.get(sid, operation.activity_id)
        starting = type(operation) is StartSpellCast
        if starting:
            if record is not None:
                return None, None, "activity_exists"
            if self.activities.active(sid, actor) or domain.active(sid, actor):
                return None, None, "activity_conflict"
            spell, source = operation.spell_id, operation.spell_source_id
            slot, ritual = operation.slot_level, operation.as_ritual
            phase, elapsed = "start", None
            target_id, material_id = operation.target_object_id, operation.material_instance_id
        else:
            _, tick = self.world.facts.time_state(sid)
            if (
                record is None
                or record["actor_id"] != actor
                or record["status"] != "waiting"
                or record["deadline_tick"] != tick
            ):
                return None, None, "activity_inactive"
            spell, source = record["spell_id"], record["spell_source_id"]
            slot, ritual = record["slot_level"], bool(record["as_ritual"])
            phase, elapsed = "finish", tick - record["start_tick"]
            target_id, material_id = record["target_object_id"], record["material_instance_id"]
        identify = None
        if spell == IDENTIFY_SPELL_ID:
            if target_id is None or material_id is None:
                return None, None, "spell.target"
            identify = domain.identify_facts(
                sid, actor, target_id, material_id, package, starting=starting
            )
            if identify is None:
                return None, None, "spell.target"
        elif target_id is not None:
            return None, None, "spell.target"
        return (
            replace(
                command,
                command_type="world.cast_activity",
                payload=ExplorationCastActivityPayload(
                    kind="world.cast_activity",
                    phase=phase,
                    activity_id=operation.activity_id,
                    spell_id=spell,
                    spell_source_id=source,
                    slot_level=slot,
                    as_ritual=ritual,
                    elapsed_seconds=elapsed,
                    identify=identify,
                ),
            ),
            dict(record) if record is not None else None,
            None,
        )

    def with_current_verbal_availability(self, snapshot, row, package, actor_id):
        """Project current area restrictions onto one caster's detached rules input."""
        casting = snapshot.casting_state
        if casting is None or not self.world.magic.blocks_verbal_casting(
            row["session_id"], row["current_scene_id"], actor_id, package
        ):
            return snapshot
        actors = tuple(
            entry.model_copy(update={"verbal_available": False})
            if entry.actor_id == actor_id and entry.verbal_available
            else entry
            for entry in casting.actors
        )
        return snapshot.model_copy(
            update={"casting_state": casting.model_copy(update={"actors": actors})}
        )

    async def submit(self, command, package, context, fingerprint, *, _spell_save_resume=None):
        """Submit rules, resolving a failed pick's save after its draft rolls back."""
        try:
            return await self._submit(
                command, package, context, fingerprint, _spell_save_resume=_spell_save_resume
            )
        except _PickFailureSaveCandidate as draft:
            mechanisms = self.world.objects.mechanisms
            inspired = draft.grant is not None
            can_choose = inspired and draft.selected_pc and self.action_choices is not None
            prepared, waiting, _ = mechanisms.evaluate_direct_saves(
                (draft.request,), stop_on_failure=can_choose, action_choice=inspired
            )
            if waiting is not None:
                return self._open_pick_save_choice(
                    draft, fingerprint, prepared, waiting, package, context
                )
            return await self._submit(
                draft.command, package, context, fingerprint, pick_failure=(draft.check, prepared)
            )

    def _open_pick_save_choice(self, draft, fingerprint, prepared, waiting, package, context):
        """Register the detached check and failed save for the selected PC."""
        command = draft.command
        prompt = PendingChoice(
            command.command_id,
            command.actor_id,
            "rules.bardic_inspiration",
            ("accept", "decline"),
            draft.grant.expires_tick,
        )
        receipt = CommandReceipt(
            command.session_id,
            command.command_id,
            command.actor_id,
            "pending_choice",
            command.expected_world_version,
            (),
            pending_choice=prompt,
        )
        pending = PendingPickSaveChoice(
            command,
            fingerprint,
            (draft.request,),
            prepared,
            waiting,
            draft.grant,
            receipt,
            check=draft.check,
        )
        return self.action_choices._register_pending_action_choice(
            pending, package, context, draft.check[0].request.rng_context
        )

    async def _reply_pick_save(self, pending, reply, package, context, fingerprint):
        """Continue the exact original save after validating its private reply."""
        sid = reply.session_id
        with self.store.transaction():
            row = self.sessions._session(sid, package, context)
            self.sessions._authorize_command(row, reply.actor_id, reply.payload, context)
            if reply.actor_id != pending.command.actor_id:
                raise StateMachineError("unauthorized", "Pick save choice belongs to its target")
            if reply.command_id == pending.command.command_id:
                raise StateMachineError(
                    "idempotency_conflict", "Reply key must differ from the pick"
                )
            if replay := self.runtime._command_replay(reply, fingerprint):
                self.action_choices._discard_pending_action_choice(sid, expected=pending)
                return replay
            if reply.expected_world_version != pending.command.expected_world_version:
                raise StateMachineError("version_conflict", "Pick save reply version changed")
            tick = self.world.facts.time_state(sid)[1]
            grant = self.world.characters.effects.held_bardic_inspiration(sid, reply.actor_id, tick)
            if tick >= pending.grant.expires_tick or grant != pending.grant:
                self.action_choices._discard_pending_action_choice(sid, expected=pending)
                raise StateMachineError("choice_stale", "Pick save grant changed or expired")
        request, result, _ = pending.prepared[pending.waiting_key]
        resolved = resolve_action_save_choice(
            request, result, pending.grant, use=reply.payload.selected_option_id == "accept"
        )
        return await self._submit(
            pending.command,
            package,
            context,
            pending.fingerprint,
            pick_failure=(pending.check, {pending.waiting_key: resolved}),
            pick_reply=(pending, reply, fingerprint),
        )

    async def _submit(
        self,
        command,
        package,
        context,
        fingerprint,
        *,
        _spell_save_resume=None,
        pick_failure=None,
        pick_reply=None,
    ):
        """Evaluate fixed rules inputs, then revalidate and adopt their guarded result.

        Preparation authorizes the actor, bindings, target and optional domain check in
        a read scope; RE evaluation runs outside the write transaction. Adoption
        rechecks authorization, replay identity, world version, snapshot/RNG and domain
        prerequisites. Allowed Delta, RNG, events and receipt share the existing write
        scope. Domain failure uses the effects savepoint; transaction/publication
        failures remain governed by the Store rather than being converted to an ordinary
        rules rejection. Exploration items and spells use the committed exploration tick.
        A willingness answer resumes the original spell with fresh facts and commits
        its own receipt together with the single adopted cast and prompt outcome.
        Multi-target replies collect consent, then the caster may confirm a subset.
        A failed independent check with a held Bardic die saves its original roll
        for the selected PC to finalize through the same choice response entry.
        """
        if type(command.payload) is ExplorationFeaturePayload:
            return self._submit_bardic_grant(command, package, context, fingerprint)
        if (
            type(command.payload) is ChoiceRespondOperationPayload
            and self.action_choices is not None
        ):
            from .action_pending import PendingSpellSaveChoice

            pending = self.action_choices._pending_action_choices.get(command.session_id)
            if (
                isinstance(pending, PendingPickSaveChoice)
                and command.payload.pending_operation_id == pending.command.command_id
            ):
                return await self._reply_pick_save(pending, command, package, context, fingerprint)
            if (
                isinstance(pending, PendingSpellSaveChoice)
                and command.payload.pending_operation_id == pending.command.command_id
            ):
                with self.store.transaction():
                    row = self.sessions._session(command.session_id, package, context)
                    self.sessions._authorize_command(
                        row, command.actor_id, command.payload, context
                    )
                    if (
                        command.actor_id != pending.progress.pending_target_id
                        or command.actor_id != row["pc_actor_id"]
                    ):
                        raise StateMachineError(
                            "unauthorized", "Spell save choice belongs to its target"
                        )
                    if command.command_id == pending.command.command_id:
                        raise StateMachineError(
                            "idempotency_conflict", "Reply key must differ from the pending cast"
                        )
                    if replay := self.runtime._command_replay(command, fingerprint):
                        self.action_choices._discard_pending_action_choice(
                            command.session_id, expected=pending
                        )
                        return replay
                    if self.runtime._command_replay(pending.command, pending.fingerprint):
                        self.action_choices._discard_pending_action_choice(
                            command.session_id, expected=pending
                        )
                        raise StateMachineError("choice_closed", "Original cast already completed")
                    if command.expected_world_version != pending.command.expected_world_version:
                        raise StateMachineError(
                            "version_conflict", "Spell save reply version changed"
                        )
                    if not self.action_choices._current_spell_save_grant(pending):
                        self.action_choices._discard_pending_action_choice(
                            command.session_id, expected=pending
                        )
                        raise StateMachineError(
                            "choice_stale", "Spell save grant changed or expired"
                        )
                return await self.submit(
                    pending.command,
                    package,
                    context,
                    pending.fingerprint,
                    _spell_save_resume=(pending, command, fingerprint),
                )
        spell_save_reply = _spell_save_resume[1] if _spell_save_resume is not None else None
        response = shield_prompt = shield_pending = shield_original = None
        confirmation = None
        if type(command.payload) is ChoiceFinalizeSpellOperationPayload:
            confirmation = command
            with self.store.transaction(write=True):
                row = self.sessions._session(confirmation.session_id, package, context)
                replay, pending_cast, error = self.multi_spell_choices.prepare_finalize(
                    confirmation, fingerprint, row, context
                )
                if replay is not None:
                    return replay
                guard_shield_window(self._conn, row)
                if error is not None:
                    return self.multi_spell_choices.reject_finalize(
                        confirmation, fingerprint, row, context, error
                    )
                command = pending_cast
        if type(command.payload) is ChoiceRespondOperationPayload:
            response = command
            with self.store.transaction():
                self.sessions._session(response.session_id, package, context)
                bardic_prompt = self.bardic_checks.for_response(response)
            if bardic_prompt is not None:
                return self.bardic_checks.respond(response, package, context, fingerprint)
            with self.store.transaction(write=True):
                row = self.sessions._session(response.session_id, package, context)
                shield_prompt = self.shield_windows.for_response(response)
                if shield_prompt is not None:
                    replay, shield_pending, error = self.shield_windows.check(
                        response, fingerprint, row, context, shield_prompt
                    )
                    if replay is not None:
                        return replay
                    if error is not None:
                        return self._finish_shield_error(response, context, fingerprint, row, error)
                    shield_original, command = self.shield_windows.resume(shield_pending, response)
                    if not self.shield_windows.source_is_authorized(shield_original):
                        return self._finish_shield_error(
                            response, context, fingerprint, row, "choice_stale"
                        )
                else:
                    guard_shield_window(self._conn, row)
                    multi_pending = self.multi_spell_choices.for_response(response)
                    if multi_pending is not None:
                        return self.multi_spell_choices.respond(
                            response, fingerprint, row, context, multi_pending
                        )
                    replay, pending, error = self.spell_choices.check(
                        response, fingerprint, row, context
                    )
                    if replay is not None:
                        return replay
                    if error is not None or response.payload.selected_option_id == "decline":
                        return self._finish_choice_without_cast(
                            response, context, fingerprint, row, error
                        )
                    command = self.spell_choices.resume(pending, response, row, context)
        willing_target_id = (
            response.actor_id if response is not None and shield_prompt is None else None
        )
        exploration_item = type(command.payload) is ExplorationItemUsePayload
        environmental_spell = type(command.payload) is ExplorationEnvironmentalSpellCastPayload
        multi_spell = type(command.payload) is ExplorationMultiSpellCastPayload
        ward_spell = (
            type(command.payload) is ExplorationSpellCastPayload
            and command.payload.target_ids is not None
        )
        spell_save_handlers = (
            (evaluate_bound_bane_step, verify_bound_bane_result)
            if ward_spell and command.payload.spell_id == "bane"
            else (evaluate_bound_faerie_step, verify_bound_faerie_result)
            if environmental_spell and command.payload.spell_id == "faerie-fire"
            else None
        )
        spell_save_recipient = None
        ward_needs = (
            prepare_ward_spell(command.payload, command.actor_id, self.rules.loader)
            if ward_spell
            else None
        )
        selected_material = (
            ward_needs.material_instance_id
            if ward_spell and ward_needs is not None
            else getattr(command.payload, "material_instance_id", None)
        )
        casting_start = type(command.payload) is StartSpellCast
        casting_finish = type(command.payload) is FinishSpellCast
        casting_activity = casting_start or casting_finish
        exploration_spell = (
            environmental_spell
            or multi_spell
            or casting_activity
            or type(command.payload) is ExplorationSpellCastPayload
        )
        exploration_rule = exploration_item or exploration_spell
        exact_condition_choice = (
            type(command.payload) is ExplorationSpellCastPayload
            and command.payload.selected_condition_id is not None
        )
        independent_condition = None
        rest = type(command.payload) in (LongRest, ShortRest)
        long_rest = type(command.payload) is LongRest
        if casting_activity or (not (exploration_rule or rest) and shield_prompt is None):
            command = self.runtime.prepare_timed_command(command, package, context, fingerprint)
        if type(command.payload) is CombatWithdraw and (
            command.payload.target_cell is None and command.payload.passage_id is None
        ):
            return await self._submit_distance_withdrawal(command, package, context, fingerprint)
        object_check = type(command.payload) in OPENABLE_CHECK_OPERATIONS
        mechanism_check = type(command.payload) is CheckMechanism
        domain_check = object_check or mechanism_check
        public_check = type(command.payload) is CheckPayload
        public_save = type(command.payload) is SavePayload
        check_service = (
            self.world.objects.mechanisms if mechanism_check else self.world.objects.openable
        )
        plan, prepare_error, rule_command = None, None, command
        withdrawal_requested = type(command.payload) is CombatWithdraw
        movement_requested = (
            withdrawal_requested or getattr(command.payload, "intent_type", None) == "move"
        )
        region, passage = None, None
        blockers = None
        unconfined = None
        policy = None
        attune_plan = None
        initial_exploration_error = None
        rest_plan = None
        cast_record = None
        spell_actor_ids, spell_positions = None, None
        environmental_schema_version = None
        environmental_world_needs = None
        multi_spell_positions = None
        sight_range = self._spell_sight_range(command)
        charged_item, item_area_facts = None, None
        check_request = None
        check_sight_needs, check_sight_facts = (), ()

        def project_multi_spell_positions(value):
            """Project the selected positions and confirmation-specific willingness."""
            return self.multi_spell_choices.positions(
                value,
                command,
                verified_target_ids=command.payload.target_ids if confirmation is not None else (),
            )

        def prepare_charged_item(value):
            """Read the authorized retained item's exact mechanical pre-value."""
            try:
                item = self.world.objects.retained_charge_item(
                    value, command.actor_id, command.payload.instance_id, package
                )
            except StateMachineError as exc:
                return None, exc.code
            return self.world.facts.item_entry(item) if item is not None else None, None

        def cast_material(value):
            """Read the exact accessible material without exposing ownership failures."""
            # RUO-62-T1: Bless's material check reads inventory access. Focus
            # posture needs a typed World fact before admission extends to it.
            item = self.world.facts.object(value["session_id"], selected_material)
            entry = (
                self.world.facts.item_entry(item)
                if item is not None and item["item_slug"] is not None
                else None
            )
            if (
                entry is None
                or entry["owner_id"] != command.actor_id
                or not entry["accessible"]
                or entry["quantity"] != 1
                or entry["charges_remaining_per_unit"] != 0
            ):
                return None
            return entry

        def exploration_error(value):
            """Check mode and protocol, deferring use while saved time work is unfinished."""
            return self._exploration_error(
                value,
                command,
                exploration_spell=exploration_spell,
                casting_finish=casting_finish,
                casting_activity=casting_activity,
            )

        def prepare_rest_plan(value):
            """A cast and rest cannot run as concurrent Activities for this actor."""
            if self.world.spell_casts.active(command.session_id, command.actor_id):
                return None, "rest_activity_active"
            return prepare_rest(
                value, command.actor_id, self.world, self.activities, command.payload
            )

        def build_snapshot(value):
            """Freeze the relevant cast, combat, check or item state for evaluation."""
            if domain_check and self.world._active_combat(command.session_id):
                # Preserve a durable rejected receipt without pretending a combat
                # snapshot is a noncombat check or relaxing any capability guard.
                return self.rules._combat_snapshot(value)
            if rest:
                return self.rules._noncombat_snapshot(value, actor_ids=(command.actor_id,))
            if exploration_spell or charged_item is not None:
                # Casts reuse the fixed mechanical grid and only project the caster's
                # current component constraint onto the detached RE snapshot.
                snapshot = self.rules._noncombat_snapshot(value, actor_ids=spell_actor_ids)
                if exploration_spell:
                    return self.with_current_verbal_availability(
                        snapshot, value, package, command.actor_id
                    )
                return snapshot
            if public_check and command.payload.dc is not None:
                actor_ids = tuple(
                    actor_id
                    for (actor_id,) in self._conn.execute(
                        "SELECT actor_id FROM actors WHERE session_id=? "
                        "AND actor_id IN (?,?) AND current_scene_id=? ORDER BY actor_id",
                        (
                            command.session_id,
                            command.actor_id,
                            command.payload.target_id,
                            value["current_scene_id"],
                        ),
                    )
                )
                return self.rules._noncombat_snapshot(value, package, actor_ids=actor_ids)
            if public_save:
                return self.rules._noncombat_snapshot(value, actor_ids=(command.actor_id,))
            if public_check or domain_check or exploration_item:
                return self.rules._noncombat_snapshot(value, package)
            intent_type = getattr(command.payload, "intent_type", None)
            projects_walk = movement_requested or (
                intent_type == "cast_spell"
                and command.payload.spell_id is not None
                and spell_attack_has_speed_effect(self.rules.loader, command.payload.spell_id)
            )
            attack = shield_original or rule_command
            attack_actors = (
                (attack.actor_id, attack.payload.target_id)
                if getattr(attack.payload, "intent_type", None) == "attack"
                else None
            )
            snapshot = self.rules._combat_snapshot(
                value,
                for_pass=intent_type == "pass",
                for_walk=projects_walk,
                attack_actors=attack_actors,
            )
            return confine_walk(snapshot, blockers)

        with self.store.transaction():
            row = self.sessions._session(command.session_id, package, context)
            if shield_prompt is None:
                self.sessions._authorize_command(row, command.actor_id, command.payload, context)
            self.runtime.authorize_time_wait_reply(row, package, command, context)
            if response is None and confirmation is None:
                self.sessions._npc_handoff_command_guard(command)
                # A resumed pick must replay the reply's receipt if another
                # submission finalizes the pick during the handoff.
                if pick_reply is None and (
                    replay := self.runtime._command_replay(command, fingerprint)
                ):
                    return replay
                guard_shield_window(self._conn, row)
            if public_save and self.world._active_combat(command.session_id):
                raise StateMachineError(
                    "unsupported_operation", "Standalone saves require exploration"
                )
            self.sessions._npc_combat_location_guard(shield_original or command)
            end_condition_ref = (
                self.rules.records.active_end_condition(command.session_id)
                if type(command.payload) is CombatClosePayload
                else None
            )
            if row["evaluator_version"] != EVALUATOR_VERSION:
                raise StateMachineError(
                    "ruleset_binding_mismatch", "Pinned evaluator version is not installed"
                )
            if movement_requested:
                policy = self.rules.records.active_withdrawal(command.session_id)
                if isinstance(policy, EncounterRegion):
                    region = policy
            if withdrawal_requested:
                try:
                    target, passage = prepare_withdrawal(
                        self.world, row, command.actor_id, command.payload, package, region
                    )
                    rule_command = replace(
                        command,
                        command_type="combat.intent",
                        payload=CombatIntentPayload(intent_type="move", target_zone_id=target),
                    )
                except StateMachineError as exc:
                    prepare_error = exc.code
            if exploration_rule:
                initial_exploration_error = exploration_error(row)
                prepare_error = initial_exploration_error
            if exploration_item and prepare_error is None:
                charged_item, prepare_error = prepare_charged_item(row)
                payload = command.payload
                if prepare_error is None and (
                    payload.cast_level is not None or payload.area_target is not None
                ):
                    asset = (
                        self.rules.loader.get_item(charged_item["item_slug"])
                        if charged_item is not None
                        else None
                    )
                    if asset is None or not is_healing_staff(asset):
                        prepare_error = "item.capability"
                if charged_item is not None and prepare_error is None:
                    prepare_error, spell_actor_ids, spell_positions, item_area_facts = (
                        self._charged_item_positions(row, package, command, charged_item)
                    )
                elif prepare_error is None and payload.target_id != command.actor_id:
                    raise StateMachineError(
                        "invalid_target", "Drinking a potion requires self target"
                    )
            if rest:
                rest_plan, prepare_error = prepare_rest_plan(row)
                if prepare_error is None:
                    rule_command = replace(
                        command,
                        command_type="rules.rest",
                        payload=RestPayload(
                            kind="rules.rest",
                            target_id=command.actor_id,
                            rest_type="long" if long_rest else "short",
                            completed_at_tick=rest_plan[1] if long_rest else None,
                            hit_dice=()
                            if long_rest
                            else tuple(
                                HitDieSpend(pool_id=spend.pool_id, amount=spend.amount)
                                for spend in command.payload.hit_dice
                            ),
                        ),
                    )
            if casting_activity and prepare_error is None:
                spell_actor_ids = (command.actor_id,)
                rule_command, cast_record, prepare_error = self._prepare_cast_activity(
                    row, command, package
                )
            elif environmental_spell and prepare_error is None:
                environmental_world_needs = self.rules.environmental_spell_world_needs(command)
                spell_positions = self.world.magic.cast_positions(
                    row, package, command, environmental_world_needs
                )
                if spell_positions is None:
                    prepare_error = (
                        "target_invalid"
                        if isinstance(
                            command.payload.target,
                            (EnvironmentalMagicEffectTarget, EnvironmentalCreatureTarget),
                        )
                        else "spell.target"
                    )
                else:
                    environmental_preparation = self.rules.prepare_environmental_spell(
                        command, spell_positions, environmental_world_needs
                    )
                    spell_actor_ids = environmental_preparation.actor_ids
                    environmental_schema_version = environmental_preparation.schema_version
            elif multi_spell and prepare_error is None:
                prepare_error, spell_actor_ids, multi_spell_positions = (
                    project_multi_spell_positions(row)
                )
            elif ward_spell and prepare_error is None:
                if ward_needs is None:
                    prepare_error = "spell.asset"
                else:
                    prepare_error, spell_actor_ids, multi_spell_positions = self._ward_positions(
                        row, command, package, ward_needs
                    )
            elif exploration_spell and prepare_error is None:
                if sight_range is not None and self.runtime.version_conflict(row, command):
                    prepare_error = "version_conflict"
                else:
                    prepare_error, spell_actor_ids, spell_positions = self._spell_positions(
                        row,
                        command,
                        package,
                        willing_target_id=willing_target_id,
                        sight_range=sight_range,
                    )
            if exact_condition_choice and prepare_error is None:
                independent_condition = self._independent_condition(command)
                if independent_condition is None:
                    prepare_error = "spell.condition_choice"
            material = None
            if (
                prepare_error is None
                and type(command.payload) is ExplorationSpellCastPayload
                and selected_material is not None
            ):
                material = cast_material(row)
                if material is None:
                    prepare_error = "spell.material"
            snapshot = None if prepare_error else build_snapshot(row)
            if snapshot is not None and region is not None and not withdrawal_requested:
                blockers = boundary_blockers(region, snapshot.scene_state.grid)
                if blockers:
                    unconfined, snapshot = snapshot, confine_walk(snapshot, blockers)
            if (
                spell_save_handlers is not None
                and self.action_choices is not None
                and snapshot is not None
                and row["pc_actor_id"] in spell_actor_ids
                and (not ward_spell or row["pc_actor_id"] in command.payload.target_ids)
                and any(
                    effect.id == "effect:inspired"
                    and effect.target_id == row["pc_actor_id"]
                    and not effect.disabled
                    for effect in snapshot.effect_states
                )
            ):
                tick = self.world.facts.time_state(command.session_id)[1]
                if (
                    self.world.characters.effects.held_bardic_inspiration(
                        command.session_id, row["pc_actor_id"], tick
                    )
                    is not None
                ):
                    spell_save_recipient = row["pc_actor_id"]
            if rest and snapshot is not None and not long_rest:
                attune_plan, prepare_error = prepare_attunement(
                    row, command.actor_id, command.payload, self.world, snapshot
                )
            spell_end_applications = (
                self.rules.spell_end_applications(command.session_id, command.actor_id)
                if snapshot is not None and (exploration_spell or charged_item is not None)
                else ()
            )
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
                (public_check and command.payload.dc is not None)
                or (
                    domain_check
                    and plan is not None
                    and plan[0] is not None
                    and not (object_check and plan[5])
                )
            ) and row["world_version"] == command.expected_world_version:
                check_request = self.rules.request(rule_command, package, snapshot, rng, row=row)
                check_request, check_sight_needs, check_sight_facts = self.rules.with_check_sight(
                    check_request, package
                )
            if (
                snapshot is not None
                and type(command.payload) not in (CheckPayload, SavePayload)
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
            request = check_request or self.rules.request(
                shield_original or rule_command,
                package,
                snapshot,
                rng,
                row=row,
                spell_positions=spell_positions,
                environmental_schema_version=environmental_schema_version,
                multi_spell_positions=multi_spell_positions,
                exploration_charge=charged_item is not None,
                spell_end_applications=spell_end_applications,
                item_area_facts=item_area_facts,
                independent_condition=independent_condition,
            )
            capability = (
                None
                if rest or casting_activity
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
        result, changes, shield_token = None, (), None
        withdrawal_turn = None
        spent_spell_grants = ()
        preverified_spell_save = False
        if _spell_save_resume is not None:
            pending = _spell_save_resume[0]
            if (
                request != pending.request
                or capability is not None
                or spell_save_recipient != pending.progress.pending_target_id
            ):
                self.action_choices._discard_pending_action_choice(
                    command.session_id, expected=pending
                )
                raise StateMachineError("choice_stale", "Spell save facts changed while waiting")
        input_version = row["world_version"] if snapshot is None else snapshot.world_version
        if input_version == command.expected_world_version:
            self.rules._verify_dnd_binding(package, row)
            if capability is None and not noop:
                try:
                    if shield_prompt is not None:
                        request, result = await answer_shield(
                            request,
                            shield_pending["checkpoint_token"],
                            accept=response.payload.selected_option_id == "accept",
                            loader=self.rules.loader,
                        )
                    elif object_check:
                        if pick_failure is None:
                            result = await evaluate_openable(spec, loader=self.rules.loader)
                        else:
                            previous_spec, result = pick_failure[0]
                            if previous_spec != spec:
                                raise StateMachineError("version_conflict", "Pick facts changed")
                    elif spell_save_handlers is not None and spell_save_recipient is not None:
                        step, verify = spell_save_handlers
                        try:
                            if _spell_save_resume is None:
                                spell_save_progress = step(
                                    request,
                                    choice_target_id=spell_save_recipient,
                                    loader=self.rules.loader,
                                )
                            else:
                                pending, reply, _ = _spell_save_resume
                                spell_save_progress = step(
                                    request,
                                    prior_steps=pending.progress.steps,
                                    choice=reply.payload.selected_option_id == "accept",
                                    choice_die_sides=pending.grant.die_sides,
                                    choice_target_id=spell_save_recipient,
                                    loader=self.rules.loader,
                                )
                        except ValueError as exc:
                            if _spell_save_resume is not None:
                                self.action_choices._discard_pending_action_choice(
                                    command.session_id, expected=_spell_save_resume[0]
                                )
                                raise StateMachineError(
                                    "choice_stale", "Spell save choice no longer evaluates"
                                ) from exc
                            ordinary = await self.rules._evaluate_dnd(copy.deepcopy(request))
                            if ordinary.status not in ("rejected", "unsupported"):
                                raise StateMachineError(
                                    "unsupported_action_choice",
                                    "Spell save choice cannot be staged",
                                ) from exc
                            result = ordinary
                        else:
                            if spell_save_progress.pending_target_id is not None:
                                if _spell_save_resume is not None:
                                    raise StateMachineError(
                                        "invalid_rule_result", "Spell save choice did not finish"
                                    )
                                return self.action_choices._open_spell_save_choice(
                                    command,
                                    fingerprint,
                                    request,
                                    spell_save_progress,
                                    package,
                                    context,
                                )
                            result = spell_save_progress.result
                            if _spell_save_resume is not None:
                                steps = spell_save_progress.steps
                                prior = pending.progress.steps
                                index = len(prior) - 1
                                if (
                                    result is None
                                    or len(steps) < len(prior)
                                    or steps[:index] != prior[:index]
                                    or steps[index].original != prior[index].original
                                    or steps[index].rng_after != prior[index].rng_after
                                    or steps[index].choice != reply.payload.selected_option_id
                                ):
                                    raise StateMachineError(
                                        "invalid_rule_result",
                                        "Spell save result changed the reply or prefix",
                                    )
                                try:
                                    spent_spell_grants = verify(
                                        result,
                                        request,
                                        steps,
                                        choice_target_id=spell_save_recipient,
                                        loader=self.rules.loader,
                                    )
                                except ValueError as exc:
                                    raise StateMachineError(
                                        "invalid_rule_result", "Invalid spell save continuation"
                                    ) from exc
                                preverified_spell_save = True
                    elif (
                        getattr(command.payload, "intent_type", None) == "attack"
                        and command.payload.stat_block_action_id is not None
                        and command.time_wait is None
                    ):
                        result = await evaluate_for_shield(
                            copy.deepcopy(request),
                            loader=self.rules.loader,
                            ordinary_evaluator=self.rules._evaluate_dnd,
                        )
                        if isinstance(result, str):
                            shield_token, result = result, None
                    else:
                        result = await self.rules._evaluate_dnd(copy.deepcopy(request))
                        if (
                            type(command.payload) is CombatClosePayload
                            and result.status == "rejected"
                            and result.error.code == "closure.nonterminal"
                            and end_condition_ref is not None
                            and end_condition_ref[1] is not None
                        ):
                            condition_id = end_condition_ref[1]
                            provider = self.rules.records_end_conditions.get(condition_id)
                            if provider is None:
                                raise StateMachineError(
                                    "unknown_end_condition",
                                    "Encounter end condition is unavailable",
                                )
                            # A host-installed predicate only sees detached fixed facts;
                            # this stage never supplies Store or a live Session owner.
                            met = provider(copy.deepcopy(request.state_snapshot))
                            if type(met) is not bool:
                                raise StateMachineError(
                                    "invalid_end_condition_result",
                                    "Condition must return a boolean",
                                )
                            if met:
                                request = type(request).model_validate(
                                    request.model_copy(
                                        update={
                                            "configured_close": ConfiguredCloseFact(
                                                condition_id=condition_id,
                                                combat_id=end_condition_ref[0],
                                                world_version=request.state_snapshot.world_version,
                                                satisfied=True,
                                            )
                                        }
                                    )
                                )
                                result = await self.rules._evaluate_dnd(copy.deepcopy(request))
                except RulesetBindingError as exc:
                    raise StateMachineError(
                        "ruleset_binding_mismatch", "Evaluator binding differs"
                    ) from exc
                except ValidationError as exc:
                    raise StateMachineError(
                        "rule_schema_error", "Evaluator schema failure"
                    ) from exc
                except StateMachineError:
                    raise
                except Exception as exc:
                    raise StateMachineError(
                        "rule_evaluation_failed",
                        "Evaluator failed; original command may be retried",
                    ) from exc
                if result is not None:
                    try:
                        result, changes = validate_dnd_result(
                            request,
                            result,
                            loader=self.rules.loader,
                            preverified_spell_save=preverified_spell_save,
                        )
                    except UnsupportedSpellConsequence:
                        capability = "spell.capability"
                    except UnsupportedEffectConsequence:
                        capability = "effect.capability"
                    except UnsupportedShieldTerminal:
                        capability = "combat.shield_terminal_unsupported"
                    except UnsupportedShieldKnockout:
                        capability = "combat.shield_knockout_unsupported"
                    if (
                        unconfined is not None
                        and capability is None
                        and result.status == "accepted"
                    ):
                        try:
                            require_no_unmasked_opportunity(
                                unconfined, walking_position(changes), self.rules.loader
                            )
                        except StateMachineError as exc:
                            capability = exc.code
                    if withdrawal_requested and capability is None and result.status == "accepted":
                        try:
                            withdrawal_turn = await self._prepare_withdrawal_turn(
                                row,
                                package,
                                context,
                                command,
                                region,
                                passage,
                                request,
                                result,
                                changes,
                            )
                        except StateMachineError as exc:
                            if exc.code == "invalid_rule_result":
                                raise
                            capability = exc.code
            self.rules._verify_dnd_binding(package, row)
        choice_requested = result is not None and result.status == "needs_choice"
        bardic_candidate = (
            self.bardic_checks.candidate(command, request, result)
            if command.actor_id == row["pc_actor_id"] and capability is None
            else None
        )
        with self.store.transaction(write=True):
            row = self.sessions._session(command.session_id, package, context)
            if pick_reply is not None:
                pending, reply, reply_fingerprint = pick_reply
                self.sessions._authorize_command(row, reply.actor_id, reply.payload, context)
                if replay := self.runtime._command_replay(reply, reply_fingerprint):
                    self.action_choices._discard_pending_action_choice(
                        command.session_id, expected=pending
                    )
                    return replay
                if self.runtime._command_replay(command, fingerprint):
                    raise StateMachineError("choice_closed", "Original pick already completed")
            if spell_save_reply is not None:
                self.sessions._authorize_command(
                    row, spell_save_reply.actor_id, spell_save_reply.payload, context
                )
                if replay := self.runtime._command_replay(spell_save_reply, _spell_save_resume[2]):
                    self.action_choices._discard_pending_action_choice(
                        command.session_id, expected=_spell_save_resume[0]
                    )
                    return replay
                if self.runtime._command_replay(command, fingerprint):
                    self.action_choices._discard_pending_action_choice(
                        command.session_id, expected=_spell_save_resume[0]
                    )
                    raise StateMachineError("choice_closed", "Original cast already completed")
                if (
                    row["pc_actor_id"] != spell_save_reply.actor_id
                    or self.rules.session_binding(row) != request.ruleset_binding
                    or not self.action_choices._current_spell_save_grant(_spell_save_resume[0])
                ):
                    self.action_choices._discard_pending_action_choice(
                        command.session_id, expected=_spell_save_resume[0]
                    )
                    raise StateMachineError("choice_stale", "Spell save grant changed or expired")
            if self.action_choices is not None:
                resume = pick_reply if pick_reply is not None else _spell_save_resume
                self.action_choices._guard_pending_action_owner(
                    command.session_id, expected=resume[0] if resume is not None else None
                )
            if shield_prompt is not None:
                replay, pending, error = self.shield_windows.check(
                    response, fingerprint, row, context, shield_prompt
                )
                if replay is not None:
                    return replay
                if error is not None:
                    return self._finish_shield_error(response, context, fingerprint, row, error)
                if dict(pending) != dict(shield_pending):
                    raise StateMachineError("version_conflict", "Shield window changed")
                if not self.shield_windows.source_is_authorized(shield_original):
                    return self._finish_shield_error(
                        response, context, fingerprint, row, "choice_stale"
                    )
            elif confirmation is not None:
                replay, pending_cast, error = self.multi_spell_choices.prepare_finalize(
                    confirmation, fingerprint, row, context
                )
                if replay is not None:
                    return replay
                guard_shield_window(self._conn, row)
                if error is not None or pending_cast != command:
                    return self.multi_spell_choices.reject_finalize(
                        confirmation, fingerprint, row, context, error or "version_conflict"
                    )
            elif response is not None:
                guard_shield_window(self._conn, row)
                replay, _, error = self.spell_choices.check(response, fingerprint, row, context)
                if replay is not None:
                    return replay
                if error is not None:
                    return self._finish_choice_without_cast(
                        response, context, fingerprint, row, error
                    )
            if shield_prompt is None:
                self.sessions._authorize_command(row, command.actor_id, command.payload, context)
            self.runtime.authorize_time_wait_reply(row, package, command, context)
            if response is None and confirmation is None:
                self.sessions._npc_handoff_command_guard(command)
                if replay := self.runtime._command_replay(command, fingerprint):
                    return replay
                guard_shield_window(self._conn, row)
            if public_save and self.world._active_combat(command.session_id):
                raise StateMachineError(
                    "unsupported_operation", "Standalone saves require exploration"
                )
            self.sessions._npc_combat_location_guard(shield_original or command)
            version = row["world_version"]
            status, error = "committed", None
            pending_choice = None
            pending_choices = None
            current_exploration_error = exploration_error(row) if exploration_rule else None
            changed_rest_plan = rest and prepare_rest_plan(row) != (
                rest_plan,
                None if rest_plan is not None else prepare_error,
            )
            if rest and attune_plan is not None and prepare_error is None:
                changed_attune_plan = prepare_attunement(
                    row, command.actor_id, command.payload, self.world, build_snapshot(row)
                ) != (attune_plan, None)
            else:
                changed_attune_plan = False
            if (
                self.runtime.version_conflict(row, command)
                or changed_rest_plan
                or changed_attune_plan
                or (exploration_rule and current_exploration_error != initial_exploration_error)
                or (
                    casting_activity
                    and prepare_error is None
                    and self._prepare_cast_activity(row, command, package)
                    != (rule_command, cast_record, None)
                )
                or (material is not None and cast_material(row) != material)
                or (
                    independent_condition is not None
                    and self._independent_condition(command) != independent_condition
                )
                or (
                    environmental_spell
                    and prepare_error is None
                    and self.world.magic.cast_positions(
                        row, package, command, environmental_world_needs
                    )
                    != spell_positions
                )
                or (
                    charged_item is not None
                    and prepare_error is None
                    and (
                        prepare_charged_item(row) != (charged_item, None)
                        or self._charged_item_positions(row, package, command, charged_item)
                        != (None, spell_actor_ids, spell_positions, item_area_facts)
                    )
                )
                or (
                    multi_spell
                    and prepare_error is None
                    and project_multi_spell_positions(row)
                    != (None, spell_actor_ids, multi_spell_positions)
                )
                or (
                    ward_spell
                    and prepare_error is None
                    and self._ward_positions(row, command, package, ward_needs)
                    != (None, spell_actor_ids, multi_spell_positions)
                )
                or (
                    not multi_spell
                    and not ward_spell
                    and (
                        sight_range is not None
                        or (response is not None and shield_prompt is None)
                        or choice_requested
                    )
                    and prepare_error is None
                    and self._spell_positions(
                        row,
                        command,
                        package,
                        willing_target_id=willing_target_id,
                        sight_range=sight_range,
                    )
                    != (prepare_error, spell_actor_ids, spell_positions)
                )
                or (snapshot is not None and build_snapshot(row) != snapshot)
                or (
                    check_sight_needs
                    and self.rules.check_sight_facts(
                        command.session_id, command.actor_id, check_sight_needs, package
                    )
                    != check_sight_facts
                )
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
            elif shield_token is not None:
                self.sessions._npc_combat_target_guard(command, package, snapshot)
                pending_choice = self.shield_windows.create(command, shield_token)
                status = "pending_choice"
            elif choice_requested and multi_spell:
                pending_choices, error = self.multi_spell_choices.create(
                    command, result.choice.target_ids
                )
                status = "pending_choice" if pending_choices else "rejected"
            elif choice_requested:
                pending_choice, error = self.spell_choices.create(command)
                status = "pending_choice" if pending_choice is not None else "rejected"
            elif result.status != "accepted":
                status, error = "rejected", result.error.code
            elif (
                bardic_candidate is not None
                and (
                    bardic_prompt := self.bardic_checks.open_if_held(
                        row, package, command, context, request, result, bardic_candidate
                    )
                )
                is not None
            ):
                pending_choice, version = bardic_prompt
                status = "pending_choice"
            else:
                if (
                    type(command.payload) is CombatClosePayload
                    and request.configured_close is not None
                    and self.rules.records.active_end_condition(command.session_id)
                    != end_condition_ref
                ):
                    raise StateMachineError("version_conflict", "Encounter condition changed")
                if not (exploration_spell or rest):
                    self.sessions._npc_combat_target_guard(
                        shield_original or command, package, build_snapshot(row)
                    )
                # These casts already verified their ordered saves before
                # writes. The live read fence and facts above guard adoption.
                if not ward_spell and not (
                    environmental_spell and command.payload.spell_id == "faerie-fire"
                ):
                    result, changes = validate_dnd_result(
                        request,
                        result,
                        loader=self.rules.loader,
                    )
                self._conn.execute("SAVEPOINT dnd_effects")
                try:
                    if command.time_wait is not None:
                        self.runtime.time.waits.validate(row, package, command)
                    position = walking_position(changes) if movement_requested else None
                    withdrew = False
                    if movement_requested and position is not None:
                        live_policy = self.rules.records.active_withdrawal(command.session_id)
                        if live_policy != policy:
                            raise StateMachineError("version_conflict", "Encounter range changed")
                        if withdrawal_requested:
                            if prepare_withdrawal(
                                self.world,
                                row,
                                command.actor_id,
                                command.payload,
                                package,
                                live_policy,
                            ) != (rule_command.payload.target_zone_id, passage):
                                raise StateMachineError("version_conflict", "Exit route changed")
                            check_withdrawal(snapshot, region, position, passage, result)
                            withdrew = True
                        elif region is not None and crosses_boundary(region, position):
                            raise StateMachineError(
                                "combat.withdrawal_route",
                                "Ordinary movement remains inside the encounter",
                            )
                        if policy is not None:
                            require_walk_bystander_clearance(
                                self.world, command.session_id, snapshot, position
                            )
                        if (
                            withdrew
                            and withdrawal_continues(snapshot, command.actor_id)
                            and (withdrawal_turn is None)
                        ):
                            raise StateMachineError("version_conflict", "Withdrawal turn changed")
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
                        expiries = prepare_rest_time_expiries(self.world, request, rest_plan)
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
                            or build_snapshot(row)
                            != rest_snapshot_after_time(snapshot, expiries, time_events)
                            or self.rules._rng_context(row) != rng
                        ):
                            raise StateMachineError(
                                "rest_interrupted", "Time work changed the rest's dependencies"
                            )
                        if long_rest:
                            self.activities.complete_long_rest(
                                sid, command.actor_id, place[0], command.command_id, start, target
                            )
                        elif attune_plan is not None:
                            if prepare_attunement(
                                row,
                                command.actor_id,
                                command.payload,
                                self.world,
                                build_snapshot(row),
                            ) != (attune_plan, None):
                                raise StateMachineError(
                                    "rest_interrupted", "Attunement contact or bond changed"
                                )
                            bond_before = None if attune_plan[0] == "attune" else command.actor_id
                            bond_after = command.actor_id if attune_plan[0] == "attune" else None
                            self.world.facts.set_item_attunement(
                                sid, attune_plan[1], bond_before, bond_after
                            )
                            time_events.append(
                                self.world._capture_event(
                                    sid,
                                    package,
                                    "item.attuned" if bond_after else "item.unattuned",
                                    {"actor_id": command.actor_id, "instance_id": attune_plan[1]},
                                    phase="activity.after_attunement",
                                    source_actor_id=command.actor_id,
                                )
                            )
                    spatial_effect_id, effects_events = None, []
                    operations = result.state_delta.operations
                    spell_ends = tuple(op for op in operations if type(op) is WorldSpellEnd)
                    spatial_operation = (
                        operations[-len(spell_ends) - 1] if environmental_spell else None
                    )
                    effect_time = self.runtime.time.consumers["effects"]
                    # A native condition edit still refers to the fixed linked
                    # effects. Adopt that edit before ending any of those effects.
                    conditions_update = any(type(op) is ConditionsUpdate for op in operations)
                    spell_end_events = []
                    if spell_ends and not conditions_update:
                        spell_end_events = apply_world_spell_ends(
                            self.world,
                            command.session_id,
                            spell_ends,
                            package=package,
                            command_id=command.command_id,
                        )
                    spatial_proposal = (
                        spatial_operation.value
                        if isinstance(spatial_operation, WorldEnvironmentalEffectCreate)
                        else None
                    )
                    visual_proposal = (
                        spatial_operation
                        if isinstance(spatial_operation, WorldVisualEffectCreate)
                        else None
                    )
                    if casting_start:
                        effects_events = self.runtime.time.consumers[
                            "effects"
                        ].end_world_concentration(
                            command.session_id,
                            package,
                            command.actor_id,
                            "concentration_replaced",
                        )
                        focus = self.world.characters.effects.create_casting_focus(
                            command.session_id, command.actor_id, command.payload.activity_id
                        )
                        effects_events.append(
                            self.world.spell_casts.start(
                                row,
                                package,
                                command.actor_id,
                                command.payload,
                                seconds=result.state_delta.operations[0].duration_seconds,
                                concentration_id=focus,
                                command_id=command.command_id,
                            )
                        )
                    elif casting_finish:
                        # The bound casting focus gives up the one concentration slot
                        # before adopting the verified completed spell in this scope.
                        focus = self.world.characters.effects.concentration_group(
                            command.session_id, command.actor_id
                        )
                        if (
                            focus is None
                            or focus.concentration_id != cast_record["concentration_id"]
                            or focus.casting_activity_id != cast_record["activity_id"]
                        ):
                            raise StateMachineError(
                                "invalid_mechanical_state", "Casting focus changed"
                            )
                        self.world.characters.effects.remove_concentration(
                            command.session_id, focus.concentration_id
                        )
                    elif exploration_spell:
                        proposal = (
                            spatial_proposal
                            or visual_proposal
                            or next(
                                (
                                    value
                                    for part, _, _field, value in changes
                                    if part == "world_effect"
                                    or (part == "world_effect_group" and value)
                                ),
                                None,
                            )
                        )
                        if ward_spell or (
                            proposal is not None
                            and (
                                proposal[0].concentration
                                if isinstance(proposal, tuple)
                                else proposal.concentration
                            )
                        ):
                            effects_events = self.runtime.time.consumers[
                                "effects"
                            ].end_world_concentration(
                                command.session_id,
                                package,
                                command.actor_id,
                                "concentration_replaced",
                            )
                    self.rules._apply_dnd_changes(
                        row,
                        changes,
                        command_id=command.command_id,
                        snapshot=request.state_snapshot,
                        cast_spell_id=rule_command.payload.spell_id if exploration_spell else None,
                        # A finished Ritual paid no slot but retains its bound
                        # spell's base level for later Dispel eligibility.
                        cast_slot_level=(
                            rule_command.payload.slot_level
                            if exploration_spell
                            and getattr(rule_command.payload, "slot_level", None) is not None
                            else self.rules.loader.get_spell(rule_command.payload.spell_id).level
                            if casting_finish
                            else None
                        ),
                        turn_starts=tuple(
                            request.state_snapshot.combat_state.initiative_ids[
                                operation.value.current_turn_index
                            ]
                            for operation in result.state_delta.operations
                            if type(operation) is TurnUpdate
                        ),
                    )
                    if spent_spell_grants:
                        from .ongoing_effects import BARDIC_INSPIRATION_EFFECT_ID

                        tick = self.world.facts.time_state(command.session_id)[1]
                        for spent in spent_spell_grants:
                            current = self.world.characters.effects.for_target_effect(
                                command.session_id, spent.target_id, BARDIC_INSPIRATION_EFFECT_ID
                            )
                            held = self.world.characters.effects.held_bardic_inspiration(
                                command.session_id, spent.target_id, tick
                            )
                            if (
                                current != [spent.model_dump(mode="json")]
                                or held is None
                                or held.origin != spent.origin
                                or held.die_sides != spent.flags["bardic_inspiration"]["die_sides"]
                            ):
                                raise StateMachineError(
                                    "choice_stale", "Spell save grant changed before adoption"
                                )
                            self.world.characters.effects.remove_effect(
                                command.session_id,
                                spent.target_id,
                                BARDIC_INSPIRATION_EFFECT_ID,
                                spent.origin,
                            )
                    if spell_ends and conditions_update:
                        spell_end_events = apply_world_spell_ends(
                            self.world,
                            command.session_id,
                            spell_ends,
                            package=package,
                            command_id=command.command_id,
                        )
                    if spatial_proposal is not None:
                        spatial_effect_id, spatial_events = self.world.magic.apply(
                            row, package, command.command_id, spatial_proposal
                        )
                        effects_events.extend(spatial_events)
                    elif visual_proposal is not None:
                        spatial_effect_id, visual_events = self.world.magic.apply_visual(
                            row, package, command.command_id, visual_proposal
                        )
                        effects_events.extend(visual_events)
                    elif isinstance(spatial_operation, WorldEnvironmentalEffectEnd):
                        spatial_effect_id = spatial_operation.expected.effect_id
                        effects_events.append(
                            self.world.magic.apply_end(row, package, spatial_operation)
                        )
                    # Failure consequences and automatic hooks both consume the
                    # original rule result's candidate RNG successor. Never
                    # overwrite a mechanism successor with this earlier state.
                    self.runtime.advance_rule_rng(command.session_id, result.rng_transition, rng)
                    if casting_finish and isinstance(result.proposed_events[-1], ObjectIdentified):
                        self.world.spell_casts.record_identification(
                            command.session_id,
                            command.payload.activity_id,
                            result.proposed_events[-1].identification,
                        )
                    events = [
                        self.capture._capture_event(
                            command.session_id,
                            package,
                            "rules.check_rolled"
                            if public_check or domain_check
                            else f"rules.{event.type}"
                            if public_save
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
                                    if public_check or public_save or domain_check
                                    else {"item_instance_id": command.payload.instance_id}
                                    if exploration_item
                                    else {
                                        "spell_id": rule_command.payload.spell_id,
                                        **(
                                            {"effect_id": spatial_effect_id}
                                            if environmental_spell
                                            else {}
                                        ),
                                    }
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
                                **(
                                    {
                                        "hp_gained": next(
                                            (
                                                op.amount
                                                for op in result.state_delta.operations
                                                if op.kind == "actor.hp_delta"
                                                and op.target_id == event.target_id
                                            ),
                                            0,
                                        )
                                    }
                                    if exploration_spell
                                    and event.type == "healing_applied"
                                    and event.roll_detail is not None
                                    else {}
                                ),
                            },
                            phase="engine.intermediate_phase_unavailable",
                            unresolved=True,
                        )
                        for event in result.proposed_events
                        if not isinstance(event, ObjectIdentified)
                    ]
                    if long_rest:
                        events[0:0] = [
                            self.capture._capture_event(
                                command.session_id,
                                package,
                                "effect.expired",
                                {
                                    "identity": (effect.target_id, effect.id, effect.origin),
                                    "actor_id": effect.target_id,
                                    "scene_id": rest_plan[2][0],
                                    "boundary_tick": operation.completed_at_tick,
                                    "reason": "long_rest",
                                },
                                phase="effects.after_rest_expiry",
                            )
                            for operation in operations
                            if type(operation) is WorldRestEffectEnd
                            for effect in operation.expected
                        ]
                    events.extend(effects_events)
                    if withdrew:
                        if withdrawal_turn is not None and (
                            self.rules._combat_snapshot(row, for_pass=True) != withdrawal_turn[0]
                        ):
                            raise StateMachineError(
                                "combat.withdrawal_handoff_facts",
                                "Movement changed the fixed withdrawal turn facts",
                            )
                        next_actor, turn_events = self._adopt_withdrawal_turn(
                            row, package, command, withdrawal_turn, rng
                        )
                        events.extend(
                            commit_withdrawal(
                                self.world,
                                self.rules.records,
                                row,
                                package,
                                snapshot,
                                command.actor_id,
                                SpatialPosition(position.scene_id, position.value),
                                next_actor,
                                landing=passage.landing if passage is not None else None,
                            )
                        )
                        events.extend(turn_events)
                    events.extend(spell_end_events)
                    events.extend(
                        effect_time.spell_end_event(row["session_id"], package, ending)
                        for ending in spell_ends
                    )
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
                    if type(command.payload) is PickLock:
                        mechanisms = self.world.objects.mechanisms
                        prepared = pick_failure[1] if pick_failure is not None else None
                        token = mechanisms._prepared_saves.set(prepared)
                        source_token = mechanisms._source_command.set(command)
                        try:
                            events = self.effects._expand_events(
                                outcome_row, package, events, command.command_id
                            )
                            if prepared:
                                raise StateMachineError("version_conflict", "Pick trigger changed")
                        except _PickFailureSaveCandidate as pending:
                            pending.command = command
                            pending.check = (spec, result)
                            raise
                        finally:
                            mechanisms._source_command.reset(source_token)
                            mechanisms._prepared_saves.reset(token)
                    else:
                        events = self.effects._expand_events(
                            outcome_row, package, events, command.command_id
                        )
                    events = time_events + events
                    events.extend(
                        self.runtime.coordinate_combat_time(
                            row, package, command.command_id, previous_round, len(events)
                        )
                    )
                    if shield_prompt is not None:
                        self.shield_windows.close(response)
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
                        "combat.region_capability",
                        "combat.withdrawal_unavailable",
                        "combat.withdrawal_route",
                        "combat.withdrawal_effects_unsupported",
                        "combat.withdrawal_handoff_facts",
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
            if response is not None and shield_prompt is None and status != "conflict":
                self.spell_choices.close(
                    response, "accepted" if status == "committed" else "rejected"
                )
            if confirmation is not None and status == "committed":
                self.multi_spell_choices.finish_finalize(confirmation, "accepted")
            receipt = self.runtime.finish(
                confirmation or response or command,
                context,
                fingerprint,
                row,
                status,
                version,
                error,
                pending_choice=pending_choice,
                pending_choices=pending_choices,
            )
            if spell_save_reply is not None:
                self.sessions._authorize_command(
                    row, spell_save_reply.actor_id, spell_save_reply.payload, context
                )
                receipt = self.runtime.finish(
                    spell_save_reply,
                    context,
                    _spell_save_resume[2],
                    row,
                    status,
                    version,
                    error,
                )
            if pick_reply is not None:
                receipt = self.runtime.finish(
                    reply, context, reply_fingerprint, row, status, version, error
                )
        if pick_reply is not None:
            # Both receipts are durable once the write scope has closed.
            self.action_choices._discard_pending_action_choice(
                command.session_id, expected=pick_reply[0]
            )
        self.runtime.refresh_time_drivers()
        if spell_save_reply is not None:
            self.action_choices._discard_pending_action_choice(
                command.session_id, expected=_spell_save_resume[0]
            )
        return receipt

    def _bardic_grant_request(self, row, command, package):
        """Freeze the two actors, current clock, positions and recipient-to-Bard sight."""
        sid = command.session_id
        if (
            command.payload.target_id != row["pc_actor_id"]
            or command.payload.target_id == command.actor_id
        ):
            return None, "target_invalid"
        error = self._exploration_error(row, command, exploration_spell=False)
        if error is not None:
            return None, error
        binding, tick = self.world.facts.time_state(sid)
        if not isinstance(binding, TimeBinding):
            return None, "unsupported_time_protocol"
        ids = (command.actor_id, command.payload.target_id)
        places = [self.world.facts.carrier_position(sid, identity) for identity in ids]
        if any(
            place is None or place[0] != row["current_scene_id"] or place[1] is None
            for place in places
        ):
            return None, "target_invalid"
        snapshot = self.rules._noncombat_snapshot(row, actor_ids=ids)
        sight = read_creature_sight(
            self.world, sid, command.payload.target_id, (command.actor_id,), package
        )
        payload = command.payload
        return BardicGrantRequest(
            kind="world.use_feature",
            session_id=sid,
            command_id=command.command_id,
            actor_id=command.actor_id,
            target_id=payload.target_id,
            feature_id=payload.feature_id,
            source_cell=places[0][1],
            target_cell=places[1][1],
            visible_source_id=command.actor_id if sight.get(command.actor_id) else None,
            tick=tick,
            snapshot=snapshot,
            ruleset_binding=self.rules.session_binding(row),
        ), None

    def _submit_bardic_grant(self, command, package, context, fingerprint):
        """Adopt a canonical paid Inspiration grant in the ordinary shared commit scope."""
        command = self.runtime.prepare_timed_command(command, package, context, fingerprint)
        with self.store.transaction():
            row = self.sessions._session(command.session_id, package, context)
            self.sessions._authorize_command(row, command.actor_id, command.payload, context)
            self.sessions._npc_handoff_command_guard(command)
            if replay := self.runtime._command_replay(command, fingerprint):
                return replay
            guard_shield_window(self._conn, row)
            request, error = self._bardic_grant_request(row, command, package)
        proposal = None
        if error is None:
            try:
                proposal = plan_bardic_grant(request, loader=self.rules.loader)
            except ValueError:
                error = "feature_ineligible"
        with self.store.transaction(write=True):
            row = self.sessions._session(command.session_id, package, context)
            self.sessions._authorize_command(row, command.actor_id, command.payload, context)
            self.sessions._npc_handoff_command_guard(command)
            if replay := self.runtime._command_replay(command, fingerprint):
                return replay
            guard_shield_window(self._conn, row)
            version = row["world_version"]
            status = "committed"
            if self.runtime.version_conflict(row, command):
                status, error = "conflict", "version_conflict"
            elif error is not None:
                status = "rejected"
            else:
                current, current_error = self._bardic_grant_request(row, command, package)
                if current_error is not None or current != request:
                    status, error = "conflict", "version_conflict"
                else:
                    sid = command.session_id
                    payment = proposal.payment
                    if (
                        payment.owner_id != command.actor_id
                        or payment.amount != -1
                        or payment.expected not in current.snapshot.resource_state.pools
                        or proposal.effect.target_id != command.payload.target_id
                        or proposal.effect.id != "effect:inspired"
                        or proposal.effect.origin != f"cast:inspired:{command.actor_id}"
                        or proposal.effect.duration.seconds != proposal.duration_seconds
                        or payment.expected.kind != "feature_uses"
                        or payment.expected.feature_slug != "bardic-inspiration"
                        or proposal.effect.flags.get("bardic_inspiration", {}).get("grant_id")
                        != command.command_id
                    ):
                        raise StateMachineError("invalid_rule_result", "Feature plan differs")
                    if (
                        self.world.characters.effects.held_bardic_inspiration(
                            sid, command.payload.target_id, current.tick
                        )
                        is not None
                    ):
                        status, error = "rejected", "feature_ineligible"
                    else:
                        binding, tick = self.world.facts.time_state(sid)
                        if tick != current.tick:
                            raise StateMachineError("version_conflict", "Feature clock changed")
                        deadline = deadline_after_seconds(binding, tick, proposal.duration_seconds)
                        self._conn.execute("SAVEPOINT bardic_grant")
                        try:
                            self.world.characters.spend_resource(
                                sid,
                                command.actor_id,
                                payment.pool_id,
                                1,
                                expected_current=payment.expected.current,
                            )
                            self.world.characters.effects.add_effect(
                                sid,
                                proposal.effect.model_dump(mode="json"),
                                expires_tick=deadline,
                            )
                            events = [
                                self.capture._capture_event(
                                    sid,
                                    package,
                                    "world.feature_used",
                                    {
                                        "actor_id": command.actor_id,
                                        "target_id": command.payload.target_id,
                                        "feature_id": command.payload.feature_id,
                                        "grant_id": command.command_id,
                                        "die_sides": proposal.effect.flags["bardic_inspiration"][
                                            "die_sides"
                                        ],
                                        "expires_tick": deadline,
                                        "scene_id": row["current_scene_id"],
                                    },
                                    phase="engine.intermediate_phase_unavailable",
                                    unresolved=True,
                                )
                            ]
                            events = self.effects._expand_events(
                                row, package, events, command.command_id
                            )
                            version = self.runtime.stage_outcome(command, row, package, events)
                        except Exception:
                            self._conn.execute("ROLLBACK TO bardic_grant")
                            self.store._scope.read_memo.clear()
                            raise
                        finally:
                            self._conn.execute("RELEASE bardic_grant")
            receipt = self.runtime.finish(
                command, context, fingerprint, row, status, version, error
            )
        self.runtime.refresh_time_drivers()
        return receipt

    def _finish_choice_without_cast(self, response, context, fingerprint, row, error):
        """Commit refusal or decline without evaluating or modifying the world."""
        status = "committed"
        if error == "version_conflict":
            status = "conflict"
        elif error is not None:
            status = "rejected"
        if error in (None, "choice_expired"):
            self.spell_choices.close(response, "declined" if error is None else "expired")
        return self.runtime.finish(
            response, context, fingerprint, row, status, row["world_version"], error
        )

    def _finish_shield_error(self, response, context, fingerprint, row, error):
        """Commit a verified refusal and discard a checkpoint whose input has expired."""
        if error == "choice_stale":
            self.shield_windows.close(response)
        status = "conflict" if error == "version_conflict" else "rejected"
        return self.runtime.finish(
            response, context, fingerprint, row, status, row["world_version"], error
        )
