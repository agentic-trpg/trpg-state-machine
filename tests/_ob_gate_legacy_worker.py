"""Real Schema-17 Gate adoption and running Task, using archived OB-2 code."""

import json
import sys

sys.path.insert(0, sys.argv[1])

from trpg_state_machine import (  # noqa: E402
    CreateSessionRequest,
    PlayerSelection,
    SetSceneFlag,
    StateMachine,
    TrustedContext,
    load_adventure_package,
)
from trpg_state_machine.npc_modes import MemoryGateJudgment  # noqa: E402
from trpg_state_machine.perception import NPCContext  # noqa: E402

package = load_adventure_package(sys.argv[3]).package
owner = TrustedContext("authenticated:alice", "player.alice")
with StateMachine(sys.argv[2]) as sm:
    request = CreateSessionRequest(
        "legacy",
        package.adventure_id,
        package.package_version,
        package.package_digest,
        (PlayerSelection(owner.player_id, "pc_template.observer"),),
    )
    sid = sm.create_session(request, package, context=owner).session_id
    sm.grant_npc_access(sid, "npc.caretaker", "agent:npc.caretaker", package, context=owner)
    sm._apply(
        sid,
        package,
        SetSceneFlag("scene.platform", "flag.lamp_checked", True),
        expected_world_version=0,
        context=owner,
    )
    for event in sm.read_events(sid, package, context=owner):
        assert sm.project_event(sid, event.event_seq, package, context=owner) == "resolved"
    npc = NPCContext("agent:npc.caretaker", sid, "npc.caretaker")
    gate = sm.get_npc_memory_gate_input(sid, package, context=npc, request_id="old-gate")
    sm.route_npc_observations(
        sid,
        package,
        context=npc,
        request_id="old-gate",
        judgments=tuple(MemoryGateJudgment(x.work_id, "deferred") for x in gate.observations),
    )
    task = sm.route_npc_observations(
        sid, package, context=npc, request_id="old-task", classification="background"
    ).task
    assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 17
    print(json.dumps([sid, task.evaluation_id]))
