"""Actual process boundary: dispatch, injected exit after Command commit, or resume."""

import json
import os
import sys
from dataclasses import asdict

from trpg_state_machine import StateMachine, TrustedContext, load_adventure_package
from trpg_state_machine.perception import NPCContext

mode, database, source, sid, eid, ordinal = sys.argv[1:]
package = load_adventure_package(source).package
owner = TrustedContext("authenticated:alice", "player.alice", ("npc.caretaker", "npc.attendant"))
npc = NPCContext("agent:npc.caretaker", sid, "npc.caretaker")
with StateMachine(database) as sm:
    if mode == "crash_after_command":
        sm._conn.create_function("crash", 0, lambda: os._exit(23))
        sm._conn.execute(
            "CREATE TEMP TRIGGER crash_after_command BEFORE UPDATE ON npc_output_handoffs "
            "BEGIN SELECT crash(); END"
        )
    if mode != "resume":
        sm.dispatch_npc_output(eid, int(ordinal), package, context=owner)
    print(
        json.dumps(
            {
                "snapshot": asdict(sm.resume_session(sid, package, context=owner)),
                "receipt": asdict(sm.get_npc_evaluation_receipt(eid, package, context=npc)),
                "input": asdict(sm.get_npc_evaluation_task(eid, package, context=npc)),
                "outputs": [asdict(o) for o in sm.get_npc_output_status(eid, package, context=npc)],
                "cursor": sm.npc_completed_cursor(sid, package, context=npc),
                "mode": sm.get_npc_mode(sid, package, context=npc),
            }
        )
    )
