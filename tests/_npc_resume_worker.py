"""Fresh process verifies the same SQLite authority and complete NPC Meta."""

import json
import sys
from dataclasses import asdict

from trpg_state_machine import StateMachine, TrustedContext, load_adventure_package
from trpg_state_machine.perception import NPCContext

database, source, sid = sys.argv[1:]
package = load_adventure_package(source).package
with StateMachine(database) as sm:
    snapshot = sm.resume_session(
        sid, package, context=TrustedContext("authenticated:alice", "player.alice")
    )
    views = [
        asdict(sm.get_npc_view(sid, npc, package, context=NPCContext("agent:" + npc, sid, npc)))
        for npc in ("npc.caretaker", "npc.attendant")
    ]
    print(json.dumps({"snapshot": asdict(snapshot), "views": views}))
