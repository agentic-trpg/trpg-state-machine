"""Fresh-process cross-phase resume, exploration and a real subsequent check."""

import json
import sys
from pathlib import Path

from modern_support import modern_loader
from test_real_checks import check_command

from trpg_state_machine import EnterScene, StateMachine, TrustedContext, TypedCommand
from trpg_state_machine.package import _validate

package = _validate(json.loads(Path(sys.argv[2]).read_text("utf-8")))
raw = json.loads(sys.argv[4])
raw["controlled_npc_ids"] = tuple(raw["controlled_npc_ids"])
context = TrustedContext(**raw)
with StateMachine(sys.argv[1], dnd_loader=modern_loader()) as sm:
    saved = sm.resume_session(sys.argv[3], package, context=context)
    for index, scene in enumerate(("scene.workshop", "scene.platform")):
        state = sm.read_session(saved.session_id, package, context=context)
        receipt = sm.submit_command(
            TypedCommand(
                saved.session_id,
                f"postcombat.explore.{index}",
                saved.pc_actor_id,
                "enter_scene",
                EnterScene(scene),
                state.world_version,
            ),
            package,
            context=context,
        )
        assert receipt.status == "committed"
    command = check_command(sm, saved.session_id, package, context, "check.after_combat")
    receipt = sm.submit_command(command, package, context=context)
    assert receipt.status == "committed"
    after = sm.read_session(saved.session_id, package, context=context)
    print(
        json.dumps(
            dict(
                before_rng=saved.rng.rng_version,
                after_rng=after.rng.rng_version,
                before_world=saved.world_version,
                xp=after.character.xp,
                hp=after.character.current_hp,
            )
        )
    )
