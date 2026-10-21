"""Session execution metadata and common publication in the caller's transaction.

No second executor, database or authoritative in-memory state. Domain owners
apply facts; this component coordinates versions, RNG, events and receipts.
"""

import hashlib
from dataclasses import asdict, replace

from dnd5e_engine.evaluation_contracts import CheckPayload, CombatClosePayload, CombatIntentPayload
from dnd5e_engine.evaluation_rng import RNGContext, RNGState
from pydantic import ValidationError

from .environment_commands import ENVIRONMENT_OPERATIONS, environment_operation_to_data
from .errors import StateMachineError
from .package import canonical_json
from .scoped_reads import scoped_read
from .session_access import _binding
from .world_commands import ActivateMechanism, RepositionObject


def _fingerprint(command, digest, binding, context):
    data = asdict(command)
    if command.time_wait is None:
        data.pop("time_wait")  # Preserve every pre-wait command fingerprint.
    if type(command.payload) in ENVIRONMENT_OPERATIONS:
        data["payload"] = environment_operation_to_data(command.payload)["payload"]
    if type(command.payload) is RepositionObject:
        # Old receipts used exactly the four original fields.
        for field in ("expected_orientation", "to_orientation"):
            if data["payload"][field] is None:
                data["payload"].pop(field)
    if type(command.payload) is ActivateMechanism and command.payload.operation_id == "activate":
        data["payload"].pop("operation_id")
    if command.check_world_effects is None:
        # Preserve canonical fingerprints of all pre-SM21 stored commands.
        data.pop("check_world_effects")
    if type(command.payload) in (CombatIntentPayload, CombatClosePayload, CheckPayload):
        data["payload"] = command.payload.model_dump(mode="json")
    return hashlib.sha256(
        canonical_json(
            {
                "command": data,
                "principal": context.principal_id,
                "player": context.player_id,
                "package": digest,
                "binding": asdict(binding),
            }
        ).encode()
    ).hexdigest()


class SessionRuntime:
    def __init__(self, store, access, persistence):
        self.store = store
        self.access = access
        self.persistence = persistence
        self.time = None
        store._recovery_listeners.append(self._release_recovered_driver)

    def _release_recovered_driver(self, sid):
        """Called only on this Store's owning thread, before reopening reads."""
        if self.time is not None:
            drivers = (
                tuple(self.time.drivers.values())
                if sid is None
                else ((self.time.drivers[sid],) if sid in self.time.drivers else ())
            )
            for driver in drivers:
                driver.close()

    @property
    def _conn(self):
        return self.store.connection

    def fingerprint(self, command, row, context):
        return _fingerprint(command, row["package_digest"], _binding(row), context)

    def admit(self, row):
        """Required session-local facts; no full SessionSnapshot for an operation."""
        sid = row["session_id"]
        world = self._conn.execute(
            "SELECT game_tick FROM world_states WHERE session_id=?", (sid,)
        ).fetchone()
        pc = self._conn.execute(
            "SELECT actor_id FROM characters WHERE session_id=? AND actor_id=?",
            (sid, row["pc_actor_id"]),
        ).fetchone()
        actor = self._conn.execute(
            "SELECT current_scene_id FROM actors WHERE session_id=? AND actor_id=?",
            (sid, row["pc_actor_id"]),
        ).fetchone()
        scene = self._conn.execute(
            "SELECT scene_id FROM scene_instances WHERE session_id=? AND scene_id=?",
            (sid, row["current_scene_id"]),
        ).fetchone()
        rng = self._conn.execute(
            "SELECT state_id FROM rng_states WHERE session_id=?", (sid,)
        ).fetchone()
        if (
            world is None
            or pc is None
            or actor is None
            or scene is None
            or rng is None
            or actor[0] != row["current_scene_id"]
            or rng[0] != row["rng_ref"]
        ):
            raise StateMachineError(
                "incomplete_session", "Required persisted Session facts missing"
            )

    def guard_time_owner(self, sid):
        """Attached Drivers use one local dispatcher; other connections may read.

        Reject foreign writers while the OS owner exists, so an earlier deadline
        cannot be added without notifying the owner. No IPC or poller is needed.
        """
        if self.time is None or sid in self.time.drivers:
            return
        from .time_driver import _DriverLock

        database = self._conn.execute("PRAGMA database_list").fetchone()[2]
        if _DriverLock.path(database, sid).exists():
            lock = _DriverLock(database, sid)
            lock.close()

    def refresh_time_drivers(self):
        if self.time:
            for driver in tuple(self.time.drivers.values()):
                driver.refresh()

    def authorize_time_wait_reply(self, row, package, command, context):
        if self.time is not None:
            return self.time.waits.authorize(row, package, command, context)

    def finish_time_wait_reply(self, row, package, command, events=()):
        return [] if self.time is None else self.time.waits.finish(row, package, command, events)

    def coordinate_combat_time(self, row, package, command_id, previous_round, event_offset=0):
        """RE retains Turn rules; only a completed Round advances six world ticks."""
        import json

        if self.time is None or previous_round is None:
            return []
        combat = self._conn.execute(
            "SELECT state_json FROM combats WHERE session_id=?", (row["session_id"],)
        ).fetchone()
        state = json.loads(combat[0])
        rounds = state["round_number"] - previous_round
        if rounds < 0 or rounds > 1:
            raise StateMachineError("invalid_rule_result", "Unexpected Round transition")
        events = []
        if rounds:
            current = self.time.world.facts.time_state(row["session_id"])[1]
            staged_row = dict(row)
            staged_row["event_seq"] += event_offset
            events = self.time._advance(staged_row, package, current + 6, command_id, "combat", 8)
        if rounds or state["ended"]:
            self.time.invalidate(row["session_id"])
        return events

    def prepare_timed_command(self, command, package, context, fingerprint):
        """Pin the authorized operation's target once, before its business snapshot.

        Only this command's admitted time changes may translate its version.
        A later unrelated change still conflicts. Faults retain the sampled target;
        replay uses the original fingerprint and never samples a second time.
        """
        if self.time is None or package.legacy_audit_only:
            return command
        if command.time_wait is not None:
            with self.store.transaction():
                row = self.access._session(command.session_id, package, context)
                self.access._authorize_command(row, command.actor_id, command.payload, context)
                self.access._npc_handoff_command_guard(command)
                self.authorize_time_wait_reply(row, package, command, context)
                if self._command_replay(command, fingerprint):
                    return command
                self.time.waits.validate(row, package, command)
            return command  # Domain reply executes at the committed boundary.
        from .time_coordination import SetTimeRunning

        control = type(command.payload) is SetTimeRunning and command.payload.action != "cancel"
        timed = (
            control
            or command.command_type.startswith("world.")
            or command.command_type.startswith("activity.")
            or command.command_type
            in (
                "rules.check",
                "combat.initialize",
                "combat.intent",
                "combat.close",
                "enter_scene",
                "move_actor",
                "set_object_state",
            )
        ) and command.command_type != "world.advance_time"
        if not timed:
            return command
        sid = command.session_id
        with self.store.transaction():
            row = self.access._session(sid, package, context)
            self.access._authorize_command(row, command.actor_id, command.payload, context)
            self.access._npc_handoff_command_guard(command)
            if self._command_replay(command, fingerprint):
                return command
            admission = self._conn.execute(
                "SELECT * FROM time_command_admissions WHERE session_id=? AND command_id=?",
                (sid, command.command_id),
            ).fetchone()
            driver = self.time.drivers.get(sid)
            pending = self.time.progress(sid)
            if control and pending and pending["status"] != "completed" and admission is None:
                return command  # control itself reconciles the existing fixed target
            combat_pending = (
                pending and pending["source"] == "combat" and pending["status"] != "completed"
            )
            if (
                admission is None
                and pending
                and pending["status"] != "completed"
                and pending["source"] != "combat"
            ):
                raise StateMachineError("time_advance_pending", "Finish the saved time work first")
            if admission is None and (
                (
                    not combat_pending
                    and (
                        driver is None
                        or self.time.run(sid)["mode"] != "running"
                        or self.time.world._active_combat(sid)
                    )
                )
                or self.version_conflict(row, command)
            ):
                return command
        with self.store.transaction(write=True):
            row = self.access._session(sid, package, context)
            self.guard_time_owner(sid)
            self.access._authorize_command(row, command.actor_id, command.payload, context)
            admission = self._conn.execute(
                "SELECT * FROM time_command_admissions WHERE session_id=? AND command_id=?",
                (sid, command.command_id),
            ).fetchone()
            if admission is None:
                if self.version_conflict(row, command):
                    return command
                sampled = (
                    pending["target_tick"] if combat_pending else driver.target(self.time.run(sid))
                )
                target = int(sampled)
                self._conn.execute(
                    "INSERT INTO time_command_admissions VALUES (?,?,?,?,?,?,0,?,?,?)",
                    (
                        sid,
                        command.command_id,
                        fingerprint,
                        command.expected_world_version,
                        row["world_version"],
                        target,
                        row["event_seq"],
                        "combat" if combat_pending else "realtime",
                        str(sampled - target),
                    ),
                )
        # Persisted admission survives evaluation/storage failures and restarts.
        with self.store.transaction(write=True):
            row = self.access._session(sid, package, context)
            self.access._authorize_command(row, command.actor_id, command.payload, context)
            self.access._npc_handoff_command_guard(command)
            admission = self._conn.execute(
                "SELECT * FROM time_command_admissions WHERE session_id=? AND command_id=?",
                (sid, command.command_id),
            ).fetchone()
            if admission["fingerprint"] != fingerprint:
                raise StateMachineError("idempotency_conflict", "Different timed command content")
            effective = replace(command, expected_world_version=admission["execution_version"])
            if admission["ready"] or self.version_conflict(row, effective):
                return effective
            events = self.time._advance(
                row, package, admission["target_tick"], command.command_id, admission["source"], 8
            )
            version = (
                self.stage_outcome(effective, row, package, events)
                if events
                else row["world_version"]
            )
            ready = self.time.progress(sid)["status"] == "completed"
            self._conn.execute(
                "UPDATE time_command_admissions SET execution_version=?,ready=? "
                "WHERE session_id=? AND command_id=?",
                (version, int(ready), sid, command.command_id),
            )
        self.refresh_time_drivers()
        if not ready:
            raise StateMachineError(
                "time_advance_pending", "Retry this command to continue its target"
            )
        return replace(command, expected_world_version=version)

    def prepare_scene_maps(self, sid, scene_ids, maps):
        """Lifecycle coordination only; Grid reads and builds the actual maps."""
        for scene_id in scene_ids:
            maps.scene(sid, scene_id)

    def release_scene_maps(self, sid, maps):
        """Dispose rebuildable representations without deleting authoritative facts."""
        maps.release(sid)

    def receipt(self, sid, command_id):
        from .commands import receipt_from_json

        row = self._conn.execute(
            "SELECT receipt_json FROM command_receipts WHERE session_id=? AND command_id=?",
            (sid, command_id),
        ).fetchone()
        return None if row is None else receipt_from_json(row[0])

    def stage_outcome(self, command, row, package, events):
        """Stage versions/events; Store publishes RAM only after durable COMMIT."""
        self.store.require_write()
        self.access._check_package_pin(row, package)
        self.guard_time_owner(command.session_id)
        if self.version_conflict(row, command):
            raise StateMachineError("version_conflict", "Command was prepared on another version")
        version = row["world_version"] + 1
        self.set_world_version(command.session_id, version)
        self.persistence._append_events(
            command.session_id,
            version,
            events,
            source_command_id=command.command_id,
            package=package,
        )
        return version

    def version_conflict(self, row, command):
        return row["world_version"] != command.expected_world_version

    def finish(self, command, context, fingerprint, row, status, version, error):
        """The receipt joins the same outer transaction as domain changes/events."""
        from .commands import CommandReceipt

        self.store.require_write()
        end = self._conn.execute(
            "SELECT event_seq FROM sessions WHERE session_id=?", (command.session_id,)
        ).fetchone()[0]
        admission = self._conn.execute(
            "SELECT start_event_seq FROM time_command_admissions "
            "WHERE session_id=? AND command_id=?",
            (command.session_id, command.command_id),
        ).fetchone()
        start_seq = admission[0] if admission else row["event_seq"]
        own_seqs = tuple(range(start_seq + 1, end + 1))
        if admission:
            own_seqs = tuple(
                r[0]
                for r in self._conn.execute(
                    "SELECT event_seq FROM event_records WHERE session_id=? AND event_seq>? "
                    "AND source_command_id=? ORDER BY event_seq",
                    (command.session_id, start_seq, command.command_id),
                )
            )
        receipt = CommandReceipt(
            command.session_id,
            command.command_id,
            command.actor_id,
            status,
            version,
            own_seqs,
            error,
        )
        self.record_receipt(command, context, fingerprint, receipt)
        return receipt

    def _command_replay(self, command, fingerprint):
        from .commands import receipt_from_json

        stored = self._conn.execute(
            "SELECT fingerprint, receipt_json FROM command_receipts "
            "WHERE session_id = ? AND command_id = ?",
            (command.session_id, command.command_id),
        ).fetchone()
        if stored is None:
            return None
        if stored["fingerprint"] != fingerprint:
            raise StateMachineError("idempotency_conflict", "Command key has different content")
        return receipt_from_json(stored["receipt_json"])

    def move_pc_scene(self, sid, scene_id):
        self.store.require_write()
        self._conn.execute(
            "UPDATE sessions SET current_scene_id=? WHERE session_id=?", (scene_id, sid)
        )

    def set_mechanical_schema(self, sid, version):
        self.store.require_write()
        self._conn.execute(
            "UPDATE sessions SET mechanical_schema_version=? WHERE session_id=?", (version, sid)
        )

    def initialize(self, sid, package, context, pc, pc_actor_id, rng_ref):
        self.store.require_write()
        self._conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,NULL)",
            (
                sid,
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

    def initialize_rng(self, sid, rng_ref, *, synthetic):
        self.store.require_write()
        self._conn.execute(
            "INSERT INTO rng_states(session_id,state_id,algorithm,encoding,position) "
            "VALUES (?,?,'unselected','foundation-placeholder/1',0)",
            (sid, rng_ref),
        )
        if synthetic:
            self._conn.execute(
                "UPDATE rng_states SET algorithm='synthetic-sha256-counter/1', "
                "encoding='synthetic-counter-json/1',state_json=? WHERE session_id=?",
                (canonical_json({"counter": 0}), sid),
            )

    def select_rule_rng(self, sid, state_json, *, reset_position=False):
        self.store.require_write()
        if reset_position:
            self._conn.execute(
                "UPDATE rng_states SET algorithm='stdlib-mt19937',encoding='stdlib-mt19937/1',"
                "position=0,state_json=? WHERE session_id=? AND algorithm='unselected'",
                (state_json, sid),
            )
        else:
            self._conn.execute(
                "UPDATE rng_states SET algorithm='stdlib-mt19937',encoding='stdlib-mt19937/1',"
                "state_json=? WHERE session_id=? AND algorithm='unselected'",
                (state_json, sid),
            )

    def set_world_version(self, sid, version):
        self.store.require_write()
        self._conn.execute("UPDATE sessions SET world_version=? WHERE session_id=?", (version, sid))

    def advance_rule_rng(self, sid, transition, rng):
        self.store.require_write()
        try:
            transition.verify_input(rng)
        except (ValueError, TypeError) as exc:
            raise StateMachineError("invalid_rule_result", "RNG transition input differs") from exc
        if self.rng_context({"session_id": sid}) != rng:
            raise StateMachineError("version_conflict", "Authoritative RNG changed")
        self._conn.execute(
            "UPDATE rng_states SET state_json=?,rng_version=? WHERE session_id=?",
            (
                transition.next_state.model_dump_json(),
                rng.version + int(transition.state_changed),
                sid,
            ),
        )

    def advance_synthetic_rng(self, sid, transition):
        self.store.require_write()
        before, rng = transition.before, transition.after
        current = self._conn.execute(
            "SELECT * FROM rng_states WHERE session_id=?", (sid,)
        ).fetchone()
        if current is None or (
            current["state_id"],
            current["algorithm"],
            current["encoding"],
            current["position"],
            current["rng_version"],
            current["state_json"],
        ) != (
            before.state_id,
            before.algorithm,
            before.encoding,
            before.position,
            before.rng_version,
            before.state_json,
        ):
            raise StateMachineError("version_conflict", "Authoritative RNG changed")
        self._conn.execute(
            "UPDATE rng_states SET position=?,rng_version=?,state_json=? WHERE session_id=?",
            (rng.position, rng.rng_version, rng.state_json, sid),
        )

    def record_receipt(self, command, context, fingerprint, receipt):
        self.store.require_write()
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

    def rng_context(self, row):
        return self._rng_context(row["session_id"])

    @scoped_read
    def _rng_context(self, sid):
        rng = self._conn.execute("SELECT * FROM rng_states WHERE session_id=?", (sid,)).fetchone()
        if (
            rng is None
            or rng["algorithm"] != "stdlib-mt19937"
            or rng["encoding"] != "stdlib-mt19937/1"
        ):
            raise StateMachineError("invalid_rng_state", "Expected pinned Engine RNG encoding")
        try:
            return RNGContext(
                stream_id=rng["state_id"],
                version=rng["rng_version"],
                state=RNGState.model_validate_json(rng["state_json"]),
            )
        except (ValidationError, ValueError) as exc:
            raise StateMachineError("invalid_rng_state", "Invalid stored RNG state") from exc
