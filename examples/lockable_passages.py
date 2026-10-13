"""Original public mechanisms using actual amulets; high-level decisions are authored.

Run: .venv/Scripts/python.exe examples/lockable_passages.py
Navigation uses the common pump. No LLM, spell effect, equipment or implicit time.
"""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from trpg_state_machine import (
    CancelNavigation,
    CreateSessionRequest,
    LockDoor,
    ObjectState,
    PickupItem,
    PlayerSelection,
    SetObjectState,
    StartNavigation,
    StateMachine,
    TrustedContext,
    TypedCommand,
    UnlockDoor,
    load_adventure_package,
    pump_navigation,
)
from trpg_state_machine.commands import OPERATIONS

A, B = "scene.platform", "scene.workshop"
DOOR, CRED = "object.platform_gate", "object.amulet_a"
CONTEXT = TrustedContext("demo:owner", "player.demo", ("npc.caretaker",))


def loader():
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
                "amulet",
            )
        ],
        monsters=[bundled.get_monster("bandit")],
    )


def package():
    loaded = load_adventure_package(Path(__file__).with_name("gc4_locks.json"))
    assert loaded.status == "valid", loaded.errors
    return loaded.package


def current(sm, sid, p):
    session = sm.read_session(sid, p, context=CONTEXT)
    door = next(
        d
        for d in sm.get_spatial_view(sid, session.pc_actor_id, p, context=CONTEXT).doors
        if d.object_id == DOOR
    )
    return {
        "scene": session.current_scene_id,
        "door": door.state,
        "locked": door.locked,
        "world_version": session.world_version,
        "owned_instances": [
            e.instance_id
            for e in sm.read_carried_items(sid, session.pc_actor_id, p, context=CONTEXT).entries
        ],
    }


def restore(database, sid):
    p = package()
    with StateMachine(database, dnd_loader=loader()) as sm:
        sm.resume_session(sid, p, context=CONTEXT)
        return current(sm, sid, p)


def run(database):
    p = package()
    with StateMachine(database, dnd_loader=loader()) as sm:
        sid = sm.create_session(
            CreateSessionRequest(
                "demo.create",
                p.adventure_id,
                p.package_version,
                p.package_digest,
                (PlayerSelection(CONTEXT.player_id, p.pc_templates[0].pc_template_id),),
            ),
            p,
            context=CONTEXT,
        ).session_id
        pc = sm.read_session(sid, p, context=CONTEXT).pc_actor_id

        def submit(payload, key):
            version = sm.read_session(sid, p, context=CONTEXT).world_version
            kind = next(k for k, v in OPERATIONS.items() if type(payload) is v)
            receipt = sm.submit_command(
                TypedCommand(sid, key, pc, kind, payload, version), p, context=CONTEXT
            )
            assert receipt.status == "committed", receipt
            return receipt

        def navigate(scene, cell, key, expected="completed"):
            aid = "activity." + key
            submit(StartNavigation(aid, scene, cell), key)
            result = pump_navigation(
                sm.submit_command,
                sm.read_navigation_activity,
                sm.get_command_receipt,
                sid,
                pc,
                aid,
                p,
                context=CONTEXT,
                request_id=key,
                budget=64,
            )
            assert result.activity.status == expected, result
            return result.activity

        blocked = navigate(B, "0,0", "blocked", expected="blocked")
        submit(CancelNavigation(blocked.activity_id, blocked.activity_version), "cancel")
        navigate(A, "1,0", "fetch")
        submit(PickupItem(CRED, A, "2,0"), "pickup")
        navigate(A, "0,0", "return")
        submit(UnlockDoor(A, DOOR, CRED, "0,0"), "unlock")
        door = sm.get_spatial_view(sid, pc, p, context=CONTEXT).doors[0]
        assert (door.state, door.locked) == ("closed", False)
        navigate(B, "0,0", "cross")  # Ordinary door opening and traversal are separate pump steps.
        submit(SetObjectState(B, DOOR, ObjectState.CLOSED), "close")
        submit(LockDoor(B, DOOR, CRED, "0,0"), "lock")
        before = current(sm, sid, p)
    worker = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--resume", str(database), sid],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    restored = json.loads(worker.stdout)
    assert restored == before
    report = {
        "workflow": (
            "blocked, cancelled, fetched, returned, unlocked, opened, traversed, closed, locked"
        ),
        "restored_in_new_process": restored,
        "decisions": "authored test controller; no LLM or amulet magic",
    }
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--resume":
        print(json.dumps(restore(sys.argv[2], sys.argv[3])))
    else:
        scratch = Path(__file__).resolve().parents[1] / ".test-tmp"
        scratch.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix="gc4-demo-", dir=scratch) as temporary:
            run(Path(temporary) / "locks.db")
