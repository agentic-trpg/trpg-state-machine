"""Create a genuine Schema-15 save using the archived SM-22 runtime."""

import sys
from dataclasses import replace

sys.path.insert(0, sys.argv[1])

from trpg_state_machine import (  # noqa: E402
    CreateSessionRequest,
    PlayerSelection,
    SetSceneFlag,
    StateMachine,
    TrustedContext,
    load_adventure_package,
)
from trpg_state_machine.npc_results import NPCEvaluationResult  # noqa: E402
from trpg_state_machine.npc_state import SourceReference  # noqa: E402
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
    ids = tuple(o.observation_id for o in sm.read_observations(sid, package, context=npc))
    task = sm.create_npc_evaluation_task(
        sid,
        package,
        context=npc,
        request_id="old-task",
        mode="interactive",
        reason="legacy",
        expected_npc_state_version=1,
        source_observation_ids=ids,
    )
    view = sm.get_npc_view(sid, "npc.caretaker", package, context=npc)
    memory = replace(
        view.memories[0], version=2, sources=(SourceReference("observation", ids[0], None),)
    )
    sm.submit_npc_evaluation_result(
        task.evaluation_id, NPCEvaluationResult(memory_updates=(memory,)), package, context=npc
    )
    assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 15
    print(sid)
