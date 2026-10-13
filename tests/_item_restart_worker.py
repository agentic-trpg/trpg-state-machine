"""New-process restore through public APIs; no templates applied or SQL writes."""

import json
import sys

from item_support import item_loader

from trpg_state_machine import StateMachine, TrustedContext, load_adventure_package

database, document, sid, pc = sys.argv[1:]
package = load_adventure_package(document).package
context = TrustedContext("authenticated:alice", "player.alice", ("npc.caretaker", "npc.attendant"))
with StateMachine(database, dnd_loader=item_loader()) as sm:
    sm.resume_session(sid, package, context=context)
    print(
        json.dumps(
            [
                e.instance_id
                for e in sm.read_carried_items(sid, pc, package, context=context).entries
            ]
        )
    )
