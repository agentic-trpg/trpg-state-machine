"""Run genuine Schema23 source to create an old save through public APIs."""

import json
import sys
from pathlib import Path

source, db, document = sys.argv[1:]
sys.path.insert(0, str(Path(source) / "src"))

from item_support import create, item_loader  # noqa: E402

from trpg_state_machine import StateMachine, TrustedContext, load_adventure_package  # noqa: E402

package = load_adventure_package(document).package
context = TrustedContext("authenticated:alice", "player.alice", ("npc.caretaker", "npc.attendant"))
with StateMachine(db, dnd_loader=item_loader()) as sm:
    sid = create(sm, package, context)
    print(json.dumps({"sid": sid, "schema": sm._conn.execute("PRAGMA user_version").fetchone()[0]}))
