"""Controlled current facts over the caller's consistent RAM/candidate scope.

No method commits or computes rules. Store coordinates the RAM representation
of these same owned facts and their incremental persistent changes.
Rules metadata edits have a closed field set matching the pinned adapter.
"""

import json

from dnd5e_engine.evaluation_casting import CastingState
from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_resources import ResourceState
from dnd5e_engine.evaluation_state import InventoryState
from pydantic import TypeAdapter

from .errors import StateMachineError
from .item_units import core_item_id, valid_item_unit
from .package import canonical_json
from .runtime_instances import instance_read
from .scoped_reads import scoped_read

_EFFECTS = TypeAdapter(tuple[EffectState, ...])


class CharacterFacts:
    """Maintain character mechanics and project inventory from World item facts."""

    RULE_FIELDS = frozenset(
        (
            "temp_hp",
            "is_alive",
            "death_saves",
            "conditions",
            "last_damaged_by",
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

    def __init__(self, store, world):
        """Bind character storage and its cooperating world facts interface."""
        self.store = store
        self.world = world

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
            "INSERT INTO characters(session_id,actor_id,current_hp,max_hp,"
            "resources_json) VALUES (?,?,?,?,?)",
            (
                sid,
                actor_id,
                pc.current_hp,
                pc.max_hp,
                canonical_json(dict(pc.resources)),
            ),
        )

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
            if row["object_id"].startswith(prefix)
            and row["item_json"] is not None
            and self.store.memory.json(row["item_json"])["item_slug"] == slug
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
            self.store.memory.json(old["item_json"])
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

    def spend_resource(self, sid, actor_id, resource_id, amount):
        """Read and debit the named character resource, persisting its remaining balance."""
        self.store.require_write()
        row = self._conn.execute(
            "SELECT resources_json FROM characters WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()
        resources = self.store.memory.json(row[0])
        resources[resource_id] -= amount
        self._conn.execute(
            "UPDATE characters SET resources_json=? WHERE session_id=? AND actor_id=?",
            (canonical_json(resources), sid, actor_id),
        )

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
        """Validate and persist character rules metadata."""
        self.store.require_write()
        if field not in self.RULE_FIELDS:
            raise StateMachineError("invalid_rule_result", "Unsupported actor fact")
        self._conn.execute(
            "UPDATE mechanical_actors SET state_json=json_set(state_json,?,json(?)) "
            "WHERE session_id=? AND actor_id=?",
            ("$." + field, canonical_json(value), sid, actor_id),
        )

    def initialize_rule_actor(self, row, actor, *, xp=None, encounter=False):
        """Initialize mechanical actor state consistently with existing character facts."""
        self.store.require_write()
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
                if self.store.memory.json(core["resources_json"]) or self.store.memory.json(
                    core["effects_json"]
                ):
                    raise StateMachineError(
                        "unsupported_combat_foundation",
                        "Legacy resources/effects cannot be converted implicitly",
                    )
            if (
                (core["current_hp"], core["max_hp"]) != (actor.hp_current, actor.hp_max)
                or self.store.memory.json(core["resources_json"])
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
                "INSERT INTO characters(session_id,actor_id,current_hp,max_hp,resources_json,"
                "effects_json,xp) VALUES (?,?,?,?,?,?,?)",
                (
                    sid,
                    actor.entity_id,
                    actor.hp_current,
                    actor.hp_max,
                    "{}",
                    "[]",
                    0 if xp is None else xp,
                ),
            )
        state = actor.model_dump(
            mode="json", exclude={"hp_current", "hp_max", "carried_item_slugs"}
        )
        if state.get("held_weapon_instance_id") is not None:
            del state["weapon_in_hands"]
        else:
            state.pop("held_weapon_instance_id", None)
        self._conn.execute(
            "INSERT INTO mechanical_actors VALUES (?,?,?)",
            (sid, actor.entity_id, canonical_json(state)),
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
        fields = ("resource_state", "casting_state", "effect_states")
        data = snapshot.model_dump(mode="json", include=set(fields))
        encoded = tuple(canonical_json(data[field]) for field in fields)
        old = self._conn.execute(
            "SELECT schema_version,resources_json,casting_json,effects_json "
            "FROM mechanical_components WHERE session_id=?",
            (sid,),
        ).fetchone()
        expected = (snapshot.snapshot_schema_version, *encoded)
        if old is not None:
            if tuple(old) != expected:
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
            "(session_id,schema_version,resources_json,casting_json,effects_json) "
            "VALUES (?,?,?,?,?)",
            (sid, *expected),
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

    @scoped_read
    def _component_inputs(self, row):
        """Decode noninventory components with reusable owner lookup indices."""
        from .component_reads import ComponentInputs

        return ComponentInputs(
            {
                field: self.store.memory.json(row[field])
                for field in ("resources_json", "casting_json", "effects_json")
            }
        )

    @scoped_read
    def binding_components(self, sid, actor_id):
        """Validate only noninventory mechanical facts needed for item bindings."""
        row = self._conn.execute(
            "SELECT * FROM mechanical_components WHERE session_id=?", (sid,)
        ).fetchone()
        if row is None or row["schema_version"] not in ("engine-snapshot/17", "engine-snapshot/18"):
            raise StateMachineError(
                "unsupported_mechanical_version", "Explicit component facts are missing"
            )
        try:
            values = self._component_inputs(row).select({actor_id})
            resources = values["resources_json"]
            if resources is not None:
                ResourceState.model_validate(
                    {**resources, "pools": tuple(resources["pools"])}
                    if isinstance(resources["pools"], list)
                    else resources
                )
            casting = values["casting_json"]
            if casting is not None:
                casting = self.store.memory.model_json(
                    CastingState, canonical_json(casting)
                ).model_dump(mode="json")
            effects = self.store.memory.model_json(_EFFECTS, canonical_json(values["effects_json"]))
            return {
                "casting_state": casting,
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
        if row is None or row["schema_version"] not in ("engine-snapshot/17", "engine-snapshot/18"):
            raise StateMachineError(
                "unsupported_mechanical_version", "Explicit component facts are missing"
            )
        try:
            _, item_entries = self.world.portable_inventory(
                sid, owner_ids=None if full_audit else owner_ids
            )
            item_inventory = {"entries": list(item_entries.values())}
            if not full_audit:
                values = self._component_inputs(row).select(owner_ids)
                values["inventory"] = item_inventory
            else:
                values = {
                    field: self.store.memory.json(row[field])
                    for field in (
                        "resources_json",
                        "casting_json",
                        "effects_json",
                    )
                }
                values["inventory"] = item_inventory
            inventory, resources, casting, effects = (
                values[field]
                for field in ("inventory", "resources_json", "casting_json", "effects_json")
            )
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
            casting = (
                self.store.memory.model_json(CastingState, canonical_json(casting)).model_dump(
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
        portable = [r for r in rows if r["item_json"] is not None]
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
        if before["object_id"] != after["object_id"] or before["item_json"] != after["item_json"]:
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
    def _rule_actor(self, sid, actor_id, *, include_carried=True):
        """Read a mechanical actor and combine its current core character facts."""
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
        if parse(core["resources_json"]) or parse(core["effects_json"]):
            raise StateMachineError(
                "invalid_mechanical_state", "Legacy core resources/effects have no typed source"
            )
        data = parse(actor[0])
        held = data.get("held_weapon_instance_id")
        if held is not None and include_carried:
            item = self.world.object(sid, held)
            entry = self.world.item_entry(item) if item is not None else None
            if entry is None or entry["owner_id"] != actor_id or not entry["accessible"]:
                raise StateMachineError(
                    "invalid_mechanical_state", "Held weapon requires an accessible owned item"
                )
            data["weapon_in_hands"] = entry["item_slug"]
        data.update(
            hp_current=core["current_hp"],
            hp_max=core["max_hp"],
            carried_item_slugs=self.carried_slugs(sid, actor_id) if include_carried else [],
        )
        return data

    def binding_actor(self, sid, actor_id):
        """Read current actor equipment metadata without assembling carried items."""
        return self._rule_actor(sid, actor_id, include_carried=False)

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
        from dataclasses import asdict

        from .environment_models import EnvironmentSceneFacts

        self.store.require_write()
        if type(facts) is not EnvironmentSceneFacts:
            raise StateMachineError("invalid_environment", "Expected typed Scene environment facts")
        self._conn.execute(
            "INSERT INTO scene_environments VALUES (?,?,?) "
            "ON CONFLICT(session_id,scene_id) DO UPDATE SET facts_json=excluded.facts_json",
            (sid, scene_id, canonical_json(asdict(facts))),
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

    def __init__(self, store):
        """Bind world storage and initialize its instance accessor."""
        from .instance_data import InstanceData

        self.store = store
        self._instances = InstanceData(self)

    @property
    def _conn(self):
        """Return the shared world-facts connection."""
        return self.store.connection

    def initialize_world(self, sid, package):
        """Initialize persisted world/object facts and Session instance configuration."""
        self.store.require_write()
        self.instances.initialize_world(sid, package)
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
                self._conn.execute(
                    "INSERT INTO object_lights VALUES (?,?,?,?) "
                    "ON CONFLICT(session_id,object_id) DO NOTHING",
                    (sid, d.object_id, int(c.initial_enabled), c.initial_mode),
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

    def set_light_state(self, sid, oid, enabled, mode):
        """Validate and update the light switch or mode."""
        self.store.require_write()
        if self.light_state(sid, oid) is None or type(enabled) is not bool or type(mode) is not str:
            raise StateMachineError("unsupported_object_capability", "No valid emitter state")
        self._conn.execute(
            "UPDATE object_lights SET enabled=?,mode=? WHERE session_id=? AND object_id=?",
            (int(enabled), mode, sid, oid),
        )

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
            "SELECT item_json FROM objects WHERE session_id=? AND object_id=?", (sid, object_id)
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
                and old["item_json"] is not None
                and self.store.memory.json(old["item_json"]) == unit
                and self.placement(sid, iid)["carrier_id"] == owner
            ):
                return
            raise StateMachineError("invalid_item_foundation", "Initial item identity conflicts")
        self._conn.execute(
            "INSERT INTO objects(session_id,object_id,scene_id,state,cell,"
            "carrier_id,item_json) VALUES (?,?,NULL,NULL,NULL,?,?)",
            (sid, iid, owner, canonical_json(unit)),
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
        """Initialize portable item instances and their related records."""
        self.store.require_write()
        sid = row["session_id"]
        if row["world_version"] or row["event_seq"]:
            raise StateMachineError(
                "invalid_item_foundation", "Item initialization requires a fresh session"
            )
        for item in package.portable_items:
            if loader.get_item(item.item_slug) is None:
                raise StateMachineError("invalid_item_foundation", "Unknown pinned mechanical item")
            owner = (
                row["pc_actor_id"]
                if item.initial_owner_id == row["pc_template_id"]
                else item.initial_owner_id
            )
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
            if old["item_json"] is not None:
                if self.store.memory.json(old["item_json"]) != data or old["carrier_id"] != owner:
                    raise StateMachineError(
                        "invalid_item_foundation", "Portable instance differs from item facts"
                    )
                continue
            self._conn.execute(
                "UPDATE objects SET item_json=?,carrier_id=?,scene_id=?,cell=? "
                "WHERE session_id=? AND object_id=?",
                (
                    canonical_json(data),
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
        """Read the direct contents of the parent object."""
        return self._conn.execute(
            "SELECT * FROM objects WHERE session_id=? AND container_id=? ORDER BY object_id",
            (sid, oid),
        ).fetchall()

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
        return {"instance_id": oid, "owner_id": placement["carrier_id"], **data}

    def require_item_unit(self, row):
        """Validate and return quantity data for one portable item instance."""
        data = self.store.memory.json(row["item_json"]) if row["item_json"] is not None else None
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
                "SELECT * FROM objects WHERE session_id=? AND item_json IS NOT NULL", (sid,)
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
                selected[oid] for oid in sorted(selected) if selected[oid]["item_json"] is not None
            ]
        for row in rows:
            identities.add(row["object_id"])
            data = self.store.memory.json(row["item_json"])
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
        if current is None or dict(current) != dict(before) or current["item_json"] is None:
            raise StateMachineError("position_conflict", "Item location changed")
        self._conn.execute(
            "UPDATE objects SET carrier_id=?,scene_id=?,cell=?,container_id=? "
            "WHERE session_id=? AND object_id=?",
            (carrier_id, scene_id, cell, container_id, sid, before["object_id"]),
        )
        return self._conn.execute(
            "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, before["object_id"])
        ).fetchone()

    def object_position(self, sid, object_id):
        """Resolve the object's current spatial position."""
        obj = self.object(sid, object_id)
        return (
            {
                **self.placement(sid, object_id),
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

    def initialize(self, sid, snapshot, *, spatial, process_id):
        """Initialize combat runtime records."""
        self.store.require_write()
        self._conn.execute(
            "INSERT INTO combats VALUES (?,?,?,?,?)",
            (
                sid,
                snapshot.combat_state.combat_id,
                snapshot.scene_state.scene_id,
                snapshot.combat_state.model_dump_json(exclude={"actor_zone"}),
                process_id,
            ),
        )

    def freeze_positions(self, sid, cells):
        """Freeze position evidence for the closing combat."""
        self.store.require_write()
        self._conn.execute(
            "UPDATE combats SET state_json=json_set(state_json,'$.actor_zone',json(?)) "
            "WHERE session_id=?",
            (canonical_json(cells), sid),
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
