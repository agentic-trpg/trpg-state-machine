"""One SQLite authority, short transactions, detached reads, no runtime cache."""

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
from .commands import OPERATIONS, CommandRuntime, TypedCommand
from .domain_facts import CharacterFacts, CombatRecords, WorldFacts
from .errors import StateMachineError
from .event_capture import EventCapture
from .event_persistence import EventPersistence
from .events import EventRuntime, _StagedEvent
from .mechanical_runtime import MechanicalRuntime
from .mechanics import SyntheticEvaluator
from .models import (
    ActorState,
    CharacterState,
    CreateSessionRequest,
    CreationResult,
    PlayerSelection,
    RNGState,
    SceneInstance,
    SessionSnapshot,
    TrustedContext,
    WorldState,
)
from .npc_bootstrap import NPCBootstrap
from .npc_evaluation import NPCEvaluationRuntime
from .npc_modes import NPCModeRuntime
from .npc_outputs import NPCOutputRuntime
from .npc_results import NPCResultRuntime
from .npc_state import NPCRuntime
from .object_durability import ObjectDurabilityService
from .observation_lifecycle import ObservationLifecycleRuntime
from .package import (
    AdventurePackage,
)
from .package_effects import PackageEffects
from .perception import PerceptionRuntime
from .perception_queue import PerceptionQueueRuntime
from .rules_adapter import RulesAdapter
from .session_access import SessionAccess, _binding
from .session_creation import SessionCreation
from .session_runtime import SessionRuntime
from .spatial import WorldHandler
from .storage import SqliteStore


class StateMachine(
    CommandRuntime,
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
    ) -> None:
        from dnd5e_srd_data import BundledAssetLoader

        loader = dnd_loader if dnd_loader is not None else BundledAssetLoader()
        self._evaluator = evaluator if evaluator is not None else SyntheticEvaluator()
        self._store = SqliteStore(database)
        self._sessions = SessionAccess(self._store)
        self._world_facts = WorldFacts(self._store)
        self._characters = CharacterFacts(self._store, self._world_facts)
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
        )

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
        return self._session_runtime.time.sync(session_id, package, context=context)

    def attach_time_driver(self, session_id, package, *, context, **options):
        from .time_driver import TimeDriver

        return TimeDriver(
            self._session_runtime, session_id, package, context, self.submit_command, **options
        )

    def read_navigation_activity(self, session_id, actor_id, activity_id, package, *, context):
        return self._activities.read(session_id, actor_id, activity_id, package, context=context)

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self) -> None:
        if not self._store._closed:
            for driver in tuple(self._session_runtime.time.drivers.values()):
                driver.close()
            self._store.close()

    def _ensure_open(self) -> None:
        self._store.ensure_open()

    def _transaction(self, *, write: bool = False):
        return self._store.transaction(write=write)

    def _session(self, session_id: str, package: AdventurePackage, context: TrustedContext):
        return self._sessions._session(session_id, package, context)

    def _check_package_pin(self, row, package):
        return self._sessions._check_package_pin(row, package)

    def create_session(
        self, request: CreateSessionRequest, package: AdventurePackage, *, context: TrustedContext
    ) -> CreationResult:
        """Compatible entry for the atomic Session Creation coordinator."""
        return self._creation.create(request, package, context=context)

    def _initialize_scene(self, session_id: str, scene_id: str, package: AdventurePackage) -> None:
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
        return self._world._operate(
            row,
            package,
            operation,
            actor_id=actor_id,
            effect_origin=effect_origin,
            source_command_id=source_command_id,
        )

    def _snapshot(self, row, package) -> SessionSnapshot:
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
                if package.scene(s["scene_id"]).grid_json is not None
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
                json.loads(pc["resources_json"]),
                tuple(json.loads(pc["inventory_json"])),
                tuple(json.loads(pc["effects_json"])),
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
        return self._combat_records.active(session_id)

    def read_session(
        self, session_id: str, package: AdventurePackage, *, context: TrustedContext
    ) -> SessionSnapshot:
        """Read a consistent detached runtime snapshot, verifying owner and all pins."""
        with self._transaction():
            return self._snapshot(self._session(session_id, package, context), package)

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

    def resume_session(
        self, session_id: str, package: AdventurePackage, *, context: TrustedContext
    ) -> SessionSnapshot:
        """Resume the persisted noncombat foundation, without replaying templates."""
        return self.save_session(session_id, package, context=context)

    def query_world_action(self, session_id, actor_id, operation, package, *, context):
        return self._world.query_world_action(
            session_id, actor_id, operation, package, context=context
        )

    def get_spatial_view(self, session_id, actor_id, package, *, context):
        return self._world.get_spatial_view(session_id, actor_id, package, context=context)

    def read_actor_position(self, session_id, actor_id, package, *, context):
        return self._world.read_actor_position(session_id, actor_id, package, context=context)

    def get_illumination(self, session_id, actor_id, cell, package, *, context):
        return self._objects.lights.query_illumination(
            session_id, actor_id, cell, package, context=context
        )

    def get_object(self, session_id, actor_id, object_id, package, *, context):
        return self._objects.get_object(session_id, actor_id, object_id, package, context=context)

    def list_scene_objects(self, session_id, actor_id, package, *, context):
        return self._objects.list_objects(session_id, actor_id, package, context=context)

    def read_carried_items(self, session_id, actor_id, package, *, context):
        return self._world.read_carried_items(session_id, actor_id, package, context=context)

    def _assert_spatial_consistency(self, sid, package):
        return self._world._assert_spatial_consistency(sid, package)

    async def submit_command_async(self, command, package, *, context):
        return await self._combat.submit_command_async(command, package, context=context)

    async def _submit_dnd(self, command, package, context, fingerprint):
        return await self._combat._submit_dnd(command, package, context, fingerprint)

    def _initialize_noncombat(self, row, package):
        return self._combat._initialize_noncombat(row, package)

    def read_noncombat_snapshot(self, session_id, package, *, context):
        return self._combat.read_noncombat_snapshot(session_id, package, context=context)

    def _verify_dnd_binding(self, package):
        return self._rules._verify_dnd_binding(package)

    def _prepare_combat(self, row, package):
        return self._rules._prepare_combat(row, package)

    def _initialize_combat(self, row, package, prepared):
        return self._combat._initialize_combat(row, package, prepared)

    def read_combat_snapshot(self, session_id, package, *, context):
        return self._combat.read_combat_snapshot(session_id, package, context=context)

    def read_rng_context(self, session_id, package, *, context):
        return self._combat.read_rng_context(session_id, package, context=context)

    def _require_noncombat_save(self, row):
        return self._combat_records.require_noncombat_save(row)
