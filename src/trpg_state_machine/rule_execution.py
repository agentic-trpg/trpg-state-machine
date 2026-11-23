"""Shared prepare/evaluate/commit path for combat and exploration rules."""

import copy
from dataclasses import asdict, replace

from dnd5e_engine.evaluation_charged_item import (
    exploration_charged_cast_profile,
    is_healing_staff,
)
from dnd5e_engine.evaluation_condition_choice import condition_choice_profile
from dnd5e_engine.evaluation_contracts import (
    CheckPayload,
    ExplorationCastActivityPayload,
    ExplorationEnvironmentalSpellCastPayload,
    ExplorationItemAreaFacts,
    ExplorationItemUsePayload,
    ExplorationMultiSpellCastPayload,
    ExplorationSpellCastPayload,
    ExplorationSpellPositions,
    IndependentConditionSelection,
    ItemUsePayload,
    RestPayload,
    SavePayload,
)
from dnd5e_engine.evaluation_delta import ConditionsUpdate, TurnUpdate
from dnd5e_engine.evaluation_environmental_contracts import (
    EnvironmentalCreatureTarget,
    EnvironmentalMagicEffectTarget,
    WorldEnvironmentalEffectCreate,
    WorldEnvironmentalEffectEnd,
    WorldVisualEffectCreate,
)
from dnd5e_engine.evaluation_exploration_healing import exploration_healing_requires_sight
from dnd5e_engine.evaluation_identify import IDENTIFY_SPELL_ID
from dnd5e_engine.evaluation_openable import OpenableCheckRequest, evaluate_openable
from dnd5e_engine.evaluation_resources import HitDieSpend
from dnd5e_engine.evaluation_ruleset import EVALUATOR_VERSION, RulesetBindingError
from dnd5e_engine.evaluation_shield_window import answer_shield, evaluate_for_shield
from dnd5e_engine.evaluation_speed_effects import spell_attack_has_speed_effect
from dnd5e_engine.evaluation_spell_termination import WorldSpellEnd
from dnd5e_engine.events import ObjectIdentified
from dnd5e_engine.spellcasting import spell_range_ft
from pydantic import ValidationError

from ._execution import _WorldEffectKind, _WorldEffectOrigin
from .choice_models import ChoiceFinalizeSpellOperationPayload, ChoiceRespondOperationPayload
from .combat import process_id
from .dnd_checks import world_effects_ready
from .effect_hp_adoption import apply_world_spell_ends
from .errors import StateMachineError
from .models import SpellConditionOption, SpellConditionOptions
from .pending_bardic_checks import PendingBardicChecks
from .pending_multi_spell_choices import PendingMultiSpellChoices
from .pending_spell_choices import PendingSpellChoices
from .perception import visible_actor_in_range
from .rest_activity import LongRest, ShortRest, advance_rest, prepare_rest
from .rules_adapter import UnsupportedEffectConsequence, validate_dnd_result
from .shield_adoption import UnsupportedShieldKnockout, UnsupportedShieldTerminal
from .shield_window import ShieldWindows, guard_shield_window
from .spell_adoption import UnsupportedSpellConsequence
from .spell_cast_activity import (
    EXPLORATION_RULES_SNAPSHOT_VERSION,
    FinishSpellCast,
    StartSpellCast,
)
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
        self.spell_choices = PendingSpellChoices(
            self.store, self.runtime, self.sessions, self.world.facts
        )
        self.multi_spell_choices = PendingMultiSpellChoices(
            self.store, self.runtime, self.sessions, self.world.facts
        )
        self.shield_windows = ShieldWindows(self.store, self.runtime, self.sessions)
        self.bardic_checks = PendingBardicChecks(self)

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

    async def submit(self, command, package, context, fingerprint):
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
        object_check = type(command.payload) in OPENABLE_CHECK_OPERATIONS
        mechanism_check = type(command.payload) is CheckMechanism
        domain_check = object_check or mechanism_check
        public_check = type(command.payload) is CheckPayload
        public_save = type(command.payload) is SavePayload
        check_service = (
            self.world.objects.mechanisms if mechanism_check else self.world.objects.openable
        )
        plan, prepare_error, rule_command = None, None, command
        initial_exploration_error = None
        rest_plan = None
        cast_record = None
        spell_actor_ids, spell_positions = None, None
        environmental_schema_version = None
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
            item = self.world.facts.object(
                value["session_id"], command.payload.material_instance_id
            )
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
            if row["evaluator_version"] != EVALUATOR_VERSION:
                raise StateMachineError(
                    "ruleset_binding_mismatch", "Pinned evaluator version is not installed"
                )
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
                spell_positions = self.world.magic.cast_positions(row, package, command)
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
                        command, spell_positions
                    )
                    spell_actor_ids = environmental_preparation.actor_ids
                    environmental_schema_version = environmental_preparation.schema_version
            elif multi_spell and prepare_error is None:
                prepare_error, spell_actor_ids, multi_spell_positions = (
                    project_multi_spell_positions(row)
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
                and command.payload.material_instance_id is not None
            ):
                material = cast_material(row)
                if material is None:
                    prepare_error = "spell.material"
            snapshot = None if prepare_error else build_snapshot(row)
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
                public_check
                and command.payload.dc is not None
                and row["world_version"] == command.expected_world_version
            ):
                check_request = self.rules.request(command, package, snapshot, rng, row=row)
                check_sight_needs, check_sight_facts = self.rules.prepare_check_sight(
                    check_request, package
                )
            if (
                type(command.payload) not in (CheckPayload, SavePayload)
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
            if check_sight_facts:
                request = request.model_copy(update={"creature_sight_facts": check_sight_facts})
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
                        result = await evaluate_openable(spec, loader=self.rules.loader)
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
                if result is not None:
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
                    except UnsupportedShieldTerminal:
                        capability = "combat.shield_terminal_unsupported"
                    except UnsupportedShieldKnockout:
                        capability = "combat.shield_knockout_unsupported"
            self.rules._verify_dnd_binding(package, row)
        choice_requested = result is not None and result.status == "needs_choice"
        bardic_candidate = (
            self.bardic_checks.candidate(command, request, result)
            if command.actor_id == row["pc_actor_id"] and capability is None
            else None
        )
        with self.store.transaction(write=True):
            row = self.sessions._session(command.session_id, package, context)
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
            changed_rest_plan = rest and prepare_rest_plan(row) != (rest_plan, prepare_error)
            if (
                self.runtime.version_conflict(row, command)
                or changed_rest_plan
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
                    and self.world.magic.cast_positions(row, package, command) != spell_positions
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
                    not multi_spell
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
                if not (exploration_spell or rest):
                    self.sessions._npc_combat_target_guard(
                        shield_original or command, package, build_snapshot(row)
                    )
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
                                ),
                                None,
                            )
                        )
                        if proposal is not None and proposal.concentration:
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
                            else "rules.save_rolled"
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
                    events.extend(effects_events)
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
                    events = time_events + self.effects._expand_events(
                        outcome_row, package, events, command.command_id
                    )
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
