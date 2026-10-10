"""Original V2 fixture with explicit mechanical facts, canonical weapons and XP."""

import random
from copy import deepcopy

from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd_support import package_data as legacy_data

from trpg_state_machine.package import _validate


def modern_loader():
    bundled = BundledAssetLoader()
    return MemoryAssetLoader(
        items=[
            bundled.get_weapon(slug) for slug in ("rapier", "longsword", "dagger", "greatsword")
        ],
        monsters=[bundled.get_monster("bandit")],
    )


def modern_data(base, assets, *, npc_hp=1, weapon="rapier", grip="one_handed"):
    data = legacy_data(deepcopy(base), assets)
    foundation = data["rule_bindings"]["combat_foundation"]
    snapshot = foundation["snapshot"]
    snapshot["snapshot_schema_version"] = "engine-snapshot/2"
    for actor in snapshot["character_states"]:
        actor.update(
            weapon_in_hands=None,
            weapon_grip="none",
            other_hand_occupied=False,
            weapon_mastery_slugs=[],
        )
    pc, npc = snapshot["character_states"]
    pc.update(
        carried_item_slugs=[weapon],
        weapon_in_hands=weapon,
        weapon_grip=grip,
        strength=16,
        dexterity=18,
        proficiency_bonus_override=2,
        attack_bonus=None,
        weapon_proficiencies=["simple_melee", "martial_melee"],
        skill_proficiencies=["athletics"],
        skill_expertise=[],
    )
    npc.update(hp_current=npc_hp, hp_max=npc_hp, ac=1, carried_item_slugs=[])
    data["pc_templates"][0]["character"]["inventory"] = [weapon]
    foundation["rng_state"] = RNGState.capture(random.Random(0)).model_dump(mode="json")
    noncombat = deepcopy(snapshot)
    del noncombat["combat_state"]
    noncombat["snapshot_kind"] = "non_combat"
    extra = deepcopy(snapshot["scene_state"])
    extra["scene_id"] = "scene.workshop"
    data["rule_bindings"]["noncombat_foundation"] = {
        "snapshot": noncombat,
        "rng_state": deepcopy(foundation["rng_state"]),
        "xp": {pc["entity_id"]: 10, npc["entity_id"]: 0},
        "additional_scenes": [extra],
    }
    data["manifest"]["ruleset_binding"] = ruleset_binding(assets).model_dump(mode="json")
    return data


def modern_package(base, assets, **kwargs):
    return _validate(modern_data(base, assets, **kwargs))
