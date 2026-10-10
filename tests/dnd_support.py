"""Original authored weapon, real SRD Bandit, and explicit complete state fixture."""

import json
from datetime import date
from pathlib import Path

from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader, Provenance, ReviewState
from dnd5e_srd_data.schema.common import ActivationBlock, AttackActivity, DamagePart, Range
from dnd5e_srd_data.schema.item import Weapon

from trpg_state_machine import CreateSessionRequest, InitializeCombat, PlayerSelection, TypedCommand
from trpg_state_machine.package import _validate

WEAPON = "item.lantern_blade"
NPC = "npc.caretaker"


def loader():
    return MemoryAssetLoader(
        items=[
            Weapon(
                slug=WEAPON,
                name="Lantern practice blade",
                description="Original simple test weapon.",
                weight=1,
                cost_gp=1,
                rarity="common",
                provenance=Provenance(
                    source="foundry",
                    source_url="https://example.invalid/lantern",
                    ingest_date=date(2026, 10, 10),
                    ingest_version="lantern/1",
                    srd_version=frozenset({"5.2"}),
                ),
                review=ReviewState(),
                weapon_category="simple_melee",
                damage_parts=[DamagePart(dice="1d6", damage_type="slashing")],
                range=Range(kind="melee", value=5, units="ft"),
                activities=[
                    AttackActivity(
                        id="lantern-strike", activation=ActivationBlock(type="action", value=1)
                    )
                ],
            )
        ],
        monsters=[BundledAssetLoader().get_monster("bandit")],
    )


def package_data(base, assets):
    base = json.loads(json.dumps(base))
    base["manifest"]["ruleset_binding"] = ruleset_binding(assets).model_dump(mode="json")
    base["pc_templates"] = [base["pc_templates"][0]]
    base["pc_templates"][0]["character"]["inventory"] = [WEAPON]
    base["actors"][0]["senses"] = {"visual": True, "auditory": True}
    base["rule_bindings"] = {
        "combat_foundation": json.loads(
            (Path(__file__).parent / "fixtures" / "dnd_lantern_combat.json").read_text("utf-8")
        )
    }
    return base


def make_package(base, assets):
    return _validate(package_data(base, assets))


def create(sm, package, context, command_id="create.dnd"):
    return sm.create_session(
        CreateSessionRequest(
            command_id,
            package.adventure_id,
            package.package_version,
            package.package_digest,
            (PlayerSelection(context.player_id, "pc_template.observer"),),
        ),
        package,
        context=context,
    ).session_id


def initialize(sm, sid, package, context, command_id="init.dnd"):
    state = sm.read_session(sid, package, context=context)
    command = TypedCommand(
        sid,
        command_id,
        state.pc_actor_id,
        "combat.initialize",
        InitializeCombat(),
        state.world_version,
    )
    return sm.submit_command(command, package, context=context)
