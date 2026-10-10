"""Real generic ability/skill checks: exact Engine parity and durable state."""

import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from dnd5e_engine.evaluation_contracts import CheckPayload, RuleEvaluationResult
from dnd_support import NPC, create
from modern_support import modern_data, modern_loader
from test_dnd_ledger_scope import stored_state

from trpg_state_machine import SetSceneFlag, StateMachine, StateMachineError, TypedCommand
from trpg_state_machine.package import _validate
from trpg_state_machine.perception import NPCContext


def check_command(sm, sid, package, context, key="check.real", **kwargs):
    state = sm.read_session(sid, package, context=context)
    fields = dict(
        actor_id=state.pc_actor_id,
        ability="str",
        skill="athletics",
        tool=None,
        dc=10,
        target_id=None,
        context="generic",
        required_sense="none",
        social_interaction=False,
        advantage=(),
        disadvantage=(),
        redeem_granted_die=None,
    )
    fields.update(kwargs)
    return TypedCommand(
        sid, key, fields["actor_id"], "rules.check", CheckPayload(**fields), state.world_version
    )


def check_setup(tmp_path, base, context, *, expertise=False):
    assets = modern_loader()
    data = modern_data(base, assets)
    if expertise:
        for kind in ("combat_foundation", "noncombat_foundation"):
            data["rule_bindings"][kind]["snapshot"]["character_states"][0]["skill_expertise"] = [
                "athletics"
            ]
    package = _validate(data)
    sm = StateMachine(tmp_path / "checks.db", dnd_loader=assets)
    sid = create(sm, package, context)
    return sm, sid, package, assets, data


@pytest.mark.parametrize("dc", [1, 99])
@pytest.mark.parametrize("mode", ["normal", "advantage", "disadvantage", "cancelled"])
@pytest.mark.parametrize("expertise", [False, True])
def test_real_check_empty_delta_exact_engine_parity_and_private_event(
    tmp_path, package_data, context, dc, mode, expertise
):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context, expertise=expertise)
    with sm:
        command = check_command(
            sm,
            sid,
            package,
            context,
            dc=dc,
            advantage=("flag",) if mode in ("advantage", "cancelled") else (),
            disadvantage=("flag",) if mode in ("disadvantage", "cancelled") else (),
        )
        original = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        real = sm._evaluate_dnd
        captured = []

        async def capture(request):
            assert not sm._conn.in_transaction and request.state_snapshot == original
            result = await real(request)
            captured.append(result)
            return result

        sm._evaluate_dnd = capture
        receipt = sm.submit_command(command, package, context=context)
        result = captured[0]
        assert receipt.status == "committed" and result.status == "accepted", result.error
        assert result.state_delta.operations == ()
        event = sm.read_events(sid, package, context=context)[-1]
        assert event.event_type == "rules.check_rolled" and not event.signals
        assert event.payload["engine_event"] == result.proposed_events[0].model_dump(mode="json")
        assert event.payload["adjudication"]["principal_id"] == context.principal_id
        assert event.payload["adjudication"]["payload"] == command.payload.model_dump(mode="json")
        rolled = result.proposed_events[0]
        assert rolled.succeeded == (dc == 1)
        assert rolled.advantage == ("normal" if mode == "cancelled" else mode)
        # Values asserted here are observations of the real Engine, not SM formulas.
        assert rolled.modifier == (7 if expertise else 5)
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert after.character_states == original.character_states
        assert after.world_version == original.world_version + 1
        next_rng = sm.read_rng_context(sid, package, context=context)
        assert (
            next_rng.state == result.rng_transition.next_state
            and next_rng.version == rng.version + 1
        )
        before_replay = stored_state(tmp_path / "checks.db")
        assert sm.submit_command(command, package, context=context) == receipt
        assert stored_state(tmp_path / "checks.db") == before_replay and len(captured) == 1
        sm.grant_npc_access(sid, NPC, "agent:caretaker", package, context=context)
        for committed in sm.read_events(sid, package, context=context):
            sm.project_event(sid, committed.event_seq, package, context=context)
        observations = sm.read_observations(
            sid, package, context=NPCContext("agent:caretaker", sid, NPC)
        )
        assert all(
            signal.content == "Someone enters the scene."
            for observation in observations
            for signal in observation.perceived_signals
        )
        assert (
            sm._conn.execute(
                "SELECT COUNT(*) FROM event_observations WHERE session_id=? AND event_seq=?",
                (sid, receipt.event_seqs[0]),
            ).fetchone()[0]
            == 0
        )
        assert sm._conn.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE session_id=?", (sid,)
        ).fetchone()[0] == 2 * len(sm.read_events(sid, package, context=context))


def test_missing_dc_is_preflight_and_player_cannot_adjudicate_or_replay(
    tmp_path, package_data, context
):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    with sm:
        command = check_command(sm, sid, package, context, dc=None)
        real = sm._evaluate_dnd

        async def forbidden(request):
            raise AssertionError("preflight must not evaluate")

        sm._evaluate_dnd = forbidden
        before = stored_state(tmp_path / "checks.db")
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "unauthorized" and stored_state(tmp_path / "checks.db") == before
        receipt = sm.submit_command(command, package, context=gm)
        assert receipt.error_code == "pending_choice_unsupported" and not receipt.event_seqs
        after = stored_state(tmp_path / "checks.db")
        assert {t: rows for t, rows in before.items() if t != "command_receipts"} == {
            t: rows for t, rows in after.items() if t != "command_receipts"
        }
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "unauthorized"
        supplied = replace(command, payload=command.payload.model_copy(update={"dc": 10}))
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(supplied, package, context=gm)
        assert error.value.code == "idempotency_conflict"
        sm._evaluate_dnd = real
        assert (
            sm.submit_command(
                replace(supplied, command_id="check.with_dc"), package, context=gm
            ).status
            == "committed"
        )


@pytest.mark.parametrize(
    "fault",
    [
        "actor",
        "dc",
        "skill",
        "extra_delta",
        "missing_event",
        "rng_input",
        "no_rng",
        "binding",
        "accepted_tool",
    ],
)
def test_invalid_real_check_result_no_rows_no_receipt_safe_retry(
    tmp_path, package_data, context, fault
):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    with sm:
        command = check_command(sm, sid, package, context)
        if fault == "accepted_tool":
            command = replace(
                command, payload=command.payload.model_copy(update={"tool": "brush", "skill": None})
            )
        before = stored_state(tmp_path / "checks.db")
        real = sm._evaluate_dnd

        async def corrupt(request):
            if fault == "accepted_tool":
                request = request.model_copy(
                    update={"payload": request.payload.model_copy(update={"tool": None})}
                )
            result = (await real(request)).model_dump(mode="json")
            event = result["proposed_events"][0]
            if fault == "actor":
                event["actor_id"] = NPC
            elif fault == "dc":
                event["dc"] = 99
            elif fault == "skill":
                event["skill"] = "acrobatics"
            elif fault == "extra_delta":
                result["state_delta"]["operations"] = [
                    {
                        "kind": "actor.hp_delta",
                        "target_id": command.actor_id,
                        "expected_hp": 8,
                        "amount": -1,
                        "resulting_hp": 7,
                    }
                ]
            elif fault == "missing_event":
                result["proposed_events"] = []
            elif fault == "rng_input":
                result["rng_transition"]["input_version"] += 1
            elif fault == "no_rng":
                result["rng_transition"]["next_state"] = result["rng_transition"]["input_state"]
                result["rng_transition"]["state_changed"] = False
            elif fault == "binding":
                result["input_ruleset_binding"]["evaluator_version"] = "wrong"
            return RuleEvaluationResult.model_validate_json(json.dumps(result))

        sm._evaluate_dnd = corrupt
        for _ in range(2):
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(command, package, context=context)
            assert error.value.code == "invalid_rule_result"
            assert stored_state(tmp_path / "checks.db") == before
        sm._evaluate_dnd = real
        retried = sm.submit_command(command, package, context=context)
        assert retried.status == ("rejected" if fault == "accepted_tool" else "committed")


@pytest.mark.parametrize("table", ["rng_states", "events", "event_outbox", "command_receipts"])
def test_check_sql_fault_rolls_back_and_retry(tmp_path, package_data, context, table):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    with sm:
        command = check_command(sm, sid, package, context)
        before = stored_state(tmp_path / "checks.db")
        action = "UPDATE" if table == "rng_states" else "INSERT"
        sm._conn.execute(
            f"CREATE TEMP TRIGGER fail BEFORE {action} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "storage_error"
        assert stored_state(tmp_path / "checks.db") == before
        sm._conn.execute("DROP TRIGGER fail")
        assert sm.submit_command(command, package, context=context).status == "committed"


def test_real_unsupported_runtime_failure_and_concurrent_world_change(
    tmp_path, package_data, context
):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    with sm:
        original = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        unsupported = check_command(sm, sid, package, context, tool="forged_tool", skill=None)
        assert (
            sm.submit_command(unsupported, package, context=context).error_code
            == "check.capability"
        )
        assert sm.read_noncombat_snapshot(sid, package, context=context) == original
        assert sm.read_rng_context(sid, package, context=context) == rng
        command = check_command(sm, sid, package, context, key="runtime.retry", skill=None)
        real = sm._evaluate_dnd

        async def failed(request):
            raise OSError("injected transport failure")

        sm._evaluate_dnd = failed
        before = stored_state(tmp_path / "checks.db")
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "rule_evaluation_failed"
        assert stored_state(tmp_path / "checks.db") == before

        async def concurrent(request):
            result = await real(request)
            world = TypedCommand(
                sid,
                "world.concurrent",
                command.actor_id,
                "set_scene_flag",
                SetSceneFlag("scene.platform", "flag.lamp_checked", True),
                command.expected_world_version,
            )
            assert sm.submit_command(world, package, context=context).status == "committed"
            return result

        sm._evaluate_dnd = concurrent
        conflict = sm.submit_command(command, package, context=context)
        assert conflict.status == "conflict" and not conflict.event_seqs
        assert sm.read_rng_context(sid, package, context=context) == rng


def test_check_fresh_process_recovers_rng_and_receipt_then_rolls_once(
    tmp_path, package_data, context
):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, assets, data = check_setup(tmp_path, package_data, context)
    with sm:
        command = check_command(sm, sid, package, context)
        receipt = sm.submit_command(command, package, context=context)
        saved = sm.save_session(sid, package, context=context)
        (tmp_path / "package.json").write_text(json.dumps(data), "utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("_check_resume_worker.py")),
            str(tmp_path / "checks.db"),
            str(tmp_path / "package.json"),
            sid,
            json.dumps(asdict(context)),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    output = json.loads(result.stdout)
    assert output["receipt"] == asdict(receipt) | {"event_seqs": list(receipt.event_seqs)}
    assert output["before_rng"] == saved.rng.rng_version
    assert output["before_world"] == saved.world_version
    assert output["after_rng"] == saved.rng.rng_version + 1
    with StateMachine(tmp_path / "checks.db", dnd_loader=assets) as reopened:
        assert (
            reopened.read_session(sid, package, context=context).world_version
            == saved.world_version + 1
        )


def test_npc_check_requires_gm_and_npc_grant_even_on_replay(tmp_path, package_data, context):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    with sm:
        command = check_command(sm, sid, package, context, actor_id=NPC, skill=None)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        before = stored_state(tmp_path / "checks.db")
        for revoked in (
            replace(context, controlled_npc_ids=()),
            replace(context, can_adjudicate_checks=False),
        ):
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(command, package, context=revoked)
            assert error.value.code == "unauthorized"
            assert stored_state(tmp_path / "checks.db") == before


@pytest.mark.parametrize("case", ["mismatched_actor", "forged_context", "unexpected_choice"])
def test_check_protocol_errors_are_independent(tmp_path, package_data, context, case):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    with sm:
        command = check_command(sm, sid, package, context)
        before = stored_state(tmp_path / "checks.db")
        if case == "mismatched_actor":
            command = replace(command, payload=command.payload.model_copy(update={"actor_id": NPC}))
            code = "invalid_actor"
        elif case == "forged_context":
            context = replace(context, can_adjudicate_checks="yes")
            code = "invalid_context"
        else:
            code = "invalid_rule_result"
            real = sm._evaluate_dnd

            async def choice(request):
                # Real Engine preflight choice, injected after an already admitted request.
                return await real(
                    request.model_copy(
                        update={"payload": request.payload.model_copy(update={"dc": None})}
                    )
                )

            sm._evaluate_dnd = choice
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == code
        assert stored_state(tmp_path / "checks.db") == before
