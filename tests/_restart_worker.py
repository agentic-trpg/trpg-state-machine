"""Executed in separate Python processes by the real-database integration test."""

import json
import os
import sys
from dataclasses import asdict

from trpg_state_machine import (
    CreateSessionRequest,
    PlayerSelection,
    StateMachine,
    TrustedContext,
    load_adventure_package,
)
from trpg_state_machine._operations import (
    EnterScene,
    MoveActor,
    ObjectState,
    SetObjectState,
    SetSceneFlag,
)


def run():
    mode, database, package_path = sys.argv[1:4]
    package = load_adventure_package(package_path).package
    assert package is not None
    context = TrustedContext("authenticated:integration", "player.integration")
    request = CreateSessionRequest(
        "cmd.integration.create",
        package.adventure_id,
        package.package_version,
        package.package_digest,
        (PlayerSelection(context.player_id, "pc_template.visitor"),),
    )
    with StateMachine(database) as sm:
        created = sm.create_session(request, package, context=context)
        sid = created.session_id
        if mode in ("prepare", "crash"):
            version = 0
            for op in (
                EnterScene("scene.workshop"),
                SetObjectState("scene.workshop", "object.workshop_door", ObjectState.OPEN),
                SetSceneFlag("scene.workshop", "flag.workshop_checked", True),
                MoveActor("npc.caretaker", "scene.workshop"),
                EnterScene("scene.platform"),
                EnterScene("scene.workshop"),
            ):
                state = sm._apply(sid, package, op, expected_world_version=version, context=context)
                assert state.world_version == version + 1
                version = state.world_version
            state = sm.save_session(sid, package, context=context)
        else:
            state = sm.resume_session(sid, package, context=context)
        print(
            json.dumps({"creation": asdict(created), "state": asdict(state)}, sort_keys=True),
            flush=True,
        )
        if mode == "crash":
            # Simulate process loss after commit, without connection close/checkpoint.
            os._exit(0)


if __name__ == "__main__":
    run()
