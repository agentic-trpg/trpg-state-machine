"""Session execution metadata and common publication in the caller's transaction.

No second executor, database or authoritative in-memory state. Domain owners
apply facts; this component coordinates versions, RNG, events and receipts.
"""

import hashlib
from dataclasses import asdict

from dnd5e_engine.evaluation_contracts import CheckPayload, CombatClosePayload, CombatIntentPayload
from dnd5e_engine.evaluation_rng import RNGContext, RNGState
from pydantic import ValidationError

from .environment_commands import ENVIRONMENT_OPERATIONS, environment_operation_to_data
from .errors import StateMachineError
from .package import canonical_json
from .session_access import _binding
from .world_commands import ActivateMechanism, RepositionObject


def _fingerprint(command, digest, binding, context):
    data = asdict(command)
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

    @property
    def _conn(self):
        return self.store.connection

    def fingerprint(self, command, row, context):
        return _fingerprint(command, row["package_digest"], _binding(row), context)

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

    def publish(self, command, row, package, events):
        """Publish an admitted changed outcome inside its existing savepoint."""
        self.store.require_write()
        self.access._check_package_pin(row, package)
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
        receipt = CommandReceipt(
            command.session_id,
            command.command_id,
            command.actor_id,
            status,
            version,
            tuple(range(row["event_seq"] + 1, end + 1)),
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
        rng = self._conn.execute(
            "SELECT * FROM rng_states WHERE session_id=?", (row["session_id"],)
        ).fetchone()
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
