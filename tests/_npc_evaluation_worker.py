"""Fresh interpreter reads the persisted task and complete authorized input."""

import json
import sys
from dataclasses import asdict

from trpg_state_machine import StateMachine, load_adventure_package
from trpg_state_machine.perception import NPCContext

database, source, evaluation_id, sid = sys.argv[1:]
package = load_adventure_package(source).package
with StateMachine(database) as sm:
    context = NPCContext("agent:npc.caretaker", sid, "npc.caretaker")
    print(json.dumps(asdict(sm.get_npc_evaluation_task(evaluation_id, package, context=context))))
