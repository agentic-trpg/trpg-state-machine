"""Create a real Schema 7 combat using archived SM-7 code, not hand-built SQL."""

import json
import sys
from pathlib import Path

import yaml

source, database = map(Path, sys.argv[1:])
sys.path.insert(0, str(source / "tests"))
sys.path.insert(0, str(source / "src"))
from dnd5e_engine.evaluation_contracts import CombatIntentPayload  # noqa: E402
from dnd_support import create, initialize, loader, make_package  # noqa: E402

from trpg_state_machine import StateMachine, TrustedContext, TypedCommand  # noqa: E402

assets = loader()
base = yaml.safe_load((source / "tests/fixtures/lantern_station.yaml").read_text("utf-8"))
package = make_package(base, assets)
context = TrustedContext("authenticated:alice", "player.alice", ("npc.caretaker",))
with StateMachine(database, dnd_loader=assets) as sm:
    sid = create(sm, package, context)
    assert initialize(sm, sid, package, context).status == "committed"
    before = sm.read_session(sid, package, context=context)
    command = TypedCommand(
        sid,
        "old.real.attack",
        before.pc_actor_id,
        "combat.intent",
        CombatIntentPayload(
            intent_type="attack", weapon_id="item.lantern_blade", target_id="npc.caretaker"
        ),
        before.world_version,
    )
    assert sm.submit_command(command, package, context=context).status == "committed"
    assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 7
    print(
        json.dumps(
            {
                "session_id": sid,
                "snapshot": json.loads(
                    sm.read_combat_snapshot(sid, package, context=context).model_dump_json()
                ),
                "rng": json.loads(
                    sm.read_rng_context(sid, package, context=context).model_dump_json()
                ),
            }
        )
    )
