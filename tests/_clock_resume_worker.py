"""Fresh process reads original persisted clock, NPC runtime and receipt."""

import json
import sys
from dataclasses import asdict, replace

from test_game_time import tick_command

from trpg_state_machine import StateMachine, TrustedContext, load_adventure_package
from trpg_state_machine.perception import NPCContext

package = load_adventure_package(sys.argv[2]).package
raw = json.loads(sys.argv[4])
raw["controlled_npc_ids"] = tuple(raw["controlled_npc_ids"])
context = TrustedContext(**raw)
sid = sys.argv[3]
with StateMachine(sys.argv[1]) as sm:
    command = replace(
        tick_command(sm, sid, package, context, 10, expected_tick=0),
        expected_world_version=int(sys.argv[5]),
    )
    receipt = sm.submit_command(command, package, context=context)
    saved = sm.resume_session(sid, package, context=context)
    npc = sm.get_npc_view(
        sid,
        "npc.caretaker",
        package,
        context=NPCContext("agent:npc.caretaker", sid, "npc.caretaker"),
    )
    print(
        json.dumps(
            dict(
                receipt=asdict(receipt),
                world_version=saved.world_version,
                game_tick=saved.world.game_tick,
                npc=asdict(npc),
            )
        )
    )
