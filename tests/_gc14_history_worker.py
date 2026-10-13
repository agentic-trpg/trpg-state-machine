"""Actual archived pre-fix runtime creates the original one-event NPC receipt."""

import json
import sys
from dataclasses import asdict
from pathlib import Path

source, database, document = sys.argv[1:]
sys.path.insert(0, str(Path(source) / "src"))

from trpg_state_machine import (  # noqa: E402
    CreateSessionRequest,
    MoveActor,
    PlayerSelection,
    StateMachine,
    TrustedContext,
    TypedCommand,
    load_adventure_package,
)

p = load_adventure_package(document).package
ctx = TrustedContext(
    "authenticated:alice", "player.alice", ("npc.caretaker", "npc.attendant", "npc.messenger")
)
with StateMachine(database) as sm:
    sid = sm.create_session(
        CreateSessionRequest(
            "create",
            p.adventure_id,
            p.package_version,
            p.package_digest,
            (PlayerSelection(ctx.player_id, p.pc_templates[0].pc_template_id),),
        ),
        p,
        context=ctx,
    ).session_id
    cmd = TypedCommand(
        sid,
        "legacy.move",
        "npc.messenger",
        "move_actor",
        MoveActor("npc.messenger", "scene.workshop"),
        0,
    )
    receipt = sm.submit_command(cmd, p, context=ctx)
    assert receipt.status == "committed"
    print(
        json.dumps(
            {
                "sid": sid,
                "command": asdict(cmd),
                "receipt": asdict(receipt),
                "types": [
                    e.event_type
                    for e in sm.read_events(sid, p, context=ctx)
                    if e.event_seq in receipt.event_seqs
                ],
            }
        )
    )
