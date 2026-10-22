"""Session access, Actor control and trusted special grants."""

import json
import sqlite3

from dnd5e_engine.evaluation_contracts import CheckPayload, CombatClosePayload, CombatIntentPayload

from ._operations import (
    DropItem,
    EnterScene,
    LockDoor,
    MoveActor,
    PickupItem,
    PlaceObject,
    SetObjectState,
    StepActor,
    TraversePassage,
    UnlockDoor,
)
from .environment_commands import ENVIRONMENT_OPERATIONS
from .errors import StateMachineError
from .game_time import AdvanceGameTime
from .models import TrustedContext
from .navigation_models import NAVIGATION_OPERATIONS
from .npc_outputs import _handoff_command
from .package import AdventurePackage, RulesetBinding
from .perception import NPCContext
from .time_coordination import AdvanceSessionTime, SetTimeRunning
from .world_commands import WORLD_CAPABILITY_OPERATIONS, RepositionObject


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
        type(context.can_place_world_objects) is not bool
        or type(context.can_adjudicate_checks) is not bool
        or type(context.can_advance_time) is not bool
        or type(context.can_manage_environment) is not bool
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
        if type(payload) in ENVIRONMENT_OPERATIONS and not context.can_manage_environment:
            raise StateMachineError("unauthorized", "Trusted environment management grant required")
        if type(payload) is RepositionObject and not context.can_place_world_objects:
            raise StateMachineError("unauthorized", "Trusted GM placement grant required")
        if (
            type(payload) in (AdvanceGameTime, AdvanceSessionTime, SetTimeRunning)
            and not context.can_advance_time
        ):
            raise StateMachineError("unauthorized", "Trusted game-time grant required")
        if isinstance(payload, CheckPayload) and not context.can_adjudicate_checks:
            raise StateMachineError("unauthorized", "Trusted GM adjudication grant required")
        if (
            (type(payload) in WORLD_CAPABILITY_OPERATIONS and actor_id != row["pc_actor_id"])
            or isinstance(payload, MoveActor)
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
                        PickupItem,
                        DropItem,
                        UnlockDoor,
                        LockDoor,
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
        if type(payload) in (*WORLD_CAPABILITY_OPERATIONS, *ENVIRONMENT_OPERATIONS) or isinstance(
            payload,
            (
                EnterScene,
                MoveActor,
                SetObjectState,
                AdvanceGameTime,
                StepActor,
                TraversePassage,
                PlaceObject,
                PickupItem,
                DropItem,
                UnlockDoor,
                LockDoor,
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
        self.require_legacy_npc_time(session_id)
        return context.observer_id

    def require_legacy_npc_time(self, sid):
        if self._conn.execute(
            "SELECT 1 FROM world_time_bindings WHERE session_id=?", (sid,)
        ).fetchone():
            raise StateMachineError(
                "unsupported_time_consumer", "NPC OB/Gate second policies are not configured"
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
        from .domain_facts import WorldFacts

        definition = WorldFacts(self.store).instances.actor(command.session_id, command.actor_id)
        locations = dict(
            self._conn.execute(
                "SELECT actor_id,current_scene_id FROM actors "
                "WHERE session_id=? AND actor_id IN (?,?)",
                (command.session_id, command.actor_id, command.payload.target_id),
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
