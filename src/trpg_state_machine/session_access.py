"""Session identity, grants and commit metadata shared by domain callers."""

import json
import sqlite3
from dataclasses import asdict

from dnd5e_engine.evaluation_contracts import CheckPayload, CombatClosePayload, CombatIntentPayload

from ._operations import (
    EnterScene,
    MoveActor,
    PlaceObject,
    SetObjectState,
    StepActor,
    TraversePassage,
)
from .commands import receipt_from_json
from .errors import StateMachineError
from .game_time import AdvanceGameTime
from .models import TrustedContext
from .navigation_models import NAVIGATION_OPERATIONS
from .npc_outputs import _handoff_command
from .package import AdventurePackage, RulesetBinding, canonical_json
from .perception import NPCContext


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
    if (
        type(context.can_adjudicate_checks) is not bool
        or type(context.can_advance_time) is not bool
    ):
        raise StateMachineError(
            "invalid_context", "GM adjudication grant must be a trusted boolean"
        )


def _binding(row: sqlite3.Row) -> RulesetBinding:
    return RulesetBinding(row["ruleset_id"], row["data_revision"], row["evaluator_version"])


class SessionAccess:
    def __init__(self, store):
        self.store = store

    @property
    def _conn(self):
        return self.store.connection

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
        self._check_package_pin(row, package)
        return row

    def _check_package_pin(self, row, package):
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

    def _authorize_command(self, row, actor_id, payload, context):
        if type(payload) is AdvanceGameTime and not context.can_advance_time:
            raise StateMachineError("unauthorized", "Trusted game-time grant required")
        if isinstance(payload, CheckPayload) and not context.can_adjudicate_checks:
            raise StateMachineError("unauthorized", "Trusted GM adjudication grant required")
        if (
            isinstance(payload, MoveActor)
            or (type(payload) in NAVIGATION_OPERATIONS and actor_id != row["pc_actor_id"])
            or (
                isinstance(
                    payload,
                    (
                        CombatIntentPayload,
                        CombatClosePayload,
                        CheckPayload,
                        StepActor,
                        TraversePassage,
                        PlaceObject,
                        SetObjectState,
                    ),
                )
                and actor_id != row["pc_actor_id"]
            )
        ):
            if isinstance(payload, MoveActor) and actor_id != payload.actor_id:
                raise StateMachineError("invalid_actor", "Envelope and payload actors differ")
            actor = self._conn.execute(
                "SELECT kind FROM actors WHERE session_id=? AND actor_id=?",
                (row["session_id"], actor_id),
            ).fetchone()
            if actor is None or actor[0] != "npc":
                raise StateMachineError("invalid_target", "Expected an existing NPC")
            if actor_id not in context.controlled_npc_ids:
                raise StateMachineError("unauthorized", "Trusted NPC control grant required")
        elif actor_id != row["pc_actor_id"]:
            raise StateMachineError("invalid_actor", "Command requires the selected PC")

    def _world_operation_capability(self, row, payload):
        if isinstance(
            payload,
            (
                EnterScene,
                MoveActor,
                SetObjectState,
                AdvanceGameTime,
                StepActor,
                TraversePassage,
                PlaceObject,
            ),
        ):
            combat = self._conn.execute(
                "SELECT state_json FROM combats WHERE session_id=?", (row["session_id"],)
            ).fetchone()
            if combat and not json.loads(combat[0])["ended"]:
                raise StateMachineError(
                    "unsupported_combat_world_operation",
                    "This world operation has no represented mechanical Delta during combat",
                )

    def _command_replay(self, command, fingerprint):
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

    def _npc_authorized(self, session_id, package, context):
        if (
            type(context) is not NPCContext
            or context.session_id != session_id
            or any(
                type(v) is not str or not v
                for v in (context.principal_id, context.session_id, context.observer_id)
            )
        ):
            raise StateMachineError("unauthorized", "Expected a session-bound NPC context")
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        grant = self._conn.execute(
            "SELECT principal_id FROM npc_access WHERE session_id=? AND observer_id=?",
            (session_id, context.observer_id),
        ).fetchone()
        if row is None or grant is None or grant[0] != context.principal_id:
            raise StateMachineError("unauthorized", "No NPC observation access grant")
        # Use the owner check only for package pins, never expose its snapshot to NPC.

        self._session(session_id, package, TrustedContext(row["principal_id"], row["player_id"]))
        return context.observer_id

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
        self._conn.execute(
            "UPDATE rng_states SET state_json=?,rng_version=? WHERE session_id=?",
            (
                transition.next_state.model_dump_json(),
                rng.version + int(transition.state_changed),
                sid,
            ),
        )

    def advance_synthetic_rng(self, sid, rng):
        self.store.require_write()
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

    def _npc_handoff_command_guard(self, command):
        """Recheck persisted origin under the existing command's world write lock."""
        row = self._conn.execute(
            "SELECT h.command_id,h.payload_json,t.session_id,t.npc_id,t.principal_id "
            "FROM npc_output_handoffs h "
            "JOIN npc_evaluation_tasks t USING(evaluation_id) WHERE h.command_id=?",
            (command.command_id,),
        ).fetchone()
        if row is None:
            return
        grant = self._conn.execute(
            "SELECT principal_id FROM npc_access WHERE session_id=? AND observer_id=?",
            (row["session_id"], row["npc_id"]),
        ).fetchone()
        if grant is None or grant[0] != row["principal_id"]:
            raise StateMachineError("unauthorized", "NPC output controller grant was revoked")
        if command != _handoff_command(row):
            raise StateMachineError(
                "idempotency_conflict", "Command differs from its reliable handoff"
            )

    def _npc_combat_location_guard(self, command):
        """Current target admission after receipt replay, before mechanical assembly."""
        if not self._conn.execute(
            "SELECT 1 FROM npc_output_handoffs WHERE command_id=?", (command.command_id,)
        ).fetchone():
            return
        if type(command.payload) is CombatIntentPayload and command.payload.target_id is not None:
            locations = dict(
                self._conn.execute(
                    "SELECT actor_id,current_scene_id FROM actors "
                    "WHERE session_id=? AND actor_id IN (?,?)",
                    (command.session_id, command.actor_id, command.payload.target_id),
                ).fetchall()
            )
            if (
                command.actor_id not in locations
                or command.payload.target_id not in locations
                or locations[command.actor_id] != locations[command.payload.target_id]
            ):
                raise StateMachineError(
                    "unauthorized_target", "NPC target is outside its current scene"
                )

    def _npc_combat_target_guard(self, command, package, snapshot):
        """Handoff visibility is deliberately limited to clear, bright current scenes."""
        if not self._conn.execute(
            "SELECT 1 FROM npc_output_handoffs WHERE command_id=?", (command.command_id,)
        ).fetchone():
            return
        if type(command.payload) is not CombatIntentPayload or command.payload.target_id is None:
            return
        definition = next(a for a in package.actors if a.actor_id == command.actor_id)
        locations = dict(
            self._conn.execute(
                "SELECT actor_id,current_scene_id FROM actors WHERE session_id=?",
                (command.session_id,),
            ).fetchall()
        )
        states = {a.entity_id: a for a in snapshot.character_states}
        grid = snapshot.scene_state.grid
        observer, target = states.get(command.actor_id), states.get(command.payload.target_id)
        if (
            definition.senses is None
            or not dict(definition.senses)["visual"]
            or command.payload.target_id not in locations
            or locations.get(command.actor_id) != locations.get(command.payload.target_id)
            or observer is None
            or target is None
            or observer.conditions
            or target.conditions
            or grid.default_lighting != "bright"
            or grid.lighting
            or grid.obscurement_cells
            or grid.wall_segments
            or grid.blocked_cells
            or grid.cover_cells
        ):
            raise StateMachineError(
                "unauthorized_target", "Target is outside the supported current perception scope"
            )
