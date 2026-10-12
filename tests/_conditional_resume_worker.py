"""Fresh process: replay the original conditional check without another evaluation."""

import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

from modern_support import modern_loader
from test_conditional_world_effects import conditional

from trpg_state_machine import StateMachine, TrustedContext
from trpg_state_machine.package import _validate

package = _validate(json.loads(Path(sys.argv[2]).read_text("utf-8")))
raw = json.loads(sys.argv[4])
raw["controlled_npc_ids"] = tuple(raw["controlled_npc_ids"])
context = TrustedContext(**raw)
with StateMachine(sys.argv[1], dnd_loader=modern_loader()) as sm:
    sid = sys.argv[3]
    command = replace(
        conditional(sm, sid, package, context), expected_world_version=int(sys.argv[5])
    )

    async def forbidden(request):
        raise AssertionError("Replay must not evaluate")

    sm._rules._evaluate_dnd = forbidden
    receipt = sm.submit_command(command, package, context=context)
    state = sm.resume_session(sid, package, context=context)
    print(
        json.dumps(
            dict(
                receipt=asdict(receipt),
                world_version=state.world_version,
                rng_version=state.rng.rng_version,
                objects=state.scenes["scene.platform"].objects,
            )
        )
    )
