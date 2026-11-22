"""Schedule elapsed-time Effects and release their concentration on the Session clock.

The caller admits an application and owns its shared UnitOfWork. This port stores
only RE's resolved effect and terminal removal policy, not a second rules clock.
"""

from dataclasses import asdict

from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_ruleset import RulesetBinding, RulesetBindingError
from dnd5e_engine.evaluation_spell_termination import plan_spell_dismissal
from dnd5e_engine.evaluation_world_effect import (
    WorldEffectApplicationRequest,
    plan_longstrider_application,
)

from .combat_movement import refresh_world_walk_expiry
from .errors import StateMachineError
from .package import canonical_json


class EffectTimeConsumer:
    """Adopt timed Effects, settle indexed deadlines, and release one caster's group."""

    def __init__(self, runtime, world):
        """Use the existing world owner, Store and runtime time coordinator."""
        self.world = world
        self.store = runtime.store
        self.owner = world.characters.effects

    @property
    def db(self):
        """Return the caller's transaction-bound SQLite connection."""
        return self.store.connection

    def apply_longstrider(self, sid, package, command_id, source_id, target_id):
        """Adopt RE's fixed passive effect and schedule its duration from current time.

        The authorized caller owns the write transaction and all command/actor
        admission. RE proposes a rule result; this method guards the one clock and
        persists effect plus deadline together before normal publication.
        """
        self.store.require_write()
        request = WorldEffectApplicationRequest(
            schema_version="engine-world-effect/1",
            session_id=sid,
            command_id=command_id,
            source_id=source_id,
            target_id=target_id,
            ruleset_binding=RulesetBinding(**asdict(package.ruleset_binding)),
        )
        result = plan_longstrider_application(request, loader=self.world.loader)
        try:
            result.verify_request(request)
        except ValueError as exc:
            raise StateMachineError("invalid_rule_result", "World effect proposal differs") from exc
        self.apply_verified(sid, result, spell_id="longstrider")
        return result

    def apply_verified(self, sid, proposal, *, spell_id):
        """Persist an admitted terminal effect at the current Session tick.

        The caller owns the write transaction. The effect table's primary key
        enforces absence of the full proposed identity at insertion.
        """
        self.store.require_write()
        if proposal.session_id != sid or proposal.expiry.action != "remove":
            raise StateMachineError("invalid_rule_result", "World effect proposal differs")
        deadline = self.world.time.seconds_deadline(sid, proposal.duration_seconds)
        self.owner.add_world_effect(sid, proposal, expires_tick=deadline, spell_id=spell_id)

    def spell_end_event(self, sid, package, proposal):
        """Capture the verified exact ending after the command's other effects."""
        application = proposal.expected
        target = application.effect.target_id
        return self.world._capture_event(
            sid,
            package,
            "effect.spell_ended",
            {
                "identity": application.identity,
                "cast_command_id": application.cast_command_id,
                "actor_id": target,
                "source_actor_id": application.caster_id,
                "reason": proposal.reason,
                "scene_id": self._actor_scene(sid, target),
            },
            phase="effects.after_spell_end",
        )

    def dismiss(self, row, package, actor_id, operation):
        """End an authorized original cast without payment or a second transaction."""
        self.store.require_write()
        sid = row["session_id"]
        identity = (operation.target_id, operation.effect_id, operation.origin)
        application = self.owner.spell_application(sid, identity)
        if (
            application is None
            or application.cast_command_id != operation.cast_command_id
            or application.expires_tick <= self.world.facts.time_state(sid)[1]
        ):
            raise StateMachineError("spell.application", "The selected spell is not ongoing")
        characters = self.world.characters
        life = characters.life(sid, actor_id)
        if life is None or not life["is_alive"]:
            raise StateMachineError("spell.dismiss", "Dismissal requires a living caster")
        conditions = characters.effects.actor_conditions(
            sid, actor_id, characters.conditions(sid, actor_id)
        )
        try:
            proposal = plan_spell_dismissal(
                application,
                actor_id=actor_id,
                actor_conditions=tuple(condition["condition"] for condition in conditions),
                binding=RulesetBinding(**asdict(package.ruleset_binding)),
                loader=self.world.loader,
            )
        except RulesetBindingError as exc:
            raise StateMachineError("ruleset_binding_mismatch", str(exc)) from exc
        except ValueError as exc:
            raise StateMachineError("spell.dismiss", str(exc)) from exc
        self.owner.apply_spell_end(sid, proposal)
        return self.spell_end_event(sid, package, proposal)

    def end_world_concentration(self, sid, package, caster_id, reason):
        """End the caster's current elapsed-time group inside the caller's write scope."""
        self.store.require_write()
        group = self.owner.concentration_group(sid, caster_id)
        if group is None:
            return []
        if group.rounds_remaining:
            raise StateMachineError("spell.concentration", "Combat concentration is active")
        if group.spatial_member is not None:
            return [self.world.magic.end_event(sid, package, group.spatial_member, reason)]
        self.owner.remove_concentration(sid, group.concentration_id)
        return [
            self.world._capture_event(
                sid,
                package,
                "effect.concentration_ended",
                {
                    "identity": identity,
                    "actor_id": identity[0],
                    "source_actor_id": caster_id,
                    "concentration_id": group.concentration_id,
                    "reason": reason,
                    "scene_id": self.world._actor_position(sid, identity[0]).scene_id,
                },
                phase="effects.after_concentration_end",
            )
            for identity in group.actor_members
        ]

    def next_boundary(self, session_id, package):
        """Find the next indexed deadline over both actor and spatial Effects."""
        actor = self.db.execute(
            "SELECT min(expires_tick) FROM ongoing_effects "
            "WHERE session_id=? AND expires_tick IS NOT NULL",
            (session_id,),
        ).fetchone()[0]
        spatial = self.owner.spatial_lights.next_deadline(session_id)
        return min((value for value in (actor, spatial) if value is not None), default=None)

    def _due_actor(self, sid, tick):
        """Read the first deadline, using effect-list order for ties on one actor."""
        return self.db.execute(
            "SELECT target_id,effect_id,origin,expires_tick,payload_json "
            "FROM ongoing_effects WHERE session_id=? AND expires_tick<=? "
            "ORDER BY expires_tick,target_id,ordinal LIMIT 1",
            (sid, tick),
        ).fetchone()

    def due_attached_to(self, sid, tick, object_id):
        """Return the event route for the next expiry when it is on this exact item.

        The actor/spatial arbitration matches settle_one, including its actor
        tie preference. The caller checks Package consequences before ordering.
        """
        actor = self._due_actor(sid, tick)
        spatial = self.owner.spatial_lights.due(sid, tick)
        if (
            spatial is None
            or spatial["expires_tick"] != tick
            or spatial["object_id"] != object_id
            or (actor is not None and actor["expires_tick"] <= spatial["expires_tick"])
        ):
            return None
        placement = self.world.facts.placement(sid, object_id)
        return "effect.spatial_light_ended", placement["scene_id"]

    def _actor_scene(self, sid, actor_id):
        """Read the scene used to route an actor effect’s expiry event."""
        location = self.db.execute(
            "SELECT current_scene_id FROM actors WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()
        return location[0] if location else None

    def next_actor_expiry_scene(self, sid, tick):
        """Return the event scene only if the next due item is an actor expiry at this tick."""
        due = self._due_actor(sid, tick)
        if due is None or due["expires_tick"] != tick:
            return None
        spatial_tick = self.owner.spatial_lights.next_deadline(sid)
        if spatial_tick is not None and spatial_tick < tick:
            return None
        return self._actor_scene(sid, due["target_id"])

    def settle_one(self, row, package, command_id):
        """Consume one due exact identity and return its event in the shared write."""
        from .time_coordination import DueResult

        self.store.require_write()
        sid = row["session_id"]
        tick = self.world.facts.time_state(sid)[1]
        due = self._due_actor(sid, tick)
        spatial = self.owner.spatial_lights.due(sid, tick)
        if spatial is not None and (due is None or spatial["expires_tick"] < due["expires_tick"]):
            event = self.world.magic.end_event(sid, package, spatial, "duration")
            return DueResult((event,))
        if due is None:
            raise StateMachineError("invalid_time_consumer", "Missing due effect boundary")
        identity = (due["target_id"], due["effect_id"], due["origin"])
        expired = EffectState.model_validate_json(
            canonical_json(
                {
                    "target_id": identity[0],
                    "id": identity[1],
                    "origin": identity[2],
                    **self.store.memory.json(due["payload_json"]),
                }
            )
        )
        self.owner.expire_world_effect(sid, identity, due["expires_tick"])
        refresh_world_walk_expiry(self.world, sid, expired)
        event = self.world._capture_event(
            sid,
            package,
            "effect.expired",
            {
                "identity": identity,
                "actor_id": identity[0],
                "scene_id": self._actor_scene(sid, identity[0]),
                "boundary_tick": due["expires_tick"],
            },
            phase="effects.after_expiry",
        )
        return DueResult((event,))
