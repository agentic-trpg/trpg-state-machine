"""Controlled current facts over the caller's consistent RAM/candidate scope.

No method commits or computes rules. Store coordinates the RAM representation
of these same owned facts and their incremental persistent changes.
Rules metadata edits have a closed field set matching the pinned adapter.
"""

import json
from collections import defaultdict, deque
from uuid import uuid4

from dnd5e_engine.activities.passive_stats import CombatantSenses, interpret_passive_stats
from dnd5e_engine.evaluation_casting import CastingState
from dnd5e_engine.evaluation_charged_item import (
    exploration_charged_cast_profile,
    is_healing_staff,
    staff_attunement_ability,
)
from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_resources import ResourceState
from dnd5e_engine.evaluation_state import InventoryState, NonCombatSnapshot
from dnd5e_engine.rules.effects import effective_effects
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_srd_data.schema.common import PassiveEffectChange
from pydantic import TypeAdapter

from .combat import DistanceWithdrawal, EncounterRegion
from .errors import StateMachineError
from .item_recharge import next_item_dawn
from .item_units import core_item_id, item_unit_from_row, valid_item_unit
from .light_fuel import LightFuel
from .package import canonical_json
from .runtime_instances import (
    EQUIPMENT_SLOTS,
    EquipmentBinding,
    EquipmentItem,
    SpellRef,
    SpellSourceRef,
    instance_read,
)
from .scoped_reads import scoped_read
from .time_binding import MAX_TICK, deadline_after_seconds

_EFFECTS = TypeAdapter(tuple[EffectState, ...])
_SENSE_KEY_PREFIX = "system.attributes.senses."
_PASSIVE_SENSE_MODES = {"add": 2, "upgrade": 4}
_EQUIPMENT_MIRRORS = ("worn_armor", "shield_equipped", "other_hand_occupied")
_EQUIPMENT_METADATA = (
    *(field for _, field in EQUIPMENT_SLOTS),
    *_EQUIPMENT_MIRRORS,
    "weapon_grip",
    "weapon_in_hands",
    "stealth_disadvantage",
)


class CharacterFacts:
    """Maintain character mechanics, typed attributes, and World item projections."""

    ATTRIBUTE_FIELDS = (
        "strength",
        "dexterity",
        "constitution",
        "intelligence",
        "wisdom",
        "charisma",
        "ac",
        "character_level",
        "proficiency_bonus_override",
    )
    SENSE_FIELDS = ("darkvision", "blindsight", "tremorsense", "truesight")
    PROFICIENCY_FIELDS = (
        ("save", "save_proficiencies"),
        ("skill", "skill_proficiencies"),
        ("tool", "tool_proficiencies"),
        ("weapon", "weapon_proficiencies"),
    )

    RULE_FIELDS = frozenset(
        (
            "action_available",
            "bonus_action_available",
            "reaction_available",
            "action_taken_this_turn",
            "bonus_action_taken_this_turn",
            "attack_action_attacks_made",
            "attack_rolls_made_this_turn",
            "action_grants_spent",
            "action_grant_groups_spent",
            "movement_remaining",
            "disengaging_this_turn",
            "sneak_attack_spent_this_turn",
            "attacks_remaining",
            "attack_action_engaged",
            "light_weapon_swing_slug",
            "restricted_light_weapon_swing_slug",
            "offhand_attack_spent",
            "dodging",
            "loading_weapon_fired_this_action",
            "cleave_spent_this_turn",
            "flurry_strikes_remaining",
            "extra_actions_remaining",
            "action_surge_used_this_turn",
        )
    )

    def __init__(self, store, world, loader):
        """Bind shared facts and the frozen rules loader used to resolve /19–/20 item kinds."""
        from .ongoing_effects import OngoingEffects

        self.store = store
        self.world = world
        self.loader = loader
        self.effects = OngoingEffects(store)

    @property
    def _conn(self):
        """Return the shared character-facts connection."""
        return self.store.connection

    def initialize_pc(self, sid, actor_id, scene_id, pc):
        """Persist the initial PC character facts."""
        self.store.require_write()
        self._conn.execute(
            "INSERT INTO actors(session_id,actor_id,kind,current_scene_id,cell) "
            "VALUES (?,?,'pc',?,?)",
            (sid, actor_id, scene_id, pc.initial_cell),
        )
        self._conn.execute(
            "INSERT INTO characters(session_id,actor_id,current_hp,max_hp) VALUES (?,?,?,?)",
            (sid, actor_id, pc.current_hp, pc.max_hp),
        )
        for ordinal, (resource_id, current) in enumerate(pc.resources):
            self._insert_resource(
                sid, actor_id, resource_id, "synthetic", current, current, ordinal, {}
            )

    def _insert_resource(
        self, sid, actor_id, resource_id, kind, current, maximum, ordinal, metadata
    ):
        """Persist one owned pool; SessionMemory captures its candidate row."""
        self.store.require_write()
        self._conn.execute(
            "INSERT INTO character_resources VALUES (?,?,?,?,?,?,?,?)",
            (sid, actor_id, resource_id, kind, current, maximum, ordinal, canonical_json(metadata)),
        )

    def resources(self, sid, actor_id, *, kind=None):
        """Read current owned balances from the keyed RAM relation in a fixed scope."""
        if kind is None:
            return tuple(
                self._conn.execute(
                    "SELECT * FROM character_resources WHERE session_id=? AND actor_id=?",
                    (sid, actor_id),
                )
            )
        return tuple(
            self._conn.execute(
                "SELECT * FROM character_resources WHERE session_id=? AND actor_id=? AND kind=?",
                (sid, actor_id, kind),
            )
        )

    def resource_balances(self, sid, actor_id):
        """Project UI balances from the same owned resource rows used by rules."""
        return {row["resource_id"]: row["current"] for row in self.resources(sid, actor_id)}

    def _typed_pool(self, row):
        """Decode one typed pool with scalar columns authoritative over metadata."""
        metadata = self.store.memory.json(row["metadata_json"])
        required = {
            "hit_dice": {"class_slug", "die_size"},
            "spell_slot": {"level"},
            "pact_slot": {"level"},
            "feature_uses": {"feature_slug"},
        }.get(row["kind"])
        if not isinstance(metadata, dict) or required is None or set(metadata) != required:
            raise StateMachineError(
                "invalid_mechanical_state", "Stored resource metadata is incomplete"
            )
        return row["ordinal"], {
            **metadata,
            "owner_id": row["actor_id"],
            "pool_id": row["resource_id"],
            "kind": row["kind"],
            "current": row["current"],
            "maximum": row["maximum"],
        }

    @scoped_read
    def _typed_owner_resources(self, sid, actor_id):
        """Reuse an owner's decoded typed rows across actor and component projections.

        The scoped dependency follows the owner's keyed resource range. Writes and
        savepoint rollback invalidate the candidate stage; cached values are copied
        before a consumer can mutate its own snapshot projection.
        """
        return tuple(
            self._typed_pool(row)
            for row in self.resources(sid, actor_id)
            if row["kind"] != "synthetic"
        )

    def _typed_resources(self, sid, owner_ids, present):
        """Assemble a stable typed closure from reusable requested owner projections."""
        if not present:
            return None
        pools = [pool for owner in owner_ids for pool in self._typed_owner_resources(sid, owner)]
        return {"pools": [value for _, value in sorted(pools, key=lambda entry: entry[0])]}

    def initialize_legacy_items(self, sid, actor_id, slugs):
        """Materialize each explicit core-only Package occurrence as an owned item."""
        self.store.require_write()
        for position, slug in enumerate(slugs):
            unit = {
                "item_slug": slug,
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
            if not valid_item_unit(unit):
                raise StateMachineError("invalid_item_foundation", "Invalid core item slug")
            self.world.materialize_owned_item(
                sid, core_item_id(sid, actor_id, position), actor_id, unit
            )

    def seed_combat_pc_items(self, sid, template_id, actor_id, foundation, slugs):
        """Expose selected PC's explicit combat item identities before encounter start."""
        snapshot = json.loads(foundation)["snapshot"]
        inventory = self._inventory_model(snapshot["inventory_state"])
        chosen = [e for e in inventory.entries if e.owner_id == template_id]
        if {e.item_slug for e in chosen} != set(slugs):
            raise StateMachineError("invalid_item_foundation", "PC inventory differs from rules")
        for entry in chosen:
            unit = {
                "item_slug": entry.item_slug,
                "quantity": 1,
                "charges_remaining_per_unit": entry.charges_remaining_per_unit,
                "accessible": entry.accessible,
            }
            if not valid_item_unit(unit) or not 1 <= entry.quantity <= 128:
                raise StateMachineError("invalid_item_foundation", "Ambiguous combat item unit")
            for n in range(entry.quantity):
                iid = entry.instance_id if n == 0 else f"{entry.instance_id}.unit.{n:08d}"
                self.world.materialize_owned_item(sid, iid, actor_id, unit, allow_existing=n == 0)

    def carried_slugs(self, sid, actor_id):
        """Return distinct owned item slugs from current object and ancestry facts."""
        _, entries = self.world.portable_inventory(sid, owner_ids={actor_id})
        return list(dict.fromkeys(e["item_slug"] for e in entries.values()))

    def _retire_legacy_item_identity(self, sid, owner, slug):
        """Replace only a proved core-only placeholder before first rules adoption."""
        prefix = core_item_id(sid, owner, 0)[:-8]
        candidates = [
            row
            for row in self._conn.execute(
                "SELECT * FROM objects WHERE session_id=? AND carrier_id=? AND object_id LIKE ?",
                (sid, owner, prefix + "%"),
            ).fetchall()
            if row["object_id"].startswith(prefix) and row["item_slug"] == slug
        ]
        if not candidates:
            return
        if len(candidates) != 1:
            raise StateMachineError("invalid_item_foundation", "Legacy item identity is ambiguous")
        old = candidates[0]
        config = self._conn.execute(
            "SELECT config_json FROM instance_configs WHERE session_id=? AND domain='object' "
            "AND instance_id=?",
            (sid, old["object_id"]),
        ).fetchone()
        if (
            item_unit_from_row(old)
            != {
                "item_slug": slug,
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
            or config is None
            or self.store.memory.json(config[0]).get("portable_policy") is not None
        ):
            raise StateMachineError("invalid_item_foundation", "Legacy item has changed")
        self.world.retire_owned_item(sid, old)

    def initialize_npc_actor(self, sid, actor):
        """Insert an NPC actor and return whether it was newly created."""
        self.store.require_write()
        return bool(
            self._conn.execute(
                "INSERT INTO actors(session_id,actor_id,kind,current_scene_id,cell) "
                "SELECT ?,?,'npc',?,? WHERE NOT EXISTS "
                "(SELECT 1 FROM actors WHERE session_id=? AND actor_id=?)",
                (
                    sid,
                    actor.actor_id,
                    actor.initial_scene_id,
                    actor.initial_cell,
                    sid,
                    actor.actor_id,
                ),
            ).rowcount
        )

    def move(self, sid, actor_id, destination):
        """Update the actor's authoritative scene and cell position."""
        self.store.require_write()
        self._conn.execute(
            "UPDATE actors SET current_scene_id=?,cell=? WHERE session_id=? AND actor_id=?",
            (destination.scene_id, destination.cell, sid, actor_id),
        )

    def set_hp(self, sid, actor_id, value):
        """Update the actor's current hit points."""
        self.store.require_write()
        self._conn.execute(
            "UPDATE characters SET current_hp=? WHERE session_id=? AND actor_id=?",
            (value, sid, actor_id),
        )

    def effect_hp_facts(self, sid, actor_id):
        """Read only the active target's paired HP and mechanical creature type."""
        row = self._conn.execute(
            "SELECT c.current_hp,c.max_hp,json_extract(m.state_json,'$.entity_type') "
            "FROM characters c JOIN mechanical_actors m USING(session_id,actor_id) "
            "WHERE c.session_id=? AND c.actor_id=?",
            (sid, actor_id),
        ).fetchone()
        if row is None or row[2] is None:
            raise StateMachineError("invalid_mechanical_state", "Effect HP target is missing")
        return tuple(row)

    def set_hp_pair(self, sid, actor_id, transition):
        """Write a guarded effect current/maximum pair through the Character owner."""
        self.store.require_write()
        changed = self._conn.execute(
            "UPDATE characters SET current_hp=?,max_hp=? WHERE session_id=? AND actor_id=? "
            "AND current_hp=? AND max_hp=?",
            (
                transition.current,
                transition.maximum,
                sid,
                actor_id,
                transition.expected_current,
                transition.expected_maximum,
            ),
        ).rowcount
        if changed != 1:
            raise StateMachineError("version_conflict", "Effect HP pair changed")

    def spend_resource(self, sid, actor_id, resource_id, amount, *, expected_current=None):
        """Debit one keyed pool, optionally checking the evaluator's fixed starting balance."""
        self.store.require_write()
        row = self._conn.execute(
            "SELECT current FROM character_resources "
            "WHERE session_id=? AND actor_id=? AND resource_id=?",
            (sid, actor_id, resource_id),
        ).fetchone()
        if row is None or not 0 < amount <= row[0]:
            raise StateMachineError(
                "invalid_delta", "Resource debit is outside its current balance"
            )
        if expected_current is not None and row[0] != expected_current:
            raise StateMachineError("invalid_rule_result", "Resource balance changed")
        self._conn.execute(
            "UPDATE character_resources SET current=current-? "
            "WHERE session_id=? AND actor_id=? AND resource_id=? AND current=?",
            (amount, sid, actor_id, resource_id, row[0]),
        )

    def restore_resource(self, sid, actor_id, resource_id, expected_current, value):
        """Credit a keyed pool from its verified old balance, within its stored capacity."""
        self.store.require_write()
        changed = self._conn.execute(
            "UPDATE character_resources SET current=? "
            "WHERE session_id=? AND actor_id=? AND resource_id=? AND current=? "
            "AND current<? AND maximum>=?",
            (value, sid, actor_id, resource_id, expected_current, value, value),
        ).rowcount
        if changed != 1:
            raise StateMachineError("invalid_rule_result", "Resource restoration balance changed")

    def award_xp(self, sid, actor_id, value):
        """Add experience, rejecting a missing character."""
        self.store.require_write()
        current = self._conn.execute(
            "SELECT xp FROM characters WHERE session_id=? AND actor_id=?", (sid, actor_id)
        ).fetchone()
        if current is None or not 0 < value <= (2**63 - 1) - current[0]:
            raise StateMachineError(
                "invalid_rule_result", "XP increment cannot be represented in authoritative storage"
            )
        self._conn.execute(
            "UPDATE characters SET xp=xp+? WHERE session_id=? AND actor_id=?",
            (value, sid, actor_id),
        )

    def update_rule_metadata(self, sid, actor_id, field, value):
        """Route approved rule fields to their narrow owner or remaining rules JSON."""
        self.store.require_write()
        if field == "conditions":
            self.replace_conditions(sid, actor_id, value)
            return
        if field in ("temp_hp", "is_alive", "death_saves", "last_damaged_by"):
            self.update_life_state(sid, actor_id, field, value)
            return
        if field not in self.RULE_FIELDS:
            raise StateMachineError("invalid_rule_result", "Unsupported actor fact")
        self._conn.execute(
            "UPDATE mechanical_actors SET state_json=json_set(state_json,?,json(?)) "
            "WHERE session_id=? AND actor_id=?",
            ("$." + field, canonical_json(value), sid, actor_id),
        )

    def set_held_weapon(self, sid, actor_id, instance_id, grip):
        """Persist one approved hand-binding transition in the command's write scope.

        The World item remains at its present location. The caller has checked its
        exact identity, direct ownership and grip before reaching this method.
        """
        self.store.require_write()
        changed = self._conn.execute(
            "UPDATE character_equipment SET held_weapon_instance_id=?,weapon_grip=?,"
            "legacy_weapon_slug=NULL "
            "WHERE session_id=? AND actor_id=?",
            (instance_id, grip, sid, actor_id),
        )
        if changed.rowcount != 1:
            raise StateMachineError("invalid_mechanical_state", "Actor equipment facts are missing")

    def wear_item(self, sid, actor_id, instance_id, slug):
        """Bind one validated wearable to its character in the current write scope."""
        self.store.require_write()
        self._conn.execute(
            "INSERT INTO character_worn_items(session_id,actor_id,instance_id,item_slug) "
            "VALUES(?,?,?,?)",
            (sid, actor_id, instance_id, slug),
        )

    def unwear_item(self, sid, actor_id, instance_id):
        """Remove an exact character-owned wearable binding without moving the item."""
        self.store.require_write()
        changed = self._conn.execute(
            "DELETE FROM character_worn_items WHERE session_id=? AND actor_id=? AND instance_id=?",
            (sid, actor_id, instance_id),
        )
        if changed.rowcount != 1:
            raise StateMachineError("equipment_binding_conflict", "Selected item is not worn")

    def worn_items(self, sid, actor_id):
        """Read exact current wearable identities and their World-owned item rows."""
        result = []
        for binding in self._conn.execute(
            "SELECT instance_id,item_slug FROM character_worn_items "
            "WHERE session_id=? AND actor_id=? ORDER BY instance_id",
            (sid, actor_id),
        ):
            item = self.world.object(sid, binding["instance_id"])
            entry = self.world.item_entry(item) if item is not None else None
            if (
                item is None
                or entry is None
                or item["container_id"] is not None
                or entry["owner_id"] != actor_id
                or entry["item_slug"] != binding["item_slug"]
                or not entry["accessible"]
                or entry["quantity"] != 1
            ):
                raise StateMachineError("invalid_mechanical_state", "Worn item binding is stale")
            result.append((item, entry))
        return tuple(result)

    def set_equipment(self, sid, actor_id, *, armor, shield, offhand, weapon, grip, ac, stealth):
        """Persist exact equipment bindings and their rule AC in the shared write scope.

        World placement stays unchanged. These two Character-owned rows publish or
        roll back together with the command event and receipt.
        """
        self.store.require_write()
        state = self._conn.execute(
            "UPDATE character_equipment SET held_weapon_instance_id=?,worn_armor_instance_id=?,"
            "equipped_shield_instance_id=?,offhand_item_instance_id=?,weapon_grip=?,"
            "stealth_disadvantage=?,legacy_weapon_slug=NULL "
            "WHERE session_id=? AND actor_id=?",
            (weapon, armor, shield, offhand, grip, int(stealth), sid, actor_id),
        )
        if state.rowcount != 1:
            raise StateMachineError("invalid_mechanical_state", "Actor equipment facts are missing")
        attributes = self._conn.execute(
            "UPDATE character_attributes SET ac=? WHERE session_id=? AND actor_id=?",
            (ac, sid, actor_id),
        )
        if attributes.rowcount != 1:
            raise StateMachineError("invalid_mechanical_state", "Actor attributes are missing")

    def update_life_state(self, sid, actor_id, field, value):
        """Persist one approved life delta in the narrow owner row without rewriting rules JSON."""
        self.store.require_write()
        if field == "temp_hp" and type(value) is int and 0 <= value <= 2**63 - 1:
            assignments, params = "temp_hp=?", (value,)
        elif field == "is_alive" and type(value) is bool:
            assignments, params = "is_alive=?", (int(value),)
        elif field == "last_damaged_by" and (value is None or type(value) is str):
            assignments, params = "last_damaged_by=?", (value,)
        elif field == "death_saves" and value is None:
            assignments, params = (
                "death_save_successes=?,death_save_failures=?,death_save_stable=?",
                (None, None, None),
            )
        elif (
            field == "death_saves"
            and type(value) is dict
            and set(value) == {"successes", "failures", "is_stable"}
            and type(value["successes"]) is int
            and 0 <= value["successes"] <= 3
            and type(value["failures"]) is int
            and 0 <= value["failures"] <= 4
            and type(value["is_stable"]) is bool
        ):
            assignments, params = (
                "death_save_successes=?,death_save_failures=?,death_save_stable=?",
                (value["successes"], value["failures"], int(value["is_stable"])),
            )
        else:
            raise StateMachineError("invalid_rule_result", "Invalid actor life fact")
        changed = self._conn.execute(
            f"UPDATE mechanical_life SET {assignments} WHERE session_id=? AND actor_id=?",
            (*params, sid, actor_id),
        )
        if changed.rowcount != 1:
            raise StateMachineError("invalid_mechanical_state", "Actor life component is missing")

    def life(self, sid, actor_id):
        """Read one required life component, or None for an actor without mechanics."""
        row = self._conn.execute(
            "SELECT * FROM mechanical_life WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()
        if (
            row is None
            and self._conn.execute(
                "SELECT 1 FROM mechanical_actors WHERE session_id=? AND actor_id=?",
                (sid, actor_id),
            ).fetchone()
        ):
            raise StateMachineError("invalid_mechanical_state", "Actor life component is missing")
        return row

    def conditions(self, sid, actor_id):
        """Read ordered independent conditions for one actor from keyed current facts."""
        rows = self._conn.execute(
            "SELECT * FROM character_conditions WHERE session_id=? AND actor_id=? ORDER BY ordinal",
            (sid, actor_id),
        )
        return [self._condition_data(row) for row in rows]

    @scoped_read
    def condition_immunities(self, sid, actor_id):
        """Read an actor's declared immunities without assembling rule inputs."""
        row = self._conn.execute(
            "SELECT state_json FROM mechanical_actors WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()
        if row is None:
            return None
        immunities = self.store.memory.json(row[0]).get("condition_immunities")
        if type(immunities) is not list or any(type(item) is not str for item in immunities):
            raise StateMachineError("invalid_mechanical_state", "Actor immunities are missing")
        return tuple(immunities)

    @staticmethod
    def _condition_data(row):
        """Restore the unchanged RE wire fields from one owned condition row."""
        return {
            key: row[key]
            for key in (
                "condition",
                "source_entity_id",
                "scope",
                "applied_round",
                "duration_rounds",
                "save_dc",
                "exhaustion_level",
            )
        } | {"source_effect_id": None}

    def condition(self, sid, actor_id, condition_id):
        """Read one independent record by stable identity within its actor scope."""
        row = self._conn.execute(
            "SELECT * FROM character_conditions WHERE session_id=? AND actor_id=? "
            "AND condition_id=?",
            (sid, actor_id, condition_id),
        ).fetchone()
        return self._condition_data(row) if row is not None else None

    def condition_index(self, sid, actor_id, condition_id):
        """Locate an exact independent row in the ordered target-only rules projection."""
        row = self._conn.execute(
            "SELECT (SELECT COUNT(*) FROM character_conditions AS preceding "
            "WHERE preceding.session_id=selected.session_id "
            "AND preceding.actor_id=selected.actor_id AND preceding.ordinal<selected.ordinal) "
            "FROM character_conditions AS selected WHERE selected.session_id=? "
            "AND selected.actor_id=? AND selected.condition_id=?",
            (sid, actor_id, condition_id),
        ).fetchone()
        return row[0] if row is not None else None

    def condition_options(self, sid, actor_id):
        """Read opaque IDs and names of one actor's independent conditions in rules order."""
        return tuple(
            (row[0], row[1])
            for row in self._conn.execute(
                "SELECT condition_id,condition FROM character_conditions "
                "WHERE session_id=? AND actor_id=? ORDER BY ordinal",
                (sid, actor_id),
            )
        )

    def condition_names(self, sid, actor_id):
        """Read only one actor's independent condition names in record order."""
        return [
            row[0]
            for row in self._conn.execute(
                "SELECT condition FROM character_conditions WHERE session_id=? AND actor_id=? "
                "ORDER BY ordinal",
                (sid, actor_id),
            )
        ]

    def has_conditions(self, sid, actor_id):
        """Test one actor's independent condition existence without decoding records."""
        return (
            self._conn.execute(
                "SELECT 1 FROM character_conditions WHERE session_id=? AND actor_id=? LIMIT 1",
                (sid, actor_id),
            ).fetchone()
            is not None
        )

    def replace_conditions(self, sid, actor_id, values):
        """Adopt an independent projection, retaining IDs of unchanged duplicate records."""
        self.store.require_write()
        if type(values) not in (list, tuple):
            raise StateMachineError("invalid_rule_result", "Invalid independent conditions")
        normalized = []
        for value in values:
            condition = ActiveCondition.model_validate(
                value.model_dump(mode="json") if hasattr(value, "model_dump") else value
            )
            if condition.source_effect_id is not None:
                raise StateMachineError(
                    "invalid_rule_result", "Linked condition belongs to Effects"
                )
            normalized.append(condition.model_dump(mode="json"))
        old = self._conn.execute(
            "SELECT * FROM character_conditions WHERE session_id=? AND actor_id=? ORDER BY ordinal",
            (sid, actor_id),
        ).fetchall()
        by_value = defaultdict(deque)
        for row in old:
            by_value[canonical_json(self._condition_data(row))].append(row)
        matched = []
        for condition in normalized:
            bucket = by_value[canonical_json(condition)]
            matched.append(bucket.popleft() if bucket else None)
        surviving = [row["ordinal"] for row in matched if row is not None]
        first_new = next((i for i, row in enumerate(matched) if row is None), len(matched))
        append_only = all(row is None for row in matched[first_new:])
        preserve_order = append_only and surviving == sorted(surviving)
        next_ordinal = max((row["ordinal"] for row in old), default=-1) + 1
        retained = set()
        for ordinal, (condition, row) in enumerate(zip(normalized, matched, strict=True)):
            if row is None:
                assigned = next_ordinal if preserve_order else ordinal
                next_ordinal += 1
                self._conn.execute(
                    "INSERT INTO character_conditions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        sid,
                        actor_id,
                        uuid4().hex,
                        condition["condition"],
                        condition["source_entity_id"],
                        condition["scope"],
                        condition["applied_round"],
                        condition["duration_rounds"],
                        condition["save_dc"],
                        condition["exhaustion_level"],
                        assigned,
                    ),
                )
            else:
                retained.add(row["condition_id"])
                if not preserve_order and row["ordinal"] != ordinal:
                    self._conn.execute(
                        "UPDATE character_conditions SET ordinal=? WHERE session_id=? "
                        "AND actor_id=? AND condition_id=?",
                        (ordinal, sid, actor_id, row["condition_id"]),
                    )
        for row in old:
            if row["condition_id"] not in retained:
                self._conn.execute(
                    "DELETE FROM character_conditions WHERE session_id=? AND actor_id=? "
                    "AND condition_id=?",
                    (sid, actor_id, row["condition_id"]),
                )

    def add_condition(self, sid, actor_id, value):
        """Append one independent condition and return its stable internal identity."""
        self.store.require_write()
        condition = ActiveCondition.model_validate(
            value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        )
        if condition.source_effect_id is not None:
            raise StateMachineError("invalid_rule_result", "Linked condition belongs to Effects")
        ordinal = self._conn.execute(
            "SELECT COALESCE(MAX(ordinal),-1)+1 FROM character_conditions "
            "WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()[0]
        cid = uuid4().hex
        self._conn.execute(
            "INSERT INTO character_conditions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                actor_id,
                cid,
                condition.condition,
                condition.source_entity_id,
                condition.scope,
                condition.applied_round,
                condition.duration_rounds,
                condition.save_dc,
                condition.exhaustion_level,
                ordinal,
            ),
        )
        return cid

    def remove_condition(self, sid, actor_id, condition_id):
        """Remove one independent record by identity without editing siblings."""
        self.store.require_write()
        deleted = self._conn.execute(
            "DELETE FROM character_conditions WHERE session_id=? AND actor_id=? AND condition_id=?",
            (sid, actor_id, condition_id),
        )
        return deleted.rowcount == 1

    def initialize_rule_actor(self, row, actor, *, xp=None, encounter=False, schema_version=None):
        """Initialize owned actor facts, including exact equipment for /19 and /20."""
        self.store.require_write()
        if any(condition.source_effect_id is not None for condition in actor.conditions):
            raise StateMachineError(
                "unsupported_combat_foundation"
                if encounter
                else "unsupported_noncombat_foundation",
                "Initial linked conditions require effect adoption",
            )
        sid = row["session_id"]
        core = self._conn.execute(
            "SELECT * FROM characters WHERE session_id=? AND actor_id=?", (sid, actor.entity_id)
        ).fetchone()
        if core is not None:
            if encounter:
                if (core["current_hp"], core["max_hp"]) != (actor.hp_current, actor.hp_max):
                    raise StateMachineError(
                        "invalid_combat_foundation", "Initialization cannot reset HP"
                    )
                if self.resources(sid, actor.entity_id, kind="synthetic") or self.store.memory.json(
                    core["effects_json"]
                ):
                    raise StateMachineError(
                        "unsupported_combat_foundation",
                        "Legacy resources/effects cannot be converted implicitly",
                    )
            if (
                (core["current_hp"], core["max_hp"]) != (actor.hp_current, actor.hp_max)
                or self.resources(sid, actor.entity_id, kind="synthetic")
                or self.store.memory.json(core["effects_json"])
            ):
                raise StateMachineError(
                    "invalid_combat_foundation", "Initialization cannot reset core state"
                )
            if xp is not None:
                self._conn.execute(
                    "UPDATE characters SET xp=? WHERE session_id=? AND actor_id=?",
                    (xp, sid, actor.entity_id),
                )
        else:
            self._conn.execute(
                "INSERT INTO characters(session_id,actor_id,current_hp,max_hp,effects_json,xp) "
                "VALUES (?,?,?,?,?,?)",
                (
                    sid,
                    actor.entity_id,
                    actor.hp_current,
                    actor.hp_max,
                    "[]",
                    0 if xp is None else xp,
                ),
            )
        state = actor.model_dump(
            mode="json",
            exclude={
                "hp_current",
                "hp_max",
                "carried_item_slugs",
                "concentration_effect_id",
                "temp_hp",
                "is_alive",
                "death_saves",
                "last_damaged_by",
                "conditions",
                *self.ATTRIBUTE_FIELDS,
                *self.PROFICIENCY_FIELDS_NAMES,
                "skill_expertise",
                "senses",
            },
        )
        equipment = None
        if schema_version in ("engine-snapshot/19", "engine-snapshot/20"):
            slots = tuple(state.pop(field, None) for _, field in EQUIPMENT_SLOTS)
            weapon_slug = state.pop("weapon_in_hands")
            equipment = (
                *slots,
                state.pop("weapon_grip"),
                int(state.pop("stealth_disadvantage")),
                weapon_slug if slots[0] is None else None,
            )
            for field in _EQUIPMENT_MIRRORS:
                state.pop(field, None)
        else:
            if state.get("held_weapon_instance_id") is not None:
                del state["weapon_in_hands"]
            for _, field in EQUIPMENT_SLOTS:
                if state.get(field) is None:
                    state.pop(field, None)
        if actor.temp_hp > 2**63 - 1:
            raise StateMachineError(
                "invalid_mechanical_state", "Temporary HP exceeds storage range"
            )
        self._conn.execute(
            "INSERT INTO mechanical_actors VALUES (?,?,?)",
            (sid, actor.entity_id, canonical_json(state)),
        )
        if equipment is not None:
            self._conn.execute(
                "INSERT INTO character_equipment VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, actor.entity_id, *equipment),
            )
        self._insert_attributes(sid, actor)
        self._conn.execute(
            "INSERT INTO mechanical_life VALUES (?,?,?,?,?,?,?,?)",
            (
                sid,
                actor.entity_id,
                actor.temp_hp,
                int(actor.is_alive),
                None if actor.death_saves is None else actor.death_saves.successes,
                None if actor.death_saves is None else actor.death_saves.failures,
                None if actor.death_saves is None else int(actor.death_saves.is_stable),
                actor.last_damaged_by,
            ),
        )
        self.replace_conditions(sid, actor.entity_id, actor.conditions)

    @property
    def PROFICIENCY_FIELDS_NAMES(self):
        """Return the exact RE proficiency fields owned by narrow rows."""
        return tuple(field for _, field in self.PROFICIENCY_FIELDS)

    def _insert_attributes(self, sid, actor):
        """Write approved initialization facts in the caller's existing UnitOfWork."""
        aid = actor.entity_id
        senses = actor.senses.model_dump(mode="python")
        values = tuple(getattr(actor, name) for name in self.ATTRIBUTE_FIELDS)
        sense_values = tuple(senses[name] for name in self.SENSE_FIELDS)
        if any(
            value is not None and not -(2**63) <= value < 2**63
            for value in (*values, *sense_values)
        ):
            raise StateMachineError(
                "invalid_mechanical_state", "Actor attribute exceeds storage range"
            )
        self._conn.execute(
            "INSERT INTO character_attributes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                aid,
                *values,
                *sense_values,
                int(actor.weapon_proficiencies is not None),
            ),
        )
        expertise = list(actor.skill_expertise)
        used = [False] * len(expertise)
        expertise_by_name = defaultdict(deque)
        for index, name in enumerate(expertise):
            expertise_by_name[name].append(index)
        for kind, field in self.PROFICIENCY_FIELDS:
            entries = getattr(actor, field) or []
            for ordinal, name in enumerate(entries):
                expertise_ordinal = None
                if kind == "skill" and expertise_by_name[name]:
                    expertise_ordinal = expertise_by_name[name].popleft()
                    used[expertise_ordinal] = True
                self._conn.execute(
                    "INSERT INTO character_proficiencies VALUES (?,?,?,?,?,?,?)",
                    (sid, aid, kind, ordinal, name, 1, expertise_ordinal),
                )
        for index, name in enumerate(expertise):
            if not used[index]:
                self._conn.execute(
                    "INSERT INTO character_proficiencies VALUES (?,?,?,?,?,?,?)",
                    (sid, aid, "skill", len(actor.skill_proficiencies) + index, name, 0, index),
                )

    def attributes(self, sid, actor_id, *, include_proficiencies=True):
        """Read one required attribute owner; binding reads can omit list projection."""
        row = self._conn.execute(
            "SELECT * FROM character_attributes WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()
        if row is None:
            raise StateMachineError("invalid_mechanical_state", "Actor attributes are missing")
        data = {field: row[field] for field in self.ATTRIBUTE_FIELDS}
        data["senses"] = {field: row[field] for field in self.SENSE_FIELDS}
        if not include_proficiencies:
            return data
        proficiencies = {field: [] for _, field in self.PROFICIENCY_FIELDS}
        expertise = []
        field_by_kind = dict(self.PROFICIENCY_FIELDS)
        for entry in self._conn.execute(
            "SELECT kind,proficiency_id,proficient,expertise_ordinal FROM character_proficiencies "
            "WHERE session_id=? AND actor_id=? ORDER BY kind,ordinal",
            (sid, actor_id),
        ):
            field = field_by_kind[entry["kind"]]
            if entry["proficient"]:
                proficiencies[field].append(entry["proficiency_id"])
            if entry["expertise_ordinal"] is not None:
                expertise.append((entry["expertise_ordinal"], entry["proficiency_id"]))
        data.update(proficiencies)
        data["skill_expertise"] = [name for _, name in sorted(expertise)]
        if not row["weapon_proficiencies_present"]:
            data["weapon_proficiencies"] = None
        return data

    def _sense_attributes(self, sid, actor_id):
        """Locate the four base mechanical sense fields for one actor."""
        return self._conn.execute(
            "SELECT darkvision,blindsight,tremorsense,truesight FROM character_attributes "
            "WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()

    def senses(self, sid, actor_id):
        """Read one actor's four persisted base senses for event evidence."""
        row = self._sense_attributes(sid, actor_id)
        if row is None:
            raise StateMachineError("invalid_mechanical_state", "Actor attributes are missing")
        return {field: row[field] for field in self.SENSE_FIELDS}

    @instance_read("actor", None, "effective_senses")
    def effective_senses(self, sid, actor_id) -> CombatantSenses:
        """Project current special-sense ranges in feet from base and active effects.

        The actor field depends on the projected attributes and the actor's exact
        effect range in this read scope. Nonmechanical actors have ordinary sight
        when authorized by the caller. Effect lifetime and precedence remain with
        the Effects owner and the pinned rules engine.
        """
        return self._project_senses(sid, actor_id)

    def senses_excluding_effect(self, sid, actor_id, identity) -> CombatantSenses:
        """Project current senses without one exact active effect identity.

        Used when a conditional source has a narrower contact rule than the
        actor's other concurrent tremorsense grants. No fact is changed.
        """
        return self._project_senses(sid, actor_id, excluded=identity)

    def _project_senses(self, sid, actor_id, *, excluded=None) -> CombatantSenses:
        """Interpret base and target-local effect changes through the pinned RE."""
        row = self._sense_attributes(sid, actor_id)
        if row is None:
            if self._conn.execute(
                "SELECT actor_id FROM mechanical_actors WHERE session_id=? AND actor_id=?",
                (sid, actor_id),
            ).fetchone():
                raise StateMachineError("invalid_mechanical_state", "Actor attributes are missing")
            return CombatantSenses()
        base = {field: row[field] for field in self.SENSE_FIELDS}
        states = self.effects.for_targets(sid, (actor_id,))
        if excluded is not None:
            states = [
                state
                for state in states
                if (state["target_id"], state["id"], state["origin"]) != excluded
            ]
        if not states:
            return CombatantSenses(**base)
        effects = self.store.memory.model_json(_EFFECTS, canonical_json(states))
        changes = [
            PassiveEffectChange(
                key=change.key,
                mode=_PASSIVE_SENSE_MODES[change.mode],
                value=str(change.value),
                priority=change.priority,
            )
            for effect in effective_effects(effects)
            if not effect.disabled
            for change in effect.changes
            if change.key.startswith(_SENSE_KEY_PREFIX) and change.mode in _PASSIVE_SENSE_MODES
        ]
        if not changes:
            return CombatantSenses(**base)
        # The interpreter merges species values after applying ADD. Seed the
        # persisted base as initial changes so ADD applies to that base.
        initial = [
            PassiveEffectChange(
                key=f"{_SENSE_KEY_PREFIX}{field}",
                mode=_PASSIVE_SENSE_MODES["upgrade"],
                value=str(value),
            )
            for field, value in base.items()
            if value is not None
        ]
        return interpret_passive_stats(
            changes=(*initial, *changes), trait_grants=(), species_senses=None
        ).senses

    def equipment(self, sid, actor_id):
        """Read the actor's equipment references in a current or fixed scope."""
        if self.store._scope is None:
            with self.store.transaction():
                return self.equipment(sid, actor_id)
        if self.store.memory.in_current_read(sid):
            return self._current_equipment(sid, actor_id)
        return self._fixed_equipment(sid, actor_id)

    @instance_read("actor", None, "equipment")
    def _current_equipment(self, sid, actor_id):
        """Resolve the actor-owned field through canonical current item identities."""
        return self._load_equipment(sid, actor_id, current=True)

    @scoped_read
    def _fixed_equipment(self, sid, actor_id):
        """Read isolated candidate or old-scope identities without active references."""
        return self._load_equipment(sid, actor_id, current=False)

    def _load_equipment(self, sid, actor_id, *, current):
        """Validate the four selected items and build their detached rule fields."""
        row = self._conn.execute(
            "SELECT * FROM character_equipment WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()
        if row is None:
            return None
        slots = {}
        direct = []
        armor_category = None
        weapon_slug = row["legacy_weapon_slug"]
        for slot, field in EQUIPMENT_SLOTS:
            oid = row[field]
            slots[slot] = None
            if oid is None:
                continue
            item = self.world.object(sid, oid)
            if item is None:
                raise StateMachineError("invalid_mechanical_state", "Equipment item is missing")
            directly_carried = item["container_id"] is None
            if directly_carried:
                unit = self.world.require_item_unit(item)
                owner = item["carrier_id"]
                allowed = unit["accessible"] and self.world.object_hp(sid, oid) != 0
            else:
                unit = self.world.item_entry(item)
                owner = unit["owner_id"] if unit is not None else None
                allowed = unit is not None and unit["accessible"]
            if owner != actor_id or not allowed:
                raise StateMachineError(
                    "invalid_mechanical_state", "Equipment requires an accessible owned item"
                )
            slug = unit["item_slug"]
            if slot == "weapon":
                weapon_slug = slug
            else:
                asset = self.loader.get_item(slug)
                category = asset.armor_category if asset and asset.item_kind == "armor" else None
                if (
                    asset is None
                    or (slot == "armor" and category in (None, "shield"))
                    or (slot == "shield" and category != "shield")
                    or (slot == "offhand" and category == "shield")
                ):
                    message = {
                        "armor": "Worn armor identity differs",
                        "shield": "Shield identity differs",
                        "offhand": "Offhand item is a shield",
                    }[slot]
                    raise StateMachineError("invalid_mechanical_state", message)
                if slot == "armor":
                    armor_category = category
            slots[slot] = self.store.memory.active_item(item) if current else EquipmentItem(oid)
            direct.append((oid, slug if directly_carried and unit["quantity"] == 1 else None))
        return EquipmentBinding(
            **slots,
            grip=row["weapon_grip"],
            stealth_disadvantage=bool(row["stealth_disadvantage"]),
            weapon_slug=weapon_slug,
            armor_category=armor_category,
            direct_slugs=tuple(direct),
        )

    def validate_initial_equipment(self, sid, snapshot):
        """Resolve /19 and /20 bindings after ingress inventory materializes in the same UoW."""
        self.store.require_write()
        if snapshot.snapshot_schema_version not in ("engine-snapshot/19", "engine-snapshot/20"):
            return
        for actor in snapshot.character_states:
            resolved = self._rule_actor(sid, actor.entity_id)
            if any(
                resolved[field] != getattr(actor, field)
                for field in ("worn_armor", "shield_equipped", "other_hand_occupied")
            ):
                raise StateMachineError(
                    "invalid_mechanical_state", "Equipment declaration differs from bound items"
                )

    def initial_rule_actor(self, sid, approved):
        """Overlay existing core balances at the first mechanical initialization."""
        core = self._conn.execute(
            "SELECT current_hp,max_hp FROM characters WHERE session_id=? AND actor_id=?",
            (sid, approved.entity_id),
        ).fetchone()
        if core is None:
            return approved
        return approved.model_copy(
            update={"hp_current": core["current_hp"], "hp_max": core["max_hp"]}
        )

    def initialize_rule_components(self, sid, snapshot):
        """Initial explicit facts only; an existing owner record may never be reset."""
        self.store.require_write()
        data = snapshot.model_dump(
            mode="json", include={"casting_state", "effect_states", "resource_state"}
        )
        old = self._conn.execute(
            "SELECT schema_version,resource_state_present,casting_state_present "
            "FROM mechanical_components WHERE session_id=?",
            (sid,),
        ).fetchone()
        present = data["resource_state"] is not None
        casting_present = data["casting_state"] is not None
        modern_casting = snapshot.snapshot_schema_version == "engine-snapshot/20"
        if casting_present:
            snapshot_actors = {actor.entity_id: actor for actor in snapshot.character_states}
            inventory_owners = {
                entry.instance_id: entry.owner_id for entry in snapshot.inventory_state.entries
            }
            for actor in data["casting_state"]["actors"]:
                aid = actor["actor_id"]
                if aid not in snapshot_actors:
                    raise StateMachineError("invalid_combat_foundation", "Casting actor is missing")
                if modern_casting:
                    for source in actor["sources"]:
                        for spell in source["spells"]:
                            material = spell["material"]
                            if (
                                material is not None
                                and inventory_owners.get(material["inventory_instance_id"]) != aid
                            ):
                                raise StateMachineError(
                                    "invalid_combat_foundation",
                                    "Spell material must belong to its caster",
                                )
                else:
                    known = {slug for source in actor["sources"] for slug in source["known_spells"]}
                    if set(snapshot_actors[aid].spells_known) != known:
                        raise StateMachineError(
                            "invalid_combat_foundation",
                            "Actor spell mirror differs from casting sources",
                        )
                    known_pairs = {
                        (source["source_id"], spell_id)
                        for source in actor["sources"]
                        for spell_id in source["known_spells"]
                    }
                    if any(
                        (material["source_id"], material["spell_slug"]) not in known_pairs
                        for material in actor["materials"]
                    ):
                        raise StateMachineError(
                            "invalid_combat_foundation",
                            "Spell material requires a known spell in its source",
                        )
        expected = (
            snapshot.snapshot_schema_version,
            int(present),
            int(casting_present),
        )
        if old is not None:
            if (
                tuple(old) != expected
                or self.effects.for_targets(sid) != data["effect_states"]
                or self._casting_state(
                    sid, {actor.entity_id for actor in snapshot.character_states}
                )
                != data["casting_state"]
                or self._typed_resources(
                    sid, {actor.entity_id for actor in snapshot.character_states}, present
                )
                != data["resource_state"]
            ):
                raise StateMachineError(
                    "invalid_combat_foundation", "Initialization cannot reset component facts"
                )
            _, current_items = self.world.portable_inventory(sid)
            expected_items = {}
            for entry in snapshot.inventory_state.entries:
                for number in range(entry.quantity):
                    iid = (
                        entry.instance_id
                        if number == 0
                        else f"{entry.instance_id}.unit.{number:08d}"
                    )
                    expected_items[iid] = {
                        "instance_id": iid,
                        "owner_id": entry.owner_id,
                        "item_slug": entry.item_slug,
                        "quantity": 1,
                        "charges_remaining_per_unit": entry.charges_remaining_per_unit,
                        "accessible": entry.accessible,
                    }
            if current_items != expected_items:
                raise StateMachineError(
                    "invalid_combat_foundation", "Initialization cannot reset item facts"
                )
            return
        self._conn.execute(
            "INSERT INTO mechanical_components "
            "(session_id,schema_version,resource_state_present,casting_state_present) "
            "VALUES (?,?,?,?)",
            (sid, *expected),
        )
        self.effects.initialize(sid, data["effect_states"])
        if casting_present:
            for actor_ordinal, actor in enumerate(data["casting_state"]["actors"]):
                aid = actor["actor_id"]
                self._conn.execute(
                    "INSERT INTO character_casting VALUES (?,?,?,?)",
                    (sid, aid, actor_ordinal, int(actor["verbal_available"])),
                )
                for source_ordinal, source in enumerate(actor["sources"]):
                    source_id = source["source_id"]
                    self._conn.execute(
                        "INSERT INTO character_spell_sources VALUES (?,?,?,?,?,?)",
                        (
                            sid,
                            aid,
                            source_id,
                            source["class_slug"],
                            source["ability"],
                            source_ordinal,
                        ),
                    )
                    prepared = {slug: n for n, slug in enumerate(source.get("prepared_spells", ()))}
                    legacy_materials = {
                        (material["source_id"], material["spell_slug"]): material
                        for material in actor.get("materials", ())
                    }
                    spells = (
                        source["spells"]
                        if modern_casting
                        else [
                            {
                                "spell_id": slug,
                                "prepared_order": prepared.get(slug),
                                "material": legacy_materials.get((source_id, slug)),
                            }
                            for slug in source["known_spells"]
                        ]
                    )
                    for known_ordinal, spell in enumerate(spells):
                        material = spell["material"]
                        self._conn.execute(
                            "INSERT INTO character_spells "
                            "(session_id,actor_id,source_id,spell_slug,known_ordinal,prepared_ordinal,"
                            "material_instance_id,material_available) VALUES (?,?,?,?,?,?,?,?)",
                            (
                                sid,
                                aid,
                                source_id,
                                spell["spell_id"],
                                known_ordinal,
                                spell["prepared_order"],
                                None if material is None else material["inventory_instance_id"],
                                None if material is None else int(material["available"]),
                            ),
                        )
                self._conn.execute(
                    "UPDATE mechanical_actors SET state_json=json_remove(state_json,"
                    "'$.spells_known') WHERE session_id=? AND actor_id=?",
                    (sid, aid),
                )
        if present:
            for ordinal, pool in enumerate(data["resource_state"]["pools"]):
                metadata = {
                    k: v
                    for k, v in pool.items()
                    if k not in ("owner_id", "pool_id", "kind", "current", "maximum")
                }
                self._insert_resource(
                    sid,
                    pool["owner_id"],
                    pool["pool_id"],
                    pool["kind"],
                    pool["current"],
                    pool["maximum"],
                    ordinal,
                    metadata,
                )
        else:
            for actor in snapshot.character_states:
                ordinal = 0
                for field, kind in (
                    ("spell_slots", "standalone_spell_slot"),
                    ("pact_slots", "standalone_pact_slot"),
                ):
                    for level, balance in getattr(actor, field).items():
                        self._insert_resource(
                            sid,
                            actor.entity_id,
                            f"{field}:{level}",
                            kind,
                            balance,
                            None,
                            ordinal,
                            {"level": int(level)},
                        )
                        ordinal += 1
        for actor in snapshot.character_states:
            retained = (
                {
                    key: value
                    for key, value in actor.custom_counters.items()
                    if not key.startswith("feature_use:")
                }
                if present
                else actor.custom_counters
            )
            self._conn.execute(
                "UPDATE mechanical_actors SET state_json=json_remove(state_json,"
                "'$.spell_slots','$.pact_slots') WHERE session_id=? AND actor_id=?",
                (sid, actor.entity_id),
            )
            self._conn.execute(
                "UPDATE mechanical_actors SET state_json=json_set(state_json,"
                "'$.custom_counters',json(?)) WHERE session_id=? AND actor_id=?",
                (canonical_json(retained), sid, actor.entity_id),
            )
        # The snapshot is ingress data. Materialize each owned unit once in the
        # World object relation before ordinary item initialization checks it.
        for entry in snapshot.inventory_state.entries:
            source_unit = {
                "item_slug": entry.item_slug,
                "quantity": 1,
                "charges_remaining_per_unit": entry.charges_remaining_per_unit,
                "accessible": entry.accessible,
            }
            if not valid_item_unit(source_unit) or not 1 <= entry.quantity <= 128:
                raise StateMachineError("invalid_item_foundation", "Ambiguous initial item unit")
            for number in range(entry.quantity):
                iid = entry.instance_id if number == 0 else f"{entry.instance_id}.unit.{number:08d}"
                if number == 0 and self.world.object(sid, iid) is None:
                    self._retire_legacy_item_identity(sid, entry.owner_id, entry.item_slug)
                self.world.materialize_owned_item(
                    sid, iid, entry.owner_id, source_unit, allow_existing=True
                )
        for actor in snapshot.character_states:
            current = set(self.carried_slugs(sid, actor.entity_id))
            expected = set(actor.carried_item_slugs)
            if not current <= expected:
                raise StateMachineError("invalid_item_foundation", "Initial item slugs disagree")
            if current != expected:
                for position, slug in enumerate(actor.carried_item_slugs):
                    if slug in current:
                        continue
                    unit = {
                        "item_slug": slug,
                        "quantity": 1,
                        "charges_remaining_per_unit": 0,
                        "accessible": True,
                    }
                    if not valid_item_unit(unit):
                        raise StateMachineError(
                            "invalid_item_foundation", "Invalid actor item slug"
                        )
                    iid = core_item_id(sid, actor.entity_id, position)
                    self.world.materialize_owned_item(sid, iid, actor.entity_id, unit)

    def _casting_state(self, sid, owner_ids=None):
        """Project keyed casting facts in original actor, source, spell and material order.

        A selected owner read stays within that actor's SessionMemory range. The
        unfiltered form is reserved for explicit initialization and recovery audits.
        """
        component = self._conn.execute(
            "SELECT schema_version,casting_state_present FROM mechanical_components "
            "WHERE session_id=?",
            (sid,),
        ).fetchone()
        if component is None or not component["casting_state_present"]:
            return None
        modern = component["schema_version"] == "engine-snapshot/20"
        actors = []
        if owner_ids is None:
            rows = self._conn.execute(
                "SELECT * FROM character_casting WHERE session_id=? ORDER BY ordinal", (sid,)
            )
        else:
            rows = (
                row
                for aid in owner_ids
                for row in self._conn.execute(
                    "SELECT * FROM character_casting WHERE session_id=? AND actor_id=?",
                    (sid, aid),
                )
            )
        for row in rows:
            aid = row["actor_id"]
            sources = []
            retained = None
            grouped = {}
            if modern and self.store._scope is not None and not self.store._scope.write:
                retained = self.all_spells(sid, aid)
                for spell in retained:
                    grouped.setdefault(spell.source.source_id, []).append(spell)
            for source in self._conn.execute(
                "SELECT * FROM character_spell_sources WHERE session_id=? AND actor_id=? "
                "ORDER BY ordinal",
                (sid, aid),
            ):
                spells = (
                    ()
                    if retained is not None
                    else tuple(
                        self._conn.execute(
                            "SELECT spell_slug,known_ordinal,prepared_ordinal,material_instance_id,"
                            "material_available FROM character_spells "
                            "WHERE session_id=? AND actor_id=? AND source_id=? "
                            "ORDER BY known_ordinal",
                            (sid, aid, source["source_id"]),
                        )
                    )
                )
                base = {
                    "source_id": source["source_id"],
                    "class_slug": source["class_slug"],
                    "ability": source["ability"],
                }
                if modern:
                    selected = (
                        grouped.get(source["source_id"], ()) if retained is not None else spells
                    )
                    base["spells"] = [
                        {
                            "spell_id": spell.spell_id
                            if retained is not None
                            else spell["spell_slug"],
                            "prepared_order": (
                                spell.prepared_order
                                if retained is not None
                                else spell["prepared_ordinal"]
                            ),
                            "material": (
                                None
                                if (
                                    spell.material_item
                                    if retained is not None
                                    else spell["material_instance_id"]
                                )
                                is None
                                else {
                                    "inventory_instance_id": (
                                        spell.material_item.object_id
                                        if retained is not None
                                        else spell["material_instance_id"]
                                    ),
                                    "available": (
                                        spell.material_available
                                        if retained is not None
                                        else bool(spell["material_available"])
                                    ),
                                }
                            ),
                        }
                        for spell in selected
                    ]
                else:
                    base["known_spells"] = [spell["spell_slug"] for spell in spells]
                    base["prepared_spells"] = [
                        spell["spell_slug"]
                        for spell in sorted(
                            (s for s in spells if s["prepared_ordinal"] is not None),
                            key=lambda spell: spell["prepared_ordinal"],
                        )
                    ]
                sources.append(base)
            materials = (
                []
                if modern
                else [
                    {
                        "source_id": spell["source_id"],
                        "spell_slug": spell["spell_slug"],
                        "inventory_instance_id": spell["material_instance_id"],
                        "available": bool(spell["material_available"]),
                    }
                    for spell in self._conn.execute(
                        "SELECT source_id,spell_slug,material_instance_id,material_available "
                        "FROM character_spells WHERE session_id=? AND actor_id=? "
                        "AND material_instance_id IS NOT NULL "
                        "ORDER BY source_id,known_ordinal",
                        (sid, aid),
                    )
                ]
            )
            actors.append(
                (
                    row["ordinal"],
                    {
                        "actor_id": aid,
                        "verbal_available": bool(row["verbal_available"]),
                        "sources": sources,
                        **({} if modern else {"materials": materials}),
                    },
                )
            )
        return {"actors": [actor for _, actor in sorted(actors, key=lambda entry: entry[0])]}

    @instance_read("actor", None, "all_spells")
    def all_spells(self, sid, actor_id):
        """Retain this actor's ordered spell and source identities under one owner field."""
        sources = tuple(
            self._conn.execute(
                "SELECT source_id,class_slug,ability,ordinal FROM character_spell_sources "
                "WHERE session_id=? AND actor_id=? ORDER BY ordinal",
                (sid, actor_id),
            )
        )
        result = []
        for source in sources:
            reference = SpellSourceRef(
                source["source_id"], source["class_slug"], source["ability"], source["ordinal"]
            )
            for spell in self._conn.execute(
                "SELECT spell_slug,known_ordinal,prepared_ordinal,material_instance_id,"
                "material_available FROM character_spells "
                "WHERE session_id=? AND actor_id=? AND source_id=? ORDER BY known_ordinal",
                (sid, actor_id, reference.source_id),
            ):
                item = None
                if spell["material_instance_id"] is not None:
                    row = self.world.object(sid, spell["material_instance_id"])
                    if row is None:
                        raise StateMachineError(
                            "invalid_mechanical_state", "Spell material is missing"
                        )
                    item = self.store.memory.active_item(row)
                result.append(
                    SpellRef(
                        spell["spell_slug"],
                        spell["known_ordinal"],
                        spell["prepared_ordinal"],
                        reference,
                        item,
                        None if item is None else bool(spell["material_available"]),
                    )
                )
        return tuple(result)

    @instance_read("actor", None, "prepared_spells")
    def prepared_spells(self, sid, actor_id):
        """Retain ordered prepared selections sharing all-spell material identities."""
        return tuple(
            sorted(
                (
                    spell
                    for spell in self.all_spells(sid, actor_id)
                    if spell.prepared_order is not None
                ),
                key=lambda spell: (spell.source.ordinal, spell.prepared_order),
            )
        )

    def casting_material_bound(self, sid, actor_id, object_ids):
        """Check exact moved object IDs through the material reverse index.

        Availability is an RE input, not permission to transfer a bound item.
        """
        return any(
            self._conn.execute(
                "SELECT material_instance_id FROM character_spells "
                "WHERE session_id=? AND actor_id=? AND material_instance_id=?",
                (sid, actor_id, oid),
            ).fetchone()
            for oid in object_ids
        )

    @scoped_read
    def binding_components(self, sid, actor_id):
        """Validate typed resources and effects needed for World item admission."""
        row = self._conn.execute(
            "SELECT * FROM mechanical_components WHERE session_id=?", (sid,)
        ).fetchone()
        if row is None or row["schema_version"] not in (
            "engine-snapshot/17",
            "engine-snapshot/18",
            "engine-snapshot/19",
            "engine-snapshot/20",
        ):
            raise StateMachineError(
                "unsupported_mechanical_version", "Explicit component facts are missing"
            )
        try:
            values = {"effects_json": self.effects.for_targets(sid, {actor_id})}
            resources = self._typed_resources(sid, {actor_id}, row["resource_state_present"])
            if resources is not None:
                ResourceState.model_validate({**resources, "pools": tuple(resources["pools"])})
            effects = self.store.memory.model_json(_EFFECTS, canonical_json(values["effects_json"]))
            return {
                "effect_states": [v.model_dump(mode="json") for v in effects],
            }
        except (ValueError, KeyError, TypeError) as exc:
            raise StateMachineError(
                "invalid_mechanical_state", "Stored binding facts are incomplete"
            ) from exc

    def rule_components(self, sid, owner_ids, *, full_audit=False):
        """Validate and return rule components for the requested owners."""
        inventory, values = self._validated_rule_components(sid, owner_ids, full_audit=full_audit)
        return {"inventory_state": inventory.model_dump(mode="json"), **values}

    @scoped_read
    def _validated_rule_components(self, sid, owner_ids, *, full_audit=False):
        """Read persisted facts, then project owners; absence is never an empty component."""
        row = self._conn.execute(
            "SELECT * FROM mechanical_components WHERE session_id=?", (sid,)
        ).fetchone()
        if row is None or row["schema_version"] not in (
            "engine-snapshot/17",
            "engine-snapshot/18",
            "engine-snapshot/19",
            "engine-snapshot/20",
        ):
            raise StateMachineError(
                "unsupported_mechanical_version", "Explicit component facts are missing"
            )
        try:
            all_rows = ()
            audit_resources = None
            if full_audit:
                all_rows = tuple(
                    self._conn.execute(
                        "SELECT * FROM character_resources WHERE session_id=?", (sid,)
                    )
                )
                typed_kinds = {"hit_dice", "spell_slot", "pact_slot", "feature_uses"}
                if any(
                    (r["kind"] not in typed_kinds)
                    if row["resource_state_present"]
                    else (r["kind"] in typed_kinds)
                    for r in all_rows
                ):
                    raise StateMachineError(
                        "invalid_mechanical_state", "Resource kind disagrees with component"
                    )
                if row["resource_state_present"]:
                    audit_resources = {
                        "pools": [
                            value
                            for _, value in sorted(
                                (self._typed_pool(resource) for resource in all_rows),
                                key=lambda entry: entry[0],
                            )
                        ]
                    }
            _, item_entries = self.world.portable_inventory(
                sid, owner_ids=None if full_audit else owner_ids
            )
            item_inventory = {"entries": list(item_entries.values())}
            if not full_audit:
                values = {"effects_json": self.effects.for_targets(sid, owner_ids)}
                values["inventory"] = item_inventory
            else:
                values = {"effects_json": self.effects.for_targets(sid)}
                values["inventory"] = item_inventory
            values["resource_state"] = (
                audit_resources
                if full_audit
                else self._typed_resources(sid, owner_ids, row["resource_state_present"])
            )
            inventory = values["inventory"]
            resources = values["resource_state"]
            casting = self._casting_state(sid, None if full_audit else owner_ids)
            if full_audit and casting is None:
                for table in (
                    "character_casting",
                    "character_spell_sources",
                    "character_spells",
                ):
                    if self._conn.execute(
                        f"SELECT session_id FROM {table} WHERE session_id=?", (sid,)
                    ).fetchone():
                        raise ValueError("casting rows exist without component marker")
            effects = values["effects_json"]
            typed_inventory = self._inventory_model(inventory)
            inventory = typed_inventory.model_dump(mode="json")
            resources = (
                ResourceState.model_validate(
                    {**resources, "pools": tuple(resources["pools"])}
                    if isinstance(resources["pools"], list)
                    else resources
                ).model_dump(mode="json")
                if resources is not None
                else None
            )
            casting_model = CastingState
            if row["schema_version"] == "engine-snapshot/20":
                from dnd5e_engine.evaluation_casting import CastingStateV20

                casting_model = CastingStateV20
            casting = (
                self.store.memory.model_json(casting_model, canonical_json(casting)).model_dump(
                    mode="json"
                )
                if casting is not None
                else None
            )
            effects = self.store.memory.model_json(_EFFECTS, canonical_json(effects))
            effects = [v.model_dump(mode="json") for v in effects]
            references = {v["owner_id"] for v in inventory["entries"]}
            references |= {v["target_id"] for v in effects}
            if resources is not None:
                references |= {v["owner_id"] for v in resources["pools"]}
            if casting is not None:
                references |= {v["actor_id"] for v in casting["actors"]}
            if full_audit:
                # Initialization/recovery diagnostics include rows omitted by a
                # caller's mechanical roster; ordinary owner reads stay local.
                references |= {r["actor_id"] for r in all_rows}
            for owner in references:
                if (
                    self._conn.execute(
                        "SELECT 1 FROM mechanical_actors m JOIN characters c "
                        "USING(session_id,actor_id) "
                        "WHERE m.session_id=? AND m.actor_id=?",
                        (sid, owner),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("component owner is missing")
            inventory["entries"] = [v for v in inventory["entries"] if v["owner_id"] in owner_ids]
            if resources is not None:
                resources["pools"] = [v for v in resources["pools"] if v["owner_id"] in owner_ids]
            if casting is not None:
                casting["actors"] = [v for v in casting["actors"] if v["actor_id"] in owner_ids]
            typed_inventory = typed_inventory.model_copy(
                update={
                    "entries": tuple(e for e in typed_inventory.entries if e.owner_id in owner_ids)
                }
            )
            return typed_inventory, {
                "resource_state": resources,
                "casting_state": casting,
                "effect_states": [v for v in effects if v["target_id"] in owner_ids],
            }
        except (ValueError, KeyError, TypeError) as exc:
            raise StateMachineError(
                "invalid_mechanical_state", "Stored component closure is incomplete"
            ) from exc

    def prepare_inventory_change(self, sid, roots):
        """Validate the old moving/accessibility subtree and affected owner closure."""
        rows = self.world.descendant_objects(sid, roots)
        portable = [r for r in rows if r["item_slug"] is not None]
        if not portable:
            return None
        identities = {r["object_id"] for r in portable}
        owners = {self.world.placement(sid, r["object_id"])["carrier_id"] for r in portable}
        owners.discard(None)
        for owner in owners:
            if self.binding_actor(sid, owner) is None:
                raise StateMachineError("unsupported_actor_state", "Item owner lacks actor facts")
            self.binding_components(sid, owner)
        self._inventory_selection(sid, set(), identities)
        return identities, owners

    @instance_read("actor", None, "inventory", maintained="inventory")
    def inventory_model(self, sid, actor_id):
        """Validated owner projection; source relationships remain World facts."""
        inventory, _ = self._validated_rule_components(sid, {actor_id}, full_audit=False)
        return inventory

    @staticmethod
    def _inventory_model(value):
        # Pinned RE models are strict. JSON arrays represent tuples at this
        # boundary; normalize only that container, never relax scalar checking.
        """Strictly validate inventory, converting only the JSON entry array to a tuple."""
        entries = value["entries"]
        return InventoryState.model_validate(
            {**value, "entries": tuple(entries)} if isinstance(entries, list) else value
        )

    @scoped_read
    def _inventory_selection(self, sid, owners, identities):
        """Select item instance facts for the requested owner and identity scope."""
        stored = self._conn.execute(
            "SELECT * FROM mechanical_components WHERE session_id=?", (sid,)
        ).fetchone()
        if stored is None:
            raise StateMachineError("unsupported_mechanical_version", "Explicit inventory missing")
        _, entries = self.world.portable_inventory(sid, owner_ids=owners, instance_ids=identities)
        selected = {"entries": list(entries.values())}
        return self._inventory_model(selected).model_dump(mode="json")

    def project_item_transfer(self, sid, before, after, *, prepared=None):
        """Preserve item identity/quantity while updating inventory projection after world
        transfer.
        """
        self.store.require_write()
        if before["object_id"] != after["object_id"] or item_unit_from_row(
            before
        ) != item_unit_from_row(after):
            raise StateMachineError("invalid_mechanical_state", "Invalid instance transition")
        if prepared is None:
            # Compatibility for trusted direct participants: reconstruct the old
            # root pose. Production callers validate the entire old subtree first.
            old_entry = self.world.item_entry(before)
            prepared = ({before["object_id"]}, {old_entry["owner_id"]} if old_entry else set())
        self.sync_inventory_change(sid, prepared)

    def sync_inventory_change(self, sid, prepared):
        """Validate affected item facts after a transfer; no inventory mirror is written."""
        self.store.require_write()
        if prepared is None:
            return
        identities, _ = prepared
        _, expected = self.world.portable_inventory(sid, owner_ids=set(), instance_ids=identities)
        self._inventory_model({"entries": list(expected.values())})
        for owner in {entry["owner_id"] for entry in expected.values()}:
            if self.binding_actor(sid, owner) is None:
                raise StateMachineError("unsupported_actor_state", "Item owner lacks actor facts")
            self.binding_components(sid, owner)

    def initialize_rule_positions(self, row, cells, scene_id):
        """Called only at fresh session creation before events or world movement."""
        self.store.require_write()
        if row["world_version"] != 0 or row["event_seq"] != 0:
            raise StateMachineError(
                "invalid_combat_foundation", "Initial positions require a fresh session"
            )
        for actor_id, cell in cells.items():
            current = self.position(row["session_id"], actor_id)
            if current.scene_id != scene_id:
                raise StateMachineError(
                    "invalid_combat_foundation", "Initial position scenes disagree"
                )
            if current.cell is not None:
                continue
            self._conn.execute(
                "UPDATE actors SET cell=? WHERE session_id=? AND actor_id=?",
                (cell, row["session_id"], actor_id),
            )

    @scoped_read
    def _rule_actor(
        self,
        sid,
        actor_id,
        *,
        include_carried=True,
        carried_slugs=None,
        include_resources=True,
        include_proficiencies=True,
    ):
        """Combine core facts with detached current or fixed equipment values.

        A binding read skips inventory projection. Complete snapshot reads verify
        owner, accessibility and kind for only the bound instances. A supplied
        snapshot-local carried slugs only replace the carried-slug projection.
        """
        actor = self._conn.execute(
            "SELECT state_json FROM mechanical_actors WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()
        core = self._conn.execute(
            "SELECT * FROM characters WHERE session_id=? AND actor_id=?", (sid, actor_id)
        ).fetchone()
        if actor is None or core is None:
            return None
        parse = self.store.memory.json
        if self.resources(sid, actor_id, kind="synthetic") or parse(core["effects_json"]):
            raise StateMachineError(
                "invalid_mechanical_state", "Legacy core resources/effects have no typed source"
            )
        life = self.life(sid, actor_id)
        data = parse(actor["state_json"])
        data.update(self.attributes(sid, actor_id, include_proficiencies=include_proficiencies))
        data.update(
            temp_hp=life["temp_hp"],
            is_alive=bool(life["is_alive"]),
            death_saves=(
                None
                if life["death_save_successes"] is None
                else {
                    "successes": life["death_save_successes"],
                    "failures": life["death_save_failures"],
                    "is_stable": bool(life["death_save_stable"]),
                }
            ),
            last_damaged_by=life["last_damaged_by"],
        )
        data["conditions"] = self.effects.actor_conditions(
            sid, actor_id, self.conditions(sid, actor_id)
        )
        data["concentration_effect_id"] = self.effects.concentration_reference(sid, actor_id)
        component = self._conn.execute(
            "SELECT schema_version,resource_state_present FROM mechanical_components "
            "WHERE session_id=?",
            (sid,),
        ).fetchone()
        modern_actor = component is not None and component["schema_version"] == "engine-snapshot/20"
        if (
            include_carried
            and not modern_actor
            and self._conn.execute(
                "SELECT actor_id FROM character_casting WHERE session_id=? AND actor_id=?",
                (sid, actor_id),
            ).fetchone()
        ):
            seen = set()
            data["spells_known"] = []
            for spell in self._conn.execute(
                "SELECT s.spell_slug AS spell_id FROM character_spells s "
                "JOIN character_spell_sources src USING(session_id,actor_id,source_id) "
                "WHERE s.session_id=? AND s.actor_id=? "
                "ORDER BY src.ordinal,s.known_ordinal",
                (sid, actor_id),
            ):
                if spell["spell_id"] not in seen:
                    seen.add(spell["spell_id"])
                    data["spells_known"].append(spell["spell_id"])
        if modern_actor:
            data.pop("spells_known", None)
        if include_resources and component is not None and component["resource_state_present"]:
            pools = [value for _, value in self._typed_owner_resources(sid, actor_id)]
            data["spell_slots"] = {
                str(p["level"]): p["current"] for p in pools if p["kind"] == "spell_slot"
            }
            data["pact_slots"] = {
                str(p["level"]): p["current"] for p in pools if p["kind"] == "pact_slot"
            }
            other = data.get("custom_counters", {})
            data["custom_counters"] = {
                **other,
                **{
                    "feature_use:" + p["feature_slug"]: {"spent": p["maximum"] - p["current"]}
                    for p in pools
                    if p["kind"] == "feature_uses"
                },
            }
        elif include_resources and component is not None:
            rows = self.resources(sid, actor_id)
            data["spell_slots"] = {
                str(self.store.memory.json(r["metadata_json"])["level"]): r["current"]
                for r in rows
                if r["kind"] == "standalone_spell_slot"
            }
            data["pact_slots"] = {
                str(self.store.memory.json(r["metadata_json"])["level"]): r["current"]
                for r in rows
                if r["kind"] == "standalone_pact_slot"
            }
            data.setdefault("custom_counters", {})
        equipment = self.equipment(sid, actor_id)
        if equipment is not None:
            for field in _EQUIPMENT_METADATA:
                data.pop(field, None)
            data.update(equipment.rule_fields(resolved=include_carried))
        elif "stealth_disadvantage" not in data:
            raise StateMachineError("invalid_mechanical_state", "Actor equipment facts are missing")
        explicit_equipment = equipment is None and "worn_armor_instance_id" in data
        held = data.get("held_weapon_instance_id") if equipment is None else None
        if held is not None and include_carried:
            item = self.world.object(sid, held)
            entry = self.world.item_entry(item) if item is not None else None
            if entry is None or entry["owner_id"] != actor_id or not entry["accessible"]:
                raise StateMachineError(
                    "invalid_mechanical_state", "Held weapon requires an accessible owned item"
                )
            data["weapon_in_hands"] = entry["item_slug"]
        if include_carried and explicit_equipment:
            for field, expected_kind in (
                ("worn_armor_instance_id", "armor"),
                ("equipped_shield_instance_id", "shield"),
                ("offhand_item_instance_id", "offhand"),
            ):
                identity = data.get(field)
                if identity is None:
                    continue
                item = self.world.object(sid, identity)
                entry = self.world.item_entry(item) if item is not None else None
                if entry is None or entry["owner_id"] != actor_id or not entry["accessible"]:
                    raise StateMachineError(
                        "invalid_mechanical_state", "Equipment requires an accessible owned item"
                    )
                asset = self.loader.get_item(entry["item_slug"])
                if expected_kind == "armor":
                    if (
                        asset is None
                        or asset.item_kind != "armor"
                        or asset.armor_category == "shield"
                    ):
                        raise StateMachineError(
                            "invalid_mechanical_state", "Worn armor identity differs"
                        )
                    data["worn_armor"] = asset.armor_category
                elif expected_kind == "shield":
                    if (
                        asset is None
                        or asset.item_kind != "armor"
                        or asset.armor_category != "shield"
                    ):
                        raise StateMachineError(
                            "invalid_mechanical_state", "Shield identity differs"
                        )
                    data["shield_equipped"] = True
                elif asset is None or (
                    asset.item_kind == "armor" and asset.armor_category == "shield"
                ):
                    raise StateMachineError("invalid_mechanical_state", "Offhand item is a shield")
            data["other_hand_occupied"] = bool(
                data.get("equipped_shield_instance_id") or data.get("offhand_item_instance_id")
            )
            data.setdefault("worn_armor", None)
            data.setdefault("shield_equipped", False)
            for field in (
                "worn_armor_instance_id",
                "equipped_shield_instance_id",
                "offhand_item_instance_id",
            ):
                if data[field] is None:
                    data.pop(field)
        if include_carried:
            worn = []
            for binding in self._conn.execute(
                "SELECT instance_id,item_slug FROM character_worn_items "
                "WHERE session_id=? AND actor_id=? ORDER BY instance_id",
                (sid, actor_id),
            ):
                item = self.world.object(sid, binding["instance_id"])
                entry = self.world.item_entry(item) if item is not None else None
                if (
                    entry is None
                    or entry["owner_id"] != actor_id
                    or entry["item_slug"] != binding["item_slug"]
                    or not entry["accessible"]
                    or entry["quantity"] != 1
                    or item["container_id"] is not None
                ):
                    raise StateMachineError(
                        "invalid_mechanical_state", "Worn item requires exact direct ownership"
                    )
                worn.append(binding["instance_id"])
            if worn:
                data["worn_item_instance_ids"] = worn
        data.update(
            hp_current=core["current_hp"],
            hp_max=core["max_hp"],
            carried_item_slugs=(
                self.carried_slugs(sid, actor_id) if carried_slugs is None else list(carried_slugs)
            )
            if include_carried
            else [],
        )
        return data

    def binding_actor(self, sid, actor_id):
        """Read equipment metadata without assembling inventory or resource mirrors."""
        return self._rule_actor(
            sid,
            actor_id,
            include_carried=False,
            include_resources=False,
            include_proficiencies=False,
        )

    @scoped_read
    def rule_actors(self, sid, owner_ids=None):
        """Return mechanical actors for the requested owners in stable order."""
        if owner_ids is None:
            owner_ids = tuple(
                row[0]
                for row in self._conn.execute(
                    "SELECT actor_id FROM mechanical_actors WHERE session_id=?", (sid,)
                )
            )
        return {
            owner: data
            for owner in sorted(owner_ids)
            if (data := self._rule_actor(sid, owner)) is not None
        }

    def combat_rule_actors(self, sid, owner_ids, inventory_entries):
        """Build combat actors from the validated component inventory.

        The bundle belongs to the caller's fixed read scope. Equipment fields carry
        their own exact World identity dependencies and supply detached rule values.
        """
        slugs = {owner: {} for owner in owner_ids}
        for entry in inventory_entries:
            if entry["owner_id"] in slugs:
                slugs[entry["owner_id"]][entry["item_slug"]] = None
        actors = {
            owner: data
            for owner in sorted(owner_ids)
            if (data := self._rule_actor(sid, owner, carried_slugs=tuple(slugs[owner]))) is not None
        }
        return actors

    @scoped_read
    def actor_exists(self, sid, actor_id):
        """Identify an exact persisted actor without reading spatial facts."""
        return (
            self._conn.execute(
                "SELECT 1 FROM actors WHERE session_id=? AND actor_id=?", (sid, actor_id)
            ).fetchone()
            is not None
        )

    def position(self, sid, actor_id):
        """Read authoritative actor position, rejecting a missing character."""
        from .spatial import SpatialPosition

        actor = self.world.carrier_position(sid, actor_id)
        if actor is None:
            raise StateMachineError("invalid_actor", "No authorized actor")
        return SpatialPosition(actor[0], actor[1])


class WorldFacts:
    """Maintain authoritative world facts and access persisted instance configuration."""

    @instance_read("scene", "environment", "facts")
    def environment(self, sid, scene_id):
        """Read and decode the scene's environment facts."""
        from .environment_commands import facts_from_data

        row = self._conn.execute(
            "SELECT facts_json FROM scene_environments WHERE session_id=? AND scene_id=?",
            (sid, scene_id),
        ).fetchone()
        return facts_from_data(self.store.memory.json(row[0])) if row else None

    def set_environment(self, sid, scene_id, facts):
        """Require typed environment facts and persist them for the scene."""
        from .environment_commands import environment_facts_to_data
        from .environment_models import EnvironmentSceneFacts

        self.store.require_write()
        if type(facts) is not EnvironmentSceneFacts:
            raise StateMachineError("invalid_environment", "Expected typed Scene environment facts")
        self._conn.execute(
            "INSERT INTO scene_environments VALUES (?,?,?) "
            "ON CONFLICT(session_id,scene_id) DO UPDATE SET facts_json=excluded.facts_json",
            (sid, scene_id, canonical_json(environment_facts_to_data(facts))),
        )

    @instance_read("object", None, "facts")
    def object(self, sid, oid):
        """Read an object row by Session and object identity, or return None."""
        return self._conn.execute(
            "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, oid)
        ).fetchone()

    @scoped_read
    def carrier_position(self, sid, actor_id):
        """Read the carrier's current scene and cell position."""
        return self._conn.execute(
            "SELECT current_scene_id,cell FROM actors WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()

    @staticmethod
    def template_object_definition(package, oid):
        """Find the Package object definition used as a creation reference."""
        return next((d for d in package.world_objects if d.object_id == oid), None)

    @property
    def instances(self):
        """Return the instance configuration interface."""
        return self._instances

    def object_definition(self, sid, oid):
        """Read the object's current definition through persisted instance configuration."""
        return self.instances.object_definition(sid, oid)

    def object_invisible(self, sid, oid) -> bool:
        """Read one object's optional creation-time Invisible fact from its instance."""
        return self.instances.get(sid, "object", oid).get("invisible", False)

    def __init__(self, store):
        """Bind world storage and initialize its instance accessor."""
        from .instance_data import InstanceData

        self.store = store
        self._instances = InstanceData(self)

    @property
    def _conn(self):
        """Return the shared world-facts connection."""
        return self.store.connection

    def initialize_world(self, sid, package, *, spatial_light_evidence=False):
        """Initialize persisted world/object facts and Session instance configuration."""
        self.store.require_write()
        self.instances.initialize_world(sid, package, spatial_light_evidence=spatial_light_evidence)
        self._conn.executemany(
            "INSERT OR IGNORE INTO scene_environment_versions(session_id,scene_id) VALUES (?,?)",
            [(sid, scene.scene_id) for scene in package.scenes],
        )
        self._conn.execute(
            "INSERT INTO world_states(session_id,flags_json) VALUES (?,?)",
            (sid, canonical_json(dict(package.world_flags))),
        )
        self._conn.executemany(
            "INSERT INTO world_facts VALUES (?,?,?,?,?)",
            [
                (sid, f.fact_id, f.text, f.visibility, canonical_json(f.authorized_npc_ids))
                for f in package.world_facts
            ],
        )

    def initialize_scene(self, sid, scene_id, package):
        """Initialize the scene and its instance facts only when not already present."""
        self.store.require_write()
        if self._conn.execute(
            "SELECT 1 FROM scene_instances WHERE session_id=? AND scene_id=?", (sid, scene_id)
        ).fetchone():
            return False
        scene = package.scene(scene_id)
        self._conn.execute(
            "INSERT INTO scene_instances VALUES (?,?,?,0)",
            (sid, scene_id, canonical_json(dict(scene.flags))),
        )
        self._conn.executemany(
            "INSERT INTO objects(session_id,object_id,scene_id,state,cell,orientation) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(session_id,object_id) DO NOTHING",
            [
                (sid, ob.object_id, scene_id, ob.state, ob.initial_cell, ob.initial_orientation)
                for ob in scene.objects
            ],
        )
        self.instances.initialize_scene(sid, scene_id, package)
        return True

    def initialize_object_capabilities(self, sid, scene_id, package):
        """Initialize the capability state declared by the object's creation definition."""
        self.store.require_write()
        for d in package.world_objects:
            obj = self.object(sid, d.object_id)
            if obj is None or obj["scene_id"] != scene_id:
                continue
            if c := d.capability("mechanism"):
                components = (
                    d,
                    *(self.template_object_definition(package, oid) for oid in c.components),
                )
                broken = any(
                    definition.capability("durability")
                    and definition.capability("durability").initial_hp == 0
                    for definition in components
                )
                self._conn.execute(
                    "INSERT INTO object_mechanisms VALUES (?,?,?,?,0,NULL,NULL) "
                    "ON CONFLICT(session_id,object_id) DO NOTHING",
                    (sid, d.object_id, "broken" if broken else c.initial_status, c.max_uses),
                )
            if c := d.capability("light"):
                fuel = LightFuel(remaining_seconds=c.fuel_seconds)
                if c.initial_enabled and c.fuel_seconds is not None:
                    fuel = LightFuel(
                        expires_tick=deadline_after_seconds(*self.time_state(sid), c.fuel_seconds),
                    )
                self._conn.execute(
                    "INSERT INTO object_lights(session_id,object_id,enabled,mode,"
                    "fuel_remaining_seconds,fuel_expires_tick) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(session_id,object_id) DO NOTHING",
                    (
                        sid,
                        d.object_id,
                        int(c.initial_enabled),
                        c.initial_mode,
                        fuel.remaining_seconds,
                        fuel.expires_tick,
                    ),
                )
            if c := d.capability("openable"):
                self._conn.execute(
                    "INSERT INTO object_openables VALUES (?,?,?,?) "
                    "ON CONFLICT(session_id,object_id) DO NOTHING",
                    (
                        sid,
                        d.object_id,
                        int(c.initial_jammed),
                        None if c.initial_barred is None else int(c.initial_barred),
                    ),
                )
            if c := d.capability("durability"):
                self._conn.execute(
                    "INSERT INTO object_durability VALUES (?,?,?) "
                    "ON CONFLICT(session_id,object_id) DO NOTHING",
                    (sid, d.object_id, c.initial_hp),
                )

    def mechanism_status(self, sid, oid):
        """Read the mechanism status string."""
        row = self._conn.execute(
            "SELECT status FROM object_mechanisms WHERE session_id=? AND object_id=?", (sid, oid)
        ).fetchone()
        return row[0] if row else None

    @scoped_read
    def mechanism_state(self, sid, oid):
        """Read and construct the mechanism lifecycle state."""
        from .mechanism_models import MechanismState

        row = self._conn.execute(
            "SELECT status,remaining_uses,activation_count,reset_at_tick,resume_status "
            "FROM object_mechanisms WHERE session_id=? AND object_id=?",
            (sid, oid),
        ).fetchone()
        return MechanismState(*row) if row else None

    def set_mechanism_state(self, sid, oid, state):
        """Strictly validate and persist the mechanism lifecycle state."""
        from .mechanism_models import MechanismState

        self.store.require_write()
        if type(state) is not MechanismState:
            raise StateMachineError("invalid_rule_result", "Typed mechanism state required")
        if not self._conn.execute(
            "UPDATE object_mechanisms SET status=?,remaining_uses=?,activation_count=?,"
            "reset_at_tick=?,resume_status=? WHERE session_id=? AND object_id=?",
            (
                state.status,
                state.remaining_uses,
                state.activation_count,
                state.reset_at_tick,
                state.resume_status,
                sid,
                oid,
            ),
        ).rowcount:
            raise StateMachineError("invalid_target", "Mechanism not initialized")

    def light_state(self, sid, oid):
        """Read the light's enabled state and selected mode."""
        row = self._conn.execute(
            "SELECT enabled,mode FROM object_lights WHERE session_id=? AND object_id=?", (sid, oid)
        ).fetchone()
        return {"enabled": bool(row[0]), "mode": row[1]} if row else None

    @scoped_read
    def light_fuel(self, sid, oid):
        """Read paused fuel or the active deadline from the light's own row."""
        row = self._conn.execute(
            "SELECT fuel_remaining_seconds,fuel_expires_tick FROM object_lights "
            "WHERE session_id=? AND object_id=?",
            (sid, oid),
        ).fetchone()
        if row is None:
            raise StateMachineError("unsupported_object_capability", "No valid emitter state")
        return LightFuel(*row)

    def next_light_expiry(self, sid):
        """Read the earliest active deadline via the Session's fuel index."""
        return self._conn.execute(
            "SELECT min(fuel_expires_tick) FROM object_lights "
            "WHERE session_id=? AND fuel_expires_tick IS NOT NULL",
            (sid,),
        ).fetchone()[0]

    def due_light(self, sid, tick):
        """Read one due object identity and mode in stable deadline order."""
        return self._conn.execute(
            "SELECT object_id,mode,fuel_expires_tick FROM object_lights "
            "WHERE session_id=? AND fuel_expires_tick<=? "
            "ORDER BY fuel_expires_tick,object_id LIMIT 1",
            (sid, tick),
        ).fetchone()

    def set_light_state(self, sid, oid, enabled, mode, *, fuel=None):
        """Update light switch, mode and any fuel transition in the caller's write."""
        self.store.require_write()
        if type(enabled) is not bool or type(mode) is not str:
            raise StateMachineError("unsupported_object_capability", "No valid emitter state")
        fields, values = "enabled=?,mode=?", (int(enabled), mode)
        if fuel is not None:
            fields += ",fuel_remaining_seconds=?,fuel_expires_tick=?"
            values += (fuel.remaining_seconds, fuel.expires_tick)
        if not self._conn.execute(
            f"UPDATE object_lights SET {fields} WHERE session_id=? AND object_id=?",
            (*values, sid, oid),
        ).rowcount:
            raise StateMachineError("unsupported_object_capability", "No valid emitter state")

    def openable_state(self, sid, oid):
        """Read the object's jam and bar state."""
        row = self._conn.execute(
            "SELECT jammed,barred FROM object_openables WHERE session_id=? AND object_id=?",
            (sid, oid),
        ).fetchone()
        return (
            {"jammed": bool(row[0]), "barred": None if row[1] is None else bool(row[1])}
            if row
            else None
        )

    def set_object_bar(self, sid, oid, barred):
        """Check object/damage prerequisites before changing its bar state."""
        self.store.require_write()
        obj = self.object(sid, oid)
        state = self.openable_state(sid, oid)
        if state is None or state["barred"] is None:
            raise StateMachineError("unsupported_object_capability", "Object has no bar")
        if self.object_hp(sid, oid) == 0:
            raise StateMachineError("object_destroyed", "Destroyed object is unusable")
        if barred and obj["state"] != "closed":
            raise StateMachineError("object_must_be_closed", "Close object before barring")
        self._conn.execute(
            "UPDATE object_openables SET barred=? WHERE session_id=? AND object_id=?",
            (int(barred), sid, oid),
        )

    def clear_object_jam(self, sid, oid):
        """Clear a supported object's jam state."""
        self.store.require_write()
        if self.openable_state(sid, oid) is None:
            raise StateMachineError("unsupported_object_capability", "No jam state")
        if self.object_hp(sid, oid) == 0:
            raise StateMachineError("object_destroyed", "Destroyed object is unusable")
        self._conn.execute(
            "UPDATE object_openables SET jammed=0 WHERE session_id=? AND object_id=?", (sid, oid)
        )

    def object_hp(self, sid, oid):
        """Read the object's current durability hit points."""
        row = self._conn.execute(
            "SELECT current_hp FROM object_durability WHERE session_id=? AND object_id=?",
            (sid, oid),
        ).fetchone()
        return row[0] if row else None

    def apply_object_hp(self, sid, oid, expected, value):
        """Validate and update object durability hit points."""
        self.store.require_write()
        if (
            type(value) is not int
            or not 0 <= value <= expected
            or not self._conn.execute(
                "UPDATE object_durability SET current_hp=? "
                "WHERE session_id=? AND object_id=? AND current_hp=?",
                (value, sid, oid, expected),
            ).rowcount
        ):
            raise StateMachineError("invalid_rule_result", "Invalid durability transition")

    def visit(self, sid, scene_id):
        """Record a scene visit."""
        self.store.require_write()
        self._conn.execute(
            "UPDATE scene_instances SET visited_count=visited_count+1 "
            "WHERE session_id=? AND scene_id=?",
            (sid, scene_id),
        )

    def place(self, sid, object_id, cell, *, orientation=None):
        """Require an ordinary object and update its authoritative placement."""
        self.store.require_write()
        self.require_ordinary_object(sid, object_id)
        self._conn.execute(
            "UPDATE objects SET cell=?,orientation=COALESCE(?,orientation) "
            "WHERE session_id=? AND object_id=?",
            (cell, orientation, sid, object_id),
        )

    def orient(self, sid, object_id, orientation):
        """Persist a validated direction without changing placement or ownership."""
        self.store.require_write()
        self._conn.execute(
            "UPDATE objects SET orientation=? WHERE session_id=? AND object_id=?",
            (orientation, sid, object_id),
        )

    def switch(self, sid, object_id, state, *, jam_policy="prevent_transitions"):
        """Validate an opening transition and update the object's open state."""
        self.store.require_write()
        self.validate_door_switch(sid, object_id, state, jam_policy=jam_policy)
        self._conn.execute(
            "UPDATE objects SET state=? WHERE session_id=? AND object_id=?", (state, sid, object_id)
        )

    def locks_for_object(self, sid, oid):
        """Read lock states associated with the object."""
        return self._conn.execute(
            "SELECT lock_id,locked FROM door_locks WHERE session_id=? AND object_id=? "
            "ORDER BY lock_id",
            (sid, oid),
        ).fetchall()

    def door_lock(self, sid, object_id):
        """Combine lock and object facts into the current locking state."""
        locks = self.locks_for_object(sid, object_id)
        if not locks:
            return None
        obj = self.object(sid, object_id)
        if (
            obj is None
            or obj["state"] not in ("open", "closed")
            or (any(lock[1] for lock in locks) and obj["state"] != "closed")
        ):
            raise StateMachineError("invalid_lock_state", "Contradictory shared object state")
        return any(bool(lock[1]) for lock in locks)

    def validate_door_switch(
        self, sid, object_id, state, *, jam_policy="prevent_transitions", current_state=None
    ):
        """Check damage, jam, bar and lock prerequisites for changing a door's open state."""
        if jam_policy not in ("prevent_transitions", "prevent_opening", "prevent_closing"):
            raise StateMachineError("invalid_rule_result", "Unknown jam policy")
        if state not in ("open", "closed"):
            raise StateMachineError("invalid_rule_result", "Unknown open state")
        if self.object_hp(sid, object_id) == 0:
            raise StateMachineError("object_destroyed", "Destroyed object is unusable")
        current = self.object(sid, object_id)
        openable = self.openable_state(sid, object_id)
        if openable and (current["state"] if current_state is None else current_state) != state:
            if openable["jammed"] and (
                jam_policy == "prevent_transitions"
                or (jam_policy == "prevent_opening" and state == "open")
                or (jam_policy == "prevent_closing" and state == "closed")
            ):
                raise StateMachineError("object_jammed", "Object is jammed")
            if openable["barred"]:
                raise StateMachineError("object_barred", "Object is barred")
        locked = self.door_lock(sid, object_id)
        if locked and state == "open":
            raise StateMachineError("door_locked", "Door is locked")

    def set_door_lock(self, sid, object_id, locked, lock_id=None):
        """Validate lock/object damage relationships and persist the lock state."""
        self.store.require_write()
        if self.door_lock(sid, object_id) is None:
            raise StateMachineError("unsupported_object_capability", "Door has no lock")
        state = self._conn.execute(
            "SELECT state FROM objects WHERE session_id=? AND object_id=?", (sid, object_id)
        ).fetchone()[0]
        if locked and state != "closed":
            raise StateMachineError("door_must_be_closed", "Close the door before locking")
        locks = self.locks_for_object(sid, object_id)
        if lock_id is None:
            if len(locks) != 1:
                raise StateMachineError("ambiguous_lock", "Explicit lock identity required")
            lock_id = locks[0][0]
        if lock_id not in {lock[0] for lock in locks}:
            raise StateMachineError("invalid_target", "Lock does not belong to object")
        if self.object_hp(sid, object_id) == 0:
            raise StateMachineError("object_destroyed", "Destroyed object is unusable")
        self._conn.execute(
            "UPDATE door_locks SET locked=? WHERE session_id=? AND object_id=? AND lock_id=?",
            (int(locked), sid, object_id, lock_id),
        )

    def initialize_locks(self, row, package):
        """Initialize locks and their accepted credential relationships."""
        self.store.require_write()
        if row["world_version"] or row["event_seq"]:
            raise StateMachineError("invalid_lock_foundation", "Locks require a fresh session")
        for d in package.world_objects:
            c = d.capability("locks")
            if c:
                for lock in c.locks:
                    self._conn.execute(
                        "INSERT INTO door_locks VALUES (?,?,?,?)",
                        (row["session_id"], d.object_id, int(lock.initial_locked), lock.lock_id),
                    )
                    self._conn.executemany(
                        "INSERT INTO door_credentials VALUES (?,?,?,?)",
                        [
                            (row["session_id"], d.object_id, iid, lock.lock_id)
                            for iid in lock.credential_instance_ids
                        ],
                    )
        for door in package.lockable_doors:
            self._conn.execute(
                "INSERT INTO door_locks VALUES (?,?,?,?)",
                (
                    row["session_id"],
                    door.object_id,
                    int(door.initial_locked),
                    "lock." + door.object_id,
                ),
            )
            self._conn.executemany(
                "INSERT INTO door_credentials VALUES (?,?,?,?)",
                [
                    (row["session_id"], door.object_id, iid, "lock." + door.object_id)
                    for iid in door.credential_instance_ids
                ],
            )

    def accepts_credential(self, sid, object_id, instance_id, lock_id=None):
        """Return whether the credential is accepted by this object's lock."""
        return (
            self._conn.execute(
                "SELECT 1 FROM door_credentials WHERE session_id=? AND object_id=? "
                "AND instance_id=? AND (? IS NULL OR lock_id=?)",
                (sid, object_id, instance_id, lock_id, lock_id),
            ).fetchone()
            is not None
        )

    def require_ordinary_object(self, sid, object_id):
        """Reject an object that does not qualify for ordinary object operations."""
        row = self._conn.execute(
            "SELECT item_slug FROM objects WHERE session_id=? AND object_id=?", (sid, object_id)
        ).fetchone()
        if row and row[0] is not None:
            raise StateMachineError(
                "unsupported_object_capability", "Portable instances require item transfer"
            )

    def materialize_owned_item(self, sid, iid, owner, unit, *, allow_existing=False):
        """Create one proved owned item in World facts or verify an admitted identity."""
        self.store.require_write()
        if not valid_item_unit(unit):
            raise StateMachineError("invalid_item_foundation", "Invalid initial item unit")
        old = self.object(sid, iid)
        if old is not None:
            if (
                allow_existing
                and item_unit_from_row(old) == unit
                and self.placement(sid, iid)["carrier_id"] == owner
            ):
                return
            raise StateMachineError("invalid_item_foundation", "Initial item identity conflicts")
        self._conn.execute(
            "INSERT INTO objects(session_id,object_id,scene_id,state,cell,"
            "carrier_id,portable_marker,item_slug,item_charges,item_accessible) "
            "VALUES (?,?,NULL,NULL,NULL,?,'{}',?,?,?)",
            (
                sid,
                iid,
                owner,
                unit["item_slug"],
                unit["charges_remaining_per_unit"],
                int(unit["accessible"]),
            ),
        )
        self.instances.put(
            sid,
            "object",
            iid,
            {
                "definition": "null",
                "movable": False,
                "blocks_passage": False,
                "portable_policy": None,
            },
        )

    def retire_owned_item(self, sid, before):
        """Remove a proved placeholder after checking its current World row."""
        self.store.require_write()
        current = self.object(sid, before["object_id"])
        if current is None or dict(current) != dict(before):
            raise StateMachineError("invalid_item_foundation", "Legacy item changed")
        self._conn.execute(
            "DELETE FROM instance_configs WHERE session_id=? AND domain='object' AND instance_id=?",
            (sid, before["object_id"]),
        )
        self._conn.execute(
            "DELETE FROM objects WHERE session_id=? AND object_id=?", (sid, before["object_id"])
        )

    def initialize_items(self, row, package, loader):
        """Initialize portable items and the bound dawn plan of eligible exact instances."""
        self.store.require_write()
        sid = row["session_id"]
        if row["world_version"] or row["event_seq"]:
            raise StateMachineError(
                "invalid_item_foundation", "Item initialization requires a fresh session"
            )
        dawn = None
        initial_actors = None
        attuned_owners = set()
        for item in package.portable_items:
            asset = loader.get_item(item.item_slug)
            if asset is None:
                raise StateMachineError("invalid_item_foundation", "Unknown pinned mechanical item")
            capacity = due = None
            if item.policy == "charge_exhaust_retain" and asset.uses and asset.uses.recovery:
                profile = exploration_charged_cast_profile(asset, loader)
                if is_healing_staff(asset) or (profile is not None and profile.recovers_at_dawn):
                    capacity = int(asset.uses.max)
                    if item.charges_remaining_per_unit > capacity:
                        raise StateMachineError(
                            "invalid_item_foundation", "Initial item charges exceed bound capacity"
                        )
                    if dawn is None:
                        dawn = next_item_dawn(*self.time_state(sid))
                    if item.charges_remaining_per_unit < capacity:
                        due = dawn
            owner = (
                row["pc_actor_id"]
                if item.initial_owner_id == row["pc_template_id"]
                else item.initial_owner_id
            )
            if item.initially_attuned:
                if initial_actors is None:
                    foundation = json.loads(package.noncombat_foundation_json)["snapshot"]
                    initial_actors = {
                        actor.entity_id: actor
                        for actor in NonCombatSnapshot.model_validate_json(
                            canonical_json(foundation)
                        ).character_states
                    }
                holder = initial_actors[item.initial_owner_id]
                if (
                    not is_healing_staff(asset)
                    or staff_attunement_ability(holder, loader) is None
                    or owner in attuned_owners
                ):
                    raise StateMachineError(
                        "invalid_item_foundation",
                        "Only one canonical Staff of Healing may start attuned per eligible owner",
                    )
                attuned_owners.add(owner)
            attuned = owner if item.initially_attuned else None
            data = {
                "item_slug": item.item_slug,
                "quantity": item.quantity,
                "charges_remaining_per_unit": item.charges_remaining_per_unit,
                "accessible": True,
            }
            old = self._conn.execute(
                "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, item.instance_id)
            ).fetchone()
            if old is None:
                raise StateMachineError(
                    "invalid_item_foundation", "Portable instance must exist exactly once"
                )
            if old["item_slug"] is not None:
                if item_unit_from_row(old) != data or old["carrier_id"] != owner:
                    raise StateMachineError(
                        "invalid_item_foundation", "Portable instance differs from item facts"
                    )
                if capacity is not None:
                    self._conn.execute(
                        "UPDATE objects SET item_recharge_capacity=?,item_recharge_due_tick=?,"
                        "item_attuned_actor_id=? WHERE session_id=? AND object_id=?",
                        (capacity, due, attuned, sid, item.instance_id),
                    )
                continue
            self._conn.execute(
                "UPDATE objects SET portable_marker='{}',item_slug=?,"
                "item_charges=?,item_accessible=?,"
                "item_recharge_capacity=?,item_recharge_due_tick=?,item_attuned_actor_id=?,"
                "carrier_id=?,scene_id=?,cell=? "
                "WHERE session_id=? AND object_id=?",
                (
                    data["item_slug"],
                    data["charges_remaining_per_unit"],
                    int(data["accessible"]),
                    capacity,
                    due,
                    attuned,
                    owner,
                    None if owner else old["scene_id"],
                    None if owner else old["cell"],
                    sid,
                    item.instance_id,
                ),
            )

    def placement(self, sid, oid, *, row=None):
        """Resolve scene, carrier and cell placement through the container ancestors."""
        obj = row if row is not None else self.object(sid, oid)
        if obj is None:
            raise StateMachineError("invalid_target", "Missing object")
        ancestry, seen = [], {oid}
        current = obj
        while current["container_id"] is not None:
            parent = current["container_id"]
            if parent in seen or len(ancestry) >= 8:
                raise StateMachineError("invalid_world_state", "Invalid containment ancestry")
            seen.add(parent)
            ancestry.append(parent)
            current = self.object(sid, parent)
            if current is None:
                raise StateMachineError("invalid_world_state", "Missing parent container")
        if current["carrier_id"] is not None:
            position = self.carrier_position(sid, current["carrier_id"])
            scene, cell = position
        else:
            scene, cell = current["scene_id"], current["cell"]
        return {
            "scene_id": scene,
            "cell": cell,
            "carrier_id": current["carrier_id"],
            "container_id": obj["container_id"],
            "ancestry": tuple(ancestry),
        }

    def item_accessible(self, sid, oid, *, row=None, include_self=True):
        """Return whether the item and its ancestor containers permit contents access."""
        from .world_openable import OpenableService

        placement = self.placement(sid, oid, row=row)
        if include_self and self.object_hp(sid, oid) == 0:
            return False
        for parent in placement["ancestry"]:
            if not OpenableService.allows_contents(self, sid, parent):
                return False
        return True

    @instance_read("object", "container", "contents")
    def contents(self, sid, oid):
        """Read only immutable direct-member identities for row-based consumers."""
        return self._conn.execute(
            "SELECT object_id FROM objects WHERE session_id=? AND container_id=? "
            "ORDER BY object_id",
            (sid, oid),
        ).fetchall()

    def carried_root_ids(self, sid, actor_id):
        """Read the direct root object identities carried by one selected actor."""
        return tuple(
            row[0]
            for row in self._conn.execute(
                "SELECT object_id FROM objects WHERE session_id=? AND carrier_id=?",
                (sid, actor_id),
            )
        )

    @instance_read("object", "container", "active_contents", maintained="container_members")
    def active_contents(self, sid, oid):
        """Resolve direct portable members to stable current identities in a read scope.

        The enclosing contents field owns the relationship. Handles share its
        item identity without copying or retaining a second container membership.
        """
        from .session_memory import ActiveContents

        return ActiveContents(
            self.store.memory.active_item(row)
            for row in self._conn.execute(
                "SELECT * FROM objects WHERE session_id=? AND container_id=? ORDER BY object_id",
                (sid, oid),
            )
            if row["item_slug"] is not None
        )

    def descendant_objects(self, sid, roots):
        """Read the complete descendant closure of the specified root object."""
        found, visited, pending = {}, set(), list(roots)
        while pending:
            oid = pending.pop()
            if oid in visited:
                continue
            visited.add(oid)
            row = self.object(sid, oid)
            if row is not None:
                found[oid] = row
            # Include children of a missing root as the previous SQL UNION did;
            # domain admission, rather than traversal, reports corrupt relations.
            pending.extend(row["object_id"] for row in self.contents(sid, oid))
        return [found[oid] for oid in sorted(found)]

    def related_objects(self, sid, roots):
        """Ancestors plus their complete descendant aggregates, in stable ID order.

        UNION terminates corrupt cycles; the domain admission still rejects them.
        Sibling subtrees are required for ancestor weight/capacity, not optional.
        """
        ancestors, pending = set(), list(roots)
        while pending:
            oid = pending.pop()
            if oid in ancestors:
                continue
            ancestors.add(oid)
            row = self.object(sid, oid)
            if row is not None and row["container_id"] is not None:
                pending.append(row["container_id"])
        return self.descendant_objects(sid, ancestors)

    @instance_read("scene", "objects", "contributors", capacity=8)
    def scene_object_rows(
        self, sid, scene_id, *, carrier_id=None, all_carriers=False, extra=(), lights_only=False
    ):
        """Ground/carried roots and descendants; actual pose, never template membership."""
        if lights_only and not self._has_lights(sid):
            return []
        roots = set(extra)
        roots.update(
            row[0]
            for row in self._conn.execute(
                "SELECT object_id FROM objects WHERE session_id=? AND scene_id=?", (sid, scene_id)
            )
        )
        carriers = set(() if carrier_id is None else (carrier_id,))
        if all_carriers:
            carriers.update(
                row[0]
                for row in self._conn.execute(
                    "SELECT actor_id FROM actors WHERE session_id=? AND current_scene_id=?",
                    (sid, scene_id),
                )
            )
        for carrier in sorted(carriers):
            roots.update(
                row[0]
                for row in self._conn.execute(
                    "SELECT object_id FROM objects WHERE session_id=? AND carrier_id=?",
                    (sid, carrier),
                )
            )
        rows = self.descendant_objects(sid, roots)
        return [row for row in rows if not lights_only or self.light_state(sid, row["object_id"])]

    @instance_read("scene", "objects", "direct")
    def direct_objects(self, sid, scene_id):
        """Stable IDs of ground roots only; carried/nested objects stay separate."""
        return tuple(
            r[0]
            for r in self._conn.execute(
                "SELECT object_id FROM objects WHERE session_id=? AND scene_id=? "
                "ORDER BY object_id",
                (sid, scene_id),
            )
        )

    @instance_read("scene", "objects", "capabilities", capacity=2, maintained="capabilities")
    def scene_capability_ids(self, sid, scene_id, table):
        """Scene-owned members, patched by the same candidate change capture.

        Missing/evicted fields are cold-constructed through position/parent
        indices; ordinary member/ownership edits patch the loaded association.
        Only identities are read before filtering, never unrelated configuration.
        """
        if table not in ("object_lights", "object_mechanisms"):
            raise ValueError("Unsupported capability relation")
        memory = self.store.memory
        # Discovery owns the Scene membership version. Do not retain one
        # validity dependency per unrelated object/carrier merely to confirm
        # an already loaded association. Individual consumers still track the
        # selected instances' actual configurations/poses/state.
        collectors, memory.collectors = memory.collectors, []
        try:
            return self._discover_capability_ids(sid, scene_id, table)
        finally:
            memory.collectors = collectors
            memory.scene_association(table, sid, scene_id)

    def magic_light_anchors(self, sid, scene_id):
        """Read the scene bucket of magic anchors through SessionMemory's indexed range."""
        return tuple(
            row[0]
            for row in self._conn.execute(
                "SELECT object_id FROM magic_light_anchors WHERE session_id=? AND scene_id=? "
                "ORDER BY object_id",
                (sid, scene_id),
            )
        )

    def _discover_capability_ids(self, sid, scene_id, table):
        """Discover capability-bearing object identities associated with the scene."""
        if not self._has_capability(sid, table):
            return ()
        pending = list(self.direct_objects(sid, scene_id))
        for actor in self._conn.execute(
            "SELECT actor_id FROM actors WHERE session_id=? AND current_scene_id=?",
            (sid, scene_id),
        ):
            pending.extend(
                r[0]
                for r in self._conn.execute(
                    "SELECT object_id FROM objects WHERE session_id=? AND carrier_id=?",
                    (sid, actor[0]),
                )
            )
        seen, result = set(), []
        while pending:
            oid = pending.pop()
            if oid in seen:
                continue
            seen.add(oid)
            if self._conn.execute(
                f"SELECT object_id FROM {table} WHERE session_id=? AND object_id=?", (sid, oid)
            ).fetchone():
                result.append(oid)
            pending.extend(
                r[0]
                for r in self._conn.execute(
                    "SELECT object_id FROM objects WHERE session_id=? AND container_id=?",
                    (sid, oid),
                )
            )
        return tuple(sorted(result))

    @scoped_read(membership=("object_lights", "object_mechanisms"))
    def _has_capability(self, sid, table):
        """Return whether the object has a record for this capability."""
        return (
            self._conn.execute(
                f"SELECT 1 FROM {table} WHERE session_id=? LIMIT 1", (sid,)
            ).fetchone()
            is not None
        )

    @scoped_read(membership=("object_lights",))
    def _has_lights(self, sid):
        """Indexed existence, not Scene enumeration when no emitter exists."""
        return (
            self._conn.execute(
                "SELECT 1 FROM object_lights WHERE session_id=? LIMIT 1", (sid,)
            ).fetchone()
            is not None
        )

    @scoped_read
    def item_entry(self, row):
        """Convert the persisted item row into a mechanical inventory entry."""
        sid, oid = row["session_id"], row["object_id"]
        data = self.require_item_unit(row)
        placement = self.placement(sid, oid, row=row)
        if placement["carrier_id"] is None:
            return None
        data["accessible"] = data["accessible"] and self.item_accessible(sid, oid, row=row)
        if row["item_attuned_actor_id"] is not None:
            data["attuned_actor_id"] = row["item_attuned_actor_id"]
        return {"instance_id": oid, "owner_id": placement["carrier_id"], **data}

    def set_item_attunement(self, sid, instance_id, expected_actor, actor_id):
        """Change one World-owned exact item bond inside the shared write scope."""
        self.store.require_write()
        changed = self._conn.execute(
            "UPDATE objects SET item_attuned_actor_id=? WHERE session_id=? AND object_id=? "
            "AND item_attuned_actor_id IS ?",
            (actor_id, sid, instance_id, expected_actor),
        )
        if changed.rowcount != 1:
            raise StateMachineError("item_attunement_conflict", "Item attunement changed")

    def require_item_unit(self, row):
        """Validate and return quantity data for one portable item instance."""
        data = item_unit_from_row(row)
        if not valid_item_unit(data):
            raise StateMachineError("invalid_mechanical_state", "Portable unit metadata is invalid")
        return data

    def portable_inventory(self, sid, *, owner_ids=None, instance_ids=()):
        """Build portable inventory and its identity set for the requested owner/instance
        scope.
        """
        identities, entries = set(), {}
        if owner_ids is None:
            rows = self._conn.execute(
                "SELECT * FROM objects WHERE session_id=? AND item_slug IS NOT NULL", (sid,)
            )
        else:
            roots = [
                r[0]
                for owner in sorted(owner_ids)
                for r in self._conn.execute(
                    "SELECT object_id FROM objects WHERE session_id=? AND carrier_id=?",
                    (sid, owner),
                )
            ]
            selected = {r["object_id"]: r for r in self.descendant_objects(sid, roots)}
            for oid in instance_ids:
                if (row := self.object(sid, oid)) is not None:
                    selected[oid] = row
            rows = [
                selected[oid] for oid in sorted(selected) if selected[oid]["item_slug"] is not None
            ]
        for row in rows:
            identities.add(row["object_id"])
            data = item_unit_from_row(row)
            # Ground metadata is validated without constructing a fictional owner.
            if not valid_item_unit(data):
                raise StateMachineError(
                    "invalid_mechanical_state", "Portable unit metadata is invalid"
                )
            if entry := self.item_entry(row):
                entries[row["object_id"]] = entry
        return identities, entries

    def transfer_item(
        self, sid, before, *, carrier_id=None, scene_id=None, cell=None, container_id=None
    ):
        """Update the item's unique ownership and containment relationship in a write
        scope.
        """
        self.store.require_write()
        current = self._conn.execute(
            "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, before["object_id"])
        ).fetchone()
        if current is None or dict(current) != dict(before) or current["item_slug"] is None:
            raise StateMachineError("position_conflict", "Item location changed")
        self._conn.execute(
            "UPDATE objects SET carrier_id=?,scene_id=?,cell=?,container_id=? "
            "WHERE session_id=? AND object_id=?",
            (carrier_id, scene_id, cell, container_id, sid, before["object_id"]),
        )
        return self._conn.execute(
            "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, before["object_id"])
        ).fetchone()

    def spend_item_charge(self, sid, before, *, amount=1):
        """Persist an admitted positive charge spend against the exact item row.

        The row remains after exhaustion. An eligible item keeps its first pending
        dawn or schedules one on the first spend after a refill. The caller owns
        rollback, receipt, version and RAM publication.
        """
        self.store.require_write()
        if before["item_slug"] is None or not 1 <= amount <= before["item_charges"]:
            raise StateMachineError("item_charges_exhausted", "No item charges remain")
        due = before["item_recharge_due_tick"]
        if before["item_recharge_capacity"] is not None and due is None:
            due = next_item_dawn(*self.time_state(sid))
        self._replace_item_charge(sid, before, before["item_charges"] - amount, due)

    def next_item_recharge(self, sid):
        """Read the earliest reachable dawn from the index of depleted items."""
        due = self._conn.execute(
            "SELECT item_recharge_due_tick FROM objects "
            "WHERE session_id=? AND item_recharge_due_tick<=? "
            "ORDER BY item_recharge_due_tick,object_id LIMIT 1",
            (sid, MAX_TICK),
        ).fetchone()
        return due[0] if due else None

    def due_item_recharge(self, sid, tick):
        """Read one due item in stable deadline order in the caller's candidate."""
        return self._conn.execute(
            "SELECT * FROM objects WHERE session_id=? AND item_recharge_due_tick<=? "
            "ORDER BY item_recharge_due_tick,object_id LIMIT 1",
            (sid, tick),
        ).fetchone()

    def recover_item_charges(self, sid, before, amount=None):
        """Apply the bounded recovery and schedule a later dawn if still depleted."""
        self.store.require_write()
        capacity = before["item_recharge_capacity"]
        if capacity is None or before["item_recharge_due_tick"] is None:
            raise StateMachineError("invalid_time_consumer", "Item recovery is not due")
        balance = capacity if amount is None else min(capacity, before["item_charges"] + amount)
        due = (
            None
            if balance == capacity
            else next_item_dawn(self.time_state(sid)[0], before["item_recharge_due_tick"])
        )
        self._replace_item_charge(sid, before, balance, due)
        return balance

    def _replace_item_charge(self, sid, before, balance, due):
        """Compare and replace one instance's balance and pending dawn together."""
        self.store.require_write()
        cursor = self._conn.execute(
            "UPDATE objects SET item_charges=?,item_recharge_due_tick=? "
            "WHERE session_id=? AND object_id=? AND item_slug=? AND item_charges=? "
            "AND item_recharge_capacity IS ? AND item_recharge_due_tick IS ?",
            (
                balance,
                due,
                sid,
                before["object_id"],
                before["item_slug"],
                before["item_charges"],
                before["item_recharge_capacity"],
                before["item_recharge_due_tick"],
            ),
        )
        if cursor.rowcount != 1:
            raise StateMachineError("version_conflict", "Item balance changed")

    def consume_item(self, sid, before, command_id, *, depleted_staff=False):
        """Remove one exact owned unit and record its receipt-linked absence.

        The caller owns the shared write scope and validated mechanical Delta.
        Authored scene dependencies are retired before deletion, so the ordinary
        integrity trigger preserves only the certificate's still-required rows.
        """
        self.store.require_write()
        oid = before["object_id"]
        current = self.object(sid, oid)
        if current is None or dict(current) != dict(before):
            raise StateMachineError("version_conflict", "Consumed item changed")
        config = self._conn.execute(
            "SELECT config_json FROM instance_configs WHERE session_id=? AND domain='object' "
            "AND instance_id=?",
            (sid, oid),
        ).fetchone()
        if config is None:
            raise StateMachineError("incomplete_session", "Item configuration is missing")
        if (
            self.store.memory.json(config[0]).get("portable_policy") == "charge_exhaust_retain"
            and not depleted_staff
        ):
            raise StateMachineError(
                "unsupported_item_binding", "Retained charge item cannot vanish"
            )
        if self._conn.execute(
            "SELECT 1 FROM objects WHERE session_id=? AND container_id=? LIMIT 1", (sid, oid)
        ).fetchone():
            raise StateMachineError("unsupported_item_binding", "Item contains other objects")
        for table, column in (
            ("door_credentials", "instance_id"),
            ("door_locks", "object_id"),
            ("object_openables", "object_id"),
            ("object_lights", "object_id"),
            ("object_mechanisms", "object_id"),
            ("object_durability", "object_id"),
            ("character_spells", "material_instance_id"),
        ):
            if self._conn.execute(
                f"SELECT 1 FROM {table} WHERE session_id=? AND {column}=? LIMIT 1",
                (sid, oid),
            ).fetchone():
                raise StateMachineError("unsupported_item_binding", "Live item reference")
        owner = self.placement(sid, oid, row=current)["carrier_id"]
        equipment = self._conn.execute(
            "SELECT * FROM character_equipment WHERE session_id=? AND actor_id=?",
            (sid, owner),
        ).fetchone()
        if equipment is None:
            actor = self._conn.execute(
                "SELECT state_json FROM mechanical_actors WHERE session_id=? AND actor_id=?",
                (sid, owner),
            ).fetchone()
            legacy = self.store.memory.json(actor[0]) if actor is not None else {}
            equipped = any(legacy.get(slot) == oid for _, slot in EQUIPMENT_SLOTS)
        else:
            equipped = any(equipment[slot] == oid for _, slot in EQUIPMENT_SLOTS)
        if equipped:
            raise StateMachineError("equipped_item_unsupported", "Item remains equipped")
        if self._conn.execute(
            "SELECT 1 FROM character_worn_items WHERE session_id=? AND instance_id=? LIMIT 1",
            (sid, oid),
        ).fetchone():
            raise StateMachineError("equipped_item_unsupported", "Item remains worn")
        scenes = self._conn.execute(
            "SELECT scene_id FROM scene_integrity_dependencies "
            "WHERE session_id=? AND domain='objects' AND instance_id=?",
            (sid, oid),
        ).fetchall()
        if len(scenes) > 1:
            raise StateMachineError("incomplete_session", "Item has ambiguous authored scene")
        scene = scenes[0][0] if scenes else None
        if scene is not None:
            for domain in ("objects", "config:object"):
                deleted = self._conn.execute(
                    "DELETE FROM scene_integrity_dependencies WHERE session_id=? "
                    "AND scene_id=? AND domain=? AND instance_id=?",
                    (sid, scene, domain, oid),
                )
                if deleted.rowcount != 1:
                    raise StateMachineError("incomplete_session", "Item dependency is incomplete")
        self._conn.execute(
            "INSERT INTO consumed_items VALUES (?,?,?,?)", (sid, oid, command_id, scene)
        )
        self._conn.execute(
            "DELETE FROM instance_configs WHERE session_id=? AND domain='object' AND instance_id=?",
            (sid, oid),
        )
        removed = self._conn.execute(
            "DELETE FROM objects WHERE session_id=? AND object_id=? AND item_slug=?",
            (sid, oid, before["item_slug"]),
        )
        if removed.rowcount != 1:
            raise StateMachineError("version_conflict", "Consumed item changed")

    def object_position(self, sid, object_id, *, row=None):
        """Resolve spatial position, reusing the object's row from the current scope if supplied."""
        obj = row if row is not None else self.object(sid, object_id)
        return (
            {
                **self.placement(sid, object_id, row=obj),
                "state": obj["state"],
                "orientation": obj["orientation"],
            }
            if obj
            else None
        )

    def scene_flags(self, sid, scene_id):
        """Read the scene flags dictionary."""
        row = self._conn.execute(
            "SELECT flags_json FROM scene_instances WHERE session_id=? AND scene_id=?",
            (sid, scene_id),
        ).fetchone()
        return self.store.memory.json(row[0]) if row else None

    def set_scene_flags(self, sid, scene_id, flags):
        """Persist scene flags."""
        self.store.require_write()
        self._conn.execute(
            "UPDATE scene_instances SET flags_json=? WHERE session_id=? AND scene_id=?",
            (canonical_json(flags), sid, scene_id),
        )

    def set_tick(self, sid, tick):
        """Persist the authoritative world game tick."""
        self.store.require_write()
        self._conn.execute("UPDATE world_states SET game_tick=? WHERE session_id=?", (tick, sid))

    def initialize_time_binding(self, sid, binding):
        """Initialize the Session's time binding."""
        from dataclasses import asdict

        self.store.require_write()
        self._conn.execute(
            "INSERT INTO world_time_bindings VALUES (?,?)", (sid, canonical_json(asdict(binding)))
        )

    @scoped_read
    def _time_binding(self, sid):
        """Read and decode the persisted time binding."""
        from .time_binding import LegacyTimeBinding, time_binding_from_data

        row = self._conn.execute(
            "SELECT binding_json FROM world_time_bindings WHERE session_id=?",
            (sid,),
        ).fetchone()
        return (
            time_binding_from_data(self.store.memory.json(row[0])) if row else LegacyTimeBinding()
        )

    def time_state(self, sid):
        """Return the current game time state."""
        row = self._conn.execute(
            "SELECT game_tick FROM world_states WHERE session_id=?", (sid,)
        ).fetchone()
        if row is None:
            raise StateMachineError("session_not_found", "Unknown clock")
        return self._time_binding(sid), row[0]

    def initialize_rule_scene(self, sid, scene):
        """Initialize rules scene facts."""
        self.store.require_write()
        self._conn.execute(
            "INSERT INTO mechanical_scenes(session_id,scene_id,state_json) VALUES (?,?,?)",
            (sid, scene.scene_id, scene.model_dump_json()),
        )

    def initialize_mechanical_scene(self, sid, scene_id, package):
        """Initialize the mechanical scene map and instance records."""
        self.store.require_write()
        definition = package.scene(scene_id)
        if definition.grid_json is not None:
            self._conn.execute(
                "INSERT INTO mechanical_scenes(session_id,scene_id,state_json,spatial_enabled) "
                "SELECT ?,?,?,1 WHERE NOT EXISTS "
                "(SELECT 1 FROM mechanical_scenes WHERE session_id=? AND scene_id=?)",
                (
                    sid,
                    scene_id,
                    canonical_json(
                        {"scene_id": scene_id, "grid": self.store.memory.json(definition.grid_json)}
                    ),
                    sid,
                    scene_id,
                ),
            )
            return
        if package.noncombat_foundation_json is None:
            return
        initial = self.store.memory.json(package.noncombat_foundation_json)
        scenes = [initial["snapshot"]["scene_state"], *initial["additional_scenes"]]
        scene = next((s for s in scenes if s["scene_id"] == scene_id), None)
        if scene is not None:
            self._conn.execute(
                "INSERT INTO mechanical_scenes(session_id,scene_id,state_json) "
                "SELECT ?,?,? WHERE NOT EXISTS "
                "(SELECT 1 FROM mechanical_scenes WHERE session_id=? AND scene_id=?)",
                (sid, scene_id, canonical_json(scene), sid, scene_id),
            )

    def spatial_scene(self, sid, scene_id):
        """Read a supported spatial scene configuration."""
        scene = self.maps.scene(sid, scene_id, spatial_only=True)
        return scene.copy_data() if scene else None

    @property
    def maps(self):
        # Grid owns the representation; Store owns its bounded lifetime. All
        # Facts/Reader instances on this connection use the same disposable pool.
        """Return the shared map access interface."""
        from .grid_maps import GridMaps

        if self.store._grid_maps is None:
            self.store._grid_maps = GridMaps(self.store)
        return self.store._grid_maps

    def mechanical_scene(self, sid, scene_id):
        """Read the persisted mechanical scene and its map."""
        scene = self.maps.scene(sid, scene_id)
        return scene.copy_data() if scene else None


class CombatRecords:
    """Persist encounter workflow and advancement records alongside shared character facts."""

    RULE_FIELDS = frozenset(
        (
            "current_turn_index",
            "round_number",
            "turn_serial",
            "monster_turn_start_done",
            "last_ended_turn",
            "departed_actor_id",
            "ended",
            "damage_instance_sequence",
            "processed_zero_hp_damage_instances",
            "dead_ids",
            "deaths_recorded",
        )
    )

    def __init__(self, store):
        """Bind combat records to shared storage."""
        self.store = store

    @property
    def _conn(self):
        """Return the combat-records connection."""
        return self.store.connection

    def initialize(self, sid, snapshot, *, process_id, withdrawal=None):
        """Store fresh encounter records, replacing an ended encounter only for dynamic start."""
        self.store.require_write()
        effect_fields = (
            "concentration_chain",
            "concentration_rounds_remaining",
            "conditions_by_effect",
            "effect_lifecycles",
        )
        state = snapshot.combat_state.model_dump(mode="json", exclude={"actor_zone"})
        if any(state[field] for field in effect_fields):
            raise StateMachineError(
                "unsupported_combat_foundation", "Initial ongoing effects are not admitted"
            )
        for field in effect_fields:
            del state[field]
        combat_id, scene_id = snapshot.combat_state.combat_id, snapshot.scene_state.scene_id
        if withdrawal is not None and (
            type(withdrawal) not in (EncounterRegion, DistanceWithdrawal)
            or (withdrawal.combat_id, withdrawal.scene_id) != (combat_id, scene_id)
        ):
            raise StateMachineError(
                "invalid_rule_result", "Withdrawal belongs to another encounter"
            )
        replace_ended = (
            " ON CONFLICT(session_id) DO UPDATE SET combat_id=excluded.combat_id,"
            " scene_id=excluded.scene_id,state_json=excluded.state_json,"
            " process_id=excluded.process_id,region_json=excluded.region_json,"
            " withdrawal_distance_ft=excluded.withdrawal_distance_ft"
            " WHERE json_extract(combats.state_json,'$.ended')=1"
            if withdrawal is not None
            else ""
        )
        changed = self._conn.execute(
            "INSERT INTO combats(session_id,combat_id,scene_id,state_json,process_id,region_json,"
            "withdrawal_distance_ft) VALUES (?,?,?,?,?,?,?)" + replace_ended,
            (
                sid,
                combat_id,
                scene_id,
                canonical_json(state),
                process_id,
                canonical_json(sorted(withdrawal.cells))
                if type(withdrawal) is EncounterRegion
                else None,
                withdrawal.distance_ft if type(withdrawal) is DistanceWithdrawal else None,
            ),
        )
        if changed.rowcount != 1:
            raise StateMachineError("combat_already_active", "An encounter is already active")

    def active_region(self, sid):
        """Read the fixed area if the active encounter uses fixed defense."""
        withdrawal = self.active_withdrawal(sid)
        return withdrawal if type(withdrawal) is EncounterRegion else None

    def active_withdrawal(self, sid) -> EncounterRegion | DistanceWithdrawal | None:
        """Read one active encounter's adopted withdrawal policy in the caller's scope."""
        row = self._conn.execute(
            "SELECT combat_id,scene_id,region_json,withdrawal_distance_ft "
            "FROM combats WHERE session_id=? AND json_extract(state_json,'$.ended')=0",
            (sid,),
        ).fetchone()
        if row is None:
            return None
        combat_id, scene_id = row["combat_id"], row["scene_id"]
        if row["withdrawal_distance_ft"] is not None:
            return DistanceWithdrawal(combat_id, scene_id, row["withdrawal_distance_ft"])
        if row["region_json"] is not None:
            return EncounterRegion(
                combat_id, scene_id, frozenset(self.store.memory.json(row["region_json"]))
            )
        return None

    def freeze_positions(self, sid, cells):
        """Freeze position evidence for the closing combat."""
        self.store.require_write()
        self._conn.execute(
            "UPDATE combats SET state_json=json_set(state_json,'$.actor_zone',json(?)) "
            "WHERE session_id=?",
            (canonical_json(cells), sid),
        )

    def withdraw_participant(self, sid, combat_id, actor_id, next_actor):
        """Remove an opponent from live mechanics after the bound evaluator's handoff."""
        self.store.require_write()
        row = self._conn.execute(
            "SELECT state_json FROM combats WHERE session_id=? AND combat_id=?",
            (sid, combat_id),
        ).fetchone()
        if row is None:
            raise StateMachineError("version_conflict", "Encounter changed during withdrawal")
        state = self.store.memory.json(row[0])
        initiative = state["initiative_ids"]
        if (
            state["ended"]
            or actor_id not in state["encounter_ids"]
            or actor_id in state["dead_ids"]
            or actor_id == next_actor
            or initiative[state["current_turn_index"]] != next_actor
        ):
            raise StateMachineError("invalid_rule_result", "Withdrawal lacks its verified handoff")
        remaining = [identity for identity in initiative if identity != actor_id]
        replacement = {
            **state,
            "initiative_ids": remaining,
            "encounter_ids": [
                identity for identity in state["encounter_ids"] if identity != actor_id
            ],
            "current_turn_index": remaining.index(next_actor),
            "last_ended_turn": None,
        }
        for field in (
            "movement_ledgers",
            "opportunity_attack_weapons",
            "monster_slug_by_entity",
            "xp_value_by_entity",
            "monster_action_uses_by_entity",
            "expended_resources",
        ):
            if actor_id in state[field]:
                replacement[field] = {k: v for k, v in state[field].items() if k != actor_id}
        replacement["processed_zero_hp_damage_instances"] = [
            pair for pair in state["processed_zero_hp_damage_instances"] if pair[0] != actor_id
        ]
        self._conn.execute(
            "UPDATE combats SET state_json=? WHERE session_id=? AND combat_id=?",
            (canonical_json(replacement), sid, combat_id),
        )

    def update_rule_state(self, sid, field, value):
        """Persist combat mechanical state."""
        self.store.require_write()
        if field not in self.RULE_FIELDS:
            raise StateMachineError("invalid_rule_result", "Unsupported combat fact")
        self._conn.execute(
            "UPDATE combats SET state_json=json_set(state_json,?,json(?)) WHERE session_id=?",
            ("$." + field, canonical_json(value), sid),
        )

    def update_ledger(self, sid, actor_id, value):
        """Persist the combat advancement ledger."""
        self.store.require_write()
        self._conn.execute(
            "UPDATE combats SET state_json=json_set(state_json,?,json(?)) WHERE session_id=?",
            ("$.movement_ledgers." + canonical_json(actor_id), canonical_json(value), sid),
        )

    def active_movement_ledger(self, sid, actor_id):
        """Read the named actor's ledger JSON from the active encounter, if present."""
        row = self._conn.execute(
            "SELECT json_extract(state_json,?) FROM combats "
            "WHERE session_id=? AND json_extract(state_json,'$.ended')=0",
            ("$.movement_ledgers." + canonical_json(actor_id), sid),
        ).fetchone()
        return row[0] if row is not None else None

    def spend_slot(self, sid, combat_id, owner_id, slot_kind, level, expected, value):
        """Apply one evaluator-guarded owner ledger replacement in the shared write scope."""
        self.store.require_write()
        row = self._conn.execute(
            "SELECT combat_id,state_json FROM combats WHERE session_id=?", (sid,)
        ).fetchone()
        if (
            row is None
            or row["combat_id"] != combat_id
            or slot_kind != "spell_slot"
            or not 1 <= level <= 9
        ):
            raise StateMachineError("invalid_rule_result", "Unsupported spell slot ledger")
        state = self.store.memory.json(row["state_json"])
        ledger = state.get("expended_resources")
        if not isinstance(ledger, dict) or ledger.get(owner_id) != expected:
            raise StateMachineError("invalid_rule_result", "Spell slot ledger changed")
        if expected is not None and not isinstance(expected, dict):
            raise StateMachineError("invalid_rule_result", "Invalid prior spell slot ledger")
        key = f"{slot_kind}:{level}"
        prior = expected or {}
        if value != {**prior, key: prior.get(key, 0) + 1}:
            raise StateMachineError("invalid_rule_result", "Invalid spell slot ledger update")
        self._conn.execute(
            "UPDATE combats SET state_json=json_set(state_json,?,json(?)) "
            "WHERE session_id=? AND combat_id=?",
            (
                "$.expended_resources." + canonical_json(owner_id),
                canonical_json(value),
                sid,
                combat_id,
            ),
        )

    def active(self, session_id):
        """Read the currently active combat, or return None."""
        combat = self._conn.execute(
            "SELECT state_json FROM combats WHERE session_id=?", (session_id,)
        ).fetchone()
        return bool(combat and not self.store.memory.json(combat[0])["ended"])

    def require_noncombat_save(self, row):
        """Reject a save outside the supported noncombat boundary."""
        combat = self._conn.execute(
            "SELECT state_json FROM combats WHERE session_id=?", (row["session_id"],)
        ).fetchone()
        if combat and (not self.store.memory.json(combat[0])["ended"]):
            raise StateMachineError(
                "combat_unrestorable", "MVP does not support active-combat save/resume"
            )
