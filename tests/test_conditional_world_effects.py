"""Original GM proposals, actual Engine checks, and independent SQLite evidence."""

import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from test_dnd_ledger_scope import stored_state
from test_events import event_data
from test_real_checks import check_command, check_setup

from trpg_state_machine import (
    CheckWorldEffects,
    MoveActor,
    ObjectState,
    SetObjectState,
    SetSceneFlag,
    StateMachineError,
    TypedCommand,
)
from trpg_state_machine.commands import command_fingerprint
from trpg_state_machine.package import canonical_json

FLAG = SetSceneFlag("scene.platform", "flag.lamp_checked", True)
OPEN = SetObjectState("scene.platform", "object.platform_gate", ObjectState.OPEN)


def conditional(sm, sid, package, context, *, dc=1, key="check.effects"):
    return replace(
        check_command(sm, sid, package, context, key=key, dc=dc),
        check_world_effects=CheckWorldEffects(
            "gm.synthetic.inspection", (replace(FLAG, value=False),), (FLAG, OPEN), (OPEN,)
        ),
    )


@pytest.mark.parametrize("dc", [1, 99])
def test_real_outcome_selects_world_branch_and_atomic_order(tmp_path, package_data, context, dc):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    with sm:
        command = conditional(sm, sid, package, context, dc=dc)
        before = sm.read_session(sid, package, context=context)
        real, results = sm._rules._evaluate_dnd, []

        async def capture(request):
            assert not sm._conn.in_transaction
            result = await real(request)
            results.append(result)
            return result

        sm._rules._evaluate_dnd = capture
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed" and results[0].status == "accepted"
        assert results[0].proposed_events[0].succeeded is (dc == 1)
        state = sm.read_session(sid, package, context=context)
        scene = state.scenes["scene.platform"]
        assert scene.local_flags["flag.lamp_checked"] is (dc == 1)
        assert scene.objects["object.platform_gate"] == "open"
        assert state.world_version == before.world_version + 1
        assert state.rng.rng_version == before.rng.rng_version + 1
        assert state.character == before.character
        events = [
            e
            for e in sm.read_events(sid, package, context=context)
            if e.event_seq in receipt.event_seqs
        ]
        assert [e.event_type for e in events] == (
            ["rules.check_rolled", "scene.flag_changed", "object.state_changed"]
            if dc == 1
            else ["rules.check_rolled", "object.state_changed"]
        )
        assert events[0].payload["adjudication"]["world_effects"] == json.loads(
            canonical_json(asdict(command.check_world_effects))
        )
        rows = stored_state(tmp_path / "checks.db")
        assert sm.submit_command(command, package, context=context) == receipt
        assert stored_state(tmp_path / "checks.db") == rows and len(results) == 1
        with pytest.raises(StateMachineError) as conflict:
            sm.submit_command(
                replace(
                    command, check_world_effects=replace(command.check_world_effects, on_failure=())
                ),
                package,
                context=context,
            )
        assert conflict.value.code == "idempotency_conflict"
        assert stored_state(tmp_path / "checks.db") == rows


@pytest.mark.parametrize(
    "fault",
    ["source", "movement", "type", "unicode", "unknown_flag", "remote", "unselected_object"],
)
def test_candidates_rejected_before_rng_even_in_unselected_branch(
    tmp_path, package_data, context, fault
):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    with sm:
        command = conditional(sm, sid, package, context)
        effects = command.check_world_effects
        effects = {
            "source": replace(effects, source_ref="player.claimed_success"),
            "movement": replace(
                effects, on_failure=(MoveActor("npc.caretaker", "scene.workshop"),)
            ),
            "type": replace(effects, on_failure=(replace(OPEN, state="open"),)),
            "unicode": replace(effects, on_failure=(replace(FLAG, flag_id="bad\ud800"),)),
            "unknown_flag": replace(effects, on_failure=(replace(FLAG, flag_id="flag.unknown"),)),
            "remote": replace(effects, on_failure=(replace(FLAG, scene_id="scene.workshop"),)),
            "unselected_object": replace(
                effects, on_failure=(replace(OPEN, object_id="object.unknown"),)
            ),
        }[fault]
        before = stored_state(tmp_path / "checks.db")

        async def forbidden(request):
            pytest.fail("Invalid proposal must never reach the evaluator")

        sm._rules._evaluate_dnd = forbidden
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(
                replace(command, check_world_effects=effects), package, context=context
            )
        assert error.value.code == (
            "invalid_command"
            if fault in {"source", "movement", "type", "unicode"}
            else "invalid_target"
        )
        assert stored_state(tmp_path / "checks.db") == before


def test_precondition_rejection_is_durable_and_gm_grant_checked(tmp_path, package_data, context):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, gm)
    with sm:
        command = conditional(sm, sid, package, gm)
        command = replace(
            command, check_world_effects=replace(command.check_world_effects, preconditions=(FLAG,))
        )
        before = stored_state(tmp_path / "checks.db")
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(command, package, context=context)
        assert denied.value.code == "unauthorized"
        assert stored_state(tmp_path / "checks.db") == before

        async def forbidden(request):
            pytest.fail("Failed precondition must not consume a roll")

        sm._rules._evaluate_dnd = forbidden
        receipt = sm.submit_command(command, package, context=gm)
        assert (receipt.status, receipt.error_code, receipt.event_seqs) == (
            "rejected",
            "world_precondition_failed",
            (),
        )
        after = stored_state(tmp_path / "checks.db")
        assert {k: v for k, v in after.items() if k != "command_receipts"} == {
            k: v for k, v in before.items() if k != "command_receipts"
        }
        assert sm.submit_command(command, package, context=gm) == receipt
        assert stored_state(tmp_path / "checks.db") == after


@pytest.mark.parametrize("table", ["rng_states", "events", "event_outbox", "command_receipts"])
def test_sql_failure_after_world_effect_rolls_back_all_and_can_retry(
    tmp_path, package_data, context, table
):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    with sm:
        command = conditional(sm, sid, package, context)
        before = stored_state(tmp_path / "checks.db")
        action = "UPDATE" if table == "rng_states" else "INSERT"
        sm._conn.execute(
            f"CREATE TEMP TRIGGER fail BEFORE {action} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(StateMachineError) as failed:
            sm.submit_command(command, package, context=context)
        assert failed.value.code == "storage_error"
        assert stored_state(tmp_path / "checks.db") == before
        sm._conn.execute("DROP TRIGGER fail")
        assert sm.submit_command(command, package, context=context).status == "committed"
        assert stored_state(tmp_path / "checks.db")["objects"] != before["objects"]


def test_real_check_concurrent_world_change_cannot_apply_candidates(
    tmp_path, package_data, context
):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    with sm:
        command = conditional(sm, sid, package, context)
        rng, real = sm.read_rng_context(sid, package, context=context), sm._rules._evaluate_dnd

        async def concurrent(request):
            result = await real(request)
            assert (
                sm.submit_command(
                    TypedCommand(
                        sid,
                        "concurrent",
                        command.actor_id,
                        "set_scene_flag",
                        FLAG,
                        command.expected_world_version,
                    ),
                    package,
                    context=context,
                ).status
                == "committed"
            )
            return result

        sm._rules._evaluate_dnd = concurrent
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "conflict" and not receipt.event_seqs
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert (
            sm.read_session(sid, package, context=context)
            .scenes["scene.platform"]
            .objects["object.platform_gate"]
            == "closed"
        )


def test_fresh_process_lost_response_replays_world_and_rng_once(tmp_path, package_data, context):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, data = check_setup(tmp_path, package_data, context)
    with sm:
        command = conditional(sm, sid, package, context)
        receipt = sm.submit_command(command, package, context=context)
        # The caller loses the response; the new process must reconcile this exact request.
        saved = sm.save_session(sid, package, context=context)
        (tmp_path / "package.json").write_text(json.dumps(data), "utf-8")
    before = stored_state(tmp_path / "checks.db")
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("_conditional_resume_worker.py")),
            str(tmp_path / "checks.db"),
            str(tmp_path / "package.json"),
            sid,
            json.dumps(asdict(context)),
            str(command.expected_world_version),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    output = json.loads(result.stdout)
    assert output["receipt"] == asdict(receipt) | {"event_seqs": list(receipt.event_seqs)}
    assert output["world_version"] == saved.world_version
    assert output["rng_version"] == saved.rng.rng_version
    assert output["objects"]["object.platform_gate"] == "open"
    assert stored_state(tmp_path / "checks.db") == before


def test_legacy_command_fingerprint_unchanged(tmp_path, package_data, context):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, gm)
    with sm:
        command = check_command(sm, sid, package, gm)
        old = asdict(command)
        old.pop("check_world_effects")
        old["payload"] = command.payload.model_dump(mode="json")
        raw = dict(
            command=old,
            principal=gm.principal_id,
            player=gm.player_id,
            package=package.package_digest,
            binding=asdict(package.ruleset_binding),
        )
        assert (
            command_fingerprint(command, package, gm)
            == hashlib.sha256(canonical_json(raw).encode()).hexdigest()
        )


def test_real_check_cascade_rejection_rolls_back_world_and_rng_keeps_receipt(
    tmp_path, package_data, context
):
    context = replace(context, can_adjudicate_checks=True)
    package_data["events"] = [event_data(f"event.cascade_{i}") for i in range(33)]
    package_data["scenes"][0]["event_definition_ids"] = [
        item["event_definition_id"] for item in package_data["events"]
    ]
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    with sm:
        command = conditional(sm, sid, package, context)
        before = stored_state(tmp_path / "checks.db")
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "event_cascade_limit")
        after = stored_state(tmp_path / "checks.db")
        assert {k: v for k, v in after.items() if k != "command_receipts"} == {
            k: v for k, v in before.items() if k != "command_receipts"
        }

        async def forbidden(request):
            pytest.fail("Rejected retry cannot roll again")

        sm._rules._evaluate_dnd = forbidden
        assert sm.submit_command(command, package, context=context) == receipt
        assert stored_state(tmp_path / "checks.db") == after
