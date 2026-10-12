"""Create a genuine RF-1 Schema 21 database from archived code and original /2 data."""

import json
import sys
from dataclasses import asdict
from pathlib import Path

source, database, document = sys.argv[1:]
sys.path.insert(0, str(Path(source) / "src"))

from trpg_state_machine import (  # noqa: E402
    CreateSessionRequest,
    PlayerSelection,
    SetSceneFlag,
    StateMachine,
    TrustedContext,
    TypedCommand,
    load_adventure_package,
)

loaded = load_adventure_package(document)
assert loaded.status == "valid", loaded.errors
package = loaded.package
context = TrustedContext("authenticated:alice", "player.alice")
with StateMachine(database) as sm:
    sid = sm.create_session(
        CreateSessionRequest(
            "create",
            package.adventure_id,
            package.package_version,
            package.package_digest,
            (PlayerSelection(context.player_id, package.pc_templates[0].pc_template_id),),
        ),
        package,
        context=context,
    ).session_id
    state = sm.read_session(sid, package, context=context)
    receipt = sm.submit_command(
        TypedCommand(
            sid,
            "legacy.flag",
            state.pc_actor_id,
            "set_scene_flag",
            SetSceneFlag(state.current_scene_id, "flag.lamp_checked", True),
            state.world_version,
        ),
        package,
        context=context,
    )
    print(
        json.dumps(
            {
                "session_id": sid,
                "receipt": asdict(receipt),
                "schema": sm._conn.execute("PRAGMA user_version").fetchone()[0],
            }
        )
    )
