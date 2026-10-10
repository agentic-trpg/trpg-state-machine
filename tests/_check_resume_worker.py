"""Actual process restart, receipt replay and next real check."""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from modern_support import modern_loader
from test_real_checks import check_command

from trpg_state_machine import StateMachine, TrustedContext
from trpg_state_machine.package import _validate

package = _validate(json.loads(Path(sys.argv[2]).read_text("utf-8")))
raw = json.loads(sys.argv[4])
raw["controlled_npc_ids"] = tuple(raw["controlled_npc_ids"])
context = TrustedContext(**raw)
with StateMachine(sys.argv[1], dnd_loader=modern_loader()) as sm:
    saved = sm.resume_session(sys.argv[3], package, context=context)
    receipt = sm.get_command_receipt(saved.session_id, "check.real", package, context=context)
    command = check_command(sm, saved.session_id, package, context, key="check.resumed")
    assert sm.submit_command(command, package, context=context).status == "committed"
    after = sm.read_session(saved.session_id, package, context=context)
    print(
        json.dumps(
            dict(
                receipt=asdict(receipt),
                before_rng=saved.rng.rng_version,
                before_world=saved.world_version,
                after_rng=after.rng.rng_version,
            )
        )
    )
