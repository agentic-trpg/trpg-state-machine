"""Two explicit high-level decisions: navigate, then transfer; no LLM planning.

Run: .venv/Scripts/python.exe examples/item_transfers.py
Uses a fresh real SQLite database and the installed pinned Engine/SRD.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from trpg_state_machine import (
    CreateSessionRequest,
    DropItem,
    PickupItem,
    PlayerSelection,
    StartNavigation,
    StateMachine,
    TrustedContext,
    TypedCommand,
    load_adventure_package,
    pump_navigation,
)
from trpg_state_machine.commands import OPERATIONS


def demo_loader():
    bundled = BundledAssetLoader()
    return MemoryAssetLoader(
        items=[
            bundled.get_item(slug)
            for slug in (
                "rapier",
                "longsword",
                "dagger",
                "greatsword",
                "rope",
                "shield",
                "leather-armor",
                "wand-of-magic-missiles",
            )
        ],
        monsters=[bundled.get_monster("bandit")],
    )


def run(database):
    loaded = load_adventure_package(Path(__file__).with_name("gc3_items.json"))
    assert loaded.status == "valid", loaded.errors
    package = loaded.package
    npc = "npc.caretaker"
    context = TrustedContext("demo:owner", "player.demo", (npc,))
    with StateMachine(database, dnd_loader=demo_loader()) as sm:
        sid = sm.create_session(
            CreateSessionRequest(
                "demo.create",
                package.adventure_id,
                package.package_version,
                package.package_digest,
                (PlayerSelection(context.player_id, "pc_template.observer"),),
            ),
            package,
            context=context,
        ).session_id
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        sm.grant_npc_access(sid, npc, "demo:npc", package, context=context)

        def submit(payload, key, actor=pc):
            version = sm.read_session(sid, package, context=context).world_version
            kind = next(k for k, v in OPERATIONS.items() if type(payload) is v)
            receipt = sm.submit_command(
                TypedCommand(sid, key, actor, kind, payload, version),
                package,
                context=context,
            )
            assert receipt.status == "committed", receipt
            return receipt

        def navigate(actor, scene, cell, key):
            aid = "activity." + key
            submit(StartNavigation(aid, scene, cell), key, actor)
            result = pump_navigation(
                sm.submit_command,
                sm.read_navigation_activity,
                sm.get_command_receipt,
                sid,
                actor,
                aid,
                package,
                context=context,
                request_id=key,
                budget=32,
            )
            assert result.activity.status == "completed", result

        navigate(pc, "scene.platform", "1,1", "approach")
        submit(PickupItem("object.dagger_a", "scene.platform", "2,1"), "pickup.dagger")
        submit(PickupItem("object.rope", "scene.platform", "1,0"), "pickup.rope")
        navigate(pc, "scene.workshop", "1,1", "carry")
        submit(DropItem("object.dagger_a", "scene.workshop", "1,1"), "drop.dagger")
        navigate(pc, "scene.workshop", "2,2", "make.room")
        navigate(npc, "scene.workshop", "0,0", "npc.approach")
        submit(PickupItem("object.dagger_a", "scene.workshop", "1,1"), "npc.pickup", npc)
        pc_items = sm.read_carried_items(sid, pc, package, context=context)
        npc_items = sm.read_carried_items(sid, npc, package, context=context)
        assert "object.rope" in {e.instance_id for e in pc_items.entries}
        assert "object.dagger_a" in {e.instance_id for e in npc_items.entries}
        print(
            json.dumps(
                {
                    "pc_instances": [e.instance_id for e in pc_items.entries],
                    "npc_instances": [e.instance_id for e in npc_items.entries],
                    "world_version": pc_items.world_version,
                    "planning": "explicit authored decisions; no LLM",
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    scratch = Path(__file__).resolve().parents[1] / ".test-tmp"
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="gc3-demo-", dir=scratch) as temporary:
        run(Path(temporary) / "items.db")
