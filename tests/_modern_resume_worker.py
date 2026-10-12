"""Fresh interpreter: resume committed closure and perform a real world operation."""

import json
import sys
from pathlib import Path

from modern_support import modern_loader

from trpg_state_machine import EnterScene, StateMachine, TrustedContext, TypedCommand
from trpg_state_machine.package import _validate

raw = json.loads(Path(sys.argv[2]).read_text("utf-8"))
package = _validate(raw)
ctx = json.loads(sys.argv[4])
ctx["controlled_npc_ids"] = tuple(ctx["controlled_npc_ids"])
context = TrustedContext(**ctx)
with StateMachine(sys.argv[1], dnd_loader=modern_loader()) as sm:
    saved = sm.resume_session(sys.argv[3], package, context=context)
    receipt = sm.submit_command(
        TypedCommand(
            saved.session_id,
            "resume.explore",
            saved.pc_actor_id,
            "enter_scene",
            EnterScene("scene.workshop"),
            saved.world_version,
        ),
        package,
        context=context,
    )
    if saved.character.current_hp == 0:
        assert receipt.status == "rejected" and receipt.error_code == "unsupported_actor_state"
    else:
        assert receipt.status == "committed"
    print(
        json.dumps(
            dict(
                xp=saved.character.xp,
                hp=saved.character.current_hp,
                before_version=saved.world_version,
                after_version=receipt.world_version,
                phase=saved.phase,
            )
        )
    )
