"""Cross-phase real Engine integration; each checkpoint reads independent SQLite."""

import asyncio
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationResult
from dnd_support import NPC, create, initialize
from modern_support import modern_data, modern_loader, modern_package
from perception_support import synthetic_notice
from test_combat_closure import close_command, modern_setup, pc_attack
from test_dnd_ledger_scope import stored_state
from test_dnd_npc_availability import npc_command
from test_real_checks import check_command

from trpg_state_machine import (
    EnterScene,
    SetSceneFlag,
    StateMachine,
    StateMachineError,
    TypedCommand,
)
from trpg_state_machine.package import _validate
from trpg_state_machine.perception import NPCContext


def audit(database, sid):
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        version, end = db.execute(
            "SELECT world_version,event_seq FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()
        rows = db.execute(
            "SELECT event_seq,world_version FROM events WHERE session_id=? ORDER BY event_seq",
            (sid,),
        ).fetchall()
        assert [seq for seq, _ in rows] == list(range(1, end + 1))
        assert [v for _, v in rows] == sorted(v for _, v in rows)
        assert all(v <= version for _, v in rows)
        for table in ("event_records", "event_evidence"):
            assert (
                db.execute(f"SELECT count(*) FROM {table} WHERE session_id=?", (sid,)).fetchone()[0]
                == end
            )
        assert (
            db.execute("SELECT count(*) FROM event_outbox WHERE session_id=?", (sid,)).fetchone()[0]
            == 2 * end
        )
        for (raw,) in db.execute(
            "SELECT state_json FROM mechanical_actors WHERE session_id=?", (sid,)
        ):
            assert not {"hp_current", "hp_max", "carried_item_slugs", "xp"} & json.loads(raw).keys()
        return version, end


def test_complete_real_cross_phase_pipeline_and_new_process(tmp_path, package_data, context):
    context = replace(context, can_adjudicate_checks=True)
    assets = modern_loader()
    data = modern_data(package_data, assets)
    data["actors"][1]["senses"] = {"visual": True, "auditory": True}
    for name in ("combat_foundation", "noncombat_foundation"):
        pc = data["rule_bindings"][name]["snapshot"]["character_states"][0]
        pc.update(
            spell_slots={"1": 2}, custom_counters={"original_supplies": {"current": 3, "max": 3}}
        )
    data["rule_bindings"]["combat_foundation"]["snapshot"]["combat_state"].update(
        current_turn_index=1, turn_serial=2, monster_turn_start_done=[2, NPC]
    )
    package = _validate(data)
    database = tmp_path / "pipeline.db"
    with StateMachine(database, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        other = create(sm, package, context, "create.isolated")
        initial = sm.read_noncombat_snapshot(sid, package, context=context)
        other_state = sm.read_session(other, package, context=context)
        checkpoints = [audit(database, sid)]
        # Explore and revisit before combat; no mechanical facts are reloaded.
        for key, scene in [("explore", "scene.workshop"), ("return", "scene.platform")]:
            state = sm.read_session(sid, package, context=context)
            assert (
                sm.submit_command(
                    TypedCommand(
                        sid,
                        key,
                        state.pc_actor_id,
                        "enter_scene",
                        EnterScene(scene),
                        state.world_version,
                    ),
                    package,
                    context=context,
                ).status
                == "committed"
            )
            checkpoints.append(audit(database, sid))
        check = check_command(sm, sid, package, context)
        assert sm.submit_command(check, package, context=context).status == "committed"
        checkpoints.append(audit(database, sid))
        rng = sm.read_rng_context(sid, package, context=context)
        assert rng.version == 1
        s = sm.read_session(sid, package, context=context)
        world = TypedCommand(
            sid,
            "world.lamp",
            s.pc_actor_id,
            "set_scene_flag",
            SetSceneFlag("scene.platform", "flag.lamp_checked", True),
            s.world_version,
        )
        assert sm.submit_command(world, package, context=context).status == "committed"
        assert sm.read_rng_context(sid, package, context=context) == rng
        checkpoints.append(audit(database, sid))
        prior = sm.read_noncombat_snapshot(sid, package, context=context)
        assert initialize(sm, sid, package, context).status == "committed"
        combat = sm.read_combat_snapshot(sid, package, context=context)
        assert {a.entity_id: a for a in combat.character_states} == {
            a.entity_id: a for a in prior.character_states
        }
        assert sm.read_rng_context(sid, package, context=context) == rng
        checkpoints.append(audit(database, sid))
        assert (
            sm.submit_command(
                npc_command(sm, sid, package, context), package, context=context
            ).status
            == "committed"
        )
        checkpoints.append(audit(database, sid))
        assert (
            sm.submit_command(pc_attack(sm, sid, package, context), package, context=context).status
            == "committed"
        )
        checkpoints.append(audit(database, sid))
        before_close = sm.read_rng_context(sid, package, context=context)
        close = close_command(sm, sid, package, context)
        receipt = sm.submit_command(close, package, context=context)
        assert receipt.status == "committed"
        checkpoints.append(audit(database, sid))
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        pc = next(a for a in after.character_states if a.entity_id == close.actor_id)
        initial_pc = next(a for a in initial.character_states if a.entity_type == "Character")
        assert pc.spell_slots == initial_pc.spell_slots == {1: 2}
        assert pc.custom_counters == initial_pc.custom_counters
        assert pc.hp_current < initial_pc.hp_current and pc.hp_current > 0
        assert (pc.weapon_in_hands, pc.weapon_grip, pc.other_hand_occupied) == (
            "rapier",
            "one_handed",
            False,
        )
        assert pc.carried_item_slugs == initial_pc.carried_item_slugs
        assert sm.read_rng_context(sid, package, context=context) == before_close
        assert before_close.version == 3
        saved = sm.save_session(sid, package, context=context)
        assert saved.character.xp > 10 and saved.phase == "exploration"
        assert saved.scenes["scene.platform"].local_flags["flag.lamp_checked"]
        sm.grant_npc_access(sid, NPC, "agent:caretaker", package, context=context)
        for event in sm.read_events(sid, package, context=context):
            # The workshop NPC has no mechanical foundation. Its Scene events
            # cannot complete by asking Host to invent historical senses/state.
            # Engine Results expose no intermediate attack stages, for either
            # nonlethal or lethal attacks. Final state cannot supply that history.
            expected = (
                "unresolved"
                if event.event_seq in (4, 5)
                or (event.event_type == "combat.engine_event" and event.signals)
                else "resolved"
            )
            assert (
                sm.project_event(
                    sid, event.event_seq, package, context=context, judgment=synthetic_notice
                )
                == expected
            )
        observed = sm.read_observations(
            sid, package, context=NPCContext("agent:caretaker", sid, NPC)
        )
        assert all("attack" not in s.content for o in observed for s in o.perceived_signals)
        assert sm.perception_cursor(sid, package, context=context) == 3
        contents = [
            s["content"]
            for (raw,) in sm._conn.execute(
                "SELECT content_json FROM event_observations WHERE session_id=? AND observer_id=?",
                (sid, NPC),
            )
            for s in json.loads(raw)
        ]  # Later correctly projected records are retained behind the gap.
        assert "Someone makes an attack." not in contents
        attack_events = [
            e
            for e in sm.read_events(sid, package, context=context)
            if e.event_type == "combat.engine_event" and e.signals
        ]
        assert len(attack_events) == 2
        for event in attack_events:
            evidence = json.loads(
                sm._conn.execute(
                    "SELECT evidence_json FROM event_evidence WHERE session_id=? AND event_seq=?",
                    (sid, event.event_seq),
                ).fetchone()[0]
            )
            assert evidence["phase"] == {
                "status": "unresolved",
                "code": "engine.intermediate_phase_unavailable",
            }
            assert (
                sm._conn.execute(
                    "SELECT 1 FROM perception_results WHERE session_id=? AND event_seq=?",
                    (sid, event.event_seq),
                ).fetchone()
                is None
            )
        assert all(
            "roll" not in content.lower() and "xp" not in content.lower() for content in contents
        )
        before_replay = stored_state(database)
        assert sm.submit_command(close, package, context=context) == receipt
        assert stored_state(database) == before_replay
        assert sm.read_session(other, package, context=context) == other_state
        assert [v for v, _ in checkpoints] == list(range(len(checkpoints)))
        assert all(b[1] > a[1] for a, b in zip(checkpoints, checkpoints[1:], strict=False))
        assert (
            sm._conn.execute(
                "SELECT COUNT(*) FROM command_receipts WHERE session_id=?", (sid,)
            ).fetchone()[0]
            == len(checkpoints) - 1
        )
        (tmp_path / "package.json").write_text(json.dumps(data), "utf-8")
    output = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("_cross_phase_worker.py")),
            str(database),
            str(tmp_path / "package.json"),
            sid,
            json.dumps(asdict(context)),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(output.stdout)
    assert report["before_rng"] == 3 and report["after_rng"] == 4
    assert report["xp"] == saved.character.xp and report["hp"] == saved.character.current_hp
    assert report["before_world"] == saved.world_version
    final_version, _ = audit(database, sid)
    assert final_version == saved.world_version + 3
    with StateMachine(database, dnd_loader=assets) as reopened:
        assert reopened.resume_session(other, package, context=context) == other_state
        assert (
            reopened.get_command_receipt(sid, close.command_id, package, context=context) == receipt
        )


@pytest.mark.parametrize(
    "weapon,grip,occupied",
    [
        ("rapier", "one_handed", False),
        ("rapier", "one_handed", True),
        ("dagger", "one_handed", True),
        ("longsword", "one_handed", False),
        ("longsword", "two_handed", False),
        ("greatsword", "two_handed", False),
    ],
)
def test_real_weapon_input_output_persisted_parity(
    tmp_path, package_data, context, weapon, grip, occupied
):
    assets = modern_loader()
    data = modern_data(package_data, assets, npc_hp=40, weapon=weapon, grip=grip)
    for name in ("combat_foundation", "noncombat_foundation"):
        data["rule_bindings"][name]["snapshot"]["character_states"][0]["other_hand_occupied"] = (
            occupied
        )
    package = _validate(data)
    database = tmp_path / "weapons.db"
    with StateMachine(database, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        assert initialize(sm, sid, package, context).status == "committed"
        state = sm.read_session(sid, package, context=context)
        command = TypedCommand(
            sid,
            "weapon.attack",
            state.pc_actor_id,
            "combat.intent",
            CombatIntentPayload(
                intent_type="attack",
                weapon_id=weapon,
                target_id=NPC,
                two_handed=grip == "two_handed",
            ),
            state.world_version,
        )
        real = sm._rules._evaluate_dnd
        proposals = []

        async def capture(request):
            actor = next(
                a
                for a in request.state_snapshot.character_states
                if a.entity_id == command.actor_id
            )
            assert (actor.weapon_in_hands, actor.weapon_grip, actor.other_hand_occupied) == (
                weapon,
                grip,
                occupied,
            )
            result = await real(request)
            proposals.append(result)
            return result

        sm._rules._evaluate_dnd = capture
        before = stored_state(database)
        receipt = sm.submit_command(command, package, context=context)
        if weapon == "dagger":
            assert receipt.status == "rejected" and proposals[0].status == "unsupported"
            after = stored_state(database)
            assert {t: v for t, v in before.items() if t != "command_receipts"} == {
                t: v for t, v in after.items() if t != "command_receipts"
            }
            return
        assert receipt.status == "committed", proposals[0].error
        events = [
            e.payload["engine_event"]
            for e in sm.read_events(sid, package, context=context)
            if e.source_command_id == command.command_id
        ]
        assert events == [e.model_dump(mode="json") for e in proposals[0].proposed_events]
        after = sm.read_combat_snapshot(sid, package, context=context)
        target = next(a for a in after.character_states if a.entity_id == NPC)
        hp = next(
            op.resulting_hp
            for op in proposals[0].state_delta.operations
            if op.kind == "actor.hp_delta"
        )
        assert target.hp_current == hp
        audit(database, sid)


@pytest.mark.parametrize("case", ["not_held", "mastery", "resource_check"])
def test_unadmitted_weapon_or_resource_operation_has_no_effect(
    tmp_path, package_data, context, case
):
    context = replace(context, can_adjudicate_checks=True)
    assets = modern_loader()
    data = modern_data(package_data, assets)
    if case == "mastery":
        for name in ("combat_foundation", "noncombat_foundation"):
            data["rule_bindings"][name]["snapshot"]["character_states"][0][
                "weapon_mastery_slugs"
            ] = ["rapier"]
        with pytest.raises(StateMachineError) as error:
            _validate(data)
        assert error.value.code == "unsupported_combat_foundation"
        return
    package = _validate(data)
    database = tmp_path / "unsupported.db"
    with StateMachine(database, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        if case == "resource_check":
            command = check_command(
                sm, sid, package, context, redeem_granted_die="feature_grant:bardic-inspiration"
            )
        else:
            assert initialize(sm, sid, package, context).status == "committed"
            command = pc_attack(sm, sid, package, context)
            if case == "not_held":
                command = replace(
                    command, payload=command.payload.model_copy(update={"weapon_id": "longsword"})
                )
        before = stored_state(database)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "rejected" and not receipt.event_seqs
        after = stored_state(database)
        assert {t: v for t, v in before.items() if t != "command_receipts"} == {
            t: v for t, v in after.items() if t != "command_receipts"
        }
        assert sm.submit_command(command, package, context=context) == receipt
        assert stored_state(database) == after


@pytest.mark.parametrize("amount", [2**63, 2**63 - 5])
def test_closure_xp_storage_bounds_reject_without_partial_commit(
    tmp_path, package_data, context, amount
):
    sm, sid, package, _, _ = modern_setup(tmp_path, package_data, context)
    with sm:
        command = close_command(sm, sid, package, context)
        real = sm._rules._evaluate_dnd

        async def overflow(request):
            raw = (await real(request)).model_dump(mode="json")
            op = next(op for op in raw["state_delta"]["operations"] if op["kind"] == "combat.close")
            op["xp_awarded"] = {command.actor_id: amount}
            return RuleEvaluationResult.model_validate_json(json.dumps(raw))

        sm._rules._evaluate_dnd = overflow
        before = stored_state(tmp_path / "modern.db")
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "invalid_rule_result"
        assert stored_state(tmp_path / "modern.db") == before
        sm._rules._evaluate_dnd = real
        assert sm.submit_command(command, package, context=context).status == "committed"


@pytest.mark.parametrize("kind", ["rules.check", "combat.close"])
def test_response_loss_after_commit_replays_without_second_evaluation(
    tmp_path, package_data, context, kind
):
    context = replace(context, can_adjudicate_checks=True)
    if kind == "combat.close":
        sm, sid, package, _, _ = modern_setup(tmp_path, package_data, context)
        command = close_command(sm, sid, package, context)
        database = tmp_path / "modern.db"
    else:
        assets = modern_loader()
        package = modern_package(package_data, assets)
        database = tmp_path / "loss.db"
        sm = StateMachine(database, dnd_loader=assets)
        sid = create(sm, package, context)
        command = check_command(sm, sid, package, context, skill=None)
    with sm:
        real = sm._rules._evaluate_dnd
        calls = []

        async def capture(request):
            calls.append(request.command_id)
            return await real(request)

        sm._rules._evaluate_dnd = capture

        async def lost_response():
            await sm.submit_command_async(command, package, context=context)
            raise ConnectionError("response dropped after durable commit")

        with pytest.raises(ConnectionError):
            asyncio.run(lost_response())
        committed = stored_state(database)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed" and calls == [command.command_id]
        assert stored_state(database) == committed


def test_documented_example_runs_real_engine_and_persists_database(tmp_path):
    directory = tmp_path / "example"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "examples/real_cross_phase.py"),
            "--directory",
            str(directory),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    output = json.loads(result.stdout)
    assert output["phase"] == "exploration" and output["xp"] > 10
    assert output["rng_version"] == 4 and 0 < output["hp"] < 8
    assert audit(directory / "session.db", output["session_id"])[0] == output["world_version"]
    with closing(sqlite3.connect(directory / "session.db")) as db:
        assert db.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 9
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE event_type='rules.check_rolled'"
            ).fetchone()[0]
            == 2
        )
