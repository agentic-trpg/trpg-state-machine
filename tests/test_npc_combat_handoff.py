"""Synthetic NPC proposal inputs, real pinned D&D evaluation, actual SQLite commits."""

import json
import sqlite3
import subprocess
import sys
from dataclasses import replace

import pytest
import yaml
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd_support import NPC, create, initialize, loader
from dnd_support import package_data as real_data
from test_dnd_npc_availability import npc_command
from test_dnd_runtime import attack, authority
from test_npc_evaluation_tasks import create_task
from test_npc_state import SOURCE

from trpg_state_machine import StateMachine, StateMachineError
from trpg_state_machine.npc_results import NPCCombatIntent, NPCEvaluationResult
from trpg_state_machine.package import _validate
from trpg_state_machine.perception import NPCContext


def prepare(tmp_path, context, change=None):
    assets = loader()
    data = real_data(yaml.safe_load(SOURCE.read_text("utf-8")), assets)
    if change:
        change(data)
    package = _validate(data)
    (tmp_path / "package.json").write_text(json.dumps(data), "utf-8")
    sm = StateMachine(tmp_path / "combat.db", dnd_loader=assets)
    sid = create(sm, package, context)
    initialize(sm, sid, package, context)
    sm.grant_npc_access(sid, NPC, "agent:caretaker", package, context=context)
    npc = NPCContext("agent:caretaker", sid, NPC)
    sm.submit_command(attack(sm, sid, package, context), package, context=context)
    task = create_task(sm, sid, package, npc)
    command = npc_command(sm, sid, package, context)
    result = NPCEvaluationResult(
        action_intents=(NPCCombatIntent(command.payload, command.expected_world_version),)
    )
    receipt = sm.submit_npc_evaluation_result(task.evaluation_id, result, package, context=npc)
    return sm, sid, package, assets, npc, task, result, receipt


def test_real_enemy_handoff_and_mode_switch_replay(tmp_path, context):
    sm, sid, package, _, npc, task, result, selected = prepare(tmp_path, context)
    with sm:
        handoff = selected.outputs[0]
        assert handoff.command_id != task.evaluation_id and handoff.status == "pending"
        sm.set_npc_inactive(sid, package, context=npc, request_id="mode.inactive")
        before = sm.read_rng_context(sid, package, context=context)
        delivered = sm.dispatch_npc_output(task.evaluation_id, 0, package, context=context)
        assert delivered.command_receipt.status == "committed"
        assert delivered.command_receipt.actor_id == NPC
        assert sm.read_rng_context(sid, package, context=context).version == before.version + 1
        rows = authority(sm, sid)
        assert sm.dispatch_npc_output(task.evaluation_id, 0, package, context=context) == delivered
        assert (
            sm.submit_npc_evaluation_result(task.evaluation_id, result, package, context=npc)
            == selected
        )
        assert authority(sm, sid) == rows
        assert (
            len(
                sm._conn.execute(
                    "SELECT * FROM command_receipts WHERE command_id=?", (handoff.command_id,)
                ).fetchall()
            )
            == 1
        )


@pytest.mark.parametrize("kind", ["control", "grant", "hidden", "remote", "other_source"])
def test_scope_denial_without_mechanical_side_effects(tmp_path, context, kind):
    def change(data):
        if kind == "hidden":
            data["actors"][0]["senses"]["visual"] = False

    sm, sid, package, _, npc, task, _, selected = prepare(tmp_path, context, change)
    with sm:
        dispatch_context = context
        if kind == "control":
            dispatch_context = replace(context, controlled_npc_ids=())
        if kind == "grant":
            sm.grant_npc_access(sid, NPC, "agent:replacement", package, context=context)
        if kind == "remote":
            # Trusted test setup simulates location change; no invented rule result.
            with sm._transaction(write=True):
                sm._initialize_scene(sid, "scene.workshop", package)
            sm._conn.execute(
                "UPDATE actors SET current_scene_id=? WHERE actor_id=?", ("scene.workshop", NPC)
            )
        if kind == "other_source":
            original = npc_command(sm, sid, package, context)
            from trpg_state_machine.commands import TypedCommand

            tampered = TypedCommand(
                sid,
                selected.outputs[0].command_id,
                original.payload.target_id,
                "combat.intent",
                original.payload.model_copy(update={"source_id": original.payload.target_id}),
                original.expected_world_version,
            )
            before = authority(sm, sid)
            with pytest.raises(StateMachineError, match="Command differs"):
                sm.submit_command(tampered, package, context=context)
            assert authority(sm, sid) == before
            return
        before = authority(sm, sid)
        with pytest.raises(StateMachineError) as caught:
            sm.dispatch_npc_output(task.evaluation_id, 0, package, context=dispatch_context)
        assert caught.value.code in ("unauthorized", "unauthorized_target")
        assert authority(sm, sid) == before
        assert (
            sm._conn.execute(
                "SELECT status FROM npc_evaluation_tasks WHERE evaluation_id=?",
                (task.evaluation_id,),
            ).fetchone()[0]
            == "completed"
        )


def test_revoked_between_evaluation_and_commit(tmp_path, context):
    sm, sid, package, _, _, task, _, _ = prepare(tmp_path, context)
    with sm:
        real = sm._rules._evaluate_dnd

        async def revoke(request):
            result = await real(request)
            sm.grant_npc_access(sid, NPC, "agent:replacement", package, context=context)
            return result

        sm._rules._evaluate_dnd = revoke
        before = authority(sm, sid)
        with pytest.raises(StateMachineError) as caught:
            sm.dispatch_npc_output(task.evaluation_id, 0, package, context=context)
        assert caught.value.code == "unauthorized"
        assert authority(sm, sid) == before


def test_sql_failure_then_same_command_retry(tmp_path, context):
    sm, sid, package, _, _, task, _, _ = prepare(tmp_path, context)
    with sm:
        sm._conn.execute(
            "CREATE TRIGGER fail_attack BEFORE UPDATE ON rng_states "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        before = authority(sm, sid)
        with pytest.raises(StateMachineError) as caught:
            sm.dispatch_npc_output(task.evaluation_id, 0, package, context=context)
        assert caught.value.code == "storage_error" and authority(sm, sid) == before
        sm._conn.execute("DROP TRIGGER fail_attack")
        assert (
            sm.dispatch_npc_output(
                task.evaluation_id, 0, package, context=context
            ).command_receipt.status
            == "committed"
        )


def test_committed_response_loss_reconciles_in_fresh_process(tmp_path, context):
    sm, sid, package, _, _, task, _, selected = prepare(tmp_path, context)
    with sm:
        real = sm.submit_command

        def lose(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("response lost after COMMIT")

        sm.submit_command = lose
        with pytest.raises(RuntimeError):
            sm.dispatch_npc_output(task.evaluation_id, 0, package, context=context)
        before = authority(sm, sid)
    script = """
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from dnd_support import loader
from trpg_state_machine import StateMachine,TrustedContext
from trpg_state_machine.package import _validate
p=_validate(json.loads(Path(sys.argv[2]).read_text('utf-8')))
with StateMachine(sys.argv[1],dnd_loader=loader()) as sm:
 c=TrustedContext('authenticated:alice','player.alice',('npc.caretaker','npc.attendant'))
 d=sm.dispatch_npc_output(sys.argv[3],0,p,context=c)
 assert d.status=='dispatched' and d.command_receipt.status=='committed'
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "combat.db"),
            str(tmp_path / "package.json"),
            task.evaluation_id,
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    with StateMachine(tmp_path / "combat.db", dnd_loader=loader()) as recovered:
        assert authority(recovered, sid) == before
        with pytest.raises(StateMachineError) as caught:
            recovered.resume_session(sid, package, context=context)
        assert caught.value.code == "combat_unrestorable"
    conn = sqlite3.connect(tmp_path / "combat.db")
    try:
        assert (
            conn.execute(
                "SELECT status FROM npc_output_handoffs WHERE command_id=?",
                (selected.outputs[0].command_id,),
            ).fetchone()[0]
            == "dispatched"
        )
    finally:
        conn.close()


def test_lost_response_replays_receipt_after_actor_scene_changes(tmp_path, context):
    sm, sid, package, _, _, task, _, _ = prepare(tmp_path, context)
    with sm:
        real = sm.submit_command

        def lose(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("response lost after COMMIT")

        sm.submit_command = lose
        with pytest.raises(RuntimeError):
            sm.dispatch_npc_output(task.evaluation_id, 0, package, context=context)
        # Trusted race setup after a genuine committed attack; subsequent target
        # admission must not prevent reconciling the original durable receipt.
        with sm._transaction(write=True):
            sm._initialize_scene(sid, "scene.workshop", package)
            sm._conn.execute(
                "UPDATE actors SET current_scene_id=? WHERE session_id=? AND actor_id=?",
                ("scene.workshop", sid, NPC),
            )
        sm.submit_command = real
        before = authority(sm, sid)
        delivered = sm.dispatch_npc_output(task.evaluation_id, 0, package, context=context)
        assert delivered.status == "dispatched"
        assert delivered.command_receipt.status == "committed"
        assert authority(sm, sid) == before
        assert (
            sm.get_command_receipt(
                sid, delivered.command_receipt.command_id, package, context=context
            )
            == delivered.command_receipt
        )


@pytest.mark.parametrize("kind", ["unsupported", "conflict"])
def test_command_failure_keeps_selected_result(tmp_path, context, kind):
    sm, sid, package, _, npc, task, result, selected = prepare(tmp_path, context)
    with sm:
        if kind == "conflict":
            sm._conn.execute(
                "UPDATE sessions SET world_version=world_version+1 WHERE session_id=?", (sid,)
            )
        else:
            # The reliable output is immutable: create a separate task/result for this intent.
            task = create_task(sm, sid, package, npc, key="task.unsupported")
            payload = CombatIntentPayload(intent_type="dash", source_id=NPC)
            result = NPCEvaluationResult(
                action_intents=(
                    NPCCombatIntent(
                        payload, sm.read_session(sid, package, context=context).world_version
                    ),
                )
            )
            selected = sm.submit_npc_evaluation_result(
                task.evaluation_id, result, package, context=npc
            )
        before = sm.read_rng_context(sid, package, context=context)
        delivered = sm.dispatch_npc_output(task.evaluation_id, 0, package, context=context)
        assert delivered.command_receipt.status == (
            "conflict" if kind == "conflict" else "rejected"
        )
        assert sm.read_rng_context(sid, package, context=context) == before
        assert (
            sm.submit_npc_evaluation_result(task.evaluation_id, result, package, context=npc)
            == selected
        )
