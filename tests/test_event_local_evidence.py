"""Real producers, independent SQLite reads, and historical-runtime recovery."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from dnd_support import create
from perception_support import synthetic_notice
from test_conditional_world_effects import conditional
from test_dnd_runtime import attack, setup
from test_events import event_data
from test_game_time import tick_command
from test_migrations import assert_original_rows_equal, stored_rows
from test_npc_evaluation_tasks import create_task, prepare
from test_npc_evaluation_tasks import evaluation_package as evaluation_package
from test_perception_conditions import sensory_event_package
from test_real_checks import check_setup

from trpg_state_machine import (
    EnterScene,
    MoveActor,
    ObjectState,
    SetObjectState,
    SetSceneFlag,
    StateMachine,
    StateMachineError,
    TypedCommand,
    load_adventure_package,
)
from trpg_state_machine.npc_results import NPCDialogue, NPCEvaluationResult
from trpg_state_machine.package import _validate
from trpg_state_machine.perception import JudgmentResult, NPCContext, SignalJudgment

A, B = "scene.platform", "scene.workshop"
N1, N2 = "npc.caretaker", "npc.attendant"
GATE, DOOR = "object.platform_gate", "object.workshop_door"


def evidence(db, sid, seq):
    with sqlite3.connect(db) as conn:
        return json.loads(
            conn.execute(
                "SELECT evidence_json FROM event_evidence WHERE session_id=? AND event_seq=?",
                (sid, seq),
            ).fetchone()[0]
        )


def command(sm, sid, package, context, operation, kind, key):
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(sid, key, state.pc_actor_id, kind, operation, state.world_version)


def enter(sm, sid, package, context, scene, key):
    result = sm.submit_command(
        command(sm, sid, package, context, EnterScene(scene), "enter_scene", key),
        package,
        context=context,
    )
    assert result.status == "committed"
    return result


@pytest.fixture
def scoped_data(package_data):
    for actor in package_data["actors"]:
        actor["senses"] = {"visual": True, "auditory": True}
    return package_data


def test_scene_crossing_real_before_after_restart_scope_cursor_and_replay(
    tmp_path, scoped_data, context
):
    package, db = _validate(scoped_data), tmp_path / "crossing.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        cmd = command(sm, sid, package, context, EnterScene(B), "enter_scene", "cross")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed" and receipt.event_seqs == (3, 4)
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        assert sm.read_session(sid, package, context=context).current_scene_id == B
        for npc in (N1, N2):
            sm.grant_npc_access(sid, npc, npc, package, context=context)
        saved = sm.save_session(sid, package, context=context)
    before, after = evidence(db, sid, 3), evidence(db, sid, 4)
    assert before["world_version"] == after["world_version"] == 1
    assert before["phase"]["code"] == "scene.before_departure"
    assert after["phase"]["code"] == "scene.after_arrival"
    assert before["discovery"]["by_signal"] == {"signal.visible": [N1]}
    assert after["discovery"]["by_signal"] == {"signal.visible": [N2]}
    assert {a["actor_id"] for a in before["actors"]} == {N1}
    assert {a["actor_id"] for a in after["actors"]} == {N2}
    assert before["source_actors"][0]["actor_id"] == after["source_actors"][0]["actor_id"] == pc
    assert before["source_actors"][0]["current_scene_id"] == A
    assert after["source_actors"][0]["current_scene_id"] == B
    assert set(before["scenes"]) == {A} and set(after["scenes"]) == {B}
    assert before["objects"] == after["objects"] == {}
    with StateMachine(db) as sm:
        assert sm.resume_session(sid, package, context=context) == saved
        events = sm.read_events(sid, package, context=context)
        assert [e.event_id for e in events] == [f"event.{sid}.{i}" for i in range(1, 5)]
        assert [e.source_command_id for e in events[-2:]] == [cmd.command_id] * 2
        assert [e.signals[0].origin.entity_id for e in events[-2:]] == [pc, pc]
        for seq in (1, 2, 4):
            assert (
                sm.project_event(sid, seq, package, context=context, judgment=synthetic_notice)
                == "resolved"
            )
        assert sm.perception_cursor(sid, package, context=context) == 2
        # Its own entry is complete; the other NPC's exit gap is unrelated.
        assert sm.npc_perception_cursor(sid, package, context=NPCContext(N2, sid, N2)) == 4
        assert (
            sm.read_observations(sid, package, context=NPCContext(N2, sid, N2))[0]
            .perceived_signals[0]
            .content
            == "Someone enters the scene."
        )
        assert (
            sm.project_event(sid, 3, package, context=context, judgment=synthetic_notice)
            == "resolved"
        )
        assert sm.perception_cursor(sid, package, context=context) == 4
        for npc, content in ((N1, "Someone leaves the scene."), (N2, "Someone enters the scene.")):
            observations = sm.read_observations(sid, package, context=NPCContext(npc, sid, npc))
            assert observations[-1].perceived_signals[0].content == content
        for e in events:
            sm.deliver_event(sid, e.event_seq, package, context=context, sink=lambda _: None)
        snapshot = stored_rows(db)
        assert sm.submit_command(cmd, package, context=context) == receipt
        assert sm.project_event(sid, 3, package, context=context) == "already_complete"
        assert stored_rows(db) == snapshot
        assert sm.delivery_cursor(sid, package, context=context) == 4
    assert evidence(db, sid, 3) == before and evidence(db, sid, 4) == after


def test_real_cascade_object_stages_are_not_command_final_state(tmp_path, scoped_data, context):
    definition = event_data(
        effect={"kind": "set_object_state", "object_id": GATE, "state": "closed"}
    )
    definition["trigger"]["event_type"] = "object.state_changed"
    definition["signals"][0]["origin"] = {"scene_id": A, "entity_id": GATE}
    scoped_data["scenes"][0]["initial_state"]["local_flags"]["flag.lamp_checked"] = True
    scoped_data["events"] = [definition]
    scoped_data["scenes"][0]["event_definition_ids"] = ["event.open_gate"]
    package, db = _validate(scoped_data), tmp_path / "cascade.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        cmd = command(
            sm,
            sid,
            package,
            context,
            SetObjectState(A, GATE, ObjectState.OPEN),
            "set_object_state",
            "open",
        )
        result = sm.submit_command(cmd, package, context=context)
        assert result.status == "committed" and result.event_seqs == (3, 4, 5)
        events = sm.read_events(sid, package, context=context)[-3:]
        assert [e.event_type for e in events] == [
            "object.state_changed",
            "object.state_changed",
            "package.event_fired",
        ]
        assert [e.payload.get("state") for e in events[:2]] == ["open", "closed"]
        assert sm.read_session(sid, package, context=context).scenes[A].objects[GATE] == "closed"
    samples = [evidence(db, sid, i) for i in (3, 4, 5)]
    assert [e["objects"][GATE]["state"] for e in samples] == ["open", "closed", "closed"]
    assert [e["phase"]["code"] for e in samples] == [
        "world.after_object_update",
        "world.after_object_update",
        "package.after_effect",
    ]
    assert all(e["world_version"] == 1 and set(e["objects"]) == {GATE} for e in samples)


def multi_scene_package(data):
    definition = event_data()
    a = definition["signals"][0]
    a["origin"] = {"scene_id": A, "entity_id": GATE}
    other = deepcopy(a)
    other.update(
        signal_id="signal.workshop",
        content="A workshop sound.",
        scope={"kind": "scene", "scene_id": B, "visibility": "clear"},
        origin={"scene_id": B, "entity_id": DOOR},
    )
    repeated = deepcopy(a)
    repeated.update(
        signal_id="signal.second",
        content="Another platform sound.",
        origin={"scene_id": A, "entity_id": N1},
    )
    definition["signals"] += [other, repeated]
    data["events"] = [definition]
    data["scenes"][0]["event_definition_ids"] = ["event.open_gate"]
    return _validate(data)


def test_multi_scene_signal_union_source_completeness_and_private_projection(
    tmp_path, scoped_data, context
):
    package, db = multi_scene_package(scoped_data), tmp_path / "union.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        enter(sm, sid, package, context, B, "visit")
        enter(sm, sid, package, context, A, "return")
        state = sm.read_session(sid, package, context=context)
        cmd = command(
            sm,
            sid,
            package,
            context,
            SetSceneFlag(A, "flag.lamp_checked", True),
            "set_scene_flag",
            "fire",
        )
        result = sm.submit_command(cmd, package, context=context)
        seq = result.event_seqs[-1]
        sample = evidence(db, sid, seq)
        assert sample["discovery"] == {
            "complete": True,
            "reason": None,
            "by_signal": {"signal.chime": [N1], "signal.second": [N1], "signal.workshop": [N2]},
        }
        assert {a["actor_id"] for a in sample["actors"]} == {N1, N2}
        assert sample["source_actors"] == []  # Source NPC shares the observer evidence.
        assert sample["objects"] == {
            GATE: {"scene_id": A, "state": "open"},
            DOOR: {"scene_id": B, "state": "closed"},
        }
        assert set(sample["scenes"]) == {A, B}
        seen = {}

        def judgment(request):
            assert not sm._conn.in_transaction
            allowed = (
                {"signal.chime", "signal.second"}
                if request.observer_id == N1
                else {"signal.workshop"}
            )
            assert {s.signal_id for s in request.signals} == allowed
            assert set(request.evidence["environments"]) == allowed
            assert request.evidence["observer"]["actor_id"] == request.observer_id
            seen[request.observer_id] = allowed
            return JudgmentResult(tuple(SignalJudgment(s.signal_id, True) for s in request.signals))

        assert sm.project_event(sid, seq, package, context=context, judgment=judgment) == "resolved"
        assert set(seen) == {N1, N2}
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT observer_id,content_json FROM event_observations WHERE event_seq=?", (seq,)
            ).fetchall()
            assert {npc: len(json.loads(raw)) for npc, raw in rows} == {N1: 2, N2: 1}
            assert conn.execute("SELECT count(*) FROM npc_inbox").fetchone()[0] == 2
            assert conn.execute("SELECT count(*) FROM npc_observation_work").fetchone()[0] == 2
            assert (
                conn.execute("SELECT count(*) FROM observation_signal_sources").fetchone()[0] == 3
            )
        assert sm.read_session(sid, package, context=context).rng == state.rng
        saved = stored_rows(db)
        assert sm.project_event(sid, seq, package, context=context) == "already_complete"
        assert stored_rows(db) == saved


def test_discovery_includes_blinded_deafened_inactive_observer(tmp_path, package_data, context):
    package, assets = sensory_event_package(package_data, deafened=True, blinded=True)
    db = tmp_path / "conditions.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        cmd = command(
            sm,
            sid,
            package,
            context,
            SetSceneFlag(A, "flag.lamp_checked", True),
            "set_scene_flag",
            "fire",
        )
        seq = sm.submit_command(cmd, package, context=context).event_seqs[-1]
        sample = evidence(db, sid, seq)
        assert set(sample["discovery"]["by_signal"]) == {"signal.chime", "signal.activity"}
        assert all(v == [N1] for v in sample["discovery"]["by_signal"].values())
        assert sample["actors"][0]["mechanical"]["conditions"] == ["deafened", "blinded"]
        assert (
            sm._conn.execute("SELECT mode FROM npc_modes WHERE npc_id=?", (N1,)).fetchone()[0]
            == "inactive"
        )
        assert (
            sm.project_event(
                sid,
                seq,
                package,
                context=context,
                judgment=lambda _: pytest.fail("Known Conditions are not JEV input"),
            )
            == "resolved"
        )
        decisions = json.loads(
            sm._conn.execute(
                "SELECT decisions_json FROM perception_results WHERE event_seq=?", (seq,)
            ).fetchone()[0]
        )
        assert {v["reason"] for v in decisions["reasons"].values()} == {
            "engine_sight_auto_fail",
            "engine_hearing_auto_fail",
        }


def test_zero_signal_capture_does_not_sample_observers_or_environment(
    tmp_path, scoped_data, context
):
    package, db = _validate(scoped_data), tmp_path / "empty.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)

        def deny_sensory_reads(action, table, *args):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ
                and table in ("mechanical_actors", "mechanical_scenes")
                else sqlite3.SQLITE_OK
            )

        sm._conn.set_authorizer(deny_sensory_reads)
        result = sm.submit_command(
            command(
                sm,
                sid,
                package,
                context,
                SetSceneFlag(A, "flag.lamp_checked", True),
                "set_scene_flag",
                "flag",
            ),
            package,
            context=context,
        )
        sm._conn.set_authorizer(None)
        sample = evidence(db, sid, result.event_seqs[0])
        assert sample["discovery"] == {"complete": True, "by_signal": {}, "reason": None}
        assert sample["actors"] == sample["source_actors"] == []
        assert sample["scenes"] == sample["objects"] == {}
        assert (
            sm.project_event(
                sid,
                result.event_seqs[0],
                package,
                context=context,
                judgment=lambda _: pytest.fail("Empty Signals"),
            )
            == "resolved"
        )


def test_zero_candidates_is_a_proven_empty_result(tmp_path, scoped_data, context):
    scoped_data["actors"] = []
    for scene in scoped_data["scenes"]:
        scene["actor_ids"] = []
    package, db = _validate(scoped_data), tmp_path / "no_npcs.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        sample = evidence(db, sid, 2)
        assert sample["discovery"] == {
            "complete": True,
            "by_signal": {"signal.visible": []},
            "reason": None,
        }
        assert sample["actors"] == [] and len(sample["source_actors"]) == 1
        for seq in (1, 2):
            assert (
                sm.project_event(
                    sid,
                    seq,
                    package,
                    context=context,
                    judgment=lambda _: pytest.fail("Proven empty candidates"),
                )
                == "resolved"
            )
        assert sm.perception_cursor(sid, package, context=context) == 2
        assert sm._conn.execute("SELECT count(*) FROM event_observations").fetchone()[0] == 0


def test_missing_scope_scene_cannot_become_empty_or_backfilled(tmp_path, scoped_data, context):
    package, db = multi_scene_package(scoped_data), tmp_path / "missing.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        cmd = command(
            sm,
            sid,
            package,
            context,
            SetSceneFlag(A, "flag.lamp_checked", True),
            "set_scene_flag",
            "fire",
        )
        seq = sm.submit_command(cmd, package, context=context).event_seqs[-1]
        sample = evidence(db, sid, seq)
        assert sample["discovery"]["complete"] is False
        assert sample["discovery"]["reason"] == "missing_scope_scene"
        enter(sm, sid, package, context, B, "instantiate")
    with StateMachine(db) as sm:
        assert (
            sm.project_event(
                sid,
                seq,
                package,
                context=context,
                judgment=lambda _: pytest.fail("Missing historical membership"),
            )
            == "unresolved"
        )
        assert evidence(db, sid, seq) == sample
        assert (
            sm._conn.execute(
                "SELECT done FROM event_outbox WHERE event_seq=? AND consumer='perception'", (seq,)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("fault", ["marker", "candidate", "observer", "signal", "foreign"])
def test_discovery_integrity_gaps_stay_unresolved(tmp_path, scoped_data, context, fault):
    package, db = _validate(scoped_data), tmp_path / "integrity.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        sample = evidence(db, sid, 2)
        if fault == "marker":
            sample["discovery"]["complete"] = False
        elif fault == "candidate":
            sample["discovery"]["by_signal"]["signal.visible"] = []
        elif fault == "observer":
            sample["actors"] = []
        elif fault == "signal":
            sample["discovery"]["by_signal"] = {}
        else:
            sample["discovery"]["by_signal"]["signal.visible"].append(N2)
        sm._conn.execute(
            "UPDATE event_evidence SET evidence_json=? WHERE event_seq=2", (json.dumps(sample),)
        )
        assert (
            sm.project_event(
                sid,
                2,
                package,
                context=context,
                judgment=lambda _: pytest.fail("Incomplete Discovery"),
            )
            == "unresolved"
        )
        assert sm._conn.execute("SELECT count(*) FROM event_observations").fetchone()[0] == 0
        assert sm.perception_cursor(sid, package, context=context) == 0


@pytest.mark.parametrize(
    "table", ["event_evidence", "event_records", "event_outbox", "command_receipts"]
)
def test_world_stage_capture_is_atomic_with_receipt_and_outbox(
    tmp_path, scoped_data, context, table
):
    package, db = _validate(scoped_data), tmp_path / "rollback.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        cmd = command(sm, sid, package, context, EnterScene(B), "enter_scene", "cross")
        before = stored_rows(db)
        sm._conn.execute(
            f"CREATE TEMP TRIGGER fault BEFORE INSERT ON {table} "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(cmd, package, context=context)
        assert error.value.code == "storage_error" and stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER fault")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed" and receipt.event_seqs == (3, 4)
        after = stored_rows(db)
        assert sm.submit_command(cmd, package, context=context) == receipt
        assert stored_rows(db) == after


def test_real_engine_result_unknown_stage_never_uses_final_state_or_jev(
    tmp_path, package_data, context
):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    db = tmp_path / "dnd.db"
    with sm:
        cmd = attack(sm, sid, package, context)
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed"
        events = [
            e
            for e in sm.read_events(sid, package, context=context)
            if e.event_seq in receipt.event_seqs
        ]
        assert events[0].payload["engine_event"]["type"] == "intent_submitted"
        assert sum(bool(e.signals) for e in events) == 1
        sample = evidence(db, sid, events[0].event_seq)
        assert sample["phase"] == {
            "status": "unresolved",
            "code": "engine.intermediate_phase_unavailable",
        }
        assert sample["discovery"]["complete"] is False
        assert sample["actors"] == sample["source_actors"] == []
        assert sample["scenes"] == sample["objects"] == {}
        before = stored_rows(db)
        assert (
            sm.project_event(
                sid,
                events[0].event_seq,
                package,
                context=context,
                judgment=lambda _: pytest.fail("Final Delta is not stage evidence"),
            )
            == "unresolved"
        )
        after = stored_rows(db)
        assert {k: v for k, v in before.items() if k != "event_outbox"} == {
            k: v for k, v in after.items() if k != "event_outbox"
        }
        assert sm.submit_command(cmd, package, context=context) == receipt


def test_real_check_delta_rng_and_local_effects_rollback_together(tmp_path, package_data, context):
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    db = tmp_path / "checks.db"
    with sm:
        cmd = conditional(sm, sid, package, context)
        before = stored_rows(db)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fault BEFORE INSERT ON event_evidence "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(cmd, package, context=context)
        assert error.value.code == "storage_error" and stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER fault")
        result = sm.submit_command(cmd, package, context=context)
        assert result.status == "committed"
        assert [evidence(db, sid, seq)["phase"]["code"] for seq in result.event_seqs] == [
            "engine.intermediate_phase_unavailable",
            "world.after_flag_update",
            "world.after_object_update",
        ]
        saved = stored_rows(db)
        assert sm.submit_command(cmd, package, context=context) == result
        assert stored_rows(db) == saved


def test_schedule_real_order_has_empty_scoped_evidence_and_recovers(
    tmp_path, evaluation_package, context
):
    context = replace(context, can_advance_time=True)
    db = tmp_path / "schedule.db"
    with StateMachine(db) as sm:
        sid, _, _ = prepare(sm, evaluation_package, context)
        cmd = tick_command(sm, sid, evaluation_package, context, 10)
        receipt = sm.submit_command(cmd, evaluation_package, context=context)
        events = [
            e
            for e in sm.read_events(sid, evaluation_package, context=context)
            if e.event_seq in receipt.event_seqs
        ]
        assert [e.event_type for e in events] == [
            "game.time_advanced",
            "npc.schedule_active",
            "npc.schedule_active",
        ]
        assert [e.payload["npc_id"] for e in events[1:]] == [N2, N1]
        samples = [evidence(db, sid, e.event_seq) for e in events]
        assert [e["phase"]["code"] for e in samples] == [
            "clock.after_advance",
            "schedule.after_lifecycle_record",
            "schedule.after_lifecycle_record",
        ]
        assert all(
            e["game_tick"] == 10 and e["actors"] == [] and e["scenes"] == {} for e in samples
        )
        saved = sm.save_session(sid, evaluation_package, context=context)
    with StateMachine(db) as sm:
        assert sm.resume_session(sid, evaluation_package, context=context) == saved
        before = stored_rows(db)
        assert sm.submit_command(cmd, evaluation_package, context=context) == receipt
        assert stored_rows(db) == before


def test_dialogue_real_publication_captures_speaker_without_source_identity_loss(
    tmp_path, evaluation_package, context
):
    db = tmp_path / "dialogue.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        state = sm.read_session(sid, evaluation_package, context=context)
        selected = NPCEvaluationResult(
            dialogue=NPCDialogue("A local statement.", (state.pc_actor_id,), "local", ids)
        )
        sm.submit_npc_evaluation_result(
            task.evaluation_id, selected, evaluation_package, context=npc
        )
        published = sm.dispatch_npc_output(
            task.evaluation_id, 0, evaluation_package, context=context
        )
        assert published.status == "published"
        event = sm.read_events(sid, evaluation_package, context=context)[-1]
        assert (
            event.event_type == "npc.dialogue_published" and event.signals[0].origin.entity_id == N1
        )
        sample = evidence(db, sid, event.event_seq)
        assert sample["phase"]["code"] == "dialogue.at_publication"
        assert sample["discovery"]["by_signal"] == {"signal.dialogue": [N1]}
        assert sample["actors"][0]["actor_id"] == N1 and sample["source_actors"] == []
        assert set(sample["scenes"]) == {A} and sample["objects"] == {}
        before = stored_rows(db)
        assert (
            sm.dispatch_npc_output(task.evaluation_id, 0, evaluation_package, context=context)
            == published
        )
        assert stored_rows(db) == before


def test_real_move_captures_destination_without_copying_identity(tmp_path, scoped_data, context):
    package, db = _validate(scoped_data), tmp_path / "move.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        initial = evidence(db, sid, 2)
        enter(sm, sid, package, context, B, "visit")
        cmd = command(sm, sid, package, context, MoveActor(N1, B), "move_actor", "move")
        cmd = replace(cmd, actor_id=N1)
        result = sm.submit_command(cmd, package, context=context)
        assert result.status == "committed"
        moved = [
            e
            for e in sm.read_events(sid, package, context=context)
            if e.event_seq in result.event_seqs
        ]
        assert [e.event_type for e in moved] == ["actor.departed", "actor.moved"]
        assert [e.signals[0].origin.entity_id for e in moved] == [N1, N1]
        departure = evidence(db, sid, moved[0].event_seq)
        assert departure["phase"]["code"] == "world.before_actor_departure"
        assert departure["discovery"]["by_signal"] == {"signal.visible": [N1]}
        assert all(a["current_scene_id"] == A for a in departure["actors"])
        sample = evidence(db, sid, moved[1].event_seq)
        assert sample["phase"]["code"] == "world.after_actor_move"
        assert sample["discovery"]["by_signal"] == {"signal.visible": [N2, N1]}
        assert {a["actor_id"] for a in sample["actors"]} == {N1, N2}
        assert all(a["current_scene_id"] == B for a in sample["actors"])
        assert sample["source_actors"] == []
        assert evidence(db, sid, 2) == initial
        assert (
            sm._conn.execute("SELECT count(*) FROM actors WHERE actor_id=?", (N1,)).fetchone()[0]
            == 1
        )


def test_actual_per1_runtime_evidence3_is_retained_without_final_state_backfill(
    tmp_path, scoped_data, context
):
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "5deea78", "src"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
    ).stdout
    old = tmp_path / "per1"
    with zipfile.ZipFile(io.BytesIO(archive)) as files:
        files.extractall(old)
    path = tmp_path / "package.json"
    path.write_text(json.dumps(scoped_data), encoding="utf-8")
    package = load_adventure_package(path).package
    db = tmp_path / "per1.db"
    code = """
import json, sys
from trpg_state_machine import (
 StateMachine, CreateSessionRequest, PlayerSelection, TrustedContext, load_adventure_package,
)
from trpg_state_machine.perception import JudgmentResult, SignalJudgment
p=load_adventure_package(sys.argv[2]).package
c=TrustedContext('authenticated:alice','player.alice',('npc.caretaker','npc.attendant'))
def notice(r):
 return JudgmentResult(tuple(SignalJudgment(s.signal_id,True) for s in r.signals))
with StateMachine(sys.argv[1]) as sm:
 ids=[]
 for key in ('complete','pending'):
  r=CreateSessionRequest(key,p.adventure_id,p.package_version,p.package_digest,(PlayerSelection(c.player_id,'pc_template.observer'),))
  sid=sm.create_session(r,p,context=c).session_id
  ids.append(sid)
  if key=='complete':
   for seq in (1,2):
    assert sm.project_event(sid,seq,p,context=c,judgment=notice)=='resolved'
 print(json.dumps(ids))
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(db), str(path)],
        env=dict(os.environ, PYTHONPATH=str(old / "src")),
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    complete, pending = json.loads(process.stdout)
    before = stored_rows(db)
    samples = {sid: evidence(db, sid, 2) for sid in (complete, pending)}
    assert all(e["schema"] == "event-evidence/3" for e in samples.values())
    for _ in range(2):
        with StateMachine(db) as sm:
            assert sm.project_event(complete, 2, package, context=context) == "already_complete"
            assert (
                sm.project_event(
                    pending,
                    2,
                    package,
                    context=context,
                    judgment=lambda _: pytest.fail("Legacy phase is unproven"),
                )
                == "unresolved"
            )
            assert sm.perception_cursor(complete, package, context=context) == 2
            assert sm.perception_cursor(pending, package, context=context) == 0
    after = stored_rows(db)
    assert_original_rows_equal(before, after, excluded=("event_outbox",))
    with StateMachine(db) as sm:
        assert sm.project_event(pending, 1, package, context=context) == "resolved"
        enter(sm, pending, package, context, B, "new-world")
        assert evidence(db, pending, 4)["schema"] == "event-evidence/4"
        assert (
            sm.project_event(
                pending,
                2,
                package,
                context=context,
                judgment=lambda _: pytest.fail("Current world cannot repair legacy"),
            )
            == "unresolved"
        )
        assert sm.perception_cursor(pending, package, context=context) == 1
        for sid in (complete, pending):
            assert evidence(db, sid, 2) == samples[sid]
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 29
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT session_id FROM event_observations").fetchall() == [(complete,)]


@pytest.mark.parametrize("field,value", [("kind", "radius"), ("scene_id", "scene.absent")])
def test_unsupported_package_scope_never_silently_discovers_empty(
    tmp_path, scoped_data, field, value
):
    definition = event_data()
    definition["signals"][0]["scope"][field] = value
    scoped_data["events"] = [definition]
    scoped_data["scenes"][0]["event_definition_ids"] = ["event.open_gate"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(scoped_data), encoding="utf-8")
    result = load_adventure_package(path)
    assert result.status == "invalid" and result.package is None and result.errors
