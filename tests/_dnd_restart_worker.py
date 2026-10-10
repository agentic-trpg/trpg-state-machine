"""Fresh-process audit/replay; never claim active combat can safely continue."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dnd5e_engine.evaluation_contracts import CombatIntentPayload  # noqa: E402
from dnd_support import loader  # noqa: E402

from trpg_state_machine import (  # noqa: E402
    StateMachine,
    StateMachineError,
    TrustedContext,
    TypedCommand,
    load_adventure_package,
)
from trpg_state_machine.perception import NPCContext  # noqa: E402

database, package_path, command_path = sys.argv[1:]
package = load_adventure_package(package_path).package
data = json.loads(Path(command_path).read_text("utf-8"))
data["payload"] = CombatIntentPayload.model_validate_json(json.dumps(data["payload"]))
command = TypedCommand(**data)
context = TrustedContext("authenticated:alice", "player.alice", ("npc.caretaker", "npc.attendant"))
with StateMachine(database, dnd_loader=loader()) as sm:
    sid = command.session_id
    snapshot = sm.read_combat_snapshot(sid, package, context=context)
    rng = sm.read_rng_context(sid, package, context=context)
    receipt = sm.submit_command(command, package, context=context)
    failures = []
    for method in (sm.save_session, sm.resume_session):
        try:
            method(sid, package, context=context)
        except StateMachineError as exc:
            failures.append(exc.code)
    current = TypedCommand(
        sid,
        "attack.after.restart",
        snapshot.character_states[0].entity_id,
        "combat.intent",
        CombatIntentPayload(
            intent_type="attack", weapon_id="item.lantern_blade", target_id="npc.caretaker"
        ),
        snapshot.world_version,
    )
    try:
        sm.submit_command(current, package, context=context)
    except StateMachineError as exc:
        failures.append(exc.code)
    observations = sm.read_observations(
        sid, package, context=NPCContext("agent:lantern", sid, "npc.caretaker")
    )
    print(
        json.dumps(
            {
                "snapshot": json.loads(snapshot.model_dump_json()),
                "rng": json.loads(rng.model_dump_json()),
                "receipt": receipt.command_id,
                "failures": failures,
                "observations": [[s.content for s in o.perceived_signals] for o in observations],
                "new_receipt": sm.get_command_receipt(
                    sid, current.command_id, package, context=context
                )
                is not None,
            }
        )
    )
