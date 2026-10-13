"""One SQLite authority, short transactions, detached reads, no runtime cache."""

import json
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from ._operations import (
    FoundationOperation,
    MoveActor,
)
from .activity_handler import ActivityHandler
from .activity_records import ActivityRecords
from .availability import AvailabilityRuntime
from .combat import DND_RULESET
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
from .observation_lifecycle import ObservationLifecycleRuntime
from .package import (
    SYNTHETIC_EVALUATION_RULESET,
    AdventurePackage,
    RulesetBinding,
    _creation_fingerprint,
    canonical_json,
)
from .package_effects import PackageEffects
from .perception import PerceptionRuntime
from .perception_queue import PerceptionQueueRuntime
from .rules_adapter import RulesAdapter
from .session_access import SessionAccess, _binding, _trusted
from .spatial import WorldHandler
from .storage import SqliteStore


def _result(raw: str) -> CreationResult:
    data = json.loads(raw)
    data["ruleset_binding"] = RulesetBinding(**data["ruleset_binding"])
    return CreationResult(**data)


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

    def __init__(self, database: str | Path, *, evaluator=None, dnd_loader=None) -> None:
        from dnd5e_srd_data import BundledAssetLoader

        loader = dnd_loader if dnd_loader is not None else BundledAssetLoader()
        self._evaluator = evaluator if evaluator is not None else SyntheticEvaluator()
        self._store = SqliteStore(database)
        self._sessions = SessionAccess(self._store)
        self._characters = CharacterFacts(self._store)
        self._world_facts = WorldFacts(self._store)
        self._combat_records = CombatRecords(self._store)
        self._npc_bootstrap = NPCBootstrap(self._store)
        self._activity_records = ActivityRecords(self._store)
        self._event_capture = EventCapture(self._store, self._characters, self._world_facts)
        self._event_persistence = EventPersistence(self._store)
        self._world = WorldHandler(
            self._store,
            self._sessions,
            self._characters,
            self._world_facts,
            self._combat_records,
            self._event_capture,
            self._npc_bootstrap,
            self._activity_records,
        )
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

    @property
    def _conn(self):
        """Transitional connection for NPC/perception runtimes, owned by the store."""
        return self._store.connection

    def read_navigation_activity(self, session_id, actor_id, activity_id, package, *, context):
        return self._activities.read(session_id, actor_id, activity_id, package, context=context)

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self) -> None:
        if not self._store._closed:
            self._store.close()

    def _ensure_open(self) -> None:
        self._store.ensure_open()

    def _transaction(self, *, write: bool = False):
        return self._store.transaction(write=write)

    def _session(self, session_id: str, package: AdventurePackage, context: TrustedContext):
        return self._sessions._session(session_id, package, context)

    def _check_package_pin(self, row, package):
        return self._sessions._check_package_pin(row, package)

    def _replay(self, context: TrustedContext, command_id: str, fingerprint: str):
        row = self._conn.execute(
            "SELECT fingerprint, result_json FROM creation_commands "
            "WHERE principal_id = ? AND command_id = ?",
            (context.principal_id, command_id),
        ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise StateMachineError(
                "idempotency_conflict", "Command key was already used for different content"
            )
        return _result(row["result_json"])

    def create_session(
        self, request: CreateSessionRequest, package: AdventurePackage, *, context: TrustedContext
    ) -> CreationResult:
        """Create once per trusted (principal_id, command_id), atomically.

        Prepare/validate/fingerprint outside the write transaction, then recheck the
        durable key inside BEGIN IMMEDIATE to handle concurrent/restarted callers.
        """
        _trusted(context)
        if not isinstance(request, CreateSessionRequest) or not isinstance(
            package, AdventurePackage
        ):
            raise StateMachineError(
                "invalid_request", "Expected typed creation request and approved package"
            )
        if any(
            not isinstance(v, str) or not v.strip() or len(v) > 256
            for v in (
                request.command_id,
                request.adventure_id,
                request.package_version,
                request.package_digest,
            )
        ):
            raise StateMachineError(
                "invalid_request", "Request IDs and versions must be nonblank strings"
            )
        if (
            not isinstance(request.players, tuple)
            or len(request.players) != 1
            or (not isinstance(request.players[0], PlayerSelection))
        ):
            raise StateMachineError(
                "invalid_player_setup", "Exactly one typed player selection is required"
            )
        player = request.players[0]
        if any(
            not isinstance(v, str) or not v.strip() or len(v) > 256
            for v in (player.player_id, player.pc_template_id)
        ):
            raise StateMachineError(
                "invalid_player_setup", "Player/template IDs must be nonblank strings"
            )
        if player.player_id != context.player_id:
            raise StateMachineError(
                "unauthorized", "Trusted context does not control the requested player"
            )
        fingerprint = _creation_fingerprint(
            {
                "adventure_id": request.adventure_id,
                "package_version": request.package_version,
                "package_digest": request.package_digest,
                "players": [asdict(player)],
            },
            package.ruleset_binding,
        )
        with self._transaction():
            replay = self._replay(context, request.command_id, fingerprint)
        if (request.adventure_id, request.package_version) != (
            package.adventure_id,
            package.package_version,
        ):
            raise StateMachineError(
                "package_identity_mismatch", "Request does not identify the approved package"
            )
        if request.package_digest != package.package_digest:
            raise StateMachineError(
                "package_digest_mismatch", "Request digest does not match approved content"
            )
        if replay is not None:
            return replay
        if package.ruleset_binding.ruleset_id == DND_RULESET:
            self._verify_dnd_binding(package)
        pc = next(
            (p for p in package.pc_templates if p.pc_template_id == player.pc_template_id), None
        )
        if pc is None:
            raise StateMachineError(
                "unknown_pc_template", "Selected PC template is not in the package"
            )
        session_id = "session." + uuid4().hex
        pc_actor_id = "pc." + uuid4().hex
        rng_ref = "rng." + uuid4().hex
        result = CreationResult(
            session_id,
            package.adventure_id,
            package.package_version,
            package.package_digest,
            package.ruleset_binding,
            0,
            2,
        )
        result_json = canonical_json(asdict(result))
        with self._transaction(write=True):
            if replay := self._replay(context, request.command_id, fingerprint):
                return replay
            self._sessions.initialize(session_id, package, context, pc, pc_actor_id, rng_ref)
            self._world_facts.initialize_world(session_id, package)
            self._initialize_scene(session_id, package.entry_scene_id, package)
            for scene in package.scenes:
                if scene.grid_json is not None and scene.scene_id != package.entry_scene_id:
                    self._initialize_scene(session_id, scene.scene_id, package)
                    self._world_facts.mark_unvisited(session_id, scene.scene_id)
            self._characters.initialize_pc(session_id, pc_actor_id, package.entry_scene_id, pc)
            if package.combat_foundation_json is not None:
                initial = json.loads(package.combat_foundation_json)["snapshot"]
                from .combat import _remap

                cells = _remap(
                    initial["combat_state"]["actor_zone"], pc.pc_template_id, pc_actor_id
                )
                fresh = self._conn.execute(
                    "SELECT * FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                self._characters.initialize_rule_positions(
                    fresh, cells, initial["scene_state"]["scene_id"]
                )
            self._sessions.initialize_rng(
                session_id,
                rng_ref,
                synthetic=package.ruleset_binding == SYNTHETIC_EVALUATION_RULESET,
            )
            initial_row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if package.noncombat_foundation_json is not None:
                self._initialize_noncombat(initial_row, package)
            self._assert_spatial_consistency(session_id, package)
            initial_events = self._expand_events(
                initial_row,
                package,
                [
                    self._capture_event(
                        session_id,
                        package,
                        "session.created",
                        {},
                        phase="session.after_initialization",
                    ),
                    self._capture_event(
                        session_id,
                        package,
                        "scene.entered",
                        {"scene_id": package.entry_scene_id},
                        phase="session.after_initialization",
                        source_actor_id=pc_actor_id,
                    ),
                ],
                request.command_id,
            )
            self._append_events(
                session_id, 0, initial_events, source_command_id=request.command_id, package=package
            )
            result = replace(
                result,
                event_seq=self._conn.execute(
                    "SELECT event_seq FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()[0],
            )
            result_json = canonical_json(asdict(result))
            self._conn.execute(
                "INSERT INTO creation_commands VALUES (?, ?, ?, ?, ?)",
                (context.principal_id, request.command_id, fingerprint, session_id, result_json),
            )
        return result

    def _initialize_scene(self, session_id: str, scene_id: str, package: AdventurePackage) -> None:
        return self._world._initialize_scene(session_id, scene_id, package)

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
    ):
        return self._world._operate(
            row, package, operation, actor_id=actor_id, effect_origin=effect_origin
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
            WorldState(json.loads(world[0]), world[1]),
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
            return self._snapshot(row, package)

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
