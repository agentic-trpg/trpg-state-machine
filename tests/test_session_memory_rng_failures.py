"""The real Engine's RNG transition shares the durable/publication boundary."""

import sqlite3
from dataclasses import replace

import pytest
from test_dnd_ledger_scope import stored_state
from test_real_checks import check_command, check_setup

from trpg_state_machine import StateMachineError


@pytest.mark.parametrize("failure", ("before", "unknown", "publish", "after_publish"))
def test_real_rng_transition_commit_fault_and_original_receipt_replay(
    tmp_path, package_data, context, monkeypatch, failure
):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    database = tmp_path / "checks.db"
    with sm:
        operation = check_command(sm, sid, package, context, key="rng.fault")
        fixed = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        disk_before = stored_state(database)
        evaluate = sm._rules._evaluate_dnd
        evaluations = []

        async def observed(request):
            assert not sm._conn.in_transaction
            result = await evaluate(request)
            evaluations.append((request, result))
            return result

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", observed)
        commit = sm._store._commit
        publish = sm._store.memory.publish

        def fail_commit():
            if failure == "unknown":
                commit()
                raise OSError("commit acknowledgement lost")
            raise sqlite3.OperationalError("commit not attempted")

        def fail_publish(scope):
            if failure == "after_publish":
                publish(scope)
            raise MemoryError("publication acknowledgement lost")

        with monkeypatch.context() as patch:
            if failure in ("before", "unknown"):
                patch.setattr(sm._store, "_commit", fail_commit)
            else:
                patch.setattr(sm._store.memory, "publish", fail_publish)
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(operation, package, context=context)
        assert (
            error.value.code
            == {
                "before": "storage_error",
                "unknown": "commit_outcome_unknown",
                "publish": "committed_publication_pending",
                "after_publish": "committed_publication_pending",
            }[failure]
        )
        assert len(evaluations) == 1 and evaluations[0][0].state_snapshot == fixed
        proposed = evaluations[0][1]
        with sqlite3.connect(database) as disk:
            disk_rng_version = disk.execute(
                "SELECT rng_version FROM rng_states WHERE session_id=?", (sid,)
            ).fetchone()[0]
            receipts = disk.execute(
                "SELECT count(*) FROM command_receipts WHERE session_id=? AND command_id=?",
                (sid, operation.command_id),
            ).fetchone()[0]
        if failure == "before":
            assert disk_rng_version == rng.version and receipts == 0
            assert stored_state(database) == disk_before
            assert sm.read_rng_context(sid, package, context=context) == rng
        else:
            assert disk_rng_version == rng.version + 1 and receipts == 1
            with pytest.raises(StateMachineError, match="reconciliation"):
                sm.read_rng_context(sid, package, context=context)
            sm.resume_session(sid, package, context=context)
        receipt = sm.submit_command(operation, package, context=context)
        assert receipt.status == "committed"
        current_rng = sm.read_rng_context(sid, package, context=context)
        assert current_rng.version == rng.version + 1
        assert current_rng.state == proposed.rng_transition.next_state
        assert receipt.world_version == fixed.world_version + 1
        assert evaluations[0][0].state_snapshot == fixed
        assert rng != current_rng
        if failure == "before":
            assert len(evaluations) == 2
            assert evaluations[1][1].rng_transition == proposed.rng_transition
        else:
            assert len(evaluations) == 1
        disk_after = stored_state(database)
        assert sm.submit_command(operation, package, context=context) == receipt
        assert stored_state(database) == disk_after
