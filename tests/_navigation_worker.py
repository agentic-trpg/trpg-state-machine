"""Fresh-interpreter navigation read/pump over the original Package and SQLite."""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trpg_state_machine import StateMachine, TrustedContext, load_adventure_package, pump_navigation  # noqa: E402

database, document, sid, actor, aid, request_id, budget = sys.argv[1:]
loaded = load_adventure_package(document)
assert loaded.status == "valid", loaded.errors
package = loaded.package
context = TrustedContext("authenticated:alice", "player.alice", ("npc.caretaker", "npc.attendant"))
with StateMachine(database) as sm:
    if budget != "0":
        pump_navigation(
            sm.submit_command,
            sm.read_navigation_activity,
            sm.get_command_receipt,
            sid,
            actor,
            aid,
            package,
            context=context,
            request_id=request_id,
            budget=int(budget),
        )
    print(
        json.dumps(
            {
                "activity": asdict(
                    sm.read_navigation_activity(sid, actor, aid, package, context=context)
                ),
                "position": asdict(sm.read_actor_position(sid, actor, package, context=context)),
                "rng": asdict(sm.read_session(sid, package, context=context).rng),
            }
        )
    )
