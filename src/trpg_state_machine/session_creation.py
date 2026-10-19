"""Atomic fresh-session coordination; domain components interpret their schemas."""

import json
from dataclasses import asdict, replace
from uuid import uuid4

from .combat import DND_RULESET
from .errors import StateMachineError
from .models import CreateSessionRequest, CreationResult, PlayerSelection, TrustedContext
from .package import (
    SYNTHETIC_EVALUATION_RULESET,
    AdventurePackage,
    RulesetBinding,
    _creation_fingerprint,
    canonical_json,
)
from .session_access import _trusted
from .time_binding import TimeBinding


def _result(raw: str) -> CreationResult:
    data = json.loads(raw)
    data["ruleset_binding"] = RulesetBinding(**data["ruleset_binding"])
    return CreationResult(**data)


class SessionCreation:
    def __init__(
        self,
        store,
        runtime,
        world,
        characters,
        rules,
        combat,
        capture,
        effects,
        persistence,
        evaluator,
    ):
        self.store, self.runtime = store, runtime
        self.world, self.characters, self.rules, self.combat = world, characters, rules, combat
        self.capture, self.effects, self.persistence = capture, effects, persistence
        self.evaluator = evaluator

    @property
    def _conn(self):
        return self.store.connection

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

    def create(
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
        selected_time = (
            request.time_binding if request.time_binding is not None else package.time_binding
        )
        if selected_time is not None and type(selected_time) is not TimeBinding:
            raise StateMachineError("invalid_time_binding", "Expected typed TimeBinding")
        request_data = {
            "adventure_id": request.adventure_id,
            "package_version": request.package_version,
            "package_digest": request.package_digest,
            "players": [asdict(player)],
        }
        if selected_time is not None:
            request_data["time_binding"] = asdict(selected_time)
        fingerprint = _creation_fingerprint(request_data, package.ruleset_binding)
        with self.store.transaction():
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
        if selected_time is not None and any(a.npc_state is not None for a in package.actors):
            raise StateMachineError(
                "unsupported_time_consumer",
                "Configured NPC Runtime/OB/Gate require legacy time until reviewed second policies",
            )
        if package.ruleset_binding.ruleset_id == DND_RULESET:
            self.rules._verify_dnd_binding(package)
        if package.ruleset_binding == SYNTHETIC_EVALUATION_RULESET and (
            getattr(self.evaluator, "ruleset_binding", None) != package.ruleset_binding
        ):
            raise StateMachineError("ruleset_binding_mismatch", "Configured evaluator differs")
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
        with self.store.transaction(write=True):
            if replay := self._replay(context, request.command_id, fingerprint):
                return replay
            self.runtime.initialize(session_id, package, context, pc, pc_actor_id, rng_ref)
            self.world.facts.initialize_world(session_id, package)
            self.world.time.initialize(session_id, selected_time)
            self.world.scenes.initialize_session(session_id, pc_actor_id, pc, package)
            if package.combat_foundation_json is not None:
                initial = json.loads(package.combat_foundation_json)["snapshot"]
                from .combat import _remap

                cells = _remap(
                    initial["combat_state"]["actor_zone"], pc.pc_template_id, pc_actor_id
                )
                fresh = self._conn.execute(
                    "SELECT * FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                self.characters.initialize_rule_positions(
                    fresh, cells, initial["scene_state"]["scene_id"]
                )
            self.runtime.initialize_rng(
                session_id,
                rng_ref,
                synthetic=package.ruleset_binding == SYNTHETIC_EVALUATION_RULESET,
            )
            initial_row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if package.noncombat_foundation_json is not None:
                self.combat._initialize_noncombat(initial_row, package)
            self.world.facts.initialize_items(initial_row, package, self.rules.loader)
            self.world.facts.initialize_locks(initial_row, package)
            if package.portable_items:
                self.characters.rule_components(
                    session_id, set(self.characters.rule_actors(session_id))
                )
            self.world.scenes.validate_initialized(initial_row, package)
            self.world._assert_spatial_consistency(session_id, package)
            initial_events = self.effects._expand_events(
                initial_row,
                package,
                [
                    self.capture._capture_event(
                        session_id,
                        package,
                        "session.created",
                        {},
                        phase="session.after_initialization",
                    ),
                    self.capture._capture_event(
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
            self.persistence._append_events(
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
