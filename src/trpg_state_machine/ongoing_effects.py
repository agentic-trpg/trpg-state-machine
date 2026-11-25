"""Persist ongoing effect instances and concentration ownership in keyed facts.

This component records adopted facts only. Rule creation, expiry, and progression
remain with the bound Rule Engine; every mutation uses the caller's UnitOfWork.
"""

from dataclasses import dataclass

from dnd5e_engine.effect_lifecycle import conditions_after_effect_application
from dnd5e_engine.evaluation_aid import reviewed_hp_bonus
from dnd5e_engine.evaluation_bardic_check import BARDIC_INSPIRATION_DIE_SIDES
from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_region_effect import (
    is_fixed_region_effect,
    matches_region_effect_statuses,
    plan_region_effect_transition,
    region_conditions_preserve_movement,
    region_conditions_preserve_visibility,
    region_effect_identity,
)
from dnd5e_engine.evaluation_ruleset import RulesetBindingError
from dnd5e_engine.evaluation_spell_termination import (
    SpellApplicationFacts,
    spell_preserves_observer_sight,
)
from dnd5e_engine.evaluation_state import ConditionLink, LifecycleRecord
from dnd5e_engine.evaluation_timed_spells import is_reviewed_longstrider_application
from dnd5e_engine.types.conditions import ActiveCondition

from .errors import StateMachineError
from .package import canonical_json
from .runtime_instances import instance_read
from .scoped_reads import scoped_read
from .shield_adoption import is_retained_shield
from .spatial_light_effects import SpatialLightEffects
from .spell_cast_activity import CASTING_FOCUS_PREFIX, casting_focus_id


def _fixed_region_effect(row, statuses):
    """Check source-scoped lineage against the stored metadata and RE identity."""
    return matches_region_effect_statuses(row["effect_id"], row["origin"], statuses) and all(
        row[key] is None for key in ("concentration_id", "lifecycle_json", "expires_tick")
    )


@dataclass(frozen=True)
class ConcentrationGroup:
    """Fix one caster's durable group and the identities of its actual members."""

    concentration_id: str
    caster_id: str
    effect_id: str
    rounds_remaining: int
    actor_members: tuple[tuple[str, str, str], ...]
    spatial_member: dict | None
    casting_activity_id: str | None = None
    visual_source: dict | None = None


BARDIC_INSPIRATION_EFFECT_ID = "effect:inspired"


@dataclass(frozen=True, slots=True)
class BardicInspirationGrant:
    """Identity, size and deadline fixed when an Inspired effect is issued."""

    origin: str
    grant_id: str
    die_sides: int
    expires_tick: int


class OngoingEffects:
    """Own target effects and concentration groups in the existing Store."""

    def __init__(self, store):
        """Bind the domain owner to the shared transaction and RAM scope."""
        self.store = store
        self.spatial_lights = SpatialLightEffects(self)

    @property
    def _conn(self):
        """Return the Store's scoped connection."""
        return self.store.connection

    def initialize(self, sid, effects=()):
        """Adopt initial target effects once in the same component write scope."""
        self.store.require_write()
        for ordinal, effect in enumerate(effects):
            self.add_effect(sid, effect, ordinal=ordinal)

    def create_concentration(
        self, sid, concentration_id, caster_id, effect_id, rounds_remaining, members
    ):
        """Create a nonempty group and every identified member in one savepoint."""
        self.store.require_write()
        members = tuple(members)
        if not members:
            raise StateMachineError("invalid_effect", "Concentration needs a target effect")
        with self.store.savepoint():
            next_ordinal = self._conn.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM ongoing_effects WHERE session_id=?",
                (sid,),
            ).fetchone()[0]
            self._conn.execute(
                "INSERT INTO effect_concentrations VALUES (?,?,?,?,?)",
                (sid, concentration_id, caster_id, effect_id, rounds_remaining),
            )
            for chain_ordinal, member in enumerate(members):
                self.add_effect(
                    sid,
                    member["effect"],
                    ordinal=member.get("ordinal", next_ordinal + chain_ordinal),
                    concentration_id=concentration_id,
                    chain_ordinal=chain_ordinal,
                    conditions=member.get("conditions"),
                    lifecycle=member.get("lifecycle"),
                    expires_tick=member.get("expires_tick"),
                    cast_by=member.get("cast_by"),
                    cast_spell_id=member.get("cast_spell_id"),
                    cast_command_id=member.get("cast_command_id"),
                    cast_slot_level=member.get("cast_slot_level"),
                )

    def create_casting_focus(self, sid, caster_id, activity_id):
        """Reserve the one concentration slot for an admitted casting Activity."""
        self.store.require_write()
        identity = casting_focus_id(activity_id)
        self._conn.execute(
            "INSERT INTO effect_concentrations VALUES (?,?,?,?,0)",
            (sid, identity, caster_id, identity),
        )
        return identity

    def add_effect(
        self,
        sid,
        effect,
        *,
        ordinal=None,
        concentration_id=None,
        chain_ordinal=None,
        conditions=None,
        lifecycle=None,
        expires_tick=None,
        cast_by=None,
        cast_spell_id=None,
        cast_command_id=None,
        cast_slot_level=None,
    ):
        """Persist one target effect and its links, assigning an ordinal when omitted."""
        self.store.require_write()
        typed = EffectState.model_validate_json(canonical_json(effect)).model_dump(mode="json")
        target, effect_id, origin = (typed.pop(key) for key in ("target_id", "id", "origin"))
        identity = (target, effect_id, origin)
        if conditions is not None:
            conditions = ConditionLink.model_validate_json(
                canonical_json({"identity": identity, "statuses": conditions})
            ).model_dump(mode="json")["statuses"]
        if lifecycle is not None:
            lifecycle = LifecycleRecord.model_validate_json(
                canonical_json({"identity": identity, "state": lifecycle})
            ).model_dump(mode="json")["state"]
        if ordinal is None:
            ordinal = self._conn.execute(
                "SELECT coalesce(max(ordinal)+1,0) FROM ongoing_effects WHERE session_id=?",
                (sid,),
            ).fetchone()[0]
        self._conn.execute(
            "INSERT INTO ongoing_effects(session_id,target_id,effect_id,origin,ordinal,"
            "payload_json,concentration_id,chain_ordinal,conditions_json,"
            "lifecycle_json,expires_tick,cast_by,cast_spell_id,cast_command_id,cast_slot_level) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                target,
                effect_id,
                origin,
                ordinal,
                canonical_json(typed),
                concentration_id,
                chain_ordinal,
                canonical_json(conditions) if conditions is not None else None,
                canonical_json(lifecycle) if lifecycle is not None else None,
                expires_tick,
                cast_by,
                cast_spell_id,
                cast_command_id,
                cast_slot_level,
            ),
        )

    def add_world_effect(self, sid, proposal, *, expires_tick, spell_id, cast_slot_level=None):
        """Persist a world effect, original cast and any one-member concentration."""
        entry = {
            "effect": proposal.effect.model_dump(mode="json"),
            "expires_tick": expires_tick,
            "conditions": sorted(proposal.effect.statuses) or None,
            "cast_by": proposal.source_id,
            "cast_spell_id": spell_id,
            "cast_command_id": proposal.command_id,
            "cast_slot_level": cast_slot_level,
        }
        if proposal.concentration:
            self.create_concentration(
                sid,
                f"actor:{proposal.command_id}",
                proposal.source_id,
                proposal.effect.id,
                0,
                (entry,),
            )
        else:
            self.add_effect(sid, **entry)

    def add_visual_source(self, sid, command_id, proposal, *, expires_tick):
        """Persist one verified visual source and its selected targets atomically.

        The source survives an empty target set, retaining concentration and
        expiry. Target placement stays authoritative in World after this write.
        """
        self.store.require_write()
        source_id = f"visual:{command_id}"
        concentration = source_id if proposal.concentration else None
        if concentration is not None:
            self._conn.execute(
                "INSERT INTO effect_concentrations VALUES (?,?,?,?,0)",
                (sid, concentration, proposal.source_id, source_id),
            )
        self._conn.execute(
            "INSERT INTO visual_effect_sources VALUES (?,?,?,?,?,?,?,?)",
            (
                sid,
                source_id,
                "spell",
                proposal.source_id,
                proposal.spell_id,
                proposal.spell_source_id,
                expires_tick,
                concentration,
            ),
        )
        for target in proposal.targets:
            table, column = (
                ("mechanical_actors", "actor_id")
                if target.target_kind == "creature"
                else ("objects", "object_id")
            )
            if (
                self._conn.execute(
                    f"SELECT 1 FROM {table} WHERE session_id=? AND {column}=?",
                    (sid, target.target_id),
                ).fetchone()
                is None
            ):
                raise StateMachineError("invalid_target", "Visual effect target disappeared")
            self._conn.execute(
                "INSERT INTO target_visual_effects VALUES (?,?,?,?,?,?,?,?)",
                (
                    sid,
                    "outlined",
                    source_id,
                    target.target_kind,
                    target.target_id,
                    target.dim_radius_ft,
                    int(target.suppresses_invisible),
                    int(target.visible_attack_advantage),
                ),
            )
        return source_id

    def remove_visual_source(self, sid, source_id, *, expires_tick=None):
        """Remove one expected source and every target before releasing its group."""
        self.store.require_write()
        source = self._conn.execute(
            "SELECT * FROM visual_effect_sources WHERE session_id=? AND source_id=?",
            (sid, source_id),
        ).fetchone()
        if source is None or (expires_tick is not None and source["expires_tick"] != expires_tick):
            raise StateMachineError("invalid_effect", "Visual source identity or deadline changed")
        self._conn.execute(
            "DELETE FROM target_visual_effects WHERE session_id=? AND source_id=?",
            (sid, source_id),
        )
        self._conn.execute(
            "DELETE FROM visual_effect_sources WHERE session_id=? AND source_id=?",
            (sid, source_id),
        )
        if source["concentration_id"] is not None:
            self._conn.execute(
                "DELETE FROM effect_concentrations WHERE session_id=? AND concentration_id=?",
                (sid, source["concentration_id"]),
            )
        return source

    def _spell_application(self, row):
        """Project the persisted original cast and active effect from one owner row."""
        if row is None or row["cast_by"] is None:
            return None
        return SpellApplicationFacts(
            effect=EffectState.model_validate_json(
                canonical_json(
                    {
                        "target_id": row["target_id"],
                        "id": row["effect_id"],
                        "origin": row["origin"],
                        **self.store.memory.json(row["payload_json"]),
                    }
                )
            ),
            caster_id=row["cast_by"],
            spell_id=row["cast_spell_id"],
            cast_command_id=row["cast_command_id"],
            expires_tick=row["expires_tick"],
            concentration_id=row["concentration_id"],
            cast_slot_level=row["cast_slot_level"],
        )

    def spell_application(self, sid, identity):
        """Read one cast application by its full key."""
        return self._spell_application(
            self._conn.execute(
                "SELECT * FROM ongoing_effects WHERE session_id=? AND target_id=? "
                "AND effect_id=? AND origin=?",
                (sid, *identity),
            ).fetchone()
        )

    def grants_passive_detection(self, sid, actor_id, spell_id):
        """Find an active self-cast detection application through the target key."""
        return (
            self._conn.execute(
                "SELECT 1 FROM ongoing_effects WHERE session_id=? AND target_id=? "
                "AND cast_spell_id=? AND cast_by=target_id AND concentration_id IS NOT NULL "
                "AND json_extract(payload_json,'$.disabled')=0 LIMIT 1",
                (sid, actor_id, spell_id),
            ).fetchone()
            is not None
        )

    def has_detectable_spell(self, sid, actor_id, observer, granting_spell):
        """Find active spell lineage, excluding the one powering the observer's detection."""
        return (
            self._conn.execute(
                "SELECT 1 FROM ongoing_effects WHERE session_id=? AND target_id=? "
                "AND cast_spell_id IS NOT NULL AND json_extract(payload_json,'$.disabled')=0 "
                "AND NOT (target_id=? AND cast_by=target_id AND cast_spell_id=?) LIMIT 1",
                (sid, actor_id, observer, granting_spell),
            ).fetchone()
            is not None
        )

    def target_spell_applications(self, sid, target_id, need):
        """Read declared application facts in the owning target effect-list order."""
        rows = self._conn.execute(
            "SELECT * FROM ongoing_effects WHERE session_id=? AND target_id=? "
            "AND effect_id=? AND cast_spell_id=?",
            (sid, target_id, need.effect_id, need.spell_id),
        )
        # SQL ORDER BY ordinal can choose the session-wide ordinal index.
        return tuple(
            self._spell_application(row) for row in sorted(rows, key=lambda r: r["ordinal"])
        )

    @scoped_read
    def all_target_spell_applications(self, sid, target_id):
        """Read cast applications on one selected creature in owner order."""
        return tuple(
            application
            for row in sorted(self._target_rows(sid, target_id), key=lambda r: r["ordinal"])
            if (application := self._spell_application(row)) is not None
        )

    def apply_spell_end(self, sid, proposal):
        """Recheck one application before removing it and preserving group siblings."""
        self.store.require_write()
        expected = proposal.expected
        if self.spell_application(sid, expected.identity) != expected:
            raise StateMachineError("version_conflict", "Spell application changed")
        if expected.concentration_id is not None:
            group = self.concentration_group(sid, expected.caster_id)
            if (
                group is None
                or group.concentration_id != expected.concentration_id
                or group.effect_id != expected.effect.id
                or group.rounds_remaining
                # Dispel targets one creature's member; another target in the
                # same cast keeps concentrating until its own member ends.
                or (
                    expected.identity not in group.actor_members
                    if proposal.reason == "dispelled"
                    else group.actor_members != (expected.identity,)
                )
                or group.spatial_member is not None
                or group.casting_activity_id is not None
            ):
                raise StateMachineError("version_conflict", "Spell concentration changed")
        self.remove_effect(sid, *expected.identity)

    def apply_rest_expiry(self, sid, expected):
        """Remove one exact recipient-rest application under its current owner guard."""
        self.store.require_write()
        identity = (expected.target_id, expected.id, expected.origin)
        payload = expected.model_dump(mode="json", exclude={"target_id", "id", "origin"})
        changed = self._conn.execute(
            "DELETE FROM ongoing_effects WHERE session_id=? AND target_id=? "
            "AND effect_id=? AND origin=? AND cast_by IS NOT NULL "
            "AND expires_tick IS NULL AND concentration_id IS NULL AND payload_json=?",
            (sid, *identity, canonical_json(payload)),
        ).rowcount
        if changed != 1:
            raise StateMachineError("version_conflict", "Rest spell application changed")

    def apply_combat_creation(self, sid, operation):
        """Insert one verified effect, clock and lineage under the keyed absence guard."""
        self.store.require_write()
        if (
            self._conn.execute(
                "SELECT 1 FROM ongoing_effects WHERE session_id=? AND target_id=? "
                "AND effect_id=? AND origin=?",
                (sid, *operation.identity),
            ).fetchone()
            is not None
        ):
            raise StateMachineError("invalid_effect", "Combat effect already exists")
        self.add_effect(
            sid,
            operation.effect.model_dump(mode="json"),
            conditions=operation.lineage.statuses,
            lifecycle=operation.lifecycle.model_dump(mode="json")["state"],
        )

    def apply_shield(self, sid, transition):
        """Apply the exact guarded Shield effect through the existing target Effects owner."""
        self.store.require_write()
        prior, value = transition.expected, transition.value
        if prior is None:
            self.add_effect(
                sid,
                value.effect.model_dump(mode="json"),
                conditions=value.lineage.statuses,
            )
        elif value is None:
            self.remove_effect(sid, *prior.expiry_identity)
        # A retained Shield's response is derived from the owner's adopted life state.

    def apply_follow_up(self, sid, operation):
        """Create an independent target effect after its exact parent has been removed."""
        self.store.require_write()
        for identity in (operation.ended_identity, operation.lifecycle.identity):
            if (
                self._conn.execute(
                    "SELECT 1 FROM ongoing_effects WHERE session_id=? AND target_id=? "
                    "AND effect_id=? AND origin=?",
                    (sid, *identity),
                ).fetchone()
                is not None
            ):
                raise StateMachineError("invalid_effect", "Follow-up effect pre-value changed")
        self.add_effect(
            sid,
            operation.effect.model_dump(mode="json"),
            conditions=operation.lineage.statuses,
            lifecycle=operation.lifecycle.model_dump(mode="json")["state"],
        )

    def transition_region_effect(
        self,
        sid,
        actor_id,
        scene_id,
        source_id,
        *,
        rule,
        entering,
        condition_immunities,
        ruleset_binding,
        loader,
    ):
        """Adopt a bound fixed-area transition against the full identity and current row."""
        self.store.require_write()
        try:
            identity = region_effect_identity(scene_id, source_id, actor_id, rule)
        except ValueError as exc:
            raise StateMachineError("invalid_environment", str(exc)) from exc
        stored = self._conn.execute(
            "SELECT effect_id,origin,conditions_json,concentration_id,lifecycle_json,expires_tick "
            "FROM ongoing_effects WHERE session_id=? AND target_id=? AND effect_id=? AND origin=?",
            (sid, *identity),
        ).fetchone()
        present = stored is not None
        if present and not _fixed_region_effect(
            stored,
            self.store.memory.json(stored["conditions_json"]) if stored["conditions_json"] else (),
        ):
            raise StateMachineError("invalid_mechanical_state", "Region effect has invalid links")
        try:
            transition = plan_region_effect_transition(
                ruleset_binding=ruleset_binding,
                scene_id=scene_id,
                source_id=source_id,
                target_id=actor_id,
                entering=entering,
                present=present,
                condition_immunities=condition_immunities,
                loader=loader,
                rule=rule,
            )
        except RulesetBindingError as exc:
            raise StateMachineError("ruleset_binding_mismatch", str(exc)) from exc
        except ValueError as exc:
            raise StateMachineError("invalid_environment", str(exc)) from exc
        valid_operations = {"none"}
        if entering and not present:
            valid_operations.add("add")
        elif not entering and present:
            valid_operations = {"remove"}
        if transition.identity != identity or transition.operation not in valid_operations:
            raise StateMachineError(
                "invalid_rule_result", "Region transition conflicts with its source"
            )
        effect = transition.effect
        if transition.operation == "add":
            if (
                not isinstance(effect, EffectState)
                or (effect.target_id, effect.id, effect.origin) != identity
            ):
                raise StateMachineError("invalid_rule_result", "Region effect identity differs")
            if not is_fixed_region_effect(effect):
                raise StateMachineError("invalid_rule_result", "Unsupported region effect profile")
            payload = effect.model_dump(mode="json")
            self.add_effect(sid, payload, conditions=payload["statuses"] or None)
        elif effect is not None:
            raise StateMachineError(
                "invalid_rule_result", "Region transition has unexpected effect"
            )
        elif transition.operation == "remove":
            self.remove_effect(sid, *identity)

    def expire_world_effect(self, sid, identity, expected_tick):
        """Remove an exact due effect and release its group if it was the last member."""
        self.store.require_write()
        target, effect_id, origin = identity
        deleted = self._conn.execute(
            "DELETE FROM ongoing_effects WHERE session_id=? AND target_id=? "
            "AND effect_id=? AND origin=? AND expires_tick=? RETURNING concentration_id",
            (sid, target, effect_id, origin, expected_tick),
        ).fetchone()
        if deleted is None:
            raise StateMachineError("invalid_effect", "Effect deadline or identity changed")
        self._remove_empty_concentration(sid, deleted[0])

    def remove_effect(self, sid, target_id, effect_id, origin):
        """Remove exactly one target effect, retaining its concentration siblings."""
        self.store.require_write()
        old = self._conn.execute(
            "SELECT concentration_id FROM ongoing_effects WHERE session_id=? AND target_id=? "
            "AND effect_id=? AND origin=?",
            (sid, target_id, effect_id, origin),
        ).fetchone()
        if old is None:
            raise StateMachineError("invalid_effect", "Target effect is missing")
        result = self._conn.execute(
            "DELETE FROM ongoing_effects WHERE session_id=? AND target_id=? "
            "AND effect_id=? AND origin=?",
            (sid, target_id, effect_id, origin),
        )
        if result.rowcount != 1:
            raise StateMachineError("invalid_effect", "Target effect is missing")
        self._remove_empty_concentration(sid, old[0])

    def _remove_empty_concentration(self, sid, group_id):
        """Delete the exact group only after its last actor member is removed."""
        if (
            group_id is not None
            and self._conn.execute(
                "SELECT 1 FROM ongoing_effects WHERE session_id=? AND concentration_id=?",
                (sid, group_id),
            ).fetchone()
            is None
        ):
            self._conn.execute(
                "DELETE FROM effect_concentrations WHERE session_id=? AND concentration_id=?",
                (sid, group_id),
            )

    def remove_concentration(self, sid, concentration_id):
        """Remove one concentration group and only its linked target effects."""
        self.store.require_write()
        for (source_id,) in self._conn.execute(
            "SELECT source_id FROM visual_effect_sources WHERE session_id=? AND concentration_id=?",
            (sid, concentration_id),
        ):
            self.remove_visual_source(sid, source_id)
            return
        self._conn.execute(
            "DELETE FROM ongoing_effects WHERE session_id=? AND concentration_id=?",
            (sid, concentration_id),
        )
        result = self._conn.execute(
            "DELETE FROM effect_concentrations WHERE session_id=? AND concentration_id=?",
            (sid, concentration_id),
        )
        if result.rowcount != 1:
            raise StateMachineError("invalid_effect", "Concentration is missing")

    def apply_lifecycle(self, sid, operation):
        """Apply one guarded RE effect and clock transition in the caller's write scope.

        Group removal follows the complete set of member transitions in the same
        UnitOfWork; a last-member deletion does not silently delete its group.
        """
        self.store.require_write()
        target, effect_id, origin = operation.identity
        row = self._conn.execute(
            "SELECT * FROM ongoing_effects WHERE session_id=? AND target_id=? "
            "AND effect_id=? AND origin=?",
            (sid, target, effect_id, origin),
        ).fetchone()
        if row is None:
            raise StateMachineError("invalid_effect", "Effect transition lost its source")
        current = {
            "id": row["effect_id"],
            "origin": row["origin"],
            "target_id": row["target_id"],
            **self.store.memory.json(row["payload_json"]),
        }
        if (
            current != operation.expected_effect.model_dump(mode="json")
            or (self.store.memory.json(row["conditions_json"]) if row["conditions_json"] else [])
            != list(operation.expected_lineage.statuses)
            or (self.store.memory.json(row["lifecycle_json"]) if row["lifecycle_json"] else None)
            != (
                operation.expected_lifecycle.model_dump(mode="json")["state"]
                if operation.expected_lifecycle
                else None
            )
        ):
            raise StateMachineError("invalid_effect", "Effect transition pre-value changed")
        if operation.effect is None:
            self._conn.execute(
                "DELETE FROM ongoing_effects WHERE session_id=? AND target_id=? "
                "AND effect_id=? AND origin=?",
                (sid, target, effect_id, origin),
            )
        else:
            self._conn.execute(
                "UPDATE ongoing_effects SET lifecycle_json=? WHERE session_id=? "
                "AND target_id=? AND effect_id=? AND origin=?",
                (
                    canonical_json(operation.lifecycle.model_dump(mode="json")["state"]),
                    sid,
                    target,
                    effect_id,
                    origin,
                ),
            )

    def apply_concentration(self, sid, operation):
        """Guard the caster's one group and adopt RE's retained duration or removal."""
        self.store.require_write()
        self.assert_concentration(sid, operation, after_members=True)
        row = self._conn.execute(
            "SELECT * FROM effect_concentrations WHERE session_id=? AND caster_id=?",
            (sid, operation.source_id),
        ).fetchone()
        if operation.chain:
            self._conn.execute(
                "UPDATE effect_concentrations SET rounds_remaining=? WHERE session_id=? "
                "AND concentration_id=?",
                (operation.rounds, sid, row["concentration_id"]),
            )
        else:
            self._conn.execute(
                "DELETE FROM effect_concentrations WHERE session_id=? AND concentration_id=?",
                (sid, row["concentration_id"]),
            )

    def assert_concentration(self, sid, operation, *, after_members=False):
        """Compare one caster's persisted group and ordered member identities."""
        row = self._conn.execute(
            "SELECT * FROM effect_concentrations WHERE session_id=? AND caster_id=?",
            (sid, operation.source_id),
        ).fetchone()
        if row is None or (
            row["effect_id"] != operation.expected_effect_id
            or row["rounds_remaining"] != operation.expected_rounds
        ):
            raise StateMachineError("invalid_effect", "Concentration pre-value changed")
        current_chain = tuple(
            (member["target_id"], member["effect_id"], member["origin"])
            for member in self._conn.execute(
                "SELECT target_id,effect_id,origin FROM ongoing_effects WHERE session_id=? "
                "AND concentration_id=? ORDER BY chain_ordinal",
                (sid, row["concentration_id"]),
            )
        )
        if current_chain != (operation.chain if after_members else operation.expected_chain):
            raise StateMachineError("invalid_effect", "Concentration members changed")

    @scoped_read
    def _target_rows(self, sid, target_id):
        """Reuse indexed target facts across actor, binding and combat projections."""
        return tuple(
            self._conn.execute(
                "SELECT * FROM ongoing_effects WHERE session_id=? AND target_id=?",
                (sid, target_id),
            )
        )

    @scoped_read
    def for_targets(self, sid, target_ids=None):
        """Project exact RE effects from selected target rows, in ingress order."""
        if target_ids is None:
            rows = self._conn.execute(
                "SELECT * FROM ongoing_effects WHERE session_id=? ORDER BY ordinal", (sid,)
            )
        else:
            rows = sorted(
                (row for target in target_ids for row in self._target_rows(sid, target)),
                key=lambda row: row["ordinal"],
            )
        return [
            {
                "id": row["effect_id"],
                "origin": row["origin"],
                "target_id": row["target_id"],
                **self.store.memory.json(row["payload_json"]),
            }
            for row in rows
        ]

    @scoped_read
    def for_target_effect(self, sid, target_id, effect_id):
        """Read one target and effect group through the composite identity index."""
        rows = self._conn.execute(
            "SELECT * FROM ongoing_effects WHERE session_id=? AND target_id=? "
            "AND effect_id=? ORDER BY ordinal",
            (sid, target_id, effect_id),
        )
        return [
            {
                "id": row["effect_id"],
                "origin": row["origin"],
                "target_id": row["target_id"],
                **self.store.memory.json(row["payload_json"]),
            }
            for row in rows
        ]

    def held_bardic_inspiration(self, sid, target_id, tick):
        """Read one active timed Inspired grant by holder and effect ID.

        A standalone grant carries an immutable issuance ID and die size in
        its effect flags. An ambiguous, disabled or expired effect provides no
        die. The Session clock and this Effects row remain the only owners.
        """
        rows = self._conn.execute(
            "SELECT origin,expires_tick,payload_json,concentration_id,"
            "lifecycle_json,conditions_json "
            "FROM ongoing_effects WHERE session_id=? AND target_id=? AND effect_id=? LIMIT 2",
            (sid, target_id, BARDIC_INSPIRATION_EFFECT_ID),
        ).fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        expiry = row["expires_tick"]
        prefix = "cast:inspired:"
        if (
            expiry is None
            or tick >= expiry
            or not row["origin"].startswith(prefix)
            or row["origin"] == prefix
            or any(
                row[field] is not None
                for field in ("concentration_id", "lifecycle_json", "conditions_json")
            )
        ):
            return None
        effect = self.store.memory.json(row["payload_json"])
        grant = effect["flags"].get("bardic_inspiration")
        if (
            effect["disabled"]
            or effect["transfer"]
            or effect["changes"]
            or effect["statuses"]
            or effect["end_effects"]
            or effect["lifecycle"] is not None
            or effect["action_policy"] is not None
            or type(grant) is not dict
            or type(grant.get("grant_id")) is not str
            or not grant["grant_id"]
            or type(grant.get("die_sides")) is not int
            or grant["die_sides"] not in BARDIC_INSPIRATION_DIE_SIDES
        ):
            return None
        return BardicInspirationGrant(row["origin"], grant["grant_id"], grant["die_sides"], expiry)

    @scoped_read
    def visible_for_target(self, sid, target_id):
        """Project active effects, including the exact shield linked by its reaction."""
        rows = sorted(self._target_rows(sid, target_id), key=lambda row: row["ordinal"])
        visible = []
        for row in rows:
            effect = {
                "id": row["effect_id"],
                "origin": row["origin"],
                "target_id": row["target_id"],
                **self.store.memory.json(row["payload_json"]),
            }
            if not effect["disabled"] or (
                effect["id"] == "effect:imperceptible_barrier"
                and row["conditions_json"] == "[]"
                and is_retained_shield(EffectState.model_validate_json(canonical_json(effect)))
            ):
                visible.append(effect)
        return visible

    @scoped_read
    def world_passive_identities(self, sid, target_ids):
        """Find roster passives owned by elapsed time or the recipient's rest.

        These rows have no turn trigger or linked condition. Pass can advance
        the encounter while the same world rows retain their original lifecycle.
        """
        identities = set()
        for target_id in target_ids:
            for row in self._target_rows(sid, target_id):
                if (row["expires_tick"] is None and row["cast_by"] is None) or any(
                    row[name] is not None
                    for name in ("concentration_id", "lifecycle_json", "conditions_json")
                ):
                    continue
                identity = (target_id, row["effect_id"], row["origin"])
                effect = self.store.memory.json(row["payload_json"])
                if row["expires_tick"] is None:
                    if effect["duration"].get("until_long_rest"):
                        application = self._spell_application(row)
                        if application is not None and (
                            reviewed_hp_bonus(application.effect) is not None
                            or is_reviewed_longstrider_application(application.effect)
                        ):
                            identities.add(identity)
                elif not any(
                    effect.get(name)
                    for name in ("action_policy", "statuses", "flags", "lifecycle", "end_effects")
                ):
                    identities.add(identity)
        return frozenset(identities)

    @scoped_read
    def concentration_group(self, sid, caster_id):
        """Fix the exact group and one supported member representation by indexed identity."""
        row = self._conn.execute(
            "SELECT concentration_id,effect_id,rounds_remaining FROM effect_concentrations "
            "WHERE session_id=? AND caster_id=?",
            (sid, caster_id),
        ).fetchone()
        if row is None:
            return None
        group_id, effect_id, rounds = row
        if effect_id.startswith(CASTING_FOCUS_PREFIX):
            activity = self._conn.execute(
                "SELECT activity_id,actor_id FROM spell_cast_activities "
                "WHERE session_id=? AND concentration_id=?",
                (sid, group_id),
            ).fetchone()
            if (
                activity is None
                or activity["actor_id"] != caster_id
                or group_id != casting_focus_id(activity["activity_id"])
                or effect_id != group_id
                or rounds != 0
            ):
                raise StateMachineError("invalid_mechanical_state", "Casting focus differs")
            return ConcentrationGroup(group_id, caster_id, effect_id, 0, (), None, activity[0])
        spatial = self.spatial_lights.for_concentration(sid, group_id)
        visual = self._conn.execute(
            "SELECT * FROM visual_effect_sources WHERE session_id=? AND concentration_id=?",
            (sid, group_id),
        ).fetchone()
        members = tuple(
            tuple(member)
            for member in self._conn.execute(
                "SELECT target_id,effect_id,origin FROM ongoing_effects WHERE session_id=? "
                "AND concentration_id=? ORDER BY chain_ordinal",
                (sid, group_id),
            )
        )
        if spatial is None and visual is None:
            if not members:
                raise StateMachineError("invalid_mechanical_state", "Empty concentration")
        elif spatial is not None and (
            visual is not None
            or members
            or (spatial["caster_id"], spatial["concentration_id"], effect_id, rounds)
            != (
                caster_id,
                group_id,
                spatial["effect_id"],
                0,
            )
        ):
            raise StateMachineError("invalid_mechanical_state", "Spatial concentration differs")
        elif visual is not None and (
            members
            or visual["caster_id"] != caster_id
            or visual["source_id"] != effect_id
            or rounds != 0
        ):
            raise StateMachineError("invalid_mechanical_state", "Visual concentration differs")
        return ConcentrationGroup(
            group_id,
            caster_id,
            effect_id,
            rounds,
            members,
            dict(spatial) if spatial else None,
            visual_source=dict(visual) if visual else None,
        )

    def apply_damage_concentration(self, sid, group, resolution, *, end_spatial, end_visual=None):
        """Adopt the verified outcome while its exact member set still matches."""
        self.store.require_write()
        if resolution.target_id != group.caster_id or group != self.concentration_group(
            sid, group.caster_id
        ):
            raise StateMachineError("version_conflict", "Concentration dependencies changed")
        if resolution.outcome == "retained":
            return ()
        if resolution.outcome not in {"broken", "ended"}:
            raise StateMachineError("invalid_rule_result", "Unknown concentration outcome")
        if group.casting_activity_id is not None:
            raise StateMachineError(
                "unsupported_mechanism_effect_lifecycle",
                "Mechanism interruption of casting is not adopted by this port",
            )
        if group.spatial_member is not None:
            return (end_spatial(group.spatial_member),)
        if group.visual_source is not None:
            if end_visual is None:
                raise StateMachineError("invalid_effect", "Visual concentration end is unavailable")
            return (end_visual(group.visual_source),)
        self.remove_concentration(sid, group.concentration_id)
        return ()

    @scoped_read
    def concentration_reference(self, sid, caster_id):
        """Return the stable caster wire token while its group exists."""
        row = self._conn.execute(
            "SELECT concentration_id,effect_id FROM effect_concentrations "
            "WHERE session_id=? AND caster_id=?",
            (sid, caster_id),
        ).fetchone()
        if row is None:
            return None
        if (
            row["effect_id"].startswith(CASTING_FOCUS_PREFIX)
            or (self.spatial_lights.for_concentration(sid, row["concentration_id"]) is not None)
            or self._conn.execute(
                "SELECT 1 FROM visual_effect_sources WHERE session_id=? AND concentration_id=?",
                (sid, row["concentration_id"]),
            ).fetchone()
            is not None
        ):
            # The actor effect wire field only accepts actor-target concentration chains.
            return None
        if (
            self._conn.execute(
                "SELECT 1 FROM ongoing_effects WHERE session_id=? AND concentration_id=?",
                (sid, row["concentration_id"]),
            ).fetchone()
            is None
        ):
            raise StateMachineError("invalid_mechanical_state", "Empty concentration")
        return row["effect_id"]

    @scoped_read
    def actor_conditions(self, sid, target_id, independent):
        """Overlay linked statuses using RE provenance without changing independent facts."""
        current = [ActiveCondition.model_validate(value) for value in independent]
        for row in sorted(self._target_rows(sid, target_id), key=lambda row: row["ordinal"]):
            if row["conditions_json"] is None:
                continue
            statuses = self.store.memory.json(row["conditions_json"])
            current, _ = conditions_after_effect_application(
                current,
                row["effect_id"],
                statuses,
                scope="session" if _fixed_region_effect(row, statuses) else "combat",
            )
        return [condition.model_dump(mode="json") for condition in current]

    @instance_read(
        "actor", None, "linked_condition_names", capacity=3, maintained="linked_conditions"
    )
    def linked_condition_names(self, sid, target_id):
        """Cache exact linked names across changes to effects without conditions."""
        names, seen = [], set()
        rows = self._conn.execute(
            "SELECT effect_id,conditions_json,ordinal FROM ongoing_effects "
            "WHERE session_id=? AND target_id=? ORDER BY ordinal",
            (sid, target_id),
        )
        for row in rows:
            if row["conditions_json"] is None:
                continue
            for name in self.store.memory.json(row["conditions_json"]):
                key = (name, row["effect_id"])
                if key not in seen:
                    names.append(name)
                    seen.add(key)
        return names

    @instance_read("actor", None, "linked_conditions", capacity=3, maintained="linked_conditions")
    def has_linked_conditions(self, sid, target_id, *, capability=None):
        """Find linked statuses that require more rules for the requested capability.

        Reuse the actor's result across noncondition effect changes. Condition links
        and their qualifying lineage are versioned by SessionMemory.
        """
        preserves = {
            "movement": region_conditions_preserve_movement,
            "visibility": region_conditions_preserve_visibility,
            "observer_visibility": region_conditions_preserve_visibility,
        }.get(capability)
        for row in self._target_rows(sid, target_id):
            if row["conditions_json"] is None:
                continue
            statuses = self.store.memory.json(row["conditions_json"])
            if capability == "observer_visibility" and spell_preserves_observer_sight(
                row["cast_spell_id"], row["effect_id"], statuses
            ):
                continue
            if statuses and (
                preserves is None
                or not _fixed_region_effect(row, statuses)
                or not preserves(statuses)
            ):
                return True
        return False

    def has_world_concentration(self, sid, caster_id):
        """Identify an elapsed-time group that combat currently cannot project."""
        return (
            self._conn.execute(
                "SELECT 1 FROM effect_concentrations WHERE session_id=? AND caster_id=? "
                "AND rounds_remaining=0",
                (sid, caster_id),
            ).fetchone()
            is not None
        )

    def has_round_concentration(self, sid, caster_id):
        """Identify a combat-clock group that ordinary World actions cannot interpret."""
        group = self._conn.execute(
            "SELECT rounds_remaining FROM effect_concentrations WHERE session_id=? AND caster_id=?",
            (sid, caster_id),
        ).fetchone()
        return group is not None and group[0] > 0

    @scoped_read
    def combat_fields(self, sid, target_ids):
        """Project RE combat fields from the fixed mechanical roster only."""
        groups = tuple(
            row
            for caster in target_ids
            for row in self._conn.execute(
                "SELECT * FROM effect_concentrations WHERE session_id=? AND caster_id=?",
                (sid, caster),
            )
        )
        rows = sorted(
            (row for target in target_ids for row in self._target_rows(sid, target)),
            key=lambda row: row["ordinal"],
        )
        chain = {group["caster_id"]: [] for group in groups}
        group_caster = {group["concentration_id"]: group["caster_id"] for group in groups}
        by_group = {group["caster_id"]: [] for group in groups}
        conditions, lifecycles = [], []
        for row in rows:
            identity = [row["target_id"], row["effect_id"], row["origin"]]
            if row["concentration_id"] is not None:
                caster = group_caster.get(row["concentration_id"])
                if caster is None:
                    raise StateMachineError(
                        "invalid_mechanical_state", "Effect group crosses mechanical roster"
                    )
                by_group[caster].append((row["chain_ordinal"], identity))
            if row["conditions_json"] is not None:
                conditions.append(
                    {
                        "identity": identity,
                        "statuses": self.store.memory.json(row["conditions_json"]),
                    }
                )
            if row["lifecycle_json"] is not None:
                lifecycles.append(
                    {"identity": identity, "state": self.store.memory.json(row["lifecycle_json"])}
                )
        for caster, members in by_group.items():
            chain[caster] = [identity for _, identity in sorted(members)]
        for group in groups:
            total = self._conn.execute(
                "SELECT count(*) FROM ongoing_effects WHERE session_id=? AND concentration_id=?",
                (sid, group["concentration_id"]),
            ).fetchone()[0]
            if not total or total != len(chain[group["caster_id"]]):
                raise StateMachineError(
                    "invalid_mechanical_state", "Concentration crosses mechanical roster"
                )
        return {
            "concentration_chain": chain,
            "concentration_rounds_remaining": {
                group["caster_id"]: group["rounds_remaining"] for group in groups
            },
            "conditions_by_effect": conditions,
            "effect_lifecycles": lifecycles,
        }

    def has_linked_effects(self, sid, target_ids):
        """Check only encounter targets for linked event uncertainty."""
        return any(
            row["conditions_json"] is not None or row["lifecycle_json"] is not None
            for target in target_ids
            for row in self._target_rows(sid, target)
        )
