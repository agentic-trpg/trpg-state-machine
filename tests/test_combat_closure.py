import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from dnd5e_engine.evaluation_contracts import (
    CombatClosePayload,
    CombatIntentPayload,
    RuleEvaluationResult,
)
from dnd_support import NPC, create, initialize
from modern_support import modern_data, modern_loader
from test_combat_start import opening_data, start_command
from test_dnd_ledger_scope import stored_state
from test_dnd_npc_availability import npc_command

from trpg_state_machine import CombatOpponent, StateMachine, StateMachineError, TypedCommand
from trpg_state_machine.package import _validate


def modern_setup(tmp_path, base, context, *, tpk=False, terminal=True):
    assets = modern_loader()
    data = modern_data(base, assets, npc_hp=1 if terminal else 50)
    if tpk:
        data["pc_templates"][0]["character"]["hp"] = {"current": 1, "max": 1}
        for name in ("combat_foundation", "noncombat_foundation"):
            data["rule_bindings"][name]["snapshot"]["character_states"][0].update(
                hp_current=1, hp_max=1, ac=1
            )
        data["rule_bindings"]["combat_foundation"]["snapshot"]["combat_state"].update(
            current_turn_index=1, turn_serial=2, monster_turn_start_done=[2, NPC]
        )
    package = _validate(data)
    sm = StateMachine(tmp_path / "modern.db", dnd_loader=assets)
    sid = create(sm, package, context)
    assert initialize(sm, sid, package, context).status == "committed"
    if terminal:
        command = (
            npc_command(sm, sid, package, context) if tpk else pc_attack(sm, sid, package, context)
        )
        assert sm.submit_command(command, package, context=context).status == "committed"
    return sm, sid, package, assets, data


def pc_attack(sm, sid, package, context, key="attack.modern"):
    s = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        key,
        s.pc_actor_id,
        "combat.intent",
        CombatIntentPayload(intent_type="attack", weapon_id="rapier", target_id=NPC),
        s.world_version,
    )


def close_command(sm, sid, package, context, key="close.modern"):
    s = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        key,
        s.pc_actor_id,
        "combat.close",
        CombatClosePayload(kind="combat.close"),
        s.world_version,
    )


@pytest.mark.parametrize("tpk", [False, True])
def test_real_closure_atomic_balances_xp_replay_restart(tmp_path, package_data, context, tpk):
    sm, sid, package, assets, data = modern_setup(tmp_path, package_data, context, tpk=tpk)
    with sm:
        command = close_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        real = sm._rules._evaluate_dnd
        results = []

        async def capture(request):
            assert not sm._conn.in_transaction
            result = await real(request)
            results.append(result)
            return result

        sm._rules._evaluate_dnd = capture
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        closure = next(op for op in results[0].state_delta.operations if op.kind == "combat.close")
        assert closure.reason == ("defeat_tpk" if tpk else "victory")
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.combat_state.ended
        assert [a.model_dump(exclude={"is_alive"}) for a in before.character_states] == [
            a.model_dump(exclude={"is_alive"}) for a in after.character_states
        ]
        assert after.combat_state.deaths_recorded == before.combat_state.deaths_recorded
        assert sm.read_rng_context(sid, package, context=context) == rng
        saved = sm.save_session(sid, package, context=context)
        assert saved.phase == "exploration"
        assert saved.character.xp == 10 + closure.xp_increments.get(saved.pc_actor_id, 0)
        assert (saved.character.xp > 10) != tpk
        committed = stored_state(tmp_path / "modern.db")
        assert sm.submit_command(command, package, context=context) == receipt
        assert stored_state(tmp_path / "modern.db") == committed and len(results) == 1
        second = sm.submit_command(
            close_command(sm, sid, package, context, "close.again"), package, context=context
        )
        assert second.error_code == "combat_ended"
        assert sm.read_session(sid, package, context=context).character.xp == saved.character.xp
        assert (
            sm.read_events(sid, package, context=context)[-1].payload["engine_event"]["reason"]
            == closure.reason
        )
        data_path = tmp_path / "package.json"
        data_path.write_text(json.dumps(data), "utf-8")
    worker = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("_modern_resume_worker.py")),
            str(tmp_path / "modern.db"),
            str(data_path),
            sid,
            json.dumps(asdict(context)),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    resumed = json.loads(worker.stdout)
    assert resumed["xp"] == saved.character.xp and resumed["hp"] == saved.character.current_hp
    assert resumed["before_version"] == saved.world_version and resumed["phase"] == "exploration"
    assert resumed["after_version"] == saved.world_version + (0 if tpk else 1)
    with StateMachine(tmp_path / "modern.db", dnd_loader=assets) as restarted:
        assert restarted.resume_session(sid, package, context=context).current_scene_id == (
            "scene.platform" if tpk else "scene.workshop"
        )
        assert (
            restarted.get_command_receipt(sid, command.command_id, package, context=context)
            == receipt
        )


@pytest.mark.parametrize(
    "fault",
    [
        "combat",
        "hp",
        "temp_hp",
        "deaths",
        "resources",
        "xp_owner",
        "event",
        "missing_close",
        "duplicate",
        "unrelated_operation",
    ],
)
def test_malformed_real_closure_leaves_all_sqlite_rows_and_allows_retry(
    tmp_path, package_data, context, fault
):
    sm, sid, package, _, _ = modern_setup(tmp_path, package_data, context)
    with sm:
        command = close_command(sm, sid, package, context)
        before = stored_state(tmp_path / "modern.db")
        real = sm._rules._evaluate_dnd

        async def corrupt(request):
            result = await real(request)
            raw = result.model_dump(mode="json")
            ops = raw["state_delta"]["operations"]
            close = next(op for op in ops if op["kind"] == "combat.close")
            pc = command.actor_id
            if fault == "combat":
                close["combat_id"] = "other"
            elif fault == "hp":
                close["historical"]["residual_hp"][pc] += 1
            elif fault == "temp_hp":
                close["historical"]["residual_temp_hp"][pc] = 2
            elif fault == "deaths":
                close["historical"]["deaths"][0]["killer_id"] = NPC
            elif fault == "resources":
                close["historical"]["expended_resources"] = {pc: {"spell_slot_1": 1}}
            elif fault == "xp_owner":
                close["xp_increments"] = {NPC: 25}
            elif fault == "event":
                raw["proposed_events"][0]["reason"] = "defeat_tpk"
            elif fault == "missing_close":
                raw["state_delta"]["operations"] = []
            elif fault == "duplicate":
                ops.append(close.copy())
            elif fault == "unrelated_operation":
                ops.append(
                    {
                        "kind": "actor.hp_delta",
                        "target_id": pc,
                        "expected_hp": 8,
                        "amount": -1,
                        "resulting_hp": 7,
                    }
                )
            return RuleEvaluationResult.model_validate_json(json.dumps(raw))

        sm._rules._evaluate_dnd = corrupt
        for _ in range(2):
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(command, package, context=context)
            assert error.value.code == "invalid_rule_result"
            assert stored_state(tmp_path / "modern.db") == before
        sm._rules._evaluate_dnd = real
        assert sm.submit_command(command, package, context=context).status == "committed"


@pytest.mark.parametrize(
    "table", ["characters", "combats", "rng_states", "event_outbox", "command_receipts"]
)
def test_closure_storage_failure_rolls_back_and_retry_once(tmp_path, package_data, context, table):
    sm, sid, package, _, _ = modern_setup(tmp_path, package_data, context)
    with sm:
        command = close_command(sm, sid, package, context)
        before = stored_state(tmp_path / "modern.db")
        action = "INSERT" if table in ("event_outbox", "command_receipts") else "UPDATE"
        sm._conn.execute(
            f"CREATE TEMP TRIGGER fail BEFORE {action} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "storage_error"
        assert stored_state(tmp_path / "modern.db") == before
        sm._conn.execute("DROP TRIGGER fail")
        receipt = sm.submit_command(command, package, context=context)
        after = stored_state(tmp_path / "modern.db")
        assert sm.submit_command(command, package, context=context) == receipt
        assert stored_state(tmp_path / "modern.db") == after


def test_nonterminal_conflict_and_revoked_npc_grant(tmp_path, package_data, context):
    sm, sid, package, _, _ = modern_setup(tmp_path, package_data, context, terminal=False)
    with sm:
        command = close_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.error_code == "closure.nonterminal" and not receipt.event_seqs
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        conflict = sm.submit_command(
            replace(command, command_id="stale", expected_world_version=0), package, context=context
        )
        assert conflict.status == "conflict"
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(
                replace(command, actor_id=NPC, command_id="npc.close"),
                package,
                context=replace(context, controlled_npc_ids=()),
            )
        assert error.value.code == "unauthorized"
        with pytest.raises(StateMachineError) as error:
            sm.save_session(sid, package, context=context)
        assert error.value.code == "combat_unrestorable"


def test_dynamic_start_attack_close_restores_exploration(tmp_path, package_data, context):
    """A real public opening and attack feed the existing terminal close path."""
    assets = modern_loader()
    data = opening_data(package_data, assets)
    enemy = data["rule_bindings"]["noncombat_foundation"]["snapshot"]["character_states"][1]
    enemy.update(hp_current=1, hp_max=1)
    package = _validate(data)
    host = replace(context, can_start_combat=True)
    path = tmp_path / "dynamic-close.db"
    with StateMachine(path, dnd_loader=assets) as sm:
        sid = create(sm, package, host)
        pc = sm.read_session(sid, package, context=host).pc_actor_id
        opening = start_command(sm, sid, package, host, initiative_order=(pc, NPC))
        assert sm.submit_command(opening, package, context=host).status == "committed"
        attack = pc_attack(sm, sid, package, host)
        assert sm.submit_command(attack, package, context=host).status == "committed"
        closing = close_command(sm, sid, package, host)
        receipt = sm.submit_command(closing, package, context=host)
        assert receipt.status == "committed"
        assert sm.read_session(sid, package, context=host).phase == "exploration"
        assert sm.submit_command(closing, package, context=host) == receipt
    with StateMachine(path, dnd_loader=assets) as reopened:
        assert (
            reopened.get_command_receipt(sid, closing.command_id, package, context=host) == receipt
        )
        assert reopened.read_session(sid, package, context=host).phase == "exploration"


def test_dynamic_start_npc_kill_closes_defeat(tmp_path, package_data, context):
    """A real NPC attack can end the sole PC before the same close path runs."""
    assets = modern_loader()
    data = opening_data(package_data, assets)
    hero = data["rule_bindings"]["noncombat_foundation"]["snapshot"]["character_states"][0]
    hero.update(hp_current=1, hp_max=1, ac=1)
    data["pc_templates"][0]["character"]["hp"].update(current=1, max=1)
    package = _validate(data)
    host = replace(context, can_start_combat=True)
    path = tmp_path / "dynamic-defeat.db"
    with StateMachine(path, dnd_loader=assets) as sm:
        sid = create(sm, package, host)
        opening = start_command(sm, sid, package, host)
        assert sm.submit_command(opening, package, context=host).status == "committed"
        attack = npc_command(sm, sid, package, host)
        assert sm.submit_command(attack, package, context=host).status == "committed"
        state = sm.read_combat_snapshot(sid, package, context=host)
        pc = sm.read_session(sid, package, context=host).pc_actor_id
        assert pc in state.combat_state.dead_ids
        assert next(a for a in state.character_states if a.entity_id == pc).hp_current == 0
        closing = close_command(sm, sid, package, host)
        receipt = sm.submit_command(closing, package, context=host)
        assert receipt.status == "committed"
        assert sm.read_session(sid, package, context=host).phase == "exploration"
        assert sm.submit_command(closing, package, context=host) == receipt
    with StateMachine(path, dnd_loader=assets) as reopened:
        assert (
            reopened.get_command_receipt(sid, closing.command_id, package, context=host) == receipt
        )
        assert reopened.read_session(sid, package, context=host).phase == "exploration"


def test_dynamic_dead_enemy_and_living_zero_hp_pc_stay_nonterminal(tmp_path, package_data, context):
    """A prior enemy death cannot turn the sole dying PC into a TPK."""
    assets = modern_loader()
    data = opening_data(package_data, assets)
    snapshot = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
    hero = snapshot["character_states"][0]
    hero.update(hp_current=1, hp_max=8, ac=1)
    data["pc_templates"][0]["character"]["hp"].update(current=1, max=8)
    first_enemy = next(a for a in snapshot["character_states"] if a["entity_id"] == NPC)
    first_enemy.update(hp_current=1, hp_max=1)
    next(actor for actor in data["actors"] if actor["actor_id"] == "npc.attendant")[
        "initial_cell"
    ] = "0,1"
    package = _validate(data)
    other = "npc.attendant"
    host = replace(context, can_start_combat=True, controlled_npc_ids=(NPC, other))
    with StateMachine(tmp_path / "dynamic-dying.db", dnd_loader=assets) as sm:
        sid = create(sm, package, host)
        pc = sm.read_session(sid, package, context=host).pc_actor_id
        opening = start_command(
            sm,
            sid,
            package,
            host,
            participant_ids=(pc, NPC, other),
            initiative_order=(pc, NPC, other),
            opponent_profiles=(CombatOpponent(NPC, "bandit"), CombatOpponent(other, "bandit")),
        )
        assert sm.submit_command(opening, package, context=host).status == "committed"
        assert (
            sm.submit_command(pc_attack(sm, sid, package, host), package, context=host).status
            == "committed"
        )
        state = sm.read_combat_snapshot(sid, package, context=host)
        assert NPC in state.combat_state.dead_ids
        pc_pass = TypedCommand(
            sid,
            "pc.pass.dying",
            pc,
            "combat.intent",
            CombatIntentPayload(intent_type="pass"),
            state.world_version,
        )
        assert sm.submit_command(pc_pass, package, context=host).status == "committed"
        state = sm.read_combat_snapshot(sid, package, context=host)
        attack = TypedCommand(
            sid,
            "other.attack.dying",
            other,
            "combat.intent",
            CombatIntentPayload(
                intent_type="attack",
                source_id=other,
                stat_block_action_id="scimitar",
                target_id=pc,
            ),
            state.world_version,
        )
        attack_receipt = sm.submit_command(attack, package, context=host)
        assert attack_receipt.status == "committed", attack_receipt
        state = sm.read_combat_snapshot(sid, package, context=host)
        dying = next(a for a in state.character_states if a.entity_id == pc)
        assert dying.hp_current == 0 and dying.is_alive
        assert NPC in state.combat_state.dead_ids and other not in state.combat_state.dead_ids
        assert not any(
            event.payload.get("engine_event", {}).get("type") == "death_save_rolled"
            for event in sm.read_events(sid, package, context=host)
        )
        close = close_command(sm, sid, package, host)
        denied = sm.submit_command(close, package, context=host)
        assert denied.error_code == "closure.nonterminal"
        assert sm.read_session(sid, package, context=host).phase == "combat"
        passing = TypedCommand(
            sid,
            "other.pass.into.dying",
            other,
            "combat.intent",
            CombatIntentPayload(intent_type="pass", source_id=other),
            state.world_version,
        )
        receipt = sm.submit_command(passing, package, context=host)
        assert receipt.status == "committed", receipt
        rolled = [
            event
            for event in sm.read_events(sid, package, context=host)
            if event.payload.get("engine_event", {}).get("type") == "death_save_rolled"
        ]
        assert len(rolled) == 1
        assert sm.submit_command(passing, package, context=host) == receipt
