"""One SQLite authority, short transactions, detached reads, no runtime cache."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from ._operations import (
    EnterScene,
    FoundationOperation,
    MoveActor,
    ObjectState,
    SetObjectState,
    SetSceneFlag,
)
from .availability import AvailabilityRuntime
from .combat import DND_RULESET, CombatRuntime
from .commands import OPERATIONS, CommandRuntime, TypedCommand
from .dnd_runtime import DndRuntime
from .errors import StateMachineError
from .events import EventRuntime
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
from .package import (
    SYNTHETIC_EVALUATION_RULESET,
    AdventurePackage,
    RulesetBinding,
    Signal,
    SignalScope,
    _creation_fingerprint,
    canonical_json,
)
from .perception import PerceptionRuntime


def _binding(row: sqlite3.Row) -> RulesetBinding:
    return RulesetBinding(row["ruleset_id"], row["data_revision"], row["evaluator_version"])


def _result(raw: str) -> CreationResult:
    data = json.loads(raw)
    data["ruleset_binding"] = RulesetBinding(**data["ruleset_binding"])
    return CreationResult(**data)


def _trusted(context: TrustedContext) -> None:
    if not isinstance(context, TrustedContext) or any(
        not isinstance(v, str) or not v.strip() or len(v) > 256
        for v in (context.principal_id, context.player_id)
    ):
        raise StateMachineError("invalid_context", "A trusted principal/player context is required")
    if type(context.controlled_npc_ids) is not tuple or any(
        type(v) is not str or not v or len(v) > 256 for v in context.controlled_npc_ids
    ):
        raise StateMachineError("invalid_context", "NPC grants must be trusted identity tuples")


def _migration_statements(script: str) -> tuple[str, ...]:
    """Read our bundled SQL without executescript's implicit transaction commits."""
    statements, pending = [], ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            normalized = " ".join(pending.split()).upper()
            if normalized not in (
                "BEGIN IMMEDIATE;",
                "COMMIT;",
                "PRAGMA FOREIGN_KEYS = OFF;",
                "PRAGMA FOREIGN_KEYS = ON;",
            ):
                statements.append(pending)
            pending = ""
    if pending.strip():
        raise StateMachineError("storage_error", "Incomplete bundled migration SQL")
    return tuple(statements)


class StateMachine(
    CommandRuntime,
    EventRuntime,
    MechanicalRuntime,
    PerceptionRuntime,
    CombatRuntime,
    DndRuntime,
    AvailabilityRuntime,
):
    """Trusted in-process runtime. One connection per instance/thread; close explicitly.

    All writes commit immediately. Public lifecycle methods are not HTTP endpoints
    or player/NPC views. Only an authenticated adapter may supply TrustedContext.
    """

    def __init__(self, database: str | Path, *, evaluator=None, dnd_loader=None) -> None:
        from dnd5e_srd_data import BundledAssetLoader

        self._dnd_loader = dnd_loader if dnd_loader is not None else BundledAssetLoader()
        self._evaluator = evaluator if evaluator is not None else SyntheticEvaluator()
        if str(database) == ":memory:":
            raise StateMachineError(
                "unsupported_storage", "A local file database with WAL is required"
            )
        self._closed = False
        try:
            self._conn = sqlite3.connect(str(database), isolation_level=None, timeout=5.0)
        except sqlite3.Error as exc:
            raise StateMachineError("storage_error", "Cannot open SQLite database") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._migrate()
            # Journal-mode changes cannot run in the migration transaction. SQLite
            # can report BUSY immediately when simultaneous openers change the mode.
            deadline = monotonic() + 5.0
            while True:
                try:
                    mode = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                    break
                except sqlite3.OperationalError as exc:
                    if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY or monotonic() >= deadline:
                        raise
                    sleep(0.01)
            if mode != "wal":
                raise StateMachineError("unsupported_storage", "Database must support WAL")
            self._conn.execute("PRAGMA synchronous = FULL")
            if not self._conn.execute("PRAGMA foreign_keys").fetchone()[0]:
                raise StateMachineError("storage_error", "Foreign keys must be enabled")
            if self._conn.execute("PRAGMA foreign_key_check").fetchone():
                raise StateMachineError("storage_error", "Database contains invalid references")
        except sqlite3.Error as exc:
            self._conn.close()
            self._closed = True
            raise StateMachineError("storage_error", "Cannot initialize SQLite database") from exc
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    def _migrate(self):
        scripts = [Path(__file__).with_name("_schema.sql")] + [
            Path(__file__).with_name(f"_migration_{v}.sql") for v in range(2, 10)
        ]
        steps = [_migration_statements(path.read_text("utf-8")) for path in scripts]
        # Migration 4 rebuilds a referenced table. Disable FK enforcement only on
        # this unopened runtime connection, then validate all references before commit.
        self._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            # Read AFTER acquiring the cross-process SQLite migration write lock.
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in range(10):
                raise StateMachineError(
                    "unsupported_storage_schema", f"Unsupported storage version: {version}"
                )
            if (
                version == 0
                and self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchone()
            ):
                raise StateMachineError(
                    "unsupported_storage_schema", "Unversioned nonempty database"
                )
            for target in range(version + 1, 10):
                for statement in steps[target - 1]:
                    self._conn.execute(statement)
                if self._conn.execute("PRAGMA user_version").fetchone()[0] != target:
                    raise StateMachineError("storage_error", "Non-contiguous schema migration")
            if version < 5:
                # The locked source version proves ALL existing events predate
                # perception, even if a user command used the legacy source sentinel.
                self._conn.execute(
                    "INSERT INTO legacy_perception_boundaries "
                    "SELECT session_id,event_seq,'legacy_event_evidence_unavailable' "
                    "FROM sessions WHERE event_seq>0 "
                    "ON CONFLICT(session_id) DO UPDATE SET "
                    "through_event_seq=excluded.through_event_seq"
                )
                self._conn.execute(
                    "INSERT INTO projection_cursors "
                    "SELECT session_id,'perception',through_event_seq "
                    "FROM legacy_perception_boundaries WHERE true "
                    "ON CONFLICT(session_id,consumer) DO UPDATE SET event_seq=excluded.event_seq"
                )
            if self._conn.execute("PRAGMA foreign_key_check").fetchone():
                raise StateMachineError("storage_error", "Migration contains invalid references")
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        finally:
            self._conn.execute("PRAGMA foreign_keys = ON")

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise StateMachineError("closed", "StateMachine connection is closed")

    @contextmanager
    def _transaction(self, *, write: bool = False):
        self._ensure_open()
        try:
            self._conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        except sqlite3.Error as exc:
            raise StateMachineError("storage_error", "SQLite transaction failed") from exc

    def _session(self, session_id: str, package: AdventurePackage, context: TrustedContext):
        _trusted(context)
        if not isinstance(package, AdventurePackage):
            raise StateMachineError("invalid_request", "An approved package is required")
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise StateMachineError("session_not_found", "Unknown session")
        if (row["principal_id"], row["player_id"]) != (context.principal_id, context.player_id):
            raise StateMachineError("unauthorized", "Context does not control this session")
        if (row["adventure_id"], row["package_version"]) != (
            package.adventure_id,
            package.package_version,
        ):
            raise StateMachineError(
                "package_identity_mismatch", "Session is pinned to another package/version"
            )
        if row["package_digest"] != package.package_digest:
            raise StateMachineError(
                "package_digest_mismatch", "Package content differs from the pinned digest"
            )
        if _binding(row) != package.ruleset_binding:
            raise StateMachineError(
                "ruleset_mismatch", "Session's fixed RulesetBinding does not match"
            )
        return row

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
            or not isinstance(request.players[0], PlayerSelection)
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
        flags_json = canonical_json(dict(package.world_flags))
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
            # All authority and creation deduplication share this transaction.
            self._conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, NULL)",
                (
                    session_id,
                    package.adventure_id,
                    package.package_version,
                    package.package_digest,
                    package.ruleset_binding.ruleset_id,
                    package.ruleset_binding.data_revision,
                    package.ruleset_binding.evaluator_version,
                    context.principal_id,
                    context.player_id,
                    pc.pc_template_id,
                    pc_actor_id,
                    package.entry_scene_id,
                    rng_ref,
                ),
            )
            self._conn.execute("INSERT INTO world_states VALUES (?, ?)", (session_id, flags_json))
            self._initialize_scene(session_id, package.entry_scene_id, package)
            self._conn.execute(
                "INSERT INTO actors VALUES (?, ?, 'pc', ?)",
                (session_id, pc_actor_id, package.entry_scene_id),
            )
            self._conn.execute(
                "INSERT INTO characters(session_id,actor_id,current_hp,max_hp,"
                "resources_json,inventory_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    pc_actor_id,
                    pc.current_hp,
                    pc.max_hp,
                    canonical_json(dict(pc.resources)),
                    canonical_json(pc.inventory),
                ),
            )
            self._conn.execute(
                "INSERT INTO rng_states(session_id,state_id,algorithm,encoding,position) "
                "VALUES (?, ?, 'unselected', 'foundation-placeholder/1', 0)",
                (session_id, rng_ref),
            )
            if package.ruleset_binding == SYNTHETIC_EVALUATION_RULESET:
                self._conn.execute(
                    "UPDATE rng_states SET algorithm='synthetic-sha256-counter/1', "
                    "encoding='synthetic-counter-json/1', state_json=? WHERE session_id=?",
                    (canonical_json({"counter": 0}), session_id),
                )
            initial_row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if package.noncombat_foundation_json is not None:
                self._initialize_noncombat(initial_row, package)
            initial_events = self._expand_events(
                initial_row,
                package,
                [("session.created", {}), ("scene.entered", {"scene_id": package.entry_scene_id})],
                request.command_id,
            )
            self._append_events(
                session_id,
                0,
                initial_events,
                source_command_id=request.command_id,
                package=package,
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
        if self._conn.execute(
            "SELECT 1 FROM scene_instances WHERE session_id = ? AND scene_id = ?",
            (session_id, scene_id),
        ).fetchone():
            return
        scene = package.scene(scene_id)
        self._conn.execute(
            "INSERT INTO scene_instances VALUES (?, ?, ?, 1)",
            (session_id, scene_id, canonical_json(dict(scene.flags))),
        )
        self._conn.executemany(
            "INSERT INTO objects VALUES (?, ?, ?, ?)",
            [(session_id, ob.object_id, scene_id, ob.state) for ob in scene.objects],
        )
        for actor in package.actors:
            if actor.initial_scene_id == scene_id:
                # A scene reference never duplicates or resets an existing world actor.
                self._conn.execute(
                    "INSERT INTO actors SELECT ?, ?, 'npc', ? WHERE NOT EXISTS "
                    "(SELECT 1 FROM actors WHERE session_id = ? AND actor_id = ?)",
                    (session_id, actor.actor_id, scene_id, session_id, actor.actor_id),
                )
        self._initialize_mechanical_scene(session_id, scene_id, package)

    def _append_events(
        self,
        session_id: str,
        version: int,
        events: list[tuple[str, dict]],
        *,
        source_command_id: str,
        package: AdventurePackage,
    ) -> None:
        seq = self._conn.execute(
            "SELECT event_seq FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        evidence = {
            "schema": "event-evidence/2",
            "actors": [
                dict(actor)
                for actor in self._conn.execute(
                    "SELECT actor_id, kind, current_scene_id FROM actors WHERE session_id=?",
                    (session_id,),
                ).fetchall()
            ],
        }
        for actor in evidence["actors"]:
            definition = next((a for a in package.actors if a.actor_id == actor["actor_id"]), None)
            actor["senses"] = (
                dict(definition.senses) if definition and definition.senses is not None else None
            )
        for event_type, payload in events:
            seq += 1
            self._conn.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                (session_id, seq, event_type, version, canonical_json(payload)),
            )
            self._conn.execute(
                "INSERT INTO event_evidence VALUES (?, ?, ?)",
                (session_id, seq, canonical_json(evidence)),
            )
            signals = []
            scene_id = payload.get("scene_id", payload.get("to_scene_id"))
            descriptions = {
                "scene.entered": "Someone enters the scene.",
                "scene.exited": "Someone leaves the scene.",
                "actor.moved": "Someone moves into view.",
                "object.state_changed": "An object changes position.",
            }
            if (
                event_type == "combat.engine_event"
                and payload["engine_event"]["type"] == "intent_submitted"
                and payload["engine_event"]["intent_type"] == "attack"
            ):
                descriptions[event_type] = "Someone makes an attack."
            if event_type in descriptions and scene_id:
                signals = [
                    Signal(
                        "signal.visible",
                        "visual",
                        descriptions[event_type],
                        SignalScope(scene_id, "clear"),
                    )
                ]
            elif event_type == "package.event_fired":
                definition = next(
                    e
                    for e in package.events
                    if e.event_definition_id == payload["event_definition_id"]
                )
                signals = list(definition.signals)
            self._conn.execute(
                "INSERT INTO event_records VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    seq,
                    f"event.{session_id}.{seq}",
                    source_command_id,
                    canonical_json([asdict(signal) for signal in signals]),
                ),
            )
            self._conn.execute(
                "INSERT INTO event_outbox(session_id,event_seq,consumer) VALUES (?, ?, 'delivery')",
                (session_id, seq),
            )
            self._conn.execute(
                "INSERT INTO event_outbox(session_id,event_seq,consumer) "
                "VALUES (?, ?, 'perception')",
                (session_id, seq),
            )
        self._conn.execute(
            "UPDATE sessions SET event_seq = ? WHERE session_id = ?", (seq, session_id)
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

    def _operate(self, row, package: AdventurePackage, operation: FoundationOperation):
        self._world_operation_capability(row, operation)
        sid = row["session_id"]
        if isinstance(operation, EnterScene):
            target = package.scene(operation.scene_id)
            if target.scene_id == row["current_scene_id"]:
                return []
            current = package.scene(row["current_scene_id"])
            if target.scene_id not in current.exits:
                raise StateMachineError("scene_unreachable", "No unconditional exit to this scene")
            existed = self._conn.execute(
                "SELECT 1 FROM scene_instances WHERE session_id = ? AND scene_id = ?",
                (sid, target.scene_id),
            ).fetchone()
            self._initialize_scene(sid, target.scene_id, package)
            if existed:
                self._conn.execute(
                    "UPDATE scene_instances SET visited_count = visited_count + 1 "
                    "WHERE session_id = ? AND scene_id = ?",
                    (sid, target.scene_id),
                )
            self._conn.execute(
                "UPDATE sessions SET current_scene_id = ? WHERE session_id = ?",
                (target.scene_id, sid),
            )
            self._conn.execute(
                "UPDATE actors SET current_scene_id = ? WHERE session_id = ? AND actor_id = ?",
                (target.scene_id, sid, row["pc_actor_id"]),
            )
            return [
                ("scene.exited", {"scene_id": current.scene_id}),
                ("scene.entered", {"scene_id": target.scene_id}),
            ]
        if isinstance(operation, (SetSceneFlag, SetObjectState)):
            if operation.scene_id != row["current_scene_id"]:
                raise StateMachineError("invalid_target", "Only the current scene can be modified")
            if isinstance(operation, SetSceneFlag):
                if type(operation.value) is not bool:
                    raise StateMachineError("invalid_operation", "Flag value must be boolean")
                flags = json.loads(
                    self._conn.execute(
                        "SELECT flags_json FROM scene_instances "
                        "WHERE session_id = ? AND scene_id = ?",
                        (sid, operation.scene_id),
                    ).fetchone()[0]
                )
                if operation.flag_id not in flags:
                    raise StateMachineError("invalid_target", "Flag must be declared in this scene")
                if flags[operation.flag_id] == operation.value:
                    return []
                flags[operation.flag_id] = operation.value
                self._conn.execute(
                    "UPDATE scene_instances SET flags_json = ? "
                    "WHERE session_id = ? AND scene_id = ?",
                    (canonical_json(flags), sid, operation.scene_id),
                )
                return [("scene.flag_changed", asdict(operation))]
            if not isinstance(operation.state, ObjectState):
                raise StateMachineError("invalid_operation", "Expected a typed ObjectState")
            obj = self._conn.execute(
                "SELECT state FROM objects WHERE session_id = ? AND scene_id = ? AND object_id = ?",
                (sid, operation.scene_id, operation.object_id),
            ).fetchone()
            if obj is None:
                raise StateMachineError("invalid_target", "Object is not in the current scene")
            if obj[0] == operation.state.value:
                return []
            self._conn.execute(
                "UPDATE objects SET state = ? WHERE session_id = ? AND object_id = ?",
                (operation.state.value, sid, operation.object_id),
            )
            return [("object.state_changed", asdict(operation))]
        actor = self._conn.execute(
            "SELECT * FROM actors WHERE session_id = ? AND actor_id = ?", (sid, operation.actor_id)
        ).fetchone()
        if actor is None or actor["kind"] != "npc":
            raise StateMachineError("invalid_target", "Only an existing synthetic NPC may be moved")
        if not self._conn.execute(
            "SELECT 1 FROM scene_instances WHERE session_id = ? AND scene_id = ?",
            (sid, operation.to_scene_id),
        ).fetchone():
            raise StateMachineError("invalid_target", "NPC destination must be initialized")
        if actor["current_scene_id"] == operation.to_scene_id:
            return []
        self._conn.execute(
            "UPDATE actors SET current_scene_id = ? WHERE session_id = ? AND actor_id = ?",
            (operation.to_scene_id, sid, operation.actor_id),
        )
        return [("actor.moved", asdict(operation))]

    def _snapshot(self, row) -> SessionSnapshot:
        sid = row["session_id"]
        world = self._conn.execute(
            "SELECT flags_json FROM world_states WHERE session_id = ?", (sid,)
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
                s["scene_id"], s["visited_count"], json.loads(s["flags_json"]), objects
            )
        actors = {
            a["actor_id"]: ActorState(a["actor_id"], a["kind"], a["current_scene_id"])
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
            WorldState(json.loads(world[0])),
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
        )

    def read_session(
        self, session_id: str, package: AdventurePackage, *, context: TrustedContext
    ) -> SessionSnapshot:
        """Read a consistent detached runtime snapshot, verifying owner and all pins."""
        with self._transaction():
            return self._snapshot(self._session(session_id, package, context))

    def save_session(
        self, session_id: str, package: AdventurePackage, *, context: TrustedContext
    ) -> SessionSnapshot:
        """All changes are already durable; verify/read the current committed save.

        No mutable snapshot is accepted and no checkpoint/event/Meta commit is made.
        """
        with self._transaction():
            row = self._session(session_id, package, context)
            self._require_noncombat_save(row)
            return self._snapshot(row)

    def resume_session(
        self, session_id: str, package: AdventurePackage, *, context: TrustedContext
    ) -> SessionSnapshot:
        """Resume the persisted noncombat foundation, without replaying templates."""
        return self.save_session(session_id, package, context=context)
