"""Create real Schema-18 saves using an archived runtime, not fabricated DDL."""

import json
import sys

from trpg_state_machine import (
    CreateSessionRequest,
    ObjectState,
    PlayerSelection,
    SetObjectState,
    StateMachine,
    TrustedContext,
    TypedCommand,
    load_adventure_package,
)
from trpg_state_machine.npc_results import NPCEvaluationResult
from trpg_state_machine.perception import JudgmentResult, NPCContext, SignalJudgment

package = load_adventure_package(sys.argv[2]).package
context = TrustedContext("authenticated:alice", "player.alice")


def notice(request):
    return JudgmentResult(tuple(SignalJudgment(s.signal_id, True) for s in request.signals))


def partial(request):
    return notice(request) if request.observer_id == "npc.caretaker" else JudgmentResult(())


with StateMachine(sys.argv[1]) as sm:
    sessions = []
    for key in ("complete", "partial"):
        request = CreateSessionRequest(
            key,
            package.adventure_id,
            package.package_version,
            package.package_digest,
            (PlayerSelection(context.player_id, "pc_template.observer"),),
        )
        sid = sm.create_session(request, package, context=context).session_id
        sessions.append(sid)
        for seq in (1, 2):
            assert (
                sm.project_event(sid, seq, package, context=context, judgment=notice) == "resolved"
            )
        for npc in ("npc.caretaker", "npc.attendant"):
            sm.grant_npc_access(sid, npc, "worker:" + npc, package, context=context)
        npc = NPCContext("worker:npc.caretaker", sid, "npc.caretaker")
        ids = tuple(o.observation_id for o in sm.read_observations(sid, package, context=npc))
        task = sm.create_npc_evaluation_task(
            sid,
            package,
            context=npc,
            request_id="old-task",
            mode="interactive",
            reason="player_input",
            player_input="Old authorized task.",
            expected_npc_state_version=1,
            source_observation_ids=ids,
        )
        sm.submit_npc_evaluation_result(
            task.evaluation_id, NPCEvaluationResult(), package, context=npc
        )
        state = sm.read_session(sid, package, context=context)
        command = TypedCommand(
            sid,
            "old.open",
            state.pc_actor_id,
            "set_object_state",
            SetObjectState("scene.platform", "object.platform_gate", ObjectState.OPEN),
            state.world_version,
        )
        assert sm.submit_command(command, package, context=context).status == "committed"
        assert sm.project_event(
            sid, 3, package, context=context, judgment=notice if key == "complete" else partial
        ) == ("resolved" if key == "complete" else "unresolved")
    assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 18
    print(json.dumps(sessions))
