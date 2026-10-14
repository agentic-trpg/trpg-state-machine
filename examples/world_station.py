"""Original public-command scenario, interrupted and continued in a fresh process.

Run: python examples/world_station.py
Player paths and explicitly authorized GM placement are reported separately.
Object damage is deliberately absent; see internal synthetic durability tests.
"""

import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from trpg_state_machine import (
    ActivateMechanism,
    CreateSessionRequest,
    LockObject,
    ObjectState,
    PickupItem,
    PlayerSelection,
    PutItem,
    RepositionObject,
    SetLightEnabled,
    SetObjectState,
    StartNavigation,
    StateMachine,
    StepActor,
    TakeItem,
    TrustedContext,
    TypedCommand,
    UnlockDoor,
    UnlockObject,
    load_adventure_package,
    pump_navigation,
    world_command_from_data,
)

A, B = "scene.platform", "scene.workshop"
KEY, CASE_KEY, BEAD = "object.amulet_a", "object.amulet_b", "object.amulet_c"
CASE, POUCH, LEVER = "object.case", "object.pouch", "object.lever"
DOOR, BARRIER = "object.platform_gate", "object.barrier"
FIXTURE = Path(__file__).with_name("fixtures") / "world_station.yaml"
CONTEXT = TrustedContext("authenticated:world_demo", "player.world_demo")
KINDS = {
    ActivateMechanism: "world.activate_mechanism",
    LockObject: "world.lock_object",
    UnlockObject: "world.unlock_object",
    PickupItem: "world.pickup_item",
    PutItem: "world.put_item",
    TakeItem: "world.take_item",
    RepositionObject: "world.reposition_object",
    SetLightEnabled: "world.set_light_enabled",
    SetObjectState: "set_object_state",
    StartNavigation: "activity.start_navigation",
    StepActor: "world.step_actor",
    UnlockDoor: "world.unlock_door",
}


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
    result = load_adventure_package(FIXTURE)
    if result.status != "valid":
        raise RuntimeError(result.errors)
    return result.package


def issue(sm, sid, p, operation, key, *, context=CONTEXT, expected_error=None):
    state = sm.read_session(sid, p, context=context)
    command = TypedCommand(
        sid, key, state.pc_actor_id, KINDS[type(operation)], operation, state.world_version
    )
    receipt = sm.submit_command(command, p, context=context)
    if expected_error is None:
        assert receipt.status == "committed", receipt
    else:
        assert receipt.error_code == expected_error, receipt
    return command, receipt


def navigate(sm, sid, p, scene, cell, key):
    aid = "activity." + key
    issue(sm, sid, p, StartNavigation(aid, scene, cell), key)
    actor = sm.read_session(sid, p, context=CONTEXT).pc_actor_id
    result = pump_navigation(
        sm.submit_command,
        sm.read_navigation_activity,
        sm.get_command_receipt,
        sid,
        actor,
        aid,
        p,
        context=CONTEXT,
        request_id=key + ".pump",
        budget=64,
    )
    assert result.activity.status == "completed", result


def first_phase(db):
    p = package()
    with StateMachine(db, dnd_loader=loader()) as sm:
        created = sm.create_session(
            CreateSessionRequest(
                "world.create",
                p.adventure_id,
                p.package_version,
                p.package_digest,
                (PlayerSelection(CONTEXT.player_id, p.pc_templates[0].pc_template_id),),
            ),
            p,
            context=CONTEXT,
        )
        sid = created.session_id
        navigate(sm, sid, p, A, "1,0", "keys.near")
        for iid in (KEY, CASE_KEY, BEAD):
            issue(sm, sid, p, PickupItem(iid, A, "2,0"), "pick." + iid)
        navigate(sm, sid, p, A, "0,0", "keys.return")
        for iid in (CASE, POUCH):
            issue(sm, sid, p, PickupItem(iid, A, "1,0"), "pick." + iid)
        issue(sm, sid, p, SetLightEnabled(A, BEAD, "0,0", True), "bead.on")
        issue(sm, sid, p, PutItem(A, BEAD, POUCH, "0,0"), "bead.inside")
        actor = sm.read_session(sid, p, context=CONTEXT).pc_actor_id
        lit = sm.get_illumination(sid, actor, "1,0", p, context=CONTEXT)
        issue(sm, sid, p, SetObjectState(A, POUCH, ObjectState.CLOSED), "pouch.close")
        shaded = sm.get_illumination(sid, actor, "1,0", p, context=CONTEXT)
        assert (lit["illumination"], shaded["illumination"]) == ("bright", "dim")
        issue(sm, sid, p, SetObjectState(A, POUCH, ObjectState.OPEN), "pouch.open")
        issue(sm, sid, p, PutItem(A, KEY, POUCH, "0,0"), "key.inside")
        issue(sm, sid, p, PutItem(A, POUCH, CASE, "0,0"), "pouch.inside")
        saved_command, saved_receipt = issue(
            sm, sid, p, SetObjectState(A, CASE, ObjectState.CLOSED), "case.close"
        )
        issue(sm, sid, p, LockObject(A, CASE, "lock.case", CASE_KEY, "0,0"), "case.lock")
        issue(
            sm,
            sid,
            p,
            UnlockDoor(A, DOOR, KEY, "0,0"),
            "blocked.key",
            expected_error="credential_unavailable",
        )
        issue(
            sm,
            sid,
            p,
            ActivateMechanism(A, LEVER, "0,0"),
            "blocked.lever",
            expected_error="door_locked",
        )
        inventory = sm.read_carried_items(sid, actor, p, context=CONTEXT)
        assert KEY not in {e.instance_id for e in inventory.entries}
        sm.save_session(sid, p, context=CONTEXT)
        raw_command = asdict(saved_command)
        raw_command.pop("check_world_effects")
        return {
            "session_id": sid,
            "package_digest": p.package_digest,
            "saved_command": raw_command,
            "saved_receipt": asdict(saved_receipt),
            "before_restart_world_version": sm.read_session(sid, p, context=CONTEXT).world_version,
            "shading": {"lit": lit["illumination"], "closed": shaded["illumination"]},
        }


def continuation(db, prior):
    p, sid = package(), prior["session_id"]
    with StateMachine(db, dnd_loader=loader()) as sm:
        state = sm.resume_session(sid, p, context=CONTEXT)
        assert state.world_version == prior["before_restart_world_version"]
        replay = sm.submit_command(
            world_command_from_data(prior["saved_command"]), p, context=CONTEXT
        )
        assert json.loads(json.dumps(asdict(replay))) == prior["saved_receipt"]
        issue(sm, sid, p, UnlockObject(A, CASE, "lock.case", CASE_KEY, "0,0"), "case.unlock")
        issue(sm, sid, p, SetObjectState(A, CASE, ObjectState.OPEN), "case.open")
        issue(sm, sid, p, TakeItem(A, KEY, POUCH, "0,0"), "key.take")
        issue(sm, sid, p, UnlockDoor(A, DOOR, KEY, "0,0"), "door.unlock")
        issue(sm, sid, p, PutItem(A, KEY, POUCH, "0,0"), "key.return")
        command, receipt = issue(sm, sid, p, ActivateMechanism(A, LEVER, "0,0"), "lever.activate")
        assert sm.submit_command(command, p, context=CONTEXT) == receipt
        issue(
            sm,
            sid,
            p,
            ActivateMechanism(A, LEVER, "0,0"),
            "lever.again",
            expected_error="mechanism_spent",
        )
        navigate(sm, sid, p, B, "0,0", "cross.shared.door")
        issue(
            sm,
            sid,
            p,
            StepActor(B, "0,0", "1,0"),
            "blocked.obstacle",
            expected_error="movement_unavailable",
        )
        gm = replace(CONTEXT, can_place_world_objects=True)
        issue(
            sm, sid, p, RepositionObject(B, BARRIER, "1,0", "3,0"), "gm.clear.obstacle", context=gm
        )
        navigate(sm, sid, p, B, "1,0", "walk.after.clear")
        actor = state.pc_actor_id
        location = sm.get_object(sid, actor, BEAD, p, context=CONTEXT)["location"]
        assert location["scene_id"] == B and location["cell"] == "1,0"
        assert location["ancestry"] == (POUCH, CASE)
        final = sm.save_session(sid, p, context=CONTEXT)
        return {
            "real_player_paths": [
                "actual instance pickup",
                "nested take/put and carry",
                "closed-container key denial",
                "ordinary key locks",
                "shared door traversal",
                "carried and fixed lights",
                "one-shot lever with locked-target denial",
                "navigation after obstacle clears",
            ],
            "gm_management_paths": ["RepositionObject with trusted can_place_world_objects grant"],
            "internal_synthetic_tests": "Separate synthetic tests; attacks unsupported",
            "new_process_restore": True,
            "receipt_replay": True,
            "shading": prior["shading"],
            "stable_object_ids": [KEY, CASE_KEY, BEAD, CASE, POUCH, DOOR, LEVER, BARRIER],
            "final_scene": final.current_scene_id,
            "final_cell": location["cell"],
            "final_world_version": final.world_version,
            "final_event_seq": final.event_seq,
            "local_illumination": sm.get_illumination(sid, actor, "2,0", p, context=CONTEXT)[
                "illumination"
            ],
        }


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--continue":
        prior = json.loads(Path(sys.argv[3]).read_text("utf-8"))
        print(json.dumps(continuation(Path(sys.argv[2]), prior)))
        return
    with TemporaryDirectory(prefix="world-station-") as directory:
        db = Path(directory) / "world.db"
        prior = first_phase(db)
        record = Path(directory) / "prior.json"
        record.write_text(json.dumps(prior), "utf-8")
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--continue", str(db), str(record)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        print(result.stdout.strip())


if __name__ == "__main__":
    main()
