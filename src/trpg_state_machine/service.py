"""Domain-owned facts, consistent Session RAM reads and durable publication."""

import json
from pathlib import Path
from uuid import uuid4

from ._operations import (
    FoundationOperation,
    MoveActor,
)
from .activity_handler import ActivityHandler
from .activity_records import ActivityRecords
from .availability import AvailabilityRuntime
from .combat_handler import CombatHandler
from .commands import OPERATIONS, CommandReceipt, CommandRuntime, TypedCommand
from .domain_facts import CharacterFacts, CombatRecords, WorldFacts
from .errors import StateMachineError
from .event_capture import EventCapture
from .event_persistence import EventPersistence
from .events import EventRuntime, _StagedEvent
from .mechanical_runtime import MechanicalRuntime
from .mechanics import SyntheticEvaluator
from .models import (
    ActorState,
    CharacterAC,
    CharacterState,
    CreateSessionRequest,
    CreationResult,
    PlayerSelection,
    RNGState,
    SceneInstance,
    SessionSnapshot,
    SpellConditionOptions,
    TrustedContext,
    WorldState,
)
from .npc_bootstrap import NPCBootstrap
from .npc_combat_policy import NPCCombatPolicyRuntime
from .npc_evaluation import NPCEvaluationRuntime
from .npc_modes import NPCModeRuntime
from .npc_outputs import NPCOutputRuntime
from .npc_results import NPCResultRuntime
from .npc_state import NPCRuntime
from .object_durability import ObjectDurabilityService
from .observation_lifecycle import ObservationLifecycleRuntime
from .package import (
    AdventurePackage,
    canonical_json,
)
from .package_effects import PackageEffects
from .perception import PerceptionRuntime
from .perception_queue import PerceptionQueueRuntime
from .rules_adapter import RulesAdapter
from .scoped_reads import scoped_read
from .session_access import SessionAccess, _binding
from .session_creation import SessionCreation
from .session_runtime import SessionRuntime
from .spatial import WorldHandler
from .storage import SqliteStore


class StateMachine(
    CommandRuntime,
    NPCCombatPolicyRuntime,
    EventRuntime,
    MechanicalRuntime,
    ObservationLifecycleRuntime,
    PerceptionQueueRuntime,
    PerceptionRuntime,
    AvailabilityRuntime,
    NPCRuntime,
    NPCEvaluationRuntime,
    NPCResultRuntime,
    NPCModeRuntime,
    NPCOutputRuntime,
):
    """Trusted in-process runtime. One connection per instance/thread; close explicitly.

    All writes commit immediately. Public lifecycle methods are not HTTP endpoints
    or player/NPC views. Only an authenticated adapter may supply TrustedContext.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        evaluator=None,
        dnd_loader=None,
        environment_effects_provider=None,
        object_protection_provider=None,
        environment_unit_height_provider=None,
        combat_end_conditions=None,
    ) -> None:
        """Assemble Store, bound rules and services with trusted encounter policies.

        Registered end conditions are synchronous, deterministic, read-only
        predicates of the detached combat snapshot passed at the close checkpoint.
        They must not call back into this StateMachine.
        """
        from dnd5e_engine.evaluation_ruleset import freeze_ruleset, get_default_ruleset

        # Select the final effective corpus once, before opening the store. All
        # consumers below retain this same RE-owned snapshot. To edit sources,
        # construct a new runtime (or inject an explicitly rebuilt FrozenRuleset).
        loader = get_default_ruleset() if dnd_loader is None else freeze_ruleset(dnd_loader)
        self._evaluator = evaluator if evaluator is not None else SyntheticEvaluator()
        self._store = SqliteStore(database)
        # Action-level choices are provisional and never part of SQLite backup.
        self._pending_action_choices = {}
        self._sessions = SessionAccess(self._store)
        self._world_facts = WorldFacts(self._store)
        self._characters = CharacterFacts(self._store, self._world_facts, loader)
        self._combat_records = CombatRecords(self._store)
        self._npc_bootstrap = NPCBootstrap(self._store)
        self._activity_records = ActivityRecords(self._store)
        self._event_capture = EventCapture(self._store, self._characters, self._world_facts)
        self._event_persistence = EventPersistence(self._store)
        self._session_runtime = SessionRuntime(self._store, self._sessions, self._event_persistence)
        self._world = WorldHandler(
            self._store,
            self._sessions,
            self._characters,
            self._world_facts,
            self._combat_records,
            self._event_capture,
            self._npc_bootstrap,
            self._activity_records,
            loader=loader,
            runtime=self._session_runtime,
        )
        self._durability = ObjectDurabilityService(self._world)
        self._objects = self._world.objects
        self.world_time = self._world.time
        self.scene_lifecycle = self._world.scenes

        self.environment = self._world.environment
        self.environment.command_submitter = self.submit_command
        self.environment.effects_provider = environment_effects_provider
        self.environment.unit_height_provider = environment_unit_height_provider
        self._objects.spatial.protection_provider = object_protection_provider
        self._activities = ActivityHandler(
            self._store,
            self._activity_records,
            self._characters,
            self._world,
            self._event_capture,
        )
        self._package_effects = PackageEffects(self._store, self._world, self._event_capture)
        self._rules = RulesAdapter(
            self._store,
            loader,
            self._characters,
            self._world_facts,
            self._combat_records,
            self._world,
        )
        self._objects.openable.rules = self._rules
        self._combat = CombatHandler(
            self._store,
            self._sessions,
            self._characters,
            self._world_facts,
            self._combat_records,
            self._world,
            self._rules,
            self._event_capture,
            self._package_effects,
            self._event_persistence,
            self._activity_records,
            end_conditions=combat_end_conditions,
        )
        self._combat.action_choice_runtime = self
        self._rules.records_end_conditions = self._combat.end_conditions

        self._creation = SessionCreation(
            self._store,
            self._session_runtime,
            self._world,
            self._characters,
            self._rules,
            self._combat,
            self._event_capture,
            self._package_effects,
            self._event_persistence,
            self._evaluator,
        )

    @property
    def _conn(self):
        """Transitional connection for NPC/perception runtimes, owned by the store."""
        return self._store.connection

    def read_time_sync(self, session_id, package, *, context):
        """Delegate reading time synchronization status to the unified time service."""
        return self._session_runtime.time.sync(session_id, package, context=context)

    def read_multi_spell_prompt(
        self, session_id, pending_operation_id, responder_id, package, *, context
    ):
        """Read the outstanding consent prompt under the responder's current grant."""
        from .pending_multi_spell_choices import PendingMultiSpellChoices

        with self._transaction():
            row = self._session(session_id, package, context)
            choices = PendingMultiSpellChoices(
                self._store, self._session_runtime, self._sessions, self._world_facts
            )
            return choices.read_prompt(row, pending_operation_id, responder_id, context)

    def attach_time_driver(self, session_id, package, *, context, **options):
        """Create a unified time driver for the selected Session."""
        from .time_driver import TimeDriver

        return TimeDriver(
            self._session_runtime, session_id, package, context, self.submit_command, **options
        )

    def read_navigation_activity(self, session_id, actor_id, activity_id, package, *, context):
        """Delegate navigation activity reading to its handler."""
        return self._activities.read(session_id, actor_id, activity_id, package, context=context)

    def read_spell_cast_activity(self, session_id, actor_id, activity_id, package, *, context):
        """Read detached casting progress under the selected caster's current Session grant."""
        with self._transaction():
            row = self._session(session_id, package, context)
            if actor_id != row["pc_actor_id"]:
                raise StateMachineError("unauthorized", "Only the selected PC can read casting")
            domain = self._world.spell_casts
            activity = domain.get(session_id, activity_id)
            if activity is None or activity["actor_id"] != actor_id:
                raise StateMachineError("invalid_activity", "Expected this caster's activity")
            return domain.snapshot(activity)

    def __enter__(self):
        """Check that storage is open and enter the context."""
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback):
        """Close the state machine on context exit."""
        self.close()

    def close(self) -> None:
        """Close drivers and Store and release rebuildable environment snapshots."""
        for session_id in tuple(self._pending_action_choices):
            self._discard_pending_action_choice(session_id)
        if not self._store._closed:
            for driver in tuple(self._session_runtime.time.drivers.values()):
                driver.close()
            self._store.close()
            self.environment.clear_snapshots()

    def _ensure_open(self) -> None:
        """Delegate open/thread eligibility checks to the Store."""
        self._store.ensure_open()

    def _transaction(self, *, write: bool = False):
        """Delegate shared transaction scope management to the Store."""
        return self._store.transaction(write=write)

    def _session(self, session_id: str, package: AdventurePackage, context: TrustedContext):
        """Delegate authorized Session record reading to SessionAccess."""
        return self._sessions._session(session_id, package, context)

    def _check_package_pin(self, row, package):
        """Delegate fixed Package binding checks."""
        return self._sessions._check_package_pin(row, package)

    def create_session(
        self, request: CreateSessionRequest, package: AdventurePackage, *, context: TrustedContext
    ) -> CreationResult:
        """Compatible entry for the atomic Session Creation coordinator."""
        return self._creation.create(request, package, context=context)

    def _initialize_scene(self, session_id: str, scene_id: str, package: AdventurePackage) -> None:
        """Delegate creation/loading of required scene instances to scene lifecycle."""
        return self.scene_lifecycle.ensure_scene(session_id, scene_id, package)

    def _append_events(
        self,
        session_id: str,
        version: int,
        events: list[_StagedEvent],
        *,
        source_command_id: str,
        package: AdventurePackage,
    ) -> None:
        """Delegate persistence of captured events to the event interface."""
        return self._event_persistence._append_events(
            session_id, version, events, source_command_id=source_command_id, package=package
        )

    def _apply(
        self,
        session_id: str,
        package: AdventurePackage,
        operation: FoundationOperation,
        *,
        expected_world_version: int,
        context: TrustedContext,
    ) -> SessionSnapshot:
        """Compatibility helper routed through the same authorized command pipeline."""
        if type(operation) not in OPERATIONS.values():
            raise StateMachineError("unsupported_operation", "Unknown operation")
        snapshot = self.read_session(session_id, package, context=context)
        actor_id = operation.actor_id if isinstance(operation, MoveActor) else snapshot.pc_actor_id
        kind = next(key for key, value in OPERATIONS.items() if type(operation) is value)
        receipt = self.submit_command(
            TypedCommand(
                session_id, str(uuid4()), actor_id, kind, operation, expected_world_version
            ),
            package,
            context=context,
        )
        if receipt.status != "committed":
            raise StateMachineError(receipt.error_code, "Command did not commit")
        return self.read_session(session_id, package, context=context)

    def _operate(
        self,
        row,
        package: AdventurePackage,
        operation: FoundationOperation,
        *,
        actor_id=None,
        effect_origin=None,
        source_command_id=None,
    ):
        """Delegate controlled world operation execution to its handler."""
        return self._world._operate(
            row,
            package,
            operation,
            actor_id=actor_id,
            effect_origin=effect_origin,
            source_command_id=source_command_id,
        )

    @scoped_read
    def _snapshot(self, row, package) -> SessionSnapshot:
        """Build a complete detached Session snapshot from consistent facts."""
        sid = row["session_id"]
        world = self._conn.execute(
            "SELECT flags_json,game_tick FROM world_states WHERE session_id = ?", (sid,)
        ).fetchone()
        scenes = {}
        for s in self._conn.execute(
            "SELECT * FROM scene_instances WHERE session_id = ? ORDER BY scene_id", (sid,)
        ):
            objects = {
                o["object_id"]: o["state"]
                for o in self._conn.execute(
                    "SELECT object_id, state FROM objects "
                    "WHERE session_id = ? AND scene_id = ? ORDER BY object_id",
                    (sid, s["scene_id"]),
                )
            }
            scenes[s["scene_id"]] = SceneInstance(
                s["scene_id"],
                s["visited_count"],
                json.loads(s["flags_json"]),
                objects,
                {
                    o[0]: o[1]
                    for o in self._conn.execute(
                        "SELECT object_id,cell FROM objects WHERE session_id=? AND scene_id=?",
                        (sid, s["scene_id"]),
                    )
                }
                if self._world_facts.instances.get(sid, "scene", s["scene_id"])["has_grid"]
                else None,
            )
        actors = {
            a["actor_id"]: ActorState(a["actor_id"], a["kind"], a["current_scene_id"], a["cell"])
            for a in self._conn.execute(
                "SELECT * FROM actors WHERE session_id = ? ORDER BY actor_id", (sid,)
            )
        }
        pc = self._conn.execute(
            "SELECT * FROM characters WHERE session_id = ? AND actor_id = ?",
            (sid, row["pc_actor_id"]),
        ).fetchone()
        rng = self._conn.execute("SELECT * FROM rng_states WHERE session_id = ?", (sid,)).fetchone()
        if (
            world is None
            or pc is None
            or rng is None
            or row["pc_actor_id"] not in actors
            or row["current_scene_id"] not in scenes
            or actors[row["pc_actor_id"]].current_scene_id != row["current_scene_id"]
            or rng["state_id"] != row["rng_ref"]
        ):
            raise StateMachineError(
                "incomplete_session", "Required persisted Session facts are missing"
            )
        return SessionSnapshot(
            sid,
            row["adventure_id"],
            row["package_version"],
            row["package_digest"],
            _binding(row),
            PlayerSelection(row["player_id"], row["pc_template_id"]),
            row["pc_actor_id"],
            row["current_scene_id"],
            row["world_version"],
            row["event_seq"],
            WorldState(json.loads(world[0]), world[1], self._world_facts.time_state(sid)[0]),
            scenes,
            actors,
            CharacterState(
                pc["actor_id"],
                pc["current_hp"],
                pc["max_hp"],
                self._characters.resource_balances(sid, pc["actor_id"]),
                tuple(self._characters.carried_slugs(sid, pc["actor_id"])),
                tuple(self._characters.effects.for_targets(sid, {pc["actor_id"]})),
                pc["xp"],
            ),
            RNGState(
                rng["state_id"],
                rng["algorithm"],
                rng["encoding"],
                rng["position"],
                rng["rng_version"],
                rng["state_json"],
            ),
            "combat" if self._active_combat(sid) else "exploration",
        )

    def _active_combat(self, session_id):
        """Read the currently active combat record."""
        return self._combat_records.active(session_id)

    def read_session(
        self, session_id: str, package: AdventurePackage, *, context: TrustedContext
    ) -> SessionSnapshot:
        """Read a consistent detached snapshot, verifying owner and all pins.

        The character projection includes the PC's current effect records.
        """
        with self._transaction():
            return self._snapshot(self._session(session_id, package, context), package)

    def unload_session(self, session_id, package, *, context):
        """Release loaded facts without re-instantiation or changing game state."""
        with self._transaction():
            self._session(session_id, package, context)
        self._discard_pending_action_choice(session_id)
        driver = self._session_runtime.time.drivers.get(session_id)
        if driver is not None:
            driver.close()
        self._store.release_memory(session_id)
        self.environment.release_session(session_id)

    def _has_pending_action_choice(self, session_id: str) -> bool:
        """Report whether this live runtime holds an uncommitted action choice."""
        return session_id in self._pending_action_choices

    def _discard_pending_action_choice(self, session_id: str, *, expected=None) -> None:
        """Forget only the selected candidate when supplied, preserving a newer action."""
        with self._store._coordinator.lock:
            if (
                expected is not None
                and self._pending_action_choices.get(session_id) is not expected
            ):
                return
            self._pending_action_choices.pop(session_id, None)
            owners = self._store._coordinator.pending_action_owners
            if owners.get(session_id) is self:
                del owners[session_id]

    def _guard_pending_action_owner(self, session_id: str, *, expected=None) -> None:
        """Block competing writes and require the exact live candidate on reply."""
        owner = self._store._coordinator.pending_action_owners.get(session_id)
        if expected is not None:
            if owner is not self or self._pending_action_choices.get(session_id) is not expected:
                raise StateMachineError("choice_stale", "Live action candidate changed")
        elif owner is not None:
            raise StateMachineError("action_choice_pending", "Resolve the current action")

    def _current_spell_save_grant(self, pending) -> bool:
        """Match the exact fixed Inspired row and its live deadline for either reply."""
        from .ongoing_effects import BARDIC_INSPIRATION_EFFECT_ID

        sid = pending.command.session_id
        target = pending.progress.pending_target_id
        expected = tuple(
            effect
            for effect in pending.request.state_snapshot.effect_states
            if effect.target_id == target and effect.id == BARDIC_INSPIRATION_EFFECT_ID
        )
        if len(expected) != 1:
            return False
        tick = self._world_facts.time_state(sid)[1]
        return (
            tick < pending.grant.expires_tick
            and self._world.characters.effects.held_bardic_inspiration(sid, target, tick)
            == pending.grant
            and self._world.characters.effects.for_target_effect(
                sid, target, BARDIC_INSPIRATION_EFFECT_ID
            )
            == [expected[0].model_dump(mode="json")]
        )

    def _register_pending_action_choice(self, pending, package, context, rng, *, previous=None):
        """Publish one provisional action under the shared file gate after fresh admission."""
        from ._operations import SetObjectState, StepActor
        from .action_pending import (
            PendingActionChoice,
            PendingSpellSaveChoice,
        )
        from .world_commands import SetLightEnabled

        command = pending.command
        sid = command.session_id
        if isinstance(pending, PendingActionChoice) and type(command.payload) in (
            StepActor,
            SetLightEnabled,
            SetObjectState,
        ):
            with self._store._coordinator.lock:
                if previous is None:
                    existing = self._pending_action_choices.get(sid)
                    if (
                        existing is not None
                        and existing.command.command_id == command.command_id
                        and existing.fingerprint == pending.fingerprint
                    ):
                        return existing.receipt
                saves = dict(pending.prepared)
                request, _, _ = saves.pop(pending.waiting_key)
                candidate = self._prepare_step_candidate(
                    command, package, context, pending.fingerprint, saves, previous
                )
                if candidate is None:
                    with self._transaction():
                        if replay := self._command_replay(command, pending.fingerprint):
                            return replay
                if (
                    candidate is None
                    or candidate.key != pending.waiting_key
                    or candidate.request != request
                    or candidate.grant != pending.grant
                ):
                    raise StateMachineError(
                        "version_conflict", "Ordered save changed before choice"
                    )
                return self._publish_pending_action_choice(pending, previous=previous)
        with self._store._coordinator.lock, self._transaction():
            row = self._session(sid, package, context)
            self._authorize_command(row, command.actor_id, command.payload, context)
            if replay := self._command_replay(command, pending.fingerprint):
                return replay
            if self._session_runtime.version_conflict(row, command):
                raise StateMachineError("version_conflict", "Action changed before choice")
            if self._rules._rng_context(row) != rng:
                raise StateMachineError("version_conflict", "Action RNG changed before choice")
            if isinstance(pending, PendingActionChoice):
                # Pick registration still fences the original check's RNG.
                tick = self._world_facts.time_state(sid)[1]
                grant = self._world.characters.effects.held_bardic_inspiration(
                    sid, command.actor_id, tick
                )
                if grant != pending.grant:
                    raise StateMachineError(
                        "version_conflict", "Action grant changed before choice"
                    )
            elif isinstance(pending, PendingSpellSaveChoice) and (
                pending.progress.pending_target_id != row["pc_actor_id"]
                or self._rules.session_binding(row) != pending.request.ruleset_binding
                or not self._current_spell_save_grant(pending)
            ):
                raise StateMachineError(
                    "version_conflict", "Spell save recipient, binding or grant changed"
                )
            return self._publish_pending_action_choice(pending)

    def _publish_pending_action_choice(self, pending, *, previous=None):
        """Install the validated prompt under the gate, replacing only its live predecessor."""
        sid = pending.command.session_id
        if previous is not None:
            self._guard_pending_action_owner(sid, expected=previous)
        else:
            owner = self._store._coordinator.pending_action_owners.get(sid)
            if owner is not None:
                existing = owner._pending_action_choices.get(sid)
                if (
                    owner is self
                    and existing is not None
                    and existing.command.command_id == pending.command.command_id
                    and existing.fingerprint == pending.fingerprint
                ):
                    return existing.receipt
                raise StateMachineError("action_choice_pending", "Resolve the current action")
        self._pending_action_choices[sid] = pending
        self._store._coordinator.pending_action_owners[sid] = self
        return pending.step_replies[-1].receipt if previous is not None else pending.receipt

    def _open_spell_save_choice(self, command, fingerprint, request, progress, package, context):
        """Offer the selected PC one private, uncommitted spell save choice."""
        from .action_pending import PendingSpellSaveChoice
        from .choice_models import PendingChoice

        sid, target = command.session_id, progress.pending_target_id
        with self._transaction():
            row = self._session(sid, package, context)
            if target != row["pc_actor_id"]:
                raise StateMachineError(
                    "unsupported_action_choice", "Only the selected PC may answer this save"
                )
            tick = self._world_facts.time_state(sid)[1]
            grant = self._world.characters.effects.held_bardic_inspiration(sid, target, tick)
            if grant is None:
                raise StateMachineError("choice_stale", "Spell save grant is unavailable")
        prompt = PendingChoice(
            command.command_id,
            target,
            "rules.bardic_inspiration",
            ("accept", "decline"),
            grant.expires_tick,
        )
        receipt = CommandReceipt(
            sid,
            command.command_id,
            command.actor_id,
            "pending_choice",
            command.expected_world_version,
            (),
            pending_choice=prompt,
        )
        pending = PendingSpellSaveChoice(command, fingerprint, request, progress, grant, receipt)
        return self._register_pending_action_choice(pending, package, context, request.rng_context)

    def save_session(
        self, session_id: str, package: AdventurePackage, *, context: TrustedContext
    ) -> SessionSnapshot:
        """All changes are already durable; verify/read the current committed save.

        No mutable snapshot is accepted and no checkpoint/event/Meta commit is made.
        """
        with self._transaction():
            row = self._session(session_id, package, context)
            self._require_noncombat_save(row)
            self.scene_lifecycle.validate_initialized(row, package)
            self._world._assert_spatial_consistency(session_id, package)
            if row["ruleset_id"] == "dnd-2024-srd-5.2.1" and not package.legacy_audit_only:
                self._rules._verify_dnd_binding(package, row)
                if row["mechanical_schema_version"] is not None:
                    self._rules.validate_saved_mechanics(row)
                    self._session_runtime.rng_context(row)
            snapshot = self._snapshot(row, package)
            self._session_runtime.prepare_scene_maps(
                session_id, (row["current_scene_id"],), self._world_facts.maps
            )
            return snapshot

    def create_checkpoint(
        self,
        session_id: str,
        package: AdventurePackage,
        destination: str | Path,
        *,
        context: TrustedContext,
    ) -> SessionSnapshot:
        """Back up one authorized, settled Session to a new SQLite checkpoint file."""
        from .session_checkpoints import create_checkpoint

        return create_checkpoint(self, session_id, package, destination, context=context)

    def load_checkpoint(
        self,
        checkpoint: str | Path,
        destination: str | Path,
        session_id: str,
        package: AdventurePackage,
        *,
        context: TrustedContext,
    ) -> "StateMachine":
        """Load a checkpoint into a fresh runtime with a new shared rules RNG stream."""
        from .session_checkpoints import load_checkpoint

        return load_checkpoint(self, checkpoint, destination, session_id, package, context=context)

    def resume_session(
        self, session_id: str, package: AdventurePackage, *, context: TrustedContext
    ) -> SessionSnapshot:
        """Resume the persisted noncombat foundation, without replaying templates."""
        if self._store.memory.paused or session_id in self._store.memory.paused_sessions:
            driver = self._session_runtime.time.drivers.get(session_id)
            if driver is not None:
                driver.close()
            self.environment.release_session(session_id)
            with self._store.reconciliation(session_id):
                return self.save_session(session_id, package, context=context)
        return self.save_session(session_id, package, context=context)

    def query_world_action(self, session_id, actor_id, operation, package, *, context):
        """Delegate world action availability querying."""
        return self._world.query_world_action(
            session_id, actor_id, operation, package, context=context
        )

    def get_spatial_view(self, session_id, actor_id, package, *, context):
        """Delegate authorized spatial view querying."""
        return self._world.get_spatial_view(session_id, actor_id, package, context=context)

    def read_actor_position(self, session_id, actor_id, package, *, context):
        """Delegate reading the actor's own position."""
        return self._world.read_actor_position(session_id, actor_id, package, context=context)

    def get_illumination_area(
        self, session_id, actor_id, cells, package, *, context, include_geometry=False
    ):
        """Read 1–256 authorized cells in order, optionally including lit polygons."""
        return self._objects.lights.query_area(
            session_id, actor_id, cells, package, context=context, include_geometry=include_geometry
        )

    def get_object(self, session_id, actor_id, object_id, package, *, context):
        """Delegate reading a visible object to the object service."""
        return self._objects.get_object(session_id, actor_id, object_id, package, context=context)

    def list_scene_objects(self, session_id, actor_id, package, *, context):
        """Delegate listing visible objects in the current scene."""
        return self._objects.list_objects(session_id, actor_id, package, context=context)

    def read_carried_items(self, session_id, actor_id, package, *, context):
        """Delegate reading authorized carried items."""
        return self._world.read_carried_items(session_id, actor_id, package, context=context)

    def read_character_ac(self, session_id, actor_id, package, *, context) -> CharacterAC:
        """Read a controlled actor's current numeric AC without changing RE's base input.

        Cover is attack-specific and excluded. RE folds active scalar AC bonuses and
        minimums. Unsupported formulas fail this narrow read alone.
        """
        from dnd5e_engine.evaluation_effects import EffectState
        from dnd5e_engine.evaluation_wearables import protective_cloak_ac_bonus
        from dnd5e_engine.reaction_effect_rules import effective_armor_class

        with self._transaction():
            self._world._spatial_reader(session_id, actor_id, package, context)
            base = self._characters.attributes(session_id, actor_id, include_proficiencies=False)[
                "ac"
            ]
            effects = [
                EffectState.model_validate_json(canonical_json(value))
                for value in self._characters.effects.visible_for_target(session_id, actor_id)
            ]
            worn_bonus = 0
            for _item, entry in self._characters.worn_items(session_id, actor_id):
                try:
                    worn_bonus += protective_cloak_ac_bonus(
                        self._world.loader.get_item(entry["item_slug"]),
                        worn=True,
                        attuned_to_wearer=entry.get("attuned_actor_id") == actor_id,
                    )
                except ValueError as exc:
                    raise StateMachineError(
                        "unsupported_armor_class", "Worn item AC rule is unreviewed"
                    ) from exc
            try:
                effective = effective_armor_class(base + worn_bonus, effects)
            except (TypeError, ValueError) as exc:
                raise StateMachineError(
                    "unsupported_armor_class", "Active AC effect has no scalar value"
                ) from exc
            return CharacterAC(base, effective - base, effective)

    def _assert_spatial_consistency(self, sid, package):
        """Delegate an explicit complete spatial consistency audit."""
        return self._world._assert_spatial_consistency(sid, package)

    async def submit_command_async(self, command, package, *, context):
        """Delegate supported asynchronous rule command submission."""
        if self._has_pending_action_choice(command.session_id):
            from .action_pending import PendingPickSaveChoice, PendingSpellSaveChoice
            from .choice_models import ChoiceRespondOperationPayload

            pending = self._pending_action_choices[command.session_id]
            if (
                isinstance(pending, (PendingPickSaveChoice, PendingSpellSaveChoice))
                and type(command.payload) is ChoiceRespondOperationPayload
                and command.payload.pending_operation_id == pending.command.command_id
            ):
                return await self._combat.submit_command_async(command, package, context=context)
            return self.submit_command(command, package, context=context)
        return await self._combat.submit_command_async(command, package, context=context)

    async def query_spell_condition_choices(
        self, command: TypedCommand, package: AdventurePackage, *, context: TrustedContext
    ) -> SpellConditionOptions:
        """Return this caster's condition options for the specified exploration cast."""
        from .rule_execution import RuleExecution

        return await RuleExecution(self._combat).query_spell_condition_choices(
            command, package, context
        )

    async def _submit_dnd(self, command, package, context, fingerprint):
        """Delegate rules submission coordination to the combat handler."""
        return await self._combat._submit_dnd(command, package, context, fingerprint)

    def _initialize_noncombat(self, row, package):
        """Delegate noncombat mechanical foundation initialization."""
        return self._combat._initialize_noncombat(row, package)

    def read_noncombat_snapshot(self, session_id, package, *, context):
        """Delegate authorized noncombat mechanical snapshot reading."""
        return self._combat.read_noncombat_snapshot(session_id, package, context=context)

    def _verify_dnd_binding(self, package):
        """Delegate rules binding checks to the adapter."""
        return self._rules._verify_dnd_binding(package)

    def _prepare_combat(self, row, package):
        """Delegate preparation of initial combat snapshot and RNG."""
        return self._rules._prepare_combat(row, package)

    def _initialize_combat(self, row, package, prepared):
        """Delegate combat-record initialization."""
        return self._combat._initialize_combat(row, package, prepared)

    def read_combat_snapshot(self, session_id, package, *, context):
        """Delegate trusted combat snapshot reading."""
        return self._combat.read_combat_snapshot(session_id, package, context=context)

    def read_combat_region(self, session_id, package, *, context):
        """Read the active dynamic encounter area for an authorized Session owner."""
        return self._combat.read_combat_region(session_id, package, context=context)

    def read_combat_withdrawal(self, session_id, package, *, context):
        """Read the active encounter's withdrawal policy for an authorized Session owner."""
        return self._combat.read_combat_withdrawal(session_id, package, context=context)

    def read_rng_context(self, session_id, package, *, context):
        """Delegate fixed RNG context reading."""
        return self._combat.read_rng_context(session_id, package, context=context)

    def _require_noncombat_save(self, row):
        """Delegate supported noncombat save-boundary checks."""
        return self._combat_records.require_noncombat_save(row)
