"""Persist ongoing effect instances and concentration ownership in keyed facts.

This component records adopted facts only. Rule creation, expiry, and progression
remain with the bound Rule Engine; every mutation uses the caller's UnitOfWork.
"""

from dnd5e_engine.effect_lifecycle import conditions_after_effect_application
from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_state import ConditionLink, LifecycleRecord
from dnd5e_engine.types.conditions import ActiveCondition

from .errors import StateMachineError
from .package import canonical_json
from .scoped_reads import scoped_read


class OngoingEffects:
    """Own target effects and concentration groups in the existing Store."""

    def __init__(self, store):
        """Bind the domain owner to the shared transaction and RAM scope."""
        self.store = store

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
                )

    def add_effect(
        self,
        sid,
        effect,
        *,
        ordinal,
        concentration_id=None,
        chain_ordinal=None,
        conditions=None,
        lifecycle=None,
        expires_tick=None,
    ):
        """Persist one identified target effect and its optional linked records."""
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
        self._conn.execute(
            "INSERT INTO ongoing_effects(session_id,target_id,effect_id,origin,ordinal,"
            "payload_json,concentration_id,chain_ordinal,conditions_json,"
            "lifecycle_json,expires_tick) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
            ),
        )

    def add_world_effect(self, sid, proposal, *, expires_tick):
        """Persist one RE-declared terminal effect and its clock deadline atomically."""
        self.store.require_write()
        ordinal = self._conn.execute(
            "SELECT coalesce(max(ordinal)+1,0) FROM ongoing_effects WHERE session_id=?",
            (sid,),
        ).fetchone()[0]
        self.add_effect(
            sid, proposal.effect.model_dump(mode="json"), ordinal=ordinal, expires_tick=expires_tick
        )

    def expire_world_effect(self, sid, identity, expected_tick):
        """Remove only the exact due effect whose stored deadline still agrees."""
        self.store.require_write()
        target, effect_id, origin = identity
        deleted = self._conn.execute(
            "DELETE FROM ongoing_effects WHERE session_id=? AND target_id=? "
            "AND effect_id=? AND origin=? AND expires_tick=? AND concentration_id IS NULL",
            (sid, target, effect_id, origin, expected_tick),
        )
        if deleted.rowcount != 1:
            raise StateMachineError("invalid_effect", "Effect deadline or identity changed")

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
        group_id = old[0]
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
            current, _ = conditions_after_effect_application(
                current, row["effect_id"], self.store.memory.json(row["conditions_json"])
            )
        return [condition.model_dump(mode="json") for condition in current]

    def linked_condition_names(self, sid, target_id):
        """Read effect-linked condition names without parsing independent actor records."""
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

    def has_linked_conditions(self, sid, target_id):
        """Check one target's linked statuses for conservative predicate callers."""
        return any(
            row["conditions_json"] is not None
            and bool(self.store.memory.json(row["conditions_json"]))
            for row in self._conn.execute(
                "SELECT conditions_json FROM ongoing_effects WHERE session_id=? AND target_id=?",
                (sid, target_id),
            )
        )

    def has_concentration(self, sid, caster_id):
        """Check the caster's keyed group without constructing its RE wire token."""
        return (
            self._conn.execute(
                "SELECT concentration_id FROM effect_concentrations "
                "WHERE session_id=? AND caster_id=?",
                (sid, caster_id),
            ).fetchone()
            is not None
        )

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
