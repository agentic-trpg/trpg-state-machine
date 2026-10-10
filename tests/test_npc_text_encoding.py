"""GR-01: real SQLite encoding failures, durable budgets and Unicode preservation."""

import hashlib
import json
from dataclasses import asdict, replace

import pytest
from test_migrations import stored_rows
from test_npc_evaluation_tasks import create_task, prepare
from test_npc_evaluation_tasks import evaluation_package as evaluation_package
from test_npc_results import full_result

from trpg_state_machine import StateMachine, StateMachineError
from trpg_state_machine.npc_results import NPCActionIntent, NPCDialogue, NPCEvaluationResult
from trpg_state_machine.npc_state import EpisodicMemory, SourceReference

MULTILINGUAL = "你好，冒险者 🧙🏽\u200d♀️！ مرحبًا / 日本語 / 한글 / café / cafe\u0301 / 😀"


def result_with_outputs(sm, sid, package, context, npc, ids):
    return replace(
        full_result(sm, sid, package, npc, ids),
        dialogue=NPCDialogue("你好 😀", (), "private", ids),
        action_intents=(
            NPCActionIntent(
                "move_actor",
                "scene.workshop",
                sm.read_session(sid, package, context=context).world_version,
            ),
        ),
    )


def invalid_text(result, field, attribute, text):
    if field == "dialogue":
        return replace(result, dialogue=replace(result.dialogue, text=text))
    value = (text,) if attribute == "steps" else text
    record = replace(getattr(result, field)[0], **{attribute: value})
    return replace(result, **{field: (record,)})


def assert_only_failure_changed(database, before, evaluation_id, attempts, status):
    after = stored_rows(database)
    assert {k: v for k, v in after.items() if k != "npc_evaluation_tasks"} == {
        k: v for k, v in before.items() if k != "npc_evaluation_tasks"
    }
    columns, rows = before["npc_evaluation_tasks"]
    assert len(rows) == 1
    expected = dict(zip(columns, next(iter(rows)), strict=True))
    expected.update(attempts=attempts, status=status, last_error="invalid_npc_result")
    assert expected["evaluation_id"] == evaluation_id
    assert after["npc_evaluation_tasks"] == (columns, {tuple(expected[c] for c in columns)})


@pytest.mark.parametrize(
    "field,attribute,text",
    [
        ("memory_updates", "text", "\ud800"),
        ("memory_updates", "text", "\udfff"),
        ("dialogue", "text", "\ud800"),
        ("dialogue", "text", "\udfff"),
        ("belief_updates", "text", "\ud800"),
        ("relationship_updates", "text", "\udfff"),
        ("goal_updates", "text", "\ud800"),
        ("plan_updates", "text", "\udfff"),
        ("plan_updates", "steps", "\ud800"),
    ],
)
def test_invalid_result_text_only_counts_failure_then_corrects_and_replays(
    tmp_path, evaluation_package, context, field, attribute, text
):
    database = tmp_path / "encoding.db"
    with StateMachine(database) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        good = result_with_outputs(sm, sid, evaluation_package, context, npc, ids)
        before = stored_rows(database)
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(
                task.evaluation_id,
                invalid_text(good, field, attribute, text),
                evaluation_package,
                context=npc,
            )
        assert error.value.code == "invalid_npc_result"
        assert isinstance(error.value.__cause__, UnicodeEncodeError)
        assert_only_failure_changed(database, before, task.evaluation_id, 1, "running")
        assert (
            sm.get_npc_evaluation_receipt(task.evaluation_id, evaluation_package, context=npc)
            is None
        )
    with StateMachine(database) as sm:
        assert (
            sm.get_npc_evaluation_task(
                task.evaluation_id, evaluation_package, context=npc
            ).task.attempts
            == 1
        )
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, good, evaluation_package, context=npc
        )
        assert receipt.npc_state_version == 2 and len(receipt.outputs) == 2
    with StateMachine(database) as sm:
        committed = stored_rows(database)
        assert (
            sm.get_npc_evaluation_receipt(task.evaluation_id, evaluation_package, context=npc)
            == receipt
        )
        assert (
            sm.submit_npc_evaluation_result(
                task.evaluation_id, good, evaluation_package, context=npc
            )
            == receipt
        )
        assert stored_rows(database) == committed


@pytest.mark.parametrize("field", ["memory_updates", "dialogue"])
def test_invalid_unicode_exhausts_persistent_budget_and_cannot_submit_valid_result(
    tmp_path, evaluation_package, context, field
):
    database = tmp_path / "budget.db"
    with StateMachine(database) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        good = result_with_outputs(sm, sid, evaluation_package, context, npc, ids)
        bad = invalid_text(good, field, "text", "\ud800")
        before = stored_rows(database)
    for attempts in range(1, task.attempt_limit + 1):
        with StateMachine(database) as sm:
            with pytest.raises(StateMachineError) as error:
                sm.submit_npc_evaluation_result(
                    task.evaluation_id, bad, evaluation_package, context=npc
                )
            assert error.value.code == "invalid_npc_result"
            status = "failed" if attempts == task.attempt_limit else "running"
            assert_only_failure_changed(database, before, task.evaluation_id, attempts, status)
    with StateMachine(database) as sm:
        exhausted = stored_rows(database)
        for candidate in (good, bad):
            with pytest.raises(StateMachineError) as error:
                sm.submit_npc_evaluation_result(
                    task.evaluation_id, candidate, evaluation_package, context=npc
                )
            assert error.value.code == "evaluation_inactive"
            assert stored_rows(database) == exhausted


@pytest.mark.parametrize(
    "entry,field",
    [
        ("create", "player_input"),
        ("create", "reason"),
        ("create", "request_id"),
        ("create", "source_observation_ids"),
        ("interaction", "player_input"),
        ("interaction", "request_id"),
        ("inactive", "request_id"),
        ("route", "request_id"),
        ("read", "evaluation_id"),
    ],
)
def test_invalid_task_text_preserves_existing_task_mode_and_all_authority(
    tmp_path, evaluation_package, context, entry, field
):
    database = tmp_path / "input.db"
    with StateMachine(database) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        before = stored_rows(database)
        bad = "\udfff"
        with pytest.raises(StateMachineError) as error:
            if entry == "create":
                value = (bad,) if field == "source_observation_ids" else bad
                args = dict(
                    context=npc,
                    request_id="new",
                    mode="interactive",
                    reason="new",
                    expected_npc_state_version=1,
                    player_input="Hello",
                    source_observation_ids=ids,
                )
                args[field] = value
                sm.create_npc_evaluation_task(sid, evaluation_package, **args)
            elif entry == "interaction":
                sm.request_npc_interaction(
                    sid,
                    bad if field == "player_input" else "Hello",
                    evaluation_package,
                    context=npc,
                    request_id=bad if field == "request_id" else "switch",
                )
            elif entry == "inactive":
                sm.set_npc_inactive(sid, evaluation_package, context=npc, request_id=bad)
            elif entry == "route":
                sm.route_npc_observations(sid, evaluation_package, context=npc, request_id=bad)
            else:
                sm.get_npc_evaluation_task(bad, evaluation_package, context=npc)
        assert error.value.code == "invalid_evaluation"
        assert stored_rows(database) == before
        assert (
            sm.get_npc_evaluation_task(task.evaluation_id, evaluation_package, context=npc).task
            == task
        )


def test_invalid_result_budget_storage_failure_rolls_back_and_remains_correctable(
    tmp_path, evaluation_package, context
):
    database = tmp_path / "rollback.db"
    with StateMachine(database) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        good = result_with_outputs(sm, sid, evaluation_package, context, npc, ids)
        bad = invalid_text(good, "memory_updates", "text", "\ud800")
        before = stored_rows(database)
        sm._conn.execute(
            "CREATE TEMP TRIGGER inject BEFORE UPDATE ON npc_evaluation_tasks "
            "BEGIN SELECT RAISE(ABORT,'budget storage failure'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(
                task.evaluation_id, bad, evaluation_package, context=npc
            )
        assert error.value.code == "storage_error"
        assert stored_rows(database) == before
        sm._conn.execute("DROP TRIGGER inject")
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(
                task.evaluation_id, bad, evaluation_package, context=npc
            )
        assert error.value.code == "invalid_npc_result"
        assert_only_failure_changed(database, before, task.evaluation_id, 1, "running")
        assert (
            sm.submit_npc_evaluation_result(
                task.evaluation_id, good, evaluation_package, context=npc
            ).status
            == "completed"
        )


def test_multilingual_task_and_result_preserve_encoding_fingerprint_and_replay(
    tmp_path, evaluation_package, context
):
    database = tmp_path / "multilingual.db"
    good = NPCEvaluationResult(
        dialogue=NPCDialogue(MULTILINGUAL, (), "private", ()),
        memory_updates=(
            EpisodicMemory(
                "memory.multilingual",
                1,
                True,
                MULTILINGUAL,
                (SourceReference("package", "npc.caretaker", None),),
                "told",
            ),
        ),
    )
    raw = json.dumps(
        asdict(good), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    with StateMachine(database) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(
            sm,
            sid,
            evaluation_package,
            npc,
            ids,
            key="请求.😀",
            reason=MULTILINGUAL,
            player_input=MULTILINGUAL,
        )
        task_raw = json.dumps(
            ["interactive", MULTILINGUAL, 1, ids, MULTILINGUAL],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        assert (
            sm._conn.execute(
                "SELECT fingerprint FROM npc_evaluation_tasks WHERE evaluation_id=?",
                (task.evaluation_id,),
            ).fetchone()[0]
            == hashlib.sha256(task_raw.encode("utf-8")).hexdigest()
        )
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, good, evaluation_package, context=npc
        )
        stored = sm._conn.execute(
            "SELECT result_json,fingerprint FROM npc_evaluation_receipts WHERE evaluation_id=?",
            (task.evaluation_id,),
        ).fetchone()
        assert stored["result_json"] == raw
        assert stored["fingerprint"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
        # Golden fingerprint captured from the unmodified ffded8e implementation.
        assert (
            stored["fingerprint"]
            == "5147a408201bb1c47744c8a5db84c1803f2a3f0806f9a927c7152a15550250ff"
        )
    with StateMachine(database) as sm:
        view = sm.get_npc_evaluation_task(task.evaluation_id, evaluation_package, context=npc)
        assert view.task.reason == view.task.player_input == MULTILINGUAL
        assert (
            next(m for m in view.npc_meta.memories if m.record_id == "memory.multilingual").text
            == MULTILINGUAL
        )
        committed = stored_rows(database)
        assert (
            create_task(
                sm,
                sid,
                evaluation_package,
                npc,
                ids,
                key="请求.😀",
                reason=MULTILINGUAL,
                player_input=MULTILINGUAL,
            ).evaluation_id
            == task.evaluation_id
        )
        bad = invalid_text(good, "dialogue", "text", "\udfff")
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(
                task.evaluation_id, bad, evaluation_package, context=npc
            )
        assert error.value.code == "invalid_npc_result" and stored_rows(database) == committed
        changed = replace(
            good, dialogue=replace(good.dialogue, text=MULTILINGUAL.replace("e\u0301", "é"))
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(
                task.evaluation_id, changed, evaluation_package, context=npc
            )
        assert error.value.code == "idempotency_conflict" and stored_rows(database) == committed
        assert (
            sm.submit_npc_evaluation_result(
                task.evaluation_id, good, evaluation_package, context=npc
            )
            == receipt
        )
        assert stored_rows(database) == committed
