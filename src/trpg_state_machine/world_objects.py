"""World object capability orchestration within the existing command transaction."""

from dataclasses import asdict

from dnd5e_engine.rules.character import (
    armor_class,
    armor_training_from_changes,
    defense_ac_bonus,
    proficiency_grants,
)
from dnd5e_engine.rules.dice import ability_modifier
from dnd5e_srd_data.schema.item import Armor, Weapon, WeaponProperty

from .errors import StateMachineError
from .scoped_reads import scoped_read
from .world_commands import (
    OPENABLE_CHECK_OPERATIONS,
    ActivateMechanism,
    BarObject,
    CheckMechanism,
    EquipArmor,
    EquipShield,
    EquipWeapon,
    LockObject,
    PickLock,
    PutItem,
    RepositionObject,
    ServiceMechanism,
    SetLightEnabled,
    SpendItemCharge,
    SwitchLightMode,
    TakeItem,
    UnbarObject,
    UnequipArmor,
    UnequipShield,
    UnequipWeapon,
    UnlockObject,
)
from .world_containers import ContainerService
from .world_lights import LightService
from .world_mechanisms import MechanismService
from .world_openable import OpenableService
from .world_spatial import ObjectSpatialService


class WorldObjectService:
    """Route object capability admission, mutation and authorized view construction."""

    def __init__(self, handler):
        """Assemble container, spatial, light, mechanism and opening capability services."""
        self.handler = handler
        self.facts = handler.facts
        self.containers = ContainerService(handler)
        self.spatial = ObjectSpatialService(handler)
        self.lights = LightService(handler)
        self.mechanisms = MechanismService(handler)
        self.openable = OpenableService(handler)

    def validate(self, row, actor_id, operation, package):
        """Dispatch admission and rules availability checks by object operation kind."""
        if type(operation) in OPENABLE_CHECK_OPERATIONS:
            return self.openable.check_availability(row, actor_id, operation, package)
        if type(operation) is CheckMechanism:
            return self.mechanisms.check_availability(row, actor_id, operation, package)
        if type(operation) is ServiceMechanism:
            return self.mechanisms.method_plan(row, actor_id, operation, package)
        if type(operation) is ActivateMechanism:
            self.mechanisms.validate(row, actor_id, operation, package)
            return
        if type(operation) in (SetLightEnabled, SwitchLightMode):
            self.lights.validate(row, actor_id, operation, package)
            return
        if type(operation) is RepositionObject:
            self.spatial.validate_placement(row, actor_id, operation, package)
            return
        if type(operation) in (PutItem, TakeItem):
            self.containers.validate(row, actor_id, operation, package)
            return
        if type(operation) is SpendItemCharge:
            return self._charge_plan(row, actor_id, operation, package)
        if type(operation) in (EquipWeapon, UnequipWeapon):
            return self._weapon_plan(row, actor_id, operation, package)
        if type(operation) in (EquipArmor, UnequipArmor, EquipShield, UnequipShield):
            return self._armor_plan(row, actor_id, operation, package)
        if type(operation) in (LockObject, UnlockObject):
            return self.openable.validate_lock(row, actor_id, operation, package)
        return self.openable.validate_bar(row, actor_id, operation, package)

    def query(self, sid, actor_id, operation, package, *, context):
        """Authorize/query object operation eligibility and return a disclosed structured
        result.
        """
        from .commands import OPERATIONS, TypedCommand, validate_command
        from .spatial import WorldActionAvailability

        kind = next(k for k, v in OPERATIONS.items() if type(operation) is v)
        validate_command(TypedCommand(sid, "query", actor_id, kind, operation, 0))
        h = self.handler
        with h._transaction():
            h._spatial_reader(sid, actor_id, package, context)
            row = h._conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
            try:
                if type(operation) in (
                    SpendItemCharge,
                    EquipWeapon,
                    UnequipWeapon,
                    EquipArmor,
                    UnequipArmor,
                    EquipShield,
                    UnequipShield,
                ):
                    h.sessions._authorize_command(row, actor_id, operation, context)
                    h._world_operation_capability(row, operation)
                if type(operation) is RepositionObject and not getattr(
                    context, "can_place_world_objects", False
                ):
                    raise StateMachineError("unauthorized", "Trusted GM placement grant required")
                plan = self.validate(row, actor_id, operation, package)
            except StateMachineError as exc:
                return WorldActionAvailability("unavailable", exc.code, row["world_version"])
            reason = "already_unlocked" if type(operation) is PickLock and plan[5] else "available"
            return WorldActionAvailability("available", reason, row["world_version"])

    def operate(self, row, actor_id, operation, package, *, source_command_id=None):
        """Dispatch object operation execution to its owning capability within the original
        write scope.
        """
        if type(operation) in OPENABLE_CHECK_OPERATIONS:
            raise StateMachineError(
                "invalid_execution_origin", "Object checks require shared rule execution"
            )
        if type(operation) is ServiceMechanism:
            return self.mechanisms.service(row, actor_id, operation, package)
        if type(operation) is ActivateMechanism:
            return self.mechanisms.operate(row, actor_id, operation, package, source_command_id)
        if type(operation) in (SetLightEnabled, SwitchLightMode):
            return self.lights.operate(row, actor_id, operation, package)
        if type(operation) is RepositionObject:
            return self.spatial.operate(row, actor_id, operation, package)
        if type(operation) in (PutItem, TakeItem):
            return self.containers.operate(row, actor_id, operation, package)
        if type(operation) is SpendItemCharge:
            item = self._charge_plan(row, actor_id, operation, package)
            sid = row["session_id"]
            self.facts.spend_item_charge(sid, item)
            return [
                self.handler._capture_event(
                    sid,
                    package,
                    "item.charge_spent",
                    {"actor_id": actor_id, "instance_id": operation.instance_id},
                    phase="world.after_item_charge_spent",
                    source_actor_id=actor_id,
                )
            ]
        if type(operation) in (EquipWeapon, UnequipWeapon):
            plan = self._weapon_plan(row, actor_id, operation, package)
            iid = operation.instance_id
            if plan["stow_other_hand"]:
                self.handler.characters.set_equipment(
                    row["session_id"], actor_id, **plan["equipment"]
                )
            else:
                self.handler.characters.set_held_weapon(
                    row["session_id"], actor_id, plan["weapon"], plan["grip"]
                )
            return [
                self.handler._capture_event(
                    row["session_id"],
                    package,
                    "item.weapon_equipped"
                    if type(operation) is EquipWeapon
                    else "item.weapon_unequipped",
                    {"actor_id": actor_id, "instance_id": iid},
                    phase="world.after_weapon_binding",
                    source_actor_id=actor_id,
                )
            ]
        if type(operation) in (EquipArmor, UnequipArmor, EquipShield, UnequipShield):
            plan = self._armor_plan(row, actor_id, operation, package)
            self.handler.characters.set_equipment(row["session_id"], actor_id, **plan)
            return [
                self.handler._capture_event(
                    row["session_id"],
                    package,
                    "item.armor_equipped"
                    if type(operation) is EquipArmor
                    else "item.armor_unequipped"
                    if type(operation) is UnequipArmor
                    else "item.shield_equipped"
                    if type(operation) is EquipShield
                    else "item.shield_unequipped",
                    {"actor_id": actor_id, "instance_id": operation.instance_id},
                    phase="world.after_equipment_binding",
                    source_actor_id=actor_id,
                )
            ]
        if type(operation) in (LockObject, UnlockObject):
            return self.openable.operate_lock(row, actor_id, operation, package)
        return self.openable.operate_bar(row, actor_id, operation, package)

    def _weapon_plan(self, row, actor_id, operation, package):
        """Plan the selected weapon and automatic grip from current hand occupancy."""
        sid, iid = row["session_id"], operation.instance_id
        state, equipment = self._equipment_actor(row, actor_id, package)
        if type(operation) is UnequipWeapon:
            if state.get("held_weapon_instance_id") != iid:
                raise StateMachineError("weapon_binding_conflict", "Selected weapon is not held")
            return {"weapon": None, "grip": "none", "stow_other_hand": False}
        weapon = self.handler.loader.get_weapon(self._equipment_slug(sid, actor_id, iid, equipment))
        if not isinstance(weapon, Weapon):
            raise StateMachineError("invalid_target", "Selected instance is not a weapon")
        if weapon.requires_attunement or weapon.passive_effects or weapon.uses is not None:
            raise StateMachineError("unsupported_item_binding", "Weapon lifecycle is unsupported")
        two_handed = WeaponProperty.TWO_HANDED in weapon.properties
        versatile = WeaponProperty.VERSATILE in weapon.properties
        occupied = bool(
            state.get("equipped_shield_instance_id") or state.get("offhand_item_instance_id")
        )
        grip = "two_handed" if two_handed or (versatile and not occupied) else "one_handed"
        if two_handed and occupied:
            shield = state.get("equipped_shield_instance_id")
            ac = state["ac"]
            if shield is not None:
                ac, _ = self._armor_ac(
                    sid, actor_id, state, equipment, state.get("worn_armor_instance_id"), None
                )
            return {
                "weapon": iid,
                "grip": grip,
                "stow_other_hand": True,
                "equipment": {
                    "armor": state.get("worn_armor_instance_id"),
                    "shield": None,
                    "offhand": None,
                    "weapon": iid,
                    "grip": grip,
                    "ac": ac,
                    "stealth": state["stealth_disadvantage"],
                },
            }
        return {"weapon": iid, "grip": grip, "stow_other_hand": False}

    def _equipment_actor(self, row, actor_id, package):
        """Read the current PC instance equipment after ordinary actor admission."""
        sid = row["session_id"]
        self.handler._world_actor_ready(sid, actor_id)
        if row["mechanical_schema_version"] not in ("engine-snapshot/19", "engine-snapshot/20"):
            raise StateMachineError(
                "unsupported_actor_state",
                "Explicit instance equipment requires snapshot /19 or /20",
            )
        self.openable.rules._verify_dnd_binding(package, row)
        state = self.handler.characters.binding_actor(sid, actor_id)
        equipment = self.handler.characters.equipment(sid, actor_id)
        if equipment is None:
            raise StateMachineError("invalid_mechanical_state", "Actor equipment facts are missing")
        if state is None or (
            state.get("held_weapon_instance_id") is None
            and state.get("weapon_in_hands") is not None
        ):
            raise StateMachineError("unsupported_actor_state", "Explicit weapon binding required")
        return state, equipment

    def _direct_item(self, sid, actor_id, iid):
        """Return one owned, accessible and directly carried item entry by exact ID."""
        item = self.facts.object(sid, iid)
        if (
            item is None
            or item["item_slug"] is None
            or item["carrier_id"] != actor_id
            or item["container_id"] is not None
        ):
            raise StateMachineError("invalid_target", "No directly carried equipment")
        entry = self.facts.item_entry(item)
        if (
            entry is None
            or entry["owner_id"] != actor_id
            or not entry["accessible"]
            or entry["quantity"] != 1
        ):
            raise StateMachineError("invalid_target", "No accessible owned equipment")
        return entry

    def _equipment_slug(self, sid, actor_id, iid, equipment):
        """Use cached validation for a bound item, or admit one new target by ID."""
        for identity, slug in equipment.direct_slugs:
            if identity == iid:
                if slug is None:
                    raise StateMachineError("invalid_target", "No directly carried equipment")
                return slug
        return self._direct_item(sid, actor_id, iid)["item_slug"]

    def _plain_armor(self, sid, actor_id, iid, category, equipment):
        """Resolve a directly carried ordinary armor piece in one supported category."""
        if iid is None:
            return None
        asset = self.handler.loader.get_item(self._equipment_slug(sid, actor_id, iid, equipment))
        if not isinstance(asset, Armor) or asset.armor_category not in category:
            raise StateMachineError("invalid_target", "Selected instance is not suitable armor")
        if (
            asset.magical
            or asset.magical_bonus
            or asset.requires_attunement
            or asset.passive_effects
            or asset.uses is not None
            or asset.strength_min is not None
        ):
            raise StateMachineError("unsupported_item_binding", "Armor lifecycle is unsupported")
        return asset

    def _armor_training(self, state):
        """Use shared RE class grants for a single known class and explicit features."""
        levels = state.get("classes") or (
            {state["class_slug"]: state["character_level"]} if state.get("class_slug") else {}
        )
        if levels not in ({"fighter": 1}, {"cleric": 1}):
            raise StateMachineError("unsupported_armor_class", "Armor training is not established")
        slug, level = next(iter(levels.items()))
        source = self.handler.loader.get_class(slug)
        if source is None:
            raise StateMachineError("unsupported_armor_class", "Armor training is not established")
        allowed_grants = {
            grant.slug
            for grant in source.granted_features
            if grant.ref_type == "feature" and grant.level <= level
        }
        if slug == "cleric":
            allowed_grants.add("divine-order-protector")
        if (
            state.get("class_slug") not in (None, slug)
            or state.get("character_level") != level
            or state.get("species_slug")
            or state.get("subclass_slug")
            or state.get("trait_mechanics")
            or not set(state.get("granted_features") or ()) <= allowed_grants
            or (state.get("fighting_styles") or ()) not in ((), [], ["defense"], ("defense",))
            or (slug == "cleric" and state.get("fighting_styles"))
        ):
            raise StateMachineError("unsupported_armor_class", "AC has an unreviewed source")
        trained = set(proficiency_grants([(source, level, "primary")]).armor)
        if slug == "cleric" and "divine-order-protector" in (state.get("granted_features") or ()):
            feature = self.handler.loader.get_feature("divine-order-protector")
            if feature is not None:
                trained.update(
                    armor_training_from_changes(
                        change
                        for effect in feature.passive_effects
                        if not effect.disabled
                        for change in effect.changes
                    )
                )
        return trained

    def _armor_ac(self, sid, actor_id, state, equipment, next_armor_id, next_shield_id):
        """Return verifiable default-mode AC and the next body armor asset."""
        assets = {}

        def piece(iid, category):
            """Reuse a selected armor asset within this one equipment transition."""
            key = (iid, category)
            if key not in assets:
                assets[key] = self._plain_armor(sid, actor_id, iid, category, equipment)
            return assets[key]

        next_armor = piece(next_armor_id, ("light", "medium"))
        next_shield = piece(next_shield_id, ("shield",))
        old_armor = piece(state.get("worn_armor_instance_id"), ("light", "medium"))
        old_shield = piece(state.get("equipped_shield_instance_id"), ("shield",))
        trained = self._armor_training(state)
        for armor_piece in (old_armor, old_shield, next_armor, next_shield):
            if armor_piece is not None and armor_piece.armor_category not in trained:
                raise StateMachineError("unsupported_armor_training", "Armor training is required")
        dex = ability_modifier(state["dexterity"])
        modifiers = {"dexterity": dex}

        def resolve(body, shield):
            return armor_class(
                "default",
                modifiers,
                body_armor=body,
                body_armor_bonus=0,
                shield_bonus=shield.base_ac if shield else 0,
            ) + defense_ac_bonus(state.get("fighting_styles") or (), wearing_armor=body is not None)

        if state["ac"] != resolve(old_armor, old_shield) or state["stealth_disadvantage"] != (
            old_armor.stealth_disadvantage if old_armor else False
        ):
            raise StateMachineError("unsupported_armor_class", "Current AC has an unknown basis")
        return resolve(next_armor, next_shield), next_armor

    def _armor_plan(self, row, actor_id, operation, package):
        """Plan exact armor slot replacement, hand stowing, grip and character AC."""
        sid, iid = row["session_id"], operation.instance_id
        state, equipment = self._equipment_actor(row, actor_id, package)
        is_armor = type(operation) in (EquipArmor, UnequipArmor)
        equip = type(operation) in (EquipArmor, EquipShield)
        field = "worn_armor_instance_id" if is_armor else "equipped_shield_instance_id"
        if not equip and state.get(field) != iid:
            raise StateMachineError("equipment_binding_conflict", "Selected item is not equipped")
        if equip and state.get(field) == iid:
            raise StateMachineError(
                "equipment_binding_conflict", "Selected item is already equipped"
            )
        armor = (
            iid
            if equip and is_armor
            else (None if not equip and is_armor else state.get("worn_armor_instance_id"))
        )
        shield = (
            iid
            if equip and not is_armor
            else (None if not equip and not is_armor else state.get("equipped_shield_instance_id"))
        )
        ac, armor_asset = self._armor_ac(sid, actor_id, state, equipment, armor, shield)
        offhand = None if equip and not is_armor else state.get("offhand_item_instance_id")
        weapon_id = state.get("held_weapon_instance_id")
        grip = state.get("weapon_grip", "none")
        if weapon_id is not None:
            weapon = self.handler.loader.get_weapon(
                self._equipment_slug(sid, actor_id, weapon_id, equipment)
            )
            if not isinstance(weapon, Weapon):
                raise StateMachineError("invalid_mechanical_state", "Held item is not a weapon")
            if shield is not None and WeaponProperty.TWO_HANDED in weapon.properties:
                weapon_id, grip = None, "none"
            elif WeaponProperty.VERSATILE in weapon.properties:
                grip = "one_handed" if shield or offhand else "two_handed"
        return {
            "armor": armor,
            "shield": shield,
            "offhand": offhand,
            "weapon": weapon_id,
            "grip": grip,
            "ac": ac,
            "stealth": armor_asset.stealth_disadvantage if armor_asset else False,
        }

    def _charge_plan(self, row, actor_id, operation, package):
        """Check exact ownership, access, portable policy, durability and balance."""
        sid, iid = row["session_id"], operation.instance_id
        item = self.facts.object(sid, iid)
        if item is None or item["item_slug"] is None:
            raise StateMachineError("invalid_target", "No usable owned item")
        if (
            self.facts.placement(sid, iid)["carrier_id"] != actor_id
            or not self.facts.instances.is_portable(sid, iid)
            or self.facts.instances.get(sid, "object", iid)["portable_policy"]
            != "charge_exhaust_retain"
            or not self.facts.require_item_unit(item)["accessible"]
        ):
            raise StateMachineError("invalid_target", "No usable owned item")
        self.containers.reachable(sid, actor_id, iid, package)
        if not self.facts.item_accessible(sid, iid):
            raise StateMachineError("invalid_target", "No usable owned item")
        asset = self.handler.loader.get_item(item["item_slug"])
        uses = getattr(asset, "uses", None)
        if (
            asset is None
            or uses is None
            or uses.auto_destroy
            or getattr(asset, "requires_attunement", False)
            or getattr(asset, "passive_effects", ())
            or getattr(asset, "activities", ())
        ):
            raise StateMachineError(
                "unsupported_item_policy", "Retained charge policy is unavailable"
            )
        if item["item_charges"] < 1:
            raise StateMachineError("item_charges_exhausted", "No item charges remain")
        return item

    def _visible(self, sid, actor_id, oid, package):
        """Check actual object placement and visible/touchable eligibility."""
        h = self.handler
        h._public_scope(sid, actor_id, package)
        obj = self.facts.object(sid, oid)
        if obj is None:
            raise StateMachineError("invalid_target", "No authorized local object")
        if obj["item_slug"] is not None and not self.facts.instances.is_portable(sid, oid):
            raise StateMachineError("invalid_target", "No authorized local object")
        if obj["container_id"] is not None:
            try:
                self.containers.reachable(sid, actor_id, oid, package)
            except StateMachineError as exc:
                raise StateMachineError("invalid_target", "No authorized local object") from exc
            return obj
        if obj["carrier_id"] == actor_id:
            return obj
        if not self.spatial.visible_cells(sid, actor_id, oid, package):
            raise StateMachineError("invalid_target", "No authorized local object")
        return obj

    @scoped_read
    def _view_inputs(self, sid, actor_id):
        """Prepare actor position and world version once for object views."""
        return self.handler._actor_position(sid, actor_id), self.handler._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()

    def view(self, sid, actor_id, oid, package, *, containment=None):
        """Combine instance capabilities/current state into an authorized contents-aware
        object view.
        """
        obj = self._visible(sid, actor_id, oid, package)
        definition = self.facts.object_definition(sid, oid)
        config = (
            asdict(definition)
            if definition
            else {
                "object_id": oid,
                "definition_ref": oid,
                "display_name": oid,
                "description_ref": None,
                "material": None,
                "size_category": None,
                "weight_lb": None,
            }
        )
        config.pop("capabilities", None)
        declared = (
            tuple(c.kind for c in definition.capabilities)
            if definition
            else (("openable",) if obj["state"] is not None else ())
        )
        openable = self.facts.openable_state(sid, oid)
        hp = self.facts.object_hp(sid, oid)
        destroyed = hp == 0 if hp is not None else None
        projection = self.spatial.project(sid, oid, package)
        from ._operations import ObjectState, SetObjectState

        position, row = self._view_inputs(sid, actor_id)
        available = {}
        for name, op in (
            ("can_activate", ActivateMechanism(position.scene_id, oid, position.cell)),
            ("can_open", SetObjectState(position.scene_id, oid, ObjectState.OPEN)),
            ("can_close", SetObjectState(position.scene_id, oid, ObjectState.CLOSED)),
            ("can_bar", BarObject(position.scene_id, oid, position.cell)),
            ("can_unbar", UnbarObject(position.scene_id, oid, position.cell)),
        ):
            try:
                if isinstance(op, SetObjectState):
                    self.openable.validate_open(row, actor_id, op, package)
                else:
                    self.validate(row, actor_id, op, package)
                available[name] = True
            except StateMachineError:
                available[name] = False
        contents_visible = self.containers.config(sid, oid) and self.openable.contents_accessible(
            sid, oid
        )
        contents_weight = self.containers.contents_weight(sid, oid) if contents_visible else None
        return {
            "contents": tuple(c.object_id for c in self.facts.active_contents(sid, oid))
            if contents_visible
            else None,
            "contents_weight_lb": contents_weight
            if definition and definition.weight_lb is not None
            else None,
            "mechanism_status": self.facts.mechanism_status(sid, oid),
            "mechanism_state": asdict(self.facts.mechanism_state(sid, oid))
            if self.facts.mechanism_state(sid, oid)
            else None,
            "linked_result_preview": "hidden" if self.mechanisms.config(sid, oid) else None,
            "light": self.facts.light_state(sid, oid),
            "spatial": asdict(self.spatial.config(sid, oid))
            if self.spatial.config(sid, oid)
            else None,
            "orientation": obj["orientation"],
            "occupied_cells": projection.occupied_cells,
            "movement_blocking_cells": projection.movement_blocking_cells,
            "difficult_terrain_cells": projection.difficult_terrain_cells,
            "vision_occlusion": projection.vision_occlusion_cells,
            "cover_candidates": projection.cover_cells,
            **available,
            "locks_for_object": tuple(
                {"lock_id": lock[0], "locked": bool(lock[1])}
                for lock in self.facts.locks_for_object(sid, oid)
            ),
            "effective_locked": self.facts.door_lock(sid, oid),
            "openable": openable,
            "current_hp": hp,
            "destroyed": destroyed,
            "durability": asdict(definition.capability("durability"))
            if definition and definition.capability("durability")
            else None,
            **config,
            "package_revision": package.package_digest,
            "capabilities": declared,
            "effective_capabilities": () if destroyed else declared,
            "open_state": obj["state"],
            "location": self.facts.object_position(sid, oid),
        }

    def get_object(self, sid, actor_id, oid, package, *, context):
        """Authorize reading the selected object's view."""
        if type(oid) is not str or not 0 < len(oid) <= 256:
            raise StateMachineError("invalid_query", "Object identity must be a bounded string")
        h = self.handler
        with h._transaction():
            h._spatial_reader(sid, actor_id, package, context)
            return self.view(sid, actor_id, oid, package)

    def list_objects(self, sid, actor_id, package, *, context):
        """List actual visible scene/door-associated objects using shared read inputs."""
        h = self.handler
        with h._transaction():
            h._spatial_reader(sid, actor_id, package, context)
            result = []
            position = h._public_scope(sid, actor_id, package)
            doors = tuple(
                p.door_object_id
                for p in self.facts.instances.passages(sid, scene_id=position.scene_id)
                if p.door_object_id and any(e.scene_id == position.scene_id for e in p.endpoints)
            )
            candidates = self.facts.scene_object_rows(
                sid, position.scene_id, carrier_id=actor_id, extra=doors
            )
            for obj in candidates:
                try:
                    result.append(self.view(sid, actor_id, obj["object_id"], package))
                except StateMachineError as exc:
                    if exc.code != "invalid_target":
                        raise
            return tuple(result)
