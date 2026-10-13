"""Create an actual Schema24 save and transfer receipt using archived GC3 source."""

import json
import sys
from dataclasses import asdict
from pathlib import Path

source, db, document = sys.argv[1:]
sys.path.insert(0, str(Path(source) / "src"))

from item_support import DAGGER, A, command, create, item_loader, navigate  # noqa: E402

from trpg_state_machine import (  # noqa: E402
    ObjectState,  # noqa: E402
    PickupItem,
    StateMachine,
    TrustedContext,
)
from trpg_state_machine.npc_results import (  # noqa: E402
    NPCDoorIntent,
    NPCDropIntent,
    NPCEvaluationResult,
    NPCNavigationIntent,
    NPCPickupIntent,
    _result_json,
)
from trpg_state_machine.package import load_adventure_package  # noqa: E402

p = load_adventure_package(document).package
ctx = TrustedContext("authenticated:alice", "player.alice", ("npc.caretaker", "npc.attendant"))
with StateMachine(db, dnd_loader=item_loader()) as sm:
    sid = create(sm, p, ctx)
    navigate(sm, sid, p, ctx, (A, "1,1"), "legacy.near")
    cmd = command(sm, sid, p, ctx, PickupItem(DAGGER, A, "2,1"), "legacy.pick")
    receipt = sm.submit_command(cmd, p, context=ctx)
    assert receipt.status == "committed"
    legacy_result = NPCEvaluationResult(
        action_intents=(
            NPCDoorIntent(A, "object.platform_gate", ObjectState.CLOSED, 7),
            NPCPickupIntent(DAGGER, A, "2,1", 7),
            NPCDropIntent(DAGGER, A, "1,1", 7),
            NPCNavigationIntent("activity.legacy", A, "1,1", 7),
        )
    )
    print(
        json.dumps(
            {
                "sid": sid,
                "schema": sm._conn.execute("PRAGMA user_version").fetchone()[0],
                "command": asdict(cmd),
                "receipt": asdict(receipt),
                "npc_result_json": _result_json(legacy_result),
            }
        )
    )
