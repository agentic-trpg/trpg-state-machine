"""Real pinned evaluate(), closed delta validation and one SQLite commit boundary."""

import json
from types import MappingProxyType
from uuid import uuid4

from dnd5e_engine.evaluation_bardic_grant import ExplorationFeaturePayload
from dnd5e_engine.evaluation_contracts import (
    CheckPayload,
    CombatClosePayload,
    CombatIntentPayload,
    ExplorationEnvironmentalSpellCastPayload,
    ExplorationItemUsePayload,
    ExplorationMultiSpellCastPayload,
    ExplorationSpellCastPayload,
    ItemUsePayload,
    SavePayload,
)

from .combat import (
    DistanceWithdrawal,
    EncounterRegion,
    _remap,
    process_id,
    validate_noncombat_foundation,
)
from .combat_regions import adopt_combat_region_turn_start
from .combat_start_rules import prepare_start
from .combat_withdrawal import CombatWithdraw
from .commands import validate_command
from .errors import StateMachineError
from .grid import cell_in_bounds
from .package import canonical_json
from .rest_activity import LongRest, ShortRest


class CombatHandler:
    """Orchestrate encounter creation, checks, attacks and closure across domain owners."""

    def __init__(
        self,
        store,
        sessions,
        characters,
        facts,
        records,
        world,
        rules,
        capture,
        effects,
        persistence,
        activities,
        *,
        end_conditions=None,
    ):
        """Bind combat domain interfaces and their shared Store."""
        self.runtime = world.runtime
        self.store = store
        self.sessions = sessions
        self.characters = characters
        self.facts = facts
        self.records = records
        self.world = world
        self.rules = rules
        self.capture = capture
        self.effects = effects
        self.persistence = persistence
        self.activities = activities
        self.end_conditions = MappingProxyType(dict(end_conditions or {}))

    @property
    def _conn(self):
        """Return the shared Store connection."""
        return self.store.connection

    def _initialize_noncombat(self, row, package):
        """Validate and initialize noncombat actor, component and scene foundations."""
        self.store.require_write()
        initial = _remap(
            json.loads(package.noncombat_foundation_json), row["pc_template_id"], row["pc_actor_id"]
        )
        snapshot = validate_noncombat_foundation(initial)
        if {a.entity_id for a in snapshot.character_states if a.entity_type == "Character"} != {
            row["pc_actor_id"]
        }:
            raise StateMachineError("invalid_noncombat_foundation", "Expected the selected PC only")
        for actor in snapshot.character_states:
            self.characters.initialize_rule_actor(
                row,
                actor,
                xp=initial["xp"][actor.entity_id],
                schema_version=snapshot.snapshot_schema_version,
            )
        self.characters.initialize_rule_components(row["session_id"], snapshot)
        self.characters.validate_initial_equipment(row["session_id"], snapshot)
        self.runtime.set_mechanical_schema(row["session_id"], snapshot.snapshot_schema_version)
        self.runtime.select_rule_rng(row["session_id"], canonical_json(initial["rng_state"]))

    def _initialize_combat(self, row, package, prepared):
        """Check world geometry and initialize combat records/mechanics, returning captured
        events.
        """
        self.store.require_write()
        sid = row["session_id"]
        if self._conn.execute("SELECT 1 FROM combats WHERE session_id=?", (sid,)).fetchone():
            raise StateMachineError("combat_already_initialized", "Encounter cannot be reset")
        snapshot, rng = prepared
        spatial = self.facts.spatial_scene(sid, row["current_scene_id"])
        if snapshot.world_version != row["world_version"]:
            raise StateMachineError("version_conflict", "Combat preparation is stale")
        if spatial is not None:
            self.world._spatial_combat_guard(sid, row["current_scene_id"], package)
        if snapshot.combat_state.actor_zone != self.rules._current_cells(
            sid,
            row["current_scene_id"],
            snapshot.combat_state.initiative_ids,
            snapshot.scene_state.grid.model_dump(),
        ):
            raise StateMachineError(
                "version_conflict", "Combat positions changed during preparation"
            )
        current_actors = (
            self.characters.rule_actors(sid, {a.entity_id for a in snapshot.character_states})
            if self.facts.instances.session_value(row["session_id"], "has_noncombat")
            else {}
        )
        for actor in snapshot.character_states:
            expected_actor = actor.model_dump(
                mode="json",
                exclude={"spells_known"}
                if snapshot.snapshot_schema_version == "engine-snapshot/20"
                else None,
            )
            if expected_actor.get("held_weapon_instance_id") is None:
                expected_actor.pop("held_weapon_instance_id", None)
            if current_actors and current_actors.get(actor.entity_id) != expected_actor:
                raise StateMachineError(
                    "version_conflict", "Actor facts changed during combat preparation"
                )
            existing = self._conn.execute(
                "SELECT * FROM actors WHERE session_id=? AND actor_id=?", (sid, actor.entity_id)
            ).fetchone()
            if existing is None or existing["current_scene_id"] != row["current_scene_id"]:
                raise StateMachineError(
                    "invalid_combat_foundation", "Encounter actor must already be present"
                )
            is_pc = actor.entity_id == row["pc_actor_id"]
            if is_pc != (actor.entity_type == "Character"):
                raise StateMachineError(
                    "invalid_combat_foundation", "Actor kind differs from Session"
                )
            if self.facts.instances.session_value(row["session_id"], "has_noncombat"):
                continue
            self.characters.initialize_rule_actor(
                row, actor, encounter=True, schema_version=snapshot.snapshot_schema_version
            )
        if (
            not self.facts.instances.session_value(row["session_id"], "has_noncombat")
            and spatial is None
        ):
            self.facts.initialize_rule_scene(sid, snapshot.scene_state)
            self.runtime.prepare_scene_maps(sid, (snapshot.scene_state.scene_id,), self.facts.maps)
        if not self.facts.instances.session_value(row["session_id"], "has_noncombat"):
            self.characters.initialize_rule_components(sid, snapshot)
            self.characters.validate_initial_equipment(sid, snapshot)
        elif self.characters.rule_components(
            sid, {a.entity_id for a in snapshot.character_states}
        ) != snapshot.model_dump(
            mode="json",
            include={"inventory_state", "resource_state", "casting_state", "effect_states"},
        ):
            raise StateMachineError(
                "version_conflict", "Component facts changed during preparation"
            )
        self.runtime.set_mechanical_schema(row["session_id"], snapshot.snapshot_schema_version)
        self.records.initialize(sid, snapshot, process_id=process_id())
        combat = snapshot.combat_state
        if not combat.ended:
            adopt_combat_region_turn_start(
                self.world, row, snapshot, combat.initiative_ids[combat.current_turn_index]
            )
        self.runtime.select_rule_rng(sid, rng.model_dump_json(), reset_position=True)
        if self.runtime.time:
            self.runtime.time.invalidate(sid)
        return [
            self.capture._capture_event(
                sid,
                package,
                "combat.initialized",
                {"scene_id": snapshot.scene_state.scene_id},
                phase="combat.after_initialization",
            )
        ]

    def start(self, row, package, command):
        """Adopt one trusted encounter with fixed withdrawal and closure policy identities."""
        self.store.require_write()
        sid, scene_id = row["session_id"], row["current_scene_id"]
        payload = command.payload
        if payload.end_condition_id is not None and (
            payload.end_condition_id not in self.end_conditions
            or not callable(self.end_conditions[payload.end_condition_id])
        ):
            raise StateMachineError(
                "unknown_end_condition", "Encounter end condition is not registered"
            )
        participant_ids = payload.participant_ids
        if row["pc_actor_id"] not in participant_ids:
            raise StateMachineError("invalid_combat_roster", "Select the Session PC")
        if self.records.active(sid):
            raise StateMachineError("combat_already_active", "An encounter is already active")
        self.rules._verify_dnd_binding(package, row)
        snapshot = self.rules._noncombat_snapshot(row, actor_ids=payload.initiative_order)
        scene = self.facts.maps.scene(sid, scene_id)
        positions = self.rules._current_cells(sid, scene_id, participant_ids, scene.grid)
        combat_id = "combat." + uuid4().hex
        if payload.withdrawal_distance_ft is None:
            cells = self._opening_cells(scene.grid, payload, positions)
            withdrawal = EncounterRegion(combat_id, scene_id, cells)
        else:
            withdrawal = DistanceWithdrawal(combat_id, scene_id, payload.withdrawal_distance_ft)
        if self.world.closed_combat_passage_cells(sid, scene_id, cells=positions.values()):
            raise StateMachineError("invalid_combat_region", "A participant is on a closed doorway")
        self.world._spatial_combat_guard(sid, scene_id, package, project_passage_doors=True)
        candidate, budgets = prepare_start(
            snapshot,
            payload,
            row["pc_actor_id"],
            combat_id,
            positions,
            self.rules.session_binding(row),
            self.rules.loader,
        )
        self.records.initialize(
            sid,
            candidate,
            process_id=process_id(),
            withdrawal=withdrawal,
            end_condition_id=payload.end_condition_id,
        )
        for actor in candidate.character_states:
            changed = budgets[actor.entity_id]
            for field in sorted(self.characters.RULE_FIELDS):
                if getattr(actor, field) != getattr(changed, field):
                    self.characters.update_rule_metadata(
                        sid, actor.entity_id, field, getattr(changed, field)
                    )
        adopt_combat_region_turn_start(
            self.world, row, candidate, candidate.combat_state.initiative_ids[0]
        )
        if self.runtime.time:
            self.runtime.time.invalidate(sid)
        return [
            self.capture._capture_event(
                sid,
                package,
                "combat.started",
                {"scene_id": scene_id, "combat_id": combat_id},
                phase="combat.after_initialization",
            )
        ]

    def declare_opportunity(self, row, package, command):
        """Persist one PC-owned OA decision before the named NPC's movement."""
        from dnd5e_engine.evaluation_opportunity import opportunity_dependencies
        from dnd5e_engine.evaluation_reaction_state import (
            OpportunityAttackState,
            OpportunityDeclineState,
        )
        from dnd5e_engine.evaluation_state import CombatSnapshot

        self.store.require_write()
        sid = row["session_id"]
        if command.actor_id != row["pc_actor_id"]:
            raise StateMachineError("unauthorized", "Only the selected PC can decide this reaction")
        combat_row = self._conn.execute(
            "SELECT process_id FROM combats WHERE session_id=?", (sid,)
        ).fetchone()
        if combat_row is None:
            raise StateMachineError("combat_not_initialized", "No active encounter")
        if combat_row[0] != process_id():
            raise StateMachineError("combat_unrestorable", "Combat belongs to another process")
        self.rules._verify_dnd_binding(package, row)
        snapshot = self.rules._combat_snapshot(row)
        state = snapshot.combat_state
        mover = command.payload.mover_id
        if (
            state.ended
            or state.pending_reactions
            or mover == command.actor_id
            or mover != state.initiative_ids[state.current_turn_index]
            or mover not in state.encounter_ids
            or command.actor_id not in state.party_ids
        ):
            raise StateMachineError("combat.reaction_unavailable", "No open NPC movement window")
        actor = next(a for a in snapshot.character_states if a.entity_id == command.actor_id)
        if (
            command.actor_id in state.dead_ids
            or not actor.is_alive
            or actor.hp_current <= 0
            or not actor.reaction_available
        ):
            raise StateMachineError("combat.reaction_unavailable", "PC cannot react")
        common = dict(
            owner_id=command.actor_id,
            mover_id=mover,
            trigger="voluntary_leave_reach",
            expiry="owner_next_turn_start",
        )
        if command.payload.accept:
            weapon = actor.weapon_in_hands
            if weapon is None or state.opportunity_attack_weapons.get(command.actor_id) != weapon:
                raise StateMachineError("combat.reaction_weapon", "No selected held melee weapon")
            declaration = OpportunityAttackState(
                source_kind="opportunity_attack",
                weapon_id=weapon,
                two_handed=actor.weapon_grip == "two_handed",
                **common,
            )
        else:
            declaration = OpportunityDeclineState(source_kind="opportunity_decline", **common)
        candidate = CombatSnapshot.model_validate_json(
            snapshot.model_copy(
                update={
                    "combat_state": state.model_copy(update={"pending_reactions": [declaration]})
                }
            ).model_dump_json()
        )
        _, failure = opportunity_dependencies(candidate, self.rules.loader)
        if failure:
            raise StateMachineError("combat.reaction_unavailable", failure)
        self.records.update_rule_state(
            sid, "pending_reactions", [declaration.model_dump(mode="json")]
        )
        return [
            self.capture._capture_event(
                sid,
                package,
                "combat.opportunity_declared",
                {"owner_id": command.actor_id, "mover_id": mover, "accept": command.payload.accept},
                phase="combat.before_movement",
            )
        ]

    @staticmethod
    def _opening_cells(grid, payload, positions):
        """Validate an explicit region or clip a thirteen-by-thirteen default to playable cells."""
        topology = grid.topology
        if payload.area_cells is not None:
            cells = payload.area_cells
            if any(
                not cell_in_bounds(topology, cell) or not topology.is_valid_cell(cell)
                for cell in cells
            ):
                raise StateMachineError(
                    "invalid_combat_region", "Area has an invalid or blocked cell"
                )
        else:
            opening = payload.opening_cell
            if not cell_in_bounds(topology, opening) or not topology.is_valid_cell(opening):
                raise StateMachineError(
                    "invalid_combat_region", "Opening cell is invalid or blocked"
                )
            x0, y0 = map(int, opening.split(","))
            radius = 6
            cells = tuple(
                f"{x},{y}"
                for y in range(max(0, y0 - radius), min(grid.data["height"], y0 + radius + 1))
                for x in range(max(0, x0 - radius), min(grid.data["width"], x0 + radius + 1))
                if topology.is_valid_cell(f"{x},{y}")
            )
        region = frozenset(cells)
        if not region.issuperset(positions.values()):
            raise StateMachineError("invalid_combat_region", "Area must contain every participant")
        return region

    def read_combat_region(self, session_id, package, *, context):
        """Authorize reading the active fixed region without returning participant data."""
        with self.store.transaction():
            self.sessions._session(session_id, package, context)
            return self.records.active_region(session_id)

    def read_combat_withdrawal(self, session_id, package, *, context):
        """Authorize reading the active policy without returning participant data."""
        with self.store.transaction():
            self.sessions._session(session_id, package, context)
            return self.records.active_withdrawal(session_id)

    def read_noncombat_snapshot(self, session_id, package, *, context):
        """Authorize access and verify bindings before constructing a noncombat snapshot."""
        with self.store.transaction():
            row = self.sessions._session(session_id, package, context)
            self.rules._verify_dnd_binding(package, row)
            return self.rules._noncombat_snapshot(row, package)

    def read_combat_snapshot(self, session_id, package, *, context):
        """Trusted audit/read boundary; this does not authorize cross-process continuation."""
        with self.store.transaction():
            row = self.sessions._session(session_id, package, context)
            if not package.legacy_audit_only:
                self.rules._verify_dnd_binding(package, row)
            return self.rules._combat_snapshot(row, for_walk=True)

    def read_rng_context(self, session_id, package, *, context):
        """Authorize access and return the RNG context for the fixed ruleset binding."""
        with self.store.transaction():
            row = self.sessions._session(session_id, package, context)
            if not package.legacy_audit_only:
                self.rules._verify_dnd_binding(package, row)
            return self.runtime.rng_context(row)

    async def submit_command_async(self, command, package, *, context):
        """Async entry for the same TypedCommand and persistent receipt protocol."""
        validate_command(command)
        if package.legacy_audit_only:
            raise StateMachineError(
                "legacy_rules_audit_only", "Old package permits audit reads only"
            )
        from .choice_models import (
            ChoiceFinalizeSpellOperationPayload,
            ChoiceRespondOperationPayload,
        )
        from .spell_cast_activity import FinishSpellCast, StartSpellCast
        from .world_commands import OPENABLE_CHECK_OPERATIONS, CheckMechanism

        if type(command.payload) not in (
            CombatIntentPayload,
            CombatWithdraw,
            ItemUsePayload,
            ExplorationItemUsePayload,
            ExplorationEnvironmentalSpellCastPayload,
            ExplorationSpellCastPayload,
            ExplorationFeaturePayload,
            ExplorationMultiSpellCastPayload,
            ChoiceRespondOperationPayload,
            ChoiceFinalizeSpellOperationPayload,
            LongRest,
            ShortRest,
            StartSpellCast,
            FinishSpellCast,
            CombatClosePayload,
            CheckPayload,
            SavePayload,
            *OPENABLE_CHECK_OPERATIONS,
            CheckMechanism,
        ):
            raise StateMachineError(
                "unsupported_operation", "Expected an admitted real Engine command"
            )
        with self.store.transaction():
            row = self.sessions._session(command.session_id, package, context)
            self.runtime.guard_time_owner(command.session_id)
            fingerprint = self.runtime.fingerprint(command, row, context)
        return await self._submit_dnd(command, package, context, fingerprint)

    async def _submit_dnd(self, command, package, context, fingerprint):
        """Delegate command execution to the rule execution coordinator."""
        from .rule_execution import RuleExecution

        return await RuleExecution(self).submit(command, package, context, fingerprint)

    def query_action_availability(self, session_id, actor_id, payload, package, *, context):
        """Authorize and build the required snapshot for a read-only rules availability
        query.
        """
        if type(payload) is not CombatIntentPayload or type(actor_id) is not str or not actor_id:
            raise StateMachineError("invalid_query", "Expected a typed intent and Actor identity")
        with self.store.transaction():
            row = self.sessions._session(session_id, package, context)
            self.sessions._authorize_command(row, actor_id, payload, context)
            attack_actors = (
                (actor_id, payload.target_id) if payload.intent_type == "attack" else None
            )
            snapshot = self.rules._combat_snapshot(row, attack_actors=attack_actors)
            owner = self._conn.execute(
                "SELECT process_id FROM combats WHERE session_id=?", (session_id,)
            ).fetchone()[0]
        return self.rules.query_availability(
            session_id,
            actor_id,
            payload,
            package,
            snapshot,
            owner,
            row=row,
            can_preview_attack_mechanics=context.can_preview_attack_mechanics,
        )
