import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Barrier

import pytest
from dnd5e_engine.evaluation import evaluate
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_projection import attack_delta
from dnd5e_engine.evaluation_ruleset import RulesetBinding
from dnd_support import NPC, WEAPON, create, initialize, loader
from dnd_support import package_data as dnd_package_data

from trpg_state_machine import StateMachine, StateMachineError, TypedCommand


def attack(sm, sid, package, context, key="attack.pc"):
    s = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        key,
        s.pc_actor_id,
        "combat.intent",
        CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id=NPC),
        s.world_version,
    )


def authority(sm, sid):
    return {
        table: tuple(
            tuple(r)
            for r in sm._conn.execute(
                f"SELECT * FROM {table} WHERE session_id=? ORDER BY 1,2", (sid,)
            )
        )
        for table in (
            "sessions",
            "characters",
            "mechanical_actors",
            "combats",
            "rng_states",
            "events",
            "event_records",
            "event_outbox",
            "command_receipts",
        )
    }


def setup(tmp_path, base, context, change=None):
    assets = loader()
    data = dnd_package_data(base, assets)
    if change:
        change(data["rule_bindings"]["combat_foundation"])
    from trpg_state_machine.package import _validate

    package = _validate(data)
    sm = StateMachine(tmp_path / "dnd.db", dnd_loader=assets)
    sid = create(sm, package, context)
    assert initialize(sm, sid, package, context).status == "committed"
    return sm, sid, package, assets


@pytest.mark.parametrize(
    "ac,resistance,temp_hp",
    [
        (10, None, 0),
        (99, None, 0),
        (10, "resistance", 3),
        (10, "immunity", 0),
        (10, "vulnerability", 0),
    ],
)
def test_real_attack_full_delta_rng_and_database_parity(
    tmp_path, package_data, context, ac, resistance, temp_hp
):
    def change(data):
        npc = data["snapshot"]["character_states"][1]
        npc.update(ac=ac, temp_hp=temp_hp)
        if resistance:
            npc[
                {
                    "resistance": "damage_resistances",
                    "immunity": "damage_immunities",
                    "vulnerability": "damage_vulnerabilities",
                }[resistance]
            ] = ["slashing"]

    sm, sid, package, assets = setup(tmp_path, package_data, context, change)
    with sm:
        command = attack(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        request = RuleEvaluationRequest(
            schema_version="engine-evaluation/1",
            session_id=sid,
            command_id=command.command_id,
            operation_kind="combat.intent",
            actor_id=command.actor_id,
            payload=command.payload,
            state_snapshot=before,
            ruleset_binding=RulesetBinding(**asdict(package.ruleset_binding)),
            rng_context=rng,
        )
        expected = asyncio.run(evaluate(request, loader=assets))
        assert expected.status == "accepted"
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        comparison = after.model_copy(update={"world_version": before.world_version})
        assert attack_delta(before, comparison) == expected.state_delta
        assert (
            comparison.combat_state.event_log
            == before.combat_state.event_log + expected.proposed_events
        )
        assert (
            sm.read_rng_context(sid, package, context=context).state
            == expected.rng_transition.next_state
        )
        assert after.world_version == before.world_version + 1
        saved = authority(sm, sid)
        assert sm.submit_command(command, package, context=context) == receipt
        assert authority(sm, sid) == saved
    with StateMachine(tmp_path / "dnd.db", dnd_loader=assets) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after
        assert (
            reopened.get_command_receipt(sid, command.command_id, package, context=context)
            == receipt
        )
        assert reopened.submit_command(command, package, context=context) == receipt
        assert authority(reopened, sid) == saved
    with sqlite3.connect(tmp_path / "dnd.db") as conn:
        hp = conn.execute(
            "SELECT current_hp FROM characters WHERE session_id=? AND actor_id=?", (sid, NPC)
        ).fetchone()[0]
        assert hp == after.character_states[1].hp_current


@pytest.mark.parametrize(
    "intent,code",
    [
        (CombatIntentPayload(intent_type="attack", target_id=NPC), "pending_choice_unsupported"),
        (
            CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id="npc.absent"),
            "target_invalid",
        ),
        (CombatIntentPayload(intent_type="dash"), "intent.capability"),
    ],
)
def test_refusals_do_not_change_rng_or_facts(tmp_path, package_data, context, intent, code):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        command = replace(attack(sm, sid, package, context), payload=intent)
        before = authority(sm, sid)
        calls = []
        original = sm._rules._evaluate_dnd

        async def tracked(request):
            calls.append(request)
            assert not sm._conn.in_transaction
            return await original(request)

        sm._rules._evaluate_dnd = tracked
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", code)
        after = authority(sm, sid)
        assert {k: v for k, v in before.items() if k != "command_receipts"} == {
            k: v for k, v in after.items() if k != "command_receipts"
        }
        assert len(calls) == (0 if code == "pending_choice_unsupported" else 1)
        assert sm.submit_command(command, package, context=context) == receipt
        assert authority(sm, sid) == after


@pytest.mark.parametrize(
    "fault", ["exception", "binding", "rng", "read_set", "delta", "sql", "receipt_sql"]
)
def test_nonbusiness_fault_has_no_receipt_and_original_request_can_retry(
    tmp_path, package_data, context, fault
):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        command = attack(sm, sid, package, context)
        before = authority(sm, sid)
        original = sm._rules._evaluate_dnd

        async def faulty(request):
            assert not sm._conn.in_transaction
            if fault == "exception":
                raise RuntimeError("injected")
            result = await original(request)
            if fault == "binding":
                return result.model_copy(
                    update={
                        "input_ruleset_binding": request.ruleset_binding.model_copy(
                            update={"evaluator_version": "wrong"}
                        )
                    }
                )
            if fault == "rng":
                return result.model_copy(
                    update={
                        "rng_transition": result.rng_transition.model_copy(
                            update={"input_version": 999}
                        )
                    }
                )
            if fault == "read_set":
                return result.model_copy(update={"read_set": ()})
            if fault == "delta":
                operations = list(result.state_delta.operations)
                operations[0] = operations[0].model_copy(update={"target_id": "npc.not_present"})
                return result.model_copy(
                    update={
                        "state_delta": result.state_delta.model_copy(
                            update={"operations": tuple(operations)}
                        )
                    }
                )
            return result

        sm._rules._evaluate_dnd = faulty
        if fault in ("sql", "receipt_sql"):
            table = "events" if fault == "sql" else "command_receipts"
            sm._conn.execute(
                f"CREATE TEMP TRIGGER fail BEFORE INSERT ON {table} "
                "BEGIN SELECT RAISE(ABORT,'fault'); END"
            )
        with pytest.raises(StateMachineError) as caught:
            sm.submit_command(command, package, context=context)
        assert caught.value.code in {
            "rule_evaluation_failed",
            "invalid_rule_result",
            "storage_error",
        }
        assert authority(sm, sid) == before
        sm._rules._evaluate_dnd = original
        if fault in ("sql", "receipt_sql"):
            sm._conn.execute("DROP TRIGGER fail")
        assert sm.submit_command(command, package, context=context).status == "committed"


def test_async_submission_and_sync_loop_guard(tmp_path, package_data, context):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        command = attack(sm, sid, package, context)

        async def run():
            with pytest.raises(StateMachineError) as caught:
                sm.submit_command(command, package, context=context)
            assert caught.value.code == "async_context_required"
            return await sm.submit_command_async(command, package, context=context)

        assert asyncio.run(run()).status == "committed"


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_same_command_commits_once(tmp_path, package_data, context, same_key):
    sm, sid, package, assets = setup(tmp_path, package_data, context)
    command = attack(sm, sid, package, context)
    before = sm.read_rng_context(sid, package, context=context)
    sm.close()
    barrier = Barrier(2)

    def worker(index):
        with StateMachine(tmp_path / "dnd.db", dnd_loader=assets) as instance:
            original = instance._rules._evaluate_dnd

            async def evaluated(request):
                result = await original(request)
                barrier.wait(timeout=15)
                return result

            instance._rules._evaluate_dnd = evaluated
            selected = command if same_key else replace(command, command_id=f"attack.race.{index}")
            return instance.submit_command(selected, package, context=context)

    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(worker, range(2)))
    if same_key:
        assert results[0] == results[1] and results[0].status == "committed"
    else:
        assert sorted(r.status for r in results) == ["committed", "conflict"]
    with StateMachine(tmp_path / "dnd.db", dnd_loader=assets) as reopened:
        assert (
            reopened.read_rng_context(sid, package, context=context).version == before.version + 1
        )
        assert reopened._conn.execute(
            "SELECT count(*) FROM command_receipts WHERE command_id LIKE 'attack.%'"
        ).fetchone()[0] == (1 if same_key else 2)


def test_real_binding_configuration_failure_can_retry(tmp_path, package_data, context):
    from dnd5e_srd_data import MemoryAssetLoader

    sm, sid, package, assets = setup(tmp_path, package_data, context)
    with sm:
        command = attack(sm, sid, package, context)
        before = authority(sm, sid)
        sm._rules.loader = MemoryAssetLoader()
        with pytest.raises(StateMachineError) as caught:
            sm.submit_command(command, package, context=context)
        assert caught.value.code == "ruleset_binding_mismatch"
        assert authority(sm, sid) == before
        sm._rules.loader = assets
        assert sm.submit_command(command, package, context=context).status == "committed"


def test_lethal_attack_commits_complete_death_state(tmp_path, package_data, context):
    def change(data):
        data["snapshot"]["character_states"][1].update(hp_current=1, hp_max=1)

    sm, sid, package, _ = setup(tmp_path, package_data, context, change)
    with sm:
        receipt = sm.submit_command(attack(sm, sid, package, context), package, context=context)
        assert receipt.status == "committed"
        snapshot = sm.read_combat_snapshot(sid, package, context=context)
        assert snapshot.character_states[1].hp_current == 0
        assert NPC in snapshot.combat_state.dead_ids
        assert snapshot.combat_state.deaths_recorded[0].target_id == NPC
        # The pinned Engine records monster death in dead_ids, retaining is_alive.
        # SM must apply its actual Delta rather than invent another death formula.
        assert snapshot.character_states[1].is_alive
        assert not snapshot.combat_state.ended
        with pytest.raises(StateMachineError) as caught:
            sm.save_session(sid, package, context=context)
        assert caught.value.code == "combat_unrestorable"


def test_actor_spoofing_and_request_fingerprint_conflict(tmp_path, package_data, context):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        command = attack(sm, sid, package, context)
        with pytest.raises(StateMachineError) as caught:
            sm.submit_command(
                replace(command, payload=command.payload.model_copy(update={"source_id": NPC})),
                package,
                context=context,
            )
        assert caught.value.code == "invalid_actor"
        assert sm.submit_command(command, package, context=context).status == "committed"
        with pytest.raises(StateMachineError) as caught:
            sm.submit_command(
                replace(
                    command, payload=command.payload.model_copy(update={"target_id": "npc.absent"})
                ),
                package,
                context=context,
            )
        assert caught.value.code == "idempotency_conflict"
