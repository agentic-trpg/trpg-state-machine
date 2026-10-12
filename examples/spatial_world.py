"""Structured GC-1 world loop, real SQLite, no LLM or success-path SQL writes.

uv run --locked python examples/spatial_world.py --directory .test-tmp/spatial-new
Use another new directory and --object stool for the same primitive with a stool.
"""

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from trpg_state_machine import (
    CreateSessionRequest,
    PlayerSelection,
    StateMachine,
    TrustedContext,
    load_adventure_package,
    world_command_from_data,
)

A, B, NPC = "scene.platform", "scene.workshop", "npc.attendant"
PASSAGE = "passage.station"
CONTEXT = TrustedContext("example:trusted-host", "player.example", (NPC,))


def load(path):
    result = load_adventure_package(path)
    if result.status != "valid":
        raise RuntimeError(result.errors)
    return result.package


def state(sm, sid, package):
    snapshot = sm.read_session(sid, package, context=CONTEXT)
    return {
        "world_version": snapshot.world_version,
        "actors": {key: asdict(value) for key, value in snapshot.actors.items()},
        "object_cells": snapshot.scenes[A].object_cells,
        "object_states": {scene_id: scene.objects for scene_id, scene in snapshot.scenes.items()},
        "rng": asdict(snapshot.rng),
    }


def run(directory, object_name):
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / "package.json"
    shutil.copyfile(Path(__file__).with_name("spatial_station.json"), source)
    package = load(source)
    database = directory / "world.db"
    trace = []
    with StateMachine(database) as sm:
        sid = sm.create_session(
            CreateSessionRequest(
                "create",
                package.adventure_id,
                package.package_version,
                package.package_digest,
                (PlayerSelection(CONTEXT.player_id, package.pc_templates[0].pc_template_id),),
            ),
            package,
            context=CONTEXT,
        ).session_id
        pc = sm.read_session(sid, package, context=CONTEXT).pc_actor_id
        rng = state(sm, sid, package)["rng"]

        def execute(kind, payload, *, actor=pc, expected="committed"):
            snapshot = sm.read_session(sid, package, context=CONTEXT)
            data = {
                "schema_version": "sm-command/1",
                "session_id": sid,
                "command_id": f"step.{len(trace)}",
                "actor_id": actor,
                "command_type": kind,
                "payload": payload,
                "expected_world_version": snapshot.world_version,
            }
            command = world_command_from_data(data)
            receipt = sm.submit_command(command, package, context=CONTEXT)
            assert receipt.status == expected, receipt
            if expected == "rejected":
                assert receipt.error_code == "passage_unavailable", receipt
            assert sm.submit_command(command, package, context=CONTEXT) == receipt
            trace.append({"input": data, "receipt": asdict(receipt)})

        def step(scene, old, new, actor=pc):
            execute(
                "world.step_actor",
                {
                    "schema_version": "actor-step/1",
                    "scene_id": scene,
                    "expected_cell": old,
                    "to_cell": new,
                },
                actor=actor,
            )

        def place(old, new):
            execute(
                "world.place_object",
                {
                    "schema_version": "object-placement/1",
                    "scene_id": A,
                    "object_id": "object." + object_name,
                    "expected_cell": old,
                    "to_cell": new,
                },
            )

        def door(scene, value, actor=pc):
            execute(
                "set_object_state",
                {"scene_id": scene, "object_id": "object.platform_gate", "state": value},
                actor=actor,
            )

        step(A, "2,1", "2,2")
        step(A, "2,2", "2,1")
        step(B, "1,2", "1,1", NPC)
        start = "3,2" if object_name == "crate" else "3,0"
        place(start, "3,1")
        execute("enter_scene", {"scene_id": B}, expected="rejected")
        execute("move_actor", {"actor_id": NPC, "to_scene_id": A}, actor=NPC, expected="rejected")
        place("3,1", start)
        step(B, "1,1", "1,2", NPC)
        execute(
            "world.traverse_passage",
            {
                "schema_version": "passage-traversal/1",
                "passage_id": PASSAGE,
                "from_scene_id": A,
                "expected_cell": "2,1",
            },
        )
        door(B, "closed")
        door(B, "open")
        step(B, "1,1", "2,1")
        step(B, "1,2", "1,1", NPC)
        execute("move_actor", {"actor_id": NPC, "to_scene_id": A}, actor=NPC)
        door(A, "closed", NPC)
        door(A, "open", NPC)
        saved = state(sm, sid, package)
        assert saved["rng"] == rng
        sm.save_session(sid, package, context=CONTEXT)
    worker = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--resume", str(directory.resolve()), sid],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(worker.stdout) == saved
    output = {"session_id": sid, "trace": trace, "fresh_process_state": saved}
    (directory / "trace.json").write_text(json.dumps(output, indent=2) + "\n", "utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--object", choices=("crate", "stool"), default="crate")
    parser.add_argument("--resume", nargs=2, metavar=("DIRECTORY", "SESSION_ID"))
    args = parser.parse_args()
    if args.resume:
        directory = Path(args.resume[0])
        package = load(directory / "package.json")
        with StateMachine(directory / "world.db") as sm:
            sm.resume_session(args.resume[1], package, context=CONTEXT)
            print(json.dumps(state(sm, args.resume[1], package)))
    elif args.directory:
        run(args.directory, args.object)
    else:
        parser.error("--directory is required for a new run")
