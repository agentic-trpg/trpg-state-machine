"""GC14 review fixes: real public commands, frozen stages and SQLite comparisons."""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import yaml
from navigation_support import navigation_data
from perception_support import synthetic_notice
from spatial_support import PASSAGE, A, B, command, create, submit
from test_events import event_data
from test_migrations import stored_rows

from trpg_state_machine import (
    AdvanceNavigation,
    EnterScene,
    MoveActor,
    ResumeNavigation,
    SetSceneFlag,
    StartNavigation,
    StateMachine,
    StateMachineError,
    StepActor,
    TraversePassage,
    load_adventure_package,
    pump_navigation,
)
from trpg_state_machine.perception import JudgmentResult, NPCContext, SignalJudgment

MOVER, SOURCE, DEST = "npc.messenger", "npc.caretaker", "npc.attendant"


def departure_data():
    data = navigation_data()
    for pc in data["pc_templates"]:
        pc["initial_cell"] = "0,0"
    data["scenes"][0]["objects"][0]["state"] = "open"
    mover = deepcopy(next(a for a in data["actors"] if a["actor_id"] == SOURCE))
    mover.update(actor_id=MOVER, initial_cell="2,1")
    mover["npc_state"]["initial_knowledge_ids"] = []
    for field in ("memories", "beliefs", "relationships", "goals", "plans"):
        mover["npc_state"][field] = []
    data["actors"].append(mover)
    data["scenes"][0]["actor_ids"].append(MOVER)
    data["navigation"]["actor_grants"].append({"actor_id": MOVER, "region_ids": ["region.public"]})
    return data


def setup(tmp_path, context, data=None):
    document = tmp_path / "package.json"
    document.write_text(json.dumps(data or departure_data()), "utf-8")
    loaded = load_adventure_package(document)
    assert loaded.status == "valid", loaded.errors
    package = loaded.package
    context = replace(context, controlled_npc_ids=(*context.controlled_npc_ids, MOVER))
    db = tmp_path / "review.db"
    sm = StateMachine(db)
    sid = create(sm, package, context)
    for npc in (SOURCE, DEST, MOVER):
        sm.grant_npc_access(sid, npc, npc, package, context=context)
    return sm, sid, package, db, context


def evidence(sm, sid, seq):
    return json.loads(
        sm._conn.execute(
            "SELECT evidence_json FROM event_evidence WHERE session_id=? AND event_seq=?",
            (sid, seq),
        ).fetchone()[0]
    )


def events_for(sm, sid, package, context, receipt):
    return tuple(
        e
        for e in sm.read_events(sid, package, context=context)
        if e.event_seq in receipt.event_seqs
    )


def test_rev01_flag_query_is_explicitly_unsupported_and_has_no_side_effects(tmp_path, context):
    sm, sid, package, db, context = setup(tmp_path, context)
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as exc:
            sm.query_world_action(
                sid, pc, SetSceneFlag(A, "flag.lamp_checked", True), package, context=context
            )
        assert exc.value.code == "unsupported_operation"
        assert stored_rows(db) == before


def test_rev02_npc_crossing_captures_departure_then_arrival_in_actual_scenes(tmp_path, context):
    sm, sid, package, db, context = setup(tmp_path, context)
    with sm:
        before = sm.read_session(sid, package, context=context)
        receipt = submit(sm, sid, package, context, MoveActor(MOVER, B), "cross", MOVER)
        assert receipt.status == "committed", receipt
        events = events_for(sm, sid, package, context, receipt)
        assert [e.event_type for e in events] == ["actor.departed", "actor.moved"]
        assert [e.source_command_id for e in events] == ["cross", "cross"]
        departure, arrival = (evidence(sm, sid, e.event_seq) for e in events)
        assert departure["phase"]["code"] == "world.before_actor_departure"
        assert arrival["phase"]["code"] == "world.after_actor_move"
        assert departure["discovery"]["by_signal"] == {"signal.visible": [SOURCE, MOVER]}
        assert arrival["discovery"]["by_signal"] == {"signal.visible": [DEST, MOVER]}
        for event, frozen, scene, cell in zip(
            events, (departure, arrival), (A, B), ("2,1", "1,1"), strict=True
        ):
            source = next(
                a for a in [*frozen["actors"], *frozen["source_actors"]] if a["actor_id"] == MOVER
            )
            assert (source["current_scene_id"], source["cell"]) == (scene, cell)
            assert (
                source["mechanical"] is None
            )  # Synthetic profile has explicit spatial facts only.
            assert set(frozen["scenes"]) == {scene}
            assert event.signals[0].origin.entity_id == MOVER
            assert event.signals[0].scope.scene_id == scene
        assert B not in json.dumps(asdict(events[0].signals[0]))
        after = sm.read_session(sid, package, context=context)
        assert after.current_scene_id == before.current_scene_id == A
        assert after.scenes[A].visited_count == before.scenes[A].visited_count
        assert after.scenes[B].visited_count == before.scenes[B].visited_count
        assert after.rng == before.rng
        assert after.actors[MOVER].current_scene_id == B
        assert stored_rows(db)["event_evidence"][1]


@pytest.mark.parametrize("entry", ["move", "passage", "navigation", "compatibility"])
def test_all_npc_crossing_entries_share_stages_receipt_and_queue_proofs(tmp_path, context, entry):
    sm, sid, p, db, context = setup(tmp_path, context)
    with sm:
        before = sm.read_session(sid, p, context=context)
        if entry == "navigation":
            assert (
                submit(
                    sm, sid, p, context, StartNavigation("activity.cross", B, "1,1"), "start", MOVER
                ).status
                == "committed"
            )
            pumped = pump_navigation(
                sm.submit_command,
                sm.read_navigation_activity,
                sm.get_command_receipt,
                sid,
                MOVER,
                "activity.cross",
                p,
                context=context,
                request_id="pump",
                budget=8,
            )
            assert pumped.activity.status == "completed" and pumped.activity.cursor == 1
            receipt = next(
                r
                for r in pumped.receipts
                if any(e.event_type == "actor.departed" for e in events_for(sm, sid, p, context, r))
            )
        elif entry == "compatibility":
            sm._apply(
                sid,
                p,
                MoveActor(MOVER, B),
                expected_world_version=before.world_version,
                context=context,
            )
            moved = next(
                e
                for e in sm.read_events(sid, p, context=context, after_seq=before.event_seq)
                if e.event_type == "actor.departed"
            )
            receipt = sm.get_command_receipt(sid, moved.source_command_id, p, context=context)
        else:
            operation = (
                MoveActor(MOVER, B) if entry == "move" else TraversePassage(PASSAGE, A, "2,1")
            )
            cmd = command(sm, sid, p, context, operation, "cross", MOVER)
            receipt = sm.submit_command(cmd, p, context=context)
            saved = stored_rows(db)
            assert sm.submit_command(cmd, p, context=context) == receipt
            assert stored_rows(db) == saved
        assert receipt.status == "committed"
        physical = [e for e in events_for(sm, sid, p, context, receipt) if e.signals]
        assert [e.event_type for e in physical] == ["actor.departed", "actor.moved"]
        assert physical[1].event_seq == physical[0].event_seq + 1
        for e in physical:
            frozen = evidence(sm, sid, e.event_seq)
            assert frozen["world_version"] == e.world_version == receipt.world_version
            candidates = frozen["discovery"]["by_signal"]["signal.visible"]
            proof_ids = {
                r[0]
                for r in sm._conn.execute(
                    "SELECT npc_id FROM npc_perception_queue WHERE session_id=? AND event_seq=?",
                    (sid, e.event_seq),
                )
            }
            assert proof_ids == set(candidates)
            assert (
                sm._conn.execute(
                    "SELECT count(*) FROM event_outbox WHERE session_id=? AND event_seq=?",
                    (sid, e.event_seq),
                ).fetchone()[0]
                == 2
            )
        # Discovery itself creates no sensory judgment, OB or private cognition update.
        assert stored_rows(db)["event_observations"][1] == set()
        after = sm.read_session(sid, p, context=context)
        assert after.current_scene_id == A and after.world.game_tick == before.world.game_tick
        assert after.rng == before.rng
        for npc, expected_seq, other_seq in (
            (SOURCE, physical[0].event_seq, physical[1].event_seq),
            (DEST, physical[1].event_seq, physical[0].event_seq),
        ):
            ctx = NPCContext(npc, sid, npc)
            queued = {q.event_seq for q in sm.get_pending_npc_perception_queue(sid, p, context=ctx)}
            assert expected_seq in queued and other_seq not in queued


@pytest.mark.parametrize(
    "operation",
    [
        MoveActor(MOVER, A),
        StepActor(A, "2,1", "2,1"),
        StepActor(A, "2,1", "1,1"),
        MoveActor(MOVER, "scene.unknown"),
    ],
)
def test_same_scene_noop_or_rejected_movement_never_departs(tmp_path, context, operation):
    sm, sid, p, db, context = setup(tmp_path, context)
    with sm:
        before = stored_rows(db)
        receipt = submit(sm, sid, p, context, operation, "local", MOVER)
        events = events_for(sm, sid, p, context, receipt)
        assert all(e.event_type != "actor.departed" for e in events)
        if type(operation) is StepActor and operation.to_cell == "1,1":
            assert receipt.status == "committed" and [e.event_type for e in events] == [
                "actor.stepped"
            ]
        else:
            assert receipt.event_seqs == ()
            assert {k: v for k, v in stored_rows(db).items() if k != "command_receipts"} == {
                k: v for k, v in before.items() if k != "command_receipts"
            }


def test_pc_crossing_retains_identity_stage_and_visit_semantics(tmp_path, context):
    data = departure_data()
    data["actors"][-1]["initial_cell"] = "2,0"
    for pc in data["pc_templates"]:
        pc["initial_cell"] = "2,1"
    sm, sid, p, db, context = setup(tmp_path, context, data)
    with sm:
        before = sm.read_session(sid, p, context=context)
        receipt = submit(sm, sid, p, context, EnterScene(B), "pc.cross")
        assert receipt.status == "committed"
        events = events_for(sm, sid, p, context, receipt)
        assert [e.event_type for e in events] == ["scene.exited", "scene.entered"]
        assert [e.payload for e in events] == [{"scene_id": A}, {"scene_id": B}]
        assert [e.signals[0].origin.entity_id for e in events] == [before.pc_actor_id] * 2
        assert [evidence(sm, sid, e.event_seq)["phase"]["code"] for e in events] == [
            "scene.before_departure",
            "scene.after_arrival",
        ]
        after = sm.read_session(sid, p, context=context)
        assert after.current_scene_id == B
        assert after.scenes[B].visited_count == before.scenes[B].visited_count + 1


@pytest.mark.parametrize("table", ["actors", "event_evidence", "event_outbox", "command_receipts"])
def test_npc_departure_position_and_all_writes_roll_back_together(
    tmp_path, context, monkeypatch, table
):
    sm, sid, p, db, context = setup(tmp_path, context)
    with sm:
        captured = []
        capture = sm._event_capture._capture_event

        def watch(*args, **kwargs):
            result = capture(*args, **kwargs)
            captured.append(result.event_type)
            return result

        monkeypatch.setattr(sm._event_capture, "_capture_event", watch)
        if table == "actors":
            sm._conn.execute(
                "CREATE TEMP TRIGGER fail BEFORE UPDATE OF current_scene_id ON actors "
                "BEGIN SELECT RAISE(ABORT,'GC14 position fault'); END"
            )
        else:
            sm._conn.execute(
                f"CREATE TEMP TRIGGER fail BEFORE INSERT ON {table} "
                "BEGIN SELECT RAISE(ABORT,'GC14 persistence fault'); END"
            )
        cmd = command(sm, sid, p, context, MoveActor(MOVER, B), "fault", MOVER)
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(cmd, p, context=context)
        assert exc.value.code == "storage_error"
        assert captured[0] == "actor.departed"  # Real source phase precedes the injected failure.
        assert stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER fail")
        receipt = sm.submit_command(cmd, p, context=context)
        assert receipt.status == "committed"
        assert [e.event_type for e in events_for(sm, sid, p, context, receipt)] == [
            "actor.departed",
            "actor.moved",
        ]
        saved = stored_rows(db)
        assert sm.submit_command(cmd, p, context=context) == receipt
        assert stored_rows(db) == saved


def test_navigation_inner_rollback_discards_captured_departure_and_can_resume(
    tmp_path, context, monkeypatch
):
    sm, sid, p, db, context = setup(tmp_path, context)
    with sm:
        assert (
            submit(
                sm, sid, p, context, StartNavigation("activity.fail", B, "1,1"), "start", MOVER
            ).status
            == "committed"
        )
        capture = sm._event_capture._capture_event
        captured = []

        def fail_arrival(*args, **kwargs):
            result = capture(*args, **kwargs)
            captured.append(result.event_type)
            if result.event_type == "actor.moved":
                raise StateMachineError(
                    "movement_unavailable", "Injected post-capture domain failure"
                )
            return result

        monkeypatch.setattr(sm._event_capture, "_capture_event", fail_arrival)
        before = stored_rows(db)
        receipt = submit(
            sm, sid, p, context, AdvanceNavigation("activity.fail", 1), "advance", MOVER
        )
        assert receipt.status == "committed"
        assert captured[:2] == ["actor.departed", "actor.moved"]
        assert [e.event_type for e in events_for(sm, sid, p, context, receipt)] == [
            "activity.navigation_blocked"
        ]
        for table in ("actors", "scene_instances", "rng_states", "world_states"):
            assert stored_rows(db)[table] == before[table]
        blocked = sm.read_navigation_activity(sid, MOVER, "activity.fail", p, context=context)
        assert blocked.status == "blocked" and blocked.cursor == 0
        monkeypatch.undo()
        assert (
            submit(
                sm,
                sid,
                p,
                context,
                ResumeNavigation(blocked.activity_id, blocked.activity_version),
                "resume",
                MOVER,
            ).status
            == "committed"
        )
        pumped = pump_navigation(
            sm.submit_command,
            sm.read_navigation_activity,
            sm.get_command_receipt,
            sid,
            MOVER,
            blocked.activity_id,
            p,
            context=context,
            request_id="after.failure",
            budget=8,
        )
        assert pumped.activity.status == "completed"
        physical = [
            e.event_type
            for r in pumped.receipts
            for e in events_for(sm, sid, p, context, r)
            if e.signals
        ]
        assert physical == ["actor.departed", "actor.moved"]


@pytest.mark.parametrize("can_see", [False, True])
def test_historical_projection_uses_frozen_scope_and_never_invents_positive(
    tmp_path, context, monkeypatch, can_see
):
    data = departure_data()
    next(a for a in data["actors"] if a["actor_id"] == SOURCE)["senses"]["visual"] = can_see
    sm, sid, p, db, context = setup(tmp_path, context, data)
    with sm:
        cmd = command(sm, sid, p, context, MoveActor(MOVER, B), "cross", MOVER)
        receipt = sm.submit_command(cmd, p, context=context)
        events = events_for(sm, sid, p, context, receipt)
        departure, arrival = events
        frozen = [evidence(sm, sid, e.event_seq) for e in events]
        assert (
            submit(sm, sid, p, context, StepActor(B, "1,1", "2,1"), "later", MOVER).status
            == "committed"
        )
        committed = stored_rows(db)
    with StateMachine(db) as restored:
        restored.resume_session(sid, p, context=context)
        assert stored_rows(db) == committed
        assert restored.submit_command(cmd, p, context=context) == receipt
        assert stored_rows(db) == committed
        # Complete earlier input with an explicit negative test judgment so its
        # legitimate per-NPC prefix fence does not hide this later Observation.
        for npc in (SOURCE, DEST):
            ctx = NPCContext(npc, sid, npc)
            for queued in restored.get_pending_npc_perception_queue(sid, p, context=ctx):
                if queued.event_seq < departure.event_seq:
                    restored.project_npc_event(
                        sid,
                        queued.event_seq,
                        p,
                        context=ctx,
                        judgment=lambda request: JudgmentResult(
                            tuple(SignalJudgment(s.signal_id, False) for s in request.signals)
                        ),
                    )
        monkeypatch.setattr(
            restored._characters,
            "position",
            lambda *a: pytest.fail("History read current position"),
        )
        traced, requests = [], []
        restored._conn.set_trace_callback(traced.append)

        def notice(request):
            requests.append(request)
            raw = json.dumps(asdict(request))
            assert B not in raw and "navigation" not in raw
            assert request.scene_id == A
            return synthetic_notice(request)  # Explicit test Host judgment, never a real LLM.

        source_ctx, dest_ctx = NPCContext(SOURCE, sid, SOURCE), NPCContext(DEST, sid, DEST)
        assert restored.project_npc_event(sid, departure.event_seq, p, context=source_ctx) == (
            "unresolved" if can_see else "resolved"
        )
        assert restored.read_observations(sid, p, context=source_ctx) == ()
        if can_see:
            assert (
                restored.project_npc_event(
                    sid, departure.event_seq, p, context=source_ctx, judgment=notice
                )
                == "resolved"
            )
            assert len(requests) == 1
            assert (
                restored.read_observations(sid, p, context=source_ctx)[0]
                .perceived_signals[0]
                .content
                == "Someone leaves the scene."
            )
        assert (
            restored.project_npc_event(sid, departure.event_seq, p, context=dest_ctx)
            == "not_candidate"
        )
        assert (
            restored.project_npc_event(
                sid, arrival.event_seq, p, context=dest_ctx, judgment=synthetic_notice
            )
            == "resolved"
        )
        assert (
            restored.read_observations(sid, p, context=dest_ctx)[0].perceived_signals[0].content
            == "Someone moves into view."
        )
        restored._conn.set_trace_callback(None)
        assert not any(
            "from mechanical_scenes" in q.lower()
            or "from mechanical_actors" in q.lower()
            or "select current_scene_id,cell from actors" in q.lower()
            for q in traced
        )
        assert [evidence(restored, sid, e.event_seq) for e in events] == frozen
        pending = {
            q.event_seq
            for q in restored.get_pending_npc_perception_queue(sid, p, context=source_ctx)
        }
        assert departure.event_seq not in pending


@pytest.mark.parametrize("pc_in_destination", [False, True])
def test_existing_arrival_package_trigger_keeps_source_link_and_scene_boundary(
    tmp_path, context, pc_in_destination
):
    data = departure_data()
    if pc_in_destination:
        data["manifest"]["entry_scene_id"] = B
    definition = event_data(
        "event.arrived",
        {"kind": "set_scene_flag", "flag_id": "flag.workshop_checked", "value": True},
    )
    definition["trigger"] = {"event_type": "actor.moved", "scene_id": B}
    definition["condition"].update(flag_id="flag.workshop_checked", value=False)
    definition["signals"][0]["scope"]["scene_id"] = B
    data["events"] = [definition]
    data["scenes"][1]["event_definition_ids"] = ["event.arrived"]
    sm, sid, p, db, context = setup(tmp_path, context, data)
    with sm:
        receipt = submit(sm, sid, p, context, MoveActor(MOVER, B), "arrive", MOVER)
        assert receipt.status == "committed"
        events = events_for(sm, sid, p, context, receipt)
        expected = ["actor.departed", "actor.moved"] + (
            ["scene.flag_changed", "package.event_fired"] if pc_in_destination else []
        )
        assert [e.event_type for e in events] == expected
        history = sm._conn.execute(
            "SELECT * FROM trigger_history WHERE session_id=?", (sid,)
        ).fetchall()
        if pc_in_destination:
            assert len(history) == 1 and history[0][3] == events[1].event_seq
            assert events[2].payload["execution"] == {
                "kind": "package",
                "reference_id": "event.arrived",
            }
        else:
            assert history == []
        assert (
            sm.read_session(sid, p, context=context).scenes[B].local_flags["flag.workshop_checked"]
            is pc_in_destination
        )


def test_uninitialized_abstract_target_refuses_then_moves_after_existing_pc_visit(
    tmp_path, context
):
    data = yaml.safe_load((Path(__file__).parent / "fixtures/npc_station.yaml").read_text("utf-8"))
    mover = deepcopy(data["actors"][0])
    mover["actor_id"] = MOVER
    mover["npc_state"]["initial_knowledge_ids"] = []
    for field in ("memories", "beliefs", "relationships", "goals", "plans"):
        mover["npc_state"][field] = []
    data["actors"].append(mover)
    data["scenes"][0]["actor_ids"].append(MOVER)
    for actor in data["actors"]:
        actor["senses"] = {"visual": True, "auditory": True}
    document, db = tmp_path / "abstract.json", tmp_path / "abstract.db"
    document.write_text(json.dumps(data), "utf-8")
    p = load_adventure_package(document).package
    context = replace(context, controlled_npc_ids=(*context.controlled_npc_ids, MOVER))
    with StateMachine(db) as sm:
        sid = create(sm, p, context)
        before = sm.read_session(sid, p, context=context)
        assert B not in before.scenes
        rejected = submit(sm, sid, p, context, MoveActor(MOVER, B), "uninitialized", MOVER)
        assert rejected.error_code == "invalid_target" and rejected.event_seqs == ()
        assert sm.read_session(sid, p, context=context).actors == before.actors
        # Existing abstract NPC policy requires prior initialization; do not widen it.
        assert submit(sm, sid, p, context, EnterScene(B), "pc.initialize").status == "committed"
        assert submit(sm, sid, p, context, EnterScene(A), "pc.return").status == "committed"
        before = sm.read_session(sid, p, context=context)
        receipt = submit(sm, sid, p, context, MoveActor(MOVER, B), "first.visit", MOVER)
        assert receipt.status == "committed", receipt
        events = events_for(sm, sid, p, context, receipt)
        assert [e.event_type for e in events] == ["actor.departed", "actor.moved"]
        departure, arrival = [evidence(sm, sid, e.event_seq) for e in events]
        assert departure["schema"] == arrival["schema"] == "event-evidence/4"
        assert departure["discovery"]["by_signal"] == {"signal.visible": [SOURCE, MOVER]}
        assert arrival["discovery"]["by_signal"] == {"signal.visible": [DEST, MOVER]}
        assert set(departure["scenes"]) == {A} and set(arrival["scenes"]) == {B}
        after = sm.read_session(sid, p, context=context)
        assert after.current_scene_id == A and after.scenes[A] == before.scenes[A]
        assert after.scenes[B].visited_count == before.scenes[B].visited_count


def test_lost_response_replays_pair_after_restart_without_new_events(tmp_path, context):
    sm, sid, p, db, context = setup(tmp_path, context)
    with sm:
        cmd = command(sm, sid, p, context, MoveActor(MOVER, B), "lost", MOVER)

        def lose():
            receipt = sm.submit_command(cmd, p, context=context)
            assert receipt.status == "committed" and len(receipt.event_seqs) == 2
            raise ConnectionError("Injected transport loss after durable commit")

        with pytest.raises(ConnectionError):
            lose()
        committed = stored_rows(db)
    with StateMachine(db) as restored:
        receipt = restored.get_command_receipt(sid, "lost", p, context=context)
        assert restored.submit_command(cmd, p, context=context) == receipt
        assert stored_rows(db) == committed
        assert [e.event_type for e in events_for(restored, sid, p, context, receipt)] == [
            "actor.departed",
            "actor.moved",
        ]


def test_arrival_cascade_limit_rolls_back_both_stages_and_actor_position(tmp_path, context):
    data = departure_data()
    data["manifest"]["entry_scene_id"] = B
    data["events"] = []
    for index in range(33):
        definition = event_data(
            f"event.cascade_{index}",
            {"kind": "set_scene_flag", "flag_id": "flag.workshop_checked", "value": False},
        )
        definition["trigger"] = {"event_type": "actor.moved", "scene_id": B}
        definition["condition"].update(flag_id="flag.workshop_checked", value=False)
        definition["signals"][0]["scope"]["scene_id"] = B
        data["events"].append(definition)
    data["scenes"][1]["event_definition_ids"] = [e["event_definition_id"] for e in data["events"]]
    sm, sid, p, db, context = setup(tmp_path, context, data)
    with sm:
        before = stored_rows(db)
        receipt = submit(sm, sid, p, context, MoveActor(MOVER, B), "cascade", MOVER)
        assert receipt.error_code == "event_cascade_limit" and receipt.event_seqs == ()
        assert {k: v for k, v in stored_rows(db).items() if k != "command_receipts"} == {
            k: v for k, v in before.items() if k != "command_receipts"
        }
        assert sm.read_actor_position(sid, MOVER, p, context=context).scene_id == A
