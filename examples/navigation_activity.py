"""One destination, bounded program execution and fresh-process continuation.

Use a new --directory. --destination platform or --actor pc reuse the same driver.
This original example contains ordinary doors/obstacles, no equipment acquisition.
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
    StartNavigation,
    StateMachine,
    TrustedContext,
    TypedCommand,
    load_adventure_package,
    pump_navigation,
)
from trpg_state_machine.npc_results import NPCEvaluationResult, NPCNavigationIntent
from trpg_state_machine.perception import NPCContext

NPC = "npc.caretaker"
CONTEXT = TrustedContext("example:trusted-host", "player.example", (NPC,))
DESTINATIONS = {"workshop": ("scene.workshop", "4,2"), "platform": ("scene.platform", "0,4")}
AID = "activity.example"


def drive(sm, sid, actor, package, request_id, budget):
    return pump_navigation(
        sm.submit_command,
        sm.read_navigation_activity,
        sm.get_command_receipt,
        sid,
        actor,
        AID,
        package,
        context=CONTEXT,
        request_id=request_id,
        budget=budget,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--actor", choices=("pc", "npc"), default="npc")
    parser.add_argument("--destination", choices=tuple(DESTINATIONS), default="workshop")
    parser.add_argument("--resume")
    args = parser.parse_args()
    document, database = args.directory / "package.json", args.directory / "world.db"
    metadata = args.directory / "run.json"
    if not args.resume:
        args.directory.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(Path(__file__).with_name("navigation_station.json"), document)
    loaded = load_adventure_package(document)
    if loaded.status != "valid":
        raise RuntimeError(loaded.errors)
    package = loaded.package
    with StateMachine(database) as sm:
        if args.resume:
            saved = json.loads(metadata.read_text("utf-8"))
            assert saved["session_id"] == args.resume
            sid, actor = args.resume, saved["actor_id"]
            # Reconcile the same two committed slots first: no new movement.
            before = sm.read_navigation_activity(sid, actor, AID, package, context=CONTEXT)
            resumed = drive(sm, sid, actor, package, "first", 2).activity
            assert resumed == before
            if before.status == "active" and before.cursor == saved["halfway"]["cursor"]:
                assert json.loads(json.dumps(asdict(resumed))) == saved["halfway"]
            done = drive(sm, sid, actor, package, "continue", 32)
            position = sm.read_actor_position(sid, actor, package, context=CONTEXT)
            assert done.activity.status == "completed"
            assert [position.scene_id, position.cell] == saved["target"]
            assert asdict(sm.read_session(sid, package, context=CONTEXT).rng) == saved["rng"]
            trace = {
                **saved,
                "completed": asdict(done.activity),
                "position": asdict(position),
                "continuation_receipts": [asdict(r) for r in done.receipts],
                "fresh_process": True,
            }
            (args.directory / "trace.json").write_text(json.dumps(trace, indent=2), "utf-8")
            print(
                json.dumps(
                    {
                        "actor_id": actor,
                        "target": saved["target"],
                        "status": done.activity.status,
                        "cursor": done.activity.cursor,
                        "fresh_process": True,
                        "rng_unchanged": True,
                    }
                )
            )
            return
        sid = sm.create_session(
            CreateSessionRequest(
                "create.example",
                package.adventure_id,
                package.package_version,
                package.package_digest,
                (PlayerSelection(CONTEXT.player_id, package.pc_templates[0].pc_template_id),),
            ),
            package,
            context=CONTEXT,
        ).session_id
        state = sm.read_session(sid, package, context=CONTEXT)
        actor = state.pc_actor_id if args.actor == "pc" else NPC
        target = DESTINATIONS[args.destination]
        task_id = None
        if args.actor == "npc":
            sm.grant_npc_access(sid, NPC, "example:npc-controller", package, context=CONTEXT)
            npc_context = NPCContext("example:npc-controller", sid, NPC)
            view = sm.get_npc_view(sid, NPC, package, context=npc_context)
            task = sm.create_npc_evaluation_task(
                sid,
                package,
                context=npc_context,
                request_id="example.navigation",
                mode="interactive",
                reason="player_input",
                expected_npc_state_version=view.npc_state_version,
                source_observation_ids=(),
                player_input="Go to the approved destination.",
            )
            task_id = task.evaluation_id
            result = NPCEvaluationResult(
                action_intents=(NPCNavigationIntent(AID, *target, state.world_version),)
            )
            sm.submit_npc_evaluation_result(task_id, result, package, context=npc_context)
            receipt = sm.dispatch_npc_output(task_id, 0, package, context=CONTEXT).command_receipt
        else:
            receipt = sm.submit_command(
                TypedCommand(
                    sid,
                    "start.example",
                    actor,
                    "activity.start_navigation",
                    StartNavigation(AID, *target),
                    state.world_version,
                ),
                package,
                context=CONTEXT,
            )
        assert receipt.status == "committed"
        halfway = drive(sm, sid, actor, package, "first", 2)
        assert halfway.activity.status in ("active", "completed")
        saved = {
            "session_id": sid,
            "actor_id": actor,
            "target": target,
            "task_id": task_id,
            "rng": asdict(state.rng),
            "halfway": asdict(halfway.activity),
            "start_receipt": asdict(receipt),
            "halfway_receipts": [asdict(r) for r in halfway.receipts],
        }
        metadata.write_text(json.dumps(saved, indent=2), "utf-8")
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
