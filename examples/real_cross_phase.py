"""Original authored fixture; actual pinned Engine, SQLite and child-process resume.

Run: uv run --locked python examples/real_cross_phase.py --directory .test-tmp/demo-new
The directory must be new; existing saves are never reset.
"""

import argparse
import json
import random
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import yaml
from dnd5e_engine.evaluation_contracts import CheckPayload, CombatClosePayload, CombatIntentPayload
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from trpg_state_machine import (
    CreateSessionRequest,
    EnterScene,
    InitializeCombat,
    PlayerSelection,
    SetSceneFlag,
    StateMachine,
    TrustedContext,
    TypedCommand,
    load_adventure_package,
)

NPC = "npc.caretaker"
CONTEXT = TrustedContext("example:trusted-host", "player.example", (NPC,), True)


def assets():
    source = BundledAssetLoader()
    return MemoryAssetLoader(
        items=[source.get_weapon("rapier")], monsters=[source.get_monster("bandit")]
    )


def author_fixture(loader):
    root = Path(__file__).resolve().parents[1] / "tests/fixtures"
    package = yaml.safe_load((root / "lantern_station.yaml").read_text("utf-8"))
    initial = json.loads((root / "dnd_lantern_combat.json").read_text("utf-8"))["snapshot"]
    initial["snapshot_schema_version"] = "engine-snapshot/2"
    for actor in initial["character_states"]:
        actor.update(
            weapon_in_hands=None,
            weapon_grip="none",
            other_hand_occupied=False,
            weapon_mastery_slugs=[],
            carried_item_slugs=[],
        )
    pc, npc = initial["character_states"]
    pc.update(
        carried_item_slugs=["rapier"],
        weapon_in_hands="rapier",
        weapon_grip="one_handed",
        dexterity=18,
        strength=16,
        attack_bonus=None,
        proficiency_bonus_override=2,
        weapon_proficiencies=["simple_melee", "martial_melee"],
        skill_proficiencies=["athletics"],
    )
    npc.update(hp_current=1, hp_max=1, ac=1)
    initial["combat_state"].update(
        current_turn_index=1, turn_serial=2, monster_turn_start_done=[2, NPC]
    )
    package["pc_templates"] = [package["pc_templates"][0]]
    package["pc_templates"][0]["character"]["inventory"] = ["rapier"]
    for actor in package["actors"]:
        actor["senses"] = {"visual": True, "auditory": True}
    noncombat = deepcopy(initial)
    del noncombat["combat_state"]
    noncombat["snapshot_kind"] = "non_combat"
    rng = RNGState.capture(random.Random(0)).model_dump(mode="json")
    extra = deepcopy(initial["scene_state"])
    extra["scene_id"] = "scene.workshop"
    package["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    package["rule_bindings"] = {
        "combat_foundation": {"snapshot": initial, "rng_state": rng},
        "noncombat_foundation": {
            "snapshot": noncombat,
            "rng_state": rng,
            "xp": {pc["entity_id"]: 10, NPC: 0},
            "additional_scenes": [extra],
        },
    }
    return package


def check(actor):
    return CheckPayload(
        actor_id=actor,
        ability="str",
        skill="athletics",
        tool=None,
        dc=10,
        target_id=None,
        context="generic",
        required_sense="none",
        social_interaction=False,
        advantage=(),
        disadvantage=(),
        redeem_granted_die=None,
    )


def send(sm, sid, package, key, kind, payload, actor=None):
    state = sm.read_session(sid, package, context=CONTEXT)
    receipt = sm.submit_command(
        TypedCommand(sid, key, actor or state.pc_actor_id, kind, payload, state.world_version),
        package,
        context=CONTEXT,
    )
    if receipt.status != "committed":
        raise RuntimeError(receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--resume")
    args = parser.parse_args()
    loader = assets()
    document = args.directory / "original-package.json"
    database = args.directory / "session.db"
    if not args.resume:
        args.directory.mkdir(parents=True, exist_ok=False)
        document.write_text(json.dumps(author_fixture(loader)), "utf-8")
    loaded = load_adventure_package(document)
    if loaded.status != "valid":
        raise RuntimeError(loaded.errors)
    package = loaded.package
    with StateMachine(database, dnd_loader=loader) as sm:
        if args.resume:
            state = sm.resume_session(args.resume, package, context=CONTEXT)
            send(sm, args.resume, package, "check.resumed", "rules.check", check(state.pc_actor_id))
            after = sm.save_session(args.resume, package, context=CONTEXT)
            print(
                json.dumps(
                    {
                        "session_id": after.session_id,
                        "phase": after.phase,
                        "xp": after.character.xp,
                        "hp": after.character.current_hp,
                        "world_version": after.world_version,
                        "rng_version": after.rng.rng_version,
                    }
                )
            )
            return
        request = CreateSessionRequest(
            "create.example",
            package.adventure_id,
            package.package_version,
            package.package_digest,
            (PlayerSelection(CONTEXT.player_id, "pc_template.observer"),),
        )
        sid = sm.create_session(request, package, context=CONTEXT).session_id
        state = sm.read_session(sid, package, context=CONTEXT)
        send(sm, sid, package, "explore.workshop", "enter_scene", EnterScene("scene.workshop"))
        send(sm, sid, package, "explore.return", "enter_scene", EnterScene("scene.platform"))
        send(sm, sid, package, "check.first", "rules.check", check(state.pc_actor_id))
        send(
            sm,
            sid,
            package,
            "world.lamp",
            "set_scene_flag",
            SetSceneFlag("scene.platform", "flag.lamp_checked", True),
        )
        send(sm, sid, package, "combat.init", "combat.initialize", InitializeCombat())
        send(
            sm,
            sid,
            package,
            "attack.npc",
            "combat.intent",
            CombatIntentPayload(
                intent_type="attack",
                source_id=NPC,
                stat_block_action_id="scimitar",
                target_id=state.pc_actor_id,
            ),
            NPC,
        )
        send(
            sm,
            sid,
            package,
            "attack.pc",
            "combat.intent",
            CombatIntentPayload(intent_type="attack", weapon_id="rapier", target_id=NPC),
        )
        send(
            sm,
            sid,
            package,
            "combat.close",
            "combat.close",
            CombatClosePayload(kind="combat.close"),
        )
        sm.save_session(sid, package, context=CONTEXT)
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--directory",
            str(args.directory),
            "--resume",
            sid,
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
