"""GC-1: coordinate authority, shared admission, durable execution and real RE."""

import json
import subprocess
import sys
from dataclasses import asdict, replace

import pytest
from spatial_support import NPC, PASSAGE, A, B, command, create, spatial_data, submit
from test_migrations import stored_rows

from trpg_state_machine import (
    EnterScene,
    MoveActor,
    ObjectState,
    PlaceObject,
    SetObjectState,
    StateMachine,
    StateMachineError,
    StepActor,
    TraversePassage,
    world_command_from_data,
)
from trpg_state_machine.package import _validate
from trpg_state_machine.perception import NPCContext


@pytest.fixture
def world(package_data):
    return _validate(spatial_data(package_data))


def assert_ok(receipt):
    assert receipt.status == "committed", receipt
    return receipt


@pytest.mark.parametrize("object_id,start", [("object.crate", "3,2"), ("object.stool", "3,0")])
def test_public_end_to_end_two_objects_and_fresh_process(
    tmp_path, package_data, context, object_id, start
):
    data = spatial_data(package_data)
    package = _validate(data)
    db = tmp_path / "world.db"
    source = tmp_path / "package.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        rng = sm.read_session(sid, package, context=context).rng
        assert (
            sm.query_world_action(sid, pc, EnterScene(B), package, context=context).status
            == "available"
        )
        placement = command(
            sm, sid, package, context, PlaceObject(object_id, A, start, "3,1"), "place"
        )
        receipt = assert_ok(
            sm.submit_command(
                world_command_from_data(
                    {k: v for k, v in asdict(placement).items() if k != "check_world_effects"}
                ),
                package,
                context=context,
            )
        )
        snap = sm.read_session(sid, package, context=context)
        assert snap.scenes[A].object_cells[object_id] == "3,1"
        assert (
            submit(sm, sid, package, context, EnterScene(B), "pc.blocked").error_code
            == "passage_unavailable"
        )
        assert_ok(
            submit(sm, sid, package, context, StepActor(B, "1,2", "1,1"), "npc.approach", NPC)
        )
        for key, op in [("legacy", MoveActor(NPC, A)), ("new", TraversePassage(PASSAGE, B, "1,1"))]:
            result = submit(sm, sid, package, context, op, key, NPC)
            assert result.error_code == "passage_unavailable"
        with pytest.raises(StateMachineError) as exc:
            sm._apply(
                sid,
                package,
                MoveActor(NPC, A),
                expected_world_version=sm.read_session(sid, package, context=context).world_version,
                context=context,
            )
        assert exc.value.code == "passage_unavailable"
        frozen = sm._conn.execute(
            "SELECT evidence_json FROM event_evidence WHERE session_id=? AND event_seq=?",
            (sid, receipt.event_seqs[0]),
        ).fetchone()[0]
        assert json.loads(frozen)["objects"][object_id]["cell"] == "3,1"
        assert json.loads(frozen)["schema"] == "event-evidence/5"
        assert sm.read_session(sid, package, context=context).rng == rng
        sm.save_session(sid, package, context=context)
    # New interpreter, revalidated Package, public resume/query/commands.
    code = """
import json, sys
from dataclasses import asdict
from trpg_state_machine import *
p=load_adventure_package(sys.argv[2]).package
c=TrustedContext('authenticated:alice','player.alice',('npc.caretaker','npc.attendant'))
with StateMachine(sys.argv[1]) as sm:
 s=sm.resume_session(sys.argv[3],p,context=c)
 print(json.dumps(asdict(sm.query_world_action(s.session_id,s.pc_actor_id,EnterScene('scene.workshop'),p,context=c))))
"""
    proc = subprocess.run(
        [sys.executable, "-c", code, str(db), str(source), sid], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["reason"] == "passage_unavailable"
    with StateMachine(db) as sm:
        assert sm.submit_command(placement, package, context=context) == receipt
        assert_ok(
            submit(sm, sid, package, context, PlaceObject(object_id, A, "3,1", start), "remove")
        )
        assert (
            sm._conn.execute(
                "SELECT evidence_json FROM event_evidence WHERE session_id=? AND event_seq=?",
                (sid, receipt.event_seqs[0]),
            ).fetchone()[0]
            == frozen
        )
        assert_ok(submit(sm, sid, package, context, StepActor(B, "1,1", "1,2"), "npc.clear", NPC))
        assert_ok(submit(sm, sid, package, context, TraversePassage(PASSAGE, A, "2,1"), "pc.cross"))
        assert sm.read_actor_position(sid, pc, package, context=context).cell == "1,1"
        assert_ok(submit(sm, sid, package, context, StepActor(B, "1,1", "2,1"), "pc.clear"))
        assert_ok(submit(sm, sid, package, context, StepActor(B, "1,2", "1,1"), "npc.return", NPC))
        assert_ok(submit(sm, sid, package, context, MoveActor(NPC, A), "npc.cross", NPC))
        assert sm.read_actor_position(sid, NPC, package, context=context).cell == "2,1"
        assert sm.read_session(sid, package, context=context).rng == rng


@pytest.mark.parametrize("cell", ["9,1", "-1,1", "02,2", "4,4", "1,3", "3,2"])
def test_illegal_steps_are_atomic(tmp_path, world, context, cell):
    db = tmp_path / "world.db"
    with StateMachine(db) as sm:
        sid = create(sm, world, context)
        before = stored_rows(db)
        result = submit(sm, sid, world, context, StepActor(A, "2,1", cell), "bad")
        assert result.status == "rejected"
        after = stored_rows(db)
        assert {k: v for k, v in before.items() if k != "command_receipts"} == {
            k: v for k, v in after.items() if k != "command_receipts"
        }


def test_walls_corners_and_movement_only_visibility(tmp_path, package_data, context):
    data = spatial_data(package_data)
    data["scenes"][0]["grid"]["wall_segments"] = [{"x1": 2, "y1": 1, "x2": 2, "y2": 2}]
    p = _validate(data)
    with StateMachine(tmp_path / "wall.db") as sm:
        sid = create(sm, p, context)
        assert (
            submit(sm, sid, p, context, StepActor(A, "2,1", "1,1"), "wall").error_code
            == "movement_unavailable"
        )
        assert (
            submit(
                sm, sid, p, context, PlaceObject("object.platform_gate", A, "1,1", "2,2"), "fixed"
            ).error_code
            == "invalid_target"
        )
    p = _validate(spatial_data(package_data))
    with StateMachine(tmp_path / "corner.db") as sm:
        sid = create(sm, p, context)
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        assert_ok(
            submit(sm, sid, p, context, PlaceObject("object.crate", A, "3,2", "3,1"), "block")
        )
        assert (
            submit(sm, sid, p, context, StepActor(A, "2,1", "3,2"), "corner").error_code
            == "movement_unavailable"
        )
        # Movement projection rejects a diagonal past the object; visual base grid is unchanged.
        assert sm._base_topology(sid, A).can_see("2,1", "4,1")
        assert sm._movement_topology(sid, A, p, exclude_actor=pc).shortest_path("2,1", "3,2") != [
            "2,1",
            "3,2",
        ]
        assert (
            json.loads(
                sm._conn.execute(
                    "SELECT state_json FROM mechanical_scenes WHERE session_id=? AND scene_id=?",
                    (sid, A),
                ).fetchone()[0]
            )["grid"]["blocked_cells"]
            == []
        )


def test_queries_grants_noop_replay_conflict_and_sql_failure(tmp_path, world, context):
    db = tmp_path / "world.db"
    with StateMachine(db) as sm:
        sid = create(sm, world, context)
        pc = sm.read_session(sid, world, context=context).pc_actor_id
        sm.grant_npc_access(sid, NPC, "npc.owner", world, context=context)
        npc = NPCContext("npc.owner", sid, NPC)
        before = stored_rows(db)
        assert sm.get_spatial_view(sid, NPC, world, context=npc).actor_position.scene_id == B
        assert (
            sm.query_world_action(
                sid, pc, StepActor(A, "2,1", "2,2"), world, context=context
            ).status
            == "available"
        )
        assert stored_rows(db) == before
        noop = assert_ok(submit(sm, sid, world, context, StepActor(A, "2,1", "2,1"), "noop"))
        assert noop.event_seqs == () and noop.world_version == 0
        cmd = command(sm, sid, world, context, StepActor(A, "2,1", "2,2"), "step")
        result = assert_ok(sm.submit_command(cmd, world, context=context))
        frozen = stored_rows(db)
        assert sm.submit_command(cmd, world, context=context) == result
        assert stored_rows(db) == frozen
        with pytest.raises(StateMachineError, match="different content"):
            sm.submit_command(
                replace(cmd, payload=StepActor(A, "2,1", "2,0")), world, context=context
            )
        assert (
            sm.submit_command(replace(cmd, command_id="stale"), world, context=context).status
            == "conflict"
        )
        current = command(sm, sid, world, context, StepActor(A, "2,2", "2,1"), "fault")
        frozen = stored_rows(db)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_gc1 BEFORE INSERT ON event_outbox "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(current, world, context=context)
        assert error.value.code == "storage_error" and stored_rows(db) == frozen
        sm._conn.execute("DROP TRIGGER fail_gc1")
        assert_ok(sm.submit_command(current, world, context=context))


def test_closed_schema_and_auth(tmp_path, world, context):
    with StateMachine(tmp_path / "auth.db") as sm:
        sid = create(sm, world, context)
        cmd = command(sm, sid, world, context, StepActor(A, "2,1", "2,2"), "x")
        for field, value in [("movable", True), ("sql", "delete")]:
            raw = asdict(cmd)
            raw["payload"][field] = value
            with pytest.raises(StateMachineError):
                world_command_from_data(raw)
        with pytest.raises(StateMachineError):
            sm.submit_command(cmd, world, context=replace(context, principal_id="intruder"))
        bad = command(sm, sid, world, context, StepActor(A, "2,1", "2,2"), "npc", "npc.caretaker")
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(bad, world, context=replace(context, controlled_npc_ids=()))
        assert error.value.code == "unauthorized"
        assert (
            submit(
                sm, sid, world, context, PlaceObject("object.unknown", A, "3,2", "3,1"), "unknown"
            ).error_code
            == "invalid_target"
        )


def combat_data(base, assets, *, blocking=False):
    from modern_support import modern_data

    data = spatial_data(modern_data(base, assets))
    if not blocking:
        data["scenes"][0]["objects"] = data["scenes"][0]["objects"][:1]
    grid = data["scenes"][0]["grid"]
    for kind in ("combat_foundation", "noncombat_foundation"):
        data["rule_bindings"][kind]["snapshot"]["scene_state"]["grid"] = grid
    data["rule_bindings"]["noncombat_foundation"]["additional_scenes"][0]["grid"] = grid
    return data


def test_real_re_positions_survive_initialize_close_and_exploration(
    tmp_path, package_data, context
):
    from dnd_support import initialize
    from modern_support import modern_loader
    from test_combat_closure import close_command, pc_attack

    assets = modern_loader()
    p = _validate(combat_data(package_data, assets))
    with StateMachine(tmp_path / "combat.db", dnd_loader=assets) as sm:
        sid = create(sm, p, context)
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        assert_ok(submit(sm, sid, p, context, StepActor(A, "2,1", "2,2"), "explore"))
        assert_ok(initialize(sm, sid, p, context))
        actual = sm.read_combat_snapshot(sid, p, context=context)
        assert actual.combat_state.actor_zone[pc] == "2,2"
        assert actual.combat_state.actor_zone["npc.caretaker"] == "1,3"
        assert "actor_zone" not in json.loads(
            sm._conn.execute("SELECT state_json FROM combats").fetchone()[0]
        )
        assert (
            submit(sm, sid, p, context, StepActor(A, "2,2", "2,1"), "active").error_code
            == "unsupported_combat_world_operation"
        )
        real = sm._evaluate_dnd
        seen = []

        async def capture(request):
            seen.append(request.state_snapshot.combat_state.actor_zone.copy())
            return await real(request)

        sm._evaluate_dnd = capture
        assert_ok(sm.submit_command(pc_attack(sm, sid, p, context), p, context=context))
        assert seen[0][pc] == "2,2"
        assert_ok(sm.submit_command(close_command(sm, sid, p, context), p, context=context))
        terminal = sm.read_combat_snapshot(sid, p, context=context).combat_state.actor_zone.copy()
        assert_ok(submit(sm, sid, p, context, StepActor(A, "2,2", "2,1"), "after"))
        assert sm.read_actor_position(sid, pc, p, context=context).cell == "2,1"
        assert sm.read_combat_snapshot(sid, p, context=context).combat_state.actor_zone == terminal
        assert terminal[pc] == "2,2"


def test_real_re_dynamic_obstacle_guard_does_not_disable_generic_checks(
    tmp_path, package_data, context
):
    from dnd_support import initialize
    from modern_support import modern_loader
    from test_real_checks import check_command

    assets = modern_loader()
    data = combat_data(package_data, assets, blocking=True)
    p = _validate(data)
    context = replace(context, can_adjudicate_checks=True)
    with StateMachine(tmp_path / "obstacle.db", dnd_loader=assets) as sm:
        sid = create(sm, p, context)
        before = sm.read_session(sid, p, context=context)
        with pytest.raises(StateMachineError) as error:
            initialize(sm, sid, p, context)
        assert error.value.code == "unsupported_combat_dynamic_obstacles"
        assert sm.read_session(sid, p, context=context) == before
        assert_ok(sm.submit_command(check_command(sm, sid, p, context), p, context=context))


def test_combat_preparation_occ_uses_new_position(tmp_path, package_data, context, monkeypatch):
    from dnd_support import initialize
    from modern_support import modern_loader

    assets = modern_loader()
    p = _validate(combat_data(package_data, assets))
    with StateMachine(tmp_path / "occ.db", dnd_loader=assets) as sm:
        sid = create(sm, p, context)
        prepare = sm._prepare_combat

        def change(row, package):
            prepared = prepare(row, package)
            assert_ok(submit(sm, sid, p, context, StepActor(A, "2,1", "2,2"), "race"))
            return prepared

        monkeypatch.setattr(sm, "_prepare_combat", change)
        assert initialize(sm, sid, p, context).status == "conflict"
        assert sm._conn.execute("SELECT count(*) FROM combats").fetchone()[0] == 0
        assert (
            sm.read_actor_position(
                sid, sm.read_session(sid, p, context=context).pc_actor_id, p, context=context
            ).cell
            == "2,2"
        )


def test_gate_multiple_obstacles_and_one_way(tmp_path, package_data, context):
    data = spatial_data(package_data)
    # Independent barrier on the opposite threshold, same passage authority.
    data["scenes"][1]["objects"].append(
        {
            "object_id": "object.screen",
            "state": None,
            "movable": True,
            "blocks_passage": True,
            "initial_cell": "0,2",
        }
    )
    p = _validate(data)
    with StateMachine(tmp_path / "gates.db") as sm:
        sid = create(sm, p, context)
        assert_ok(submit(sm, sid, p, context, StepActor(B, "1,2", "1,1"), "approach", NPC))
        assert_ok(
            submit(
                sm,
                sid,
                p,
                context,
                SetObjectState(A, "object.platform_gate", ObjectState.CLOSED),
                "close",
            )
        )
        assert_ok(submit(sm, sid, p, context, PlaceObject("object.crate", A, "3,2", "3,1"), "one"))
        assert_ok(
            submit(sm, sid, p, context, PlaceObject("object.screen", B, "0,2", "0,1"), "two", NPC)
        )
        assert_ok(
            submit(sm, sid, p, context, PlaceObject("object.crate", A, "3,1", "3,2"), "clear.one")
        )
        assert (
            submit(sm, sid, p, context, EnterScene(B), "still.blocked").error_code
            == "passage_unavailable"
        )
        assert_ok(
            submit(
                sm, sid, p, context, PlaceObject("object.screen", B, "0,1", "0,2"), "clear.two", NPC
            )
        )
        assert_ok(submit(sm, sid, p, context, StepActor(B, "1,1", "1,2"), "clear.npc", NPC))
        assert (
            submit(sm, sid, p, context, EnterScene(B), "door.blocked").error_code
            == "passage_unavailable"
        )
        assert_ok(
            submit(
                sm,
                sid,
                p,
                context,
                SetObjectState(A, "object.platform_gate", ObjectState.OPEN),
                "open",
            )
        )
        assert_ok(submit(sm, sid, p, context, EnterScene(B), "cross"))
    data = spatial_data(package_data)
    data["scenes"][1]["exits"] = []
    p = _validate(data)
    with StateMachine(tmp_path / "oneway.db") as sm:
        sid = create(sm, p, context)
        assert_ok(submit(sm, sid, p, context, EnterScene(B), "cross"))
        assert submit(sm, sid, p, context, EnterScene(A), "reverse").error_code == "invalid_target"


def test_response_loss_and_domain_failure_after_mutation(tmp_path, world, context, monkeypatch):
    from trpg_state_machine.game_time import sync_npc_schedules

    db = tmp_path / "lost.db"
    with StateMachine(db) as sm:
        sid = create(sm, world, context)
        cmd = command(sm, sid, world, context, PlaceObject("object.crate", A, "3,2", "3,1"), "lost")
        real = sm.submit_command

        def lost_reply(*args, **kwargs):
            real(*args, **kwargs)
            raise ConnectionError("response lost after commit")

        with pytest.raises(ConnectionError):
            lost_reply(cmd, world, context=context)
        rows = stored_rows(db)
        assert real(cmd, world, context=context) == sm.get_command_receipt(
            sid, "lost", world, context=context
        )
        assert stored_rows(db) == rows
        other = command(
            sm, sid, world, context, PlaceObject("object.crate", A, "3,1", "3,2"), "rollback"
        )

        def fail(*args):
            sync_npc_schedules(*args)
            raise StateMachineError("event_cascade_limit", "injected tail failure")

        monkeypatch.setattr("trpg_state_machine.commands.sync_npc_schedules", fail)
        assert real(other, world, context=context).error_code == "event_cascade_limit"
        after = stored_rows(db)
        assert {k: v for k, v in rows.items() if k != "command_receipts"} == {
            k: v for k, v in after.items() if k != "command_receipts"
        }


@pytest.mark.parametrize(
    "change",
    [
        "missing_cell",
        "noncanonical",
        "outside",
        "actor_overlap",
        "pc_overlap",
        "endpoint",
        "unknown_passage",
        "unbound_fine_exit",
        "door_movable",
        "unknown_capability",
    ],
)
def test_package_rejects_inconsistent_spatial_config(package_data, change):
    data = spatial_data(package_data)
    if change == "missing_cell":
        data["actors"][0]["initial_cell"] = None
    elif change == "noncanonical":
        data["actors"][0]["initial_cell"] = "01,3"
    elif change == "outside":
        data["scenes"][0]["objects"][0]["initial_cell"] = "9,0"
    elif change == "actor_overlap":
        data["actors"][1].update(initial_scene_id=A, initial_cell="1,3")
        data["scenes"][0]["actor_ids"].append(NPC)
    elif change == "pc_overlap":
        data["pc_templates"][0]["initial_cell"] = "1,3"
    elif change == "endpoint":
        data["passages"][0]["endpoints"][0]["threshold_cell"] = "4,4"
    elif change == "unknown_passage":
        data["scenes"][0]["exits"][0]["passage_id"] = "passage.unknown"
    elif change == "unbound_fine_exit":
        data["scenes"][0]["exits"][0]["passage_id"] = None
    elif change == "door_movable":
        data["scenes"][0]["objects"][0]["movable"] = True
    else:
        data["scenes"][0]["objects"][0]["teleport"] = True
    with pytest.raises(StateMachineError):
        _validate(data)


def test_bounds_and_object_source_evidence_are_distinct_from_walkability(
    tmp_path, package_data, context
):
    data = spatial_data(package_data)
    data["scenes"][0]["grid"]["blocked_cells"] = ["4,4"]
    data["scenes"][0]["objects"][0]["initial_cell"] = "4,4"
    p = _validate(data)
    with StateMachine(tmp_path / "source.db") as sm:
        sid = create(sm, p, context)
        assert not sm._base_topology(sid, A).is_valid_cell("4,4")
        from trpg_state_machine.spatial import cell_in_bounds

        assert cell_in_bounds(sm._base_topology(sid, A), "4,4")
        assert (
            sm.read_session(sid, p, context=context).scenes[A].object_cells["object.platform_gate"]
            == "4,4"
        )


def test_no_hidden_scope_leak_or_abstract_fine_fallback(tmp_path, package_data, context):
    data = spatial_data(package_data)
    data["scenes"][0]["access_policy"] = "internal_only"
    p = _validate(data)
    with StateMachine(tmp_path / "secret.db") as sm:
        sid = create(sm, p, context)
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        result = sm.query_world_action(sid, pc, EnterScene(B), p, context=context)
        assert result.reason == "unsupported_interaction_scope"
        assert (
            sm.query_world_action(sid, pc, EnterScene("scene.unknown"), p, context=context).reason
            == result.reason
        )
        assert "object." not in str(result)
        with pytest.raises(StateMachineError):
            sm.get_spatial_view(sid, pc, p, context=context)
    # A legacy abstract scene cannot enter a fine map using an old unbound exit.
    data = spatial_data(package_data)
    data["scenes"][0]["grid"] = None
    data["scenes"][0]["exits"][0]["passage_id"] = None
    data["scenes"][1]["exits"] = []
    data["passages"] = []
    data["scenes"][0]["objects"] = []
    data["actors"][0]["initial_cell"] = None
    for pc in data["pc_templates"]:
        pc["initial_cell"] = None
    p = _validate(data)
    with StateMachine(tmp_path / "mixed.db") as sm:
        sid = create(sm, p, context)
        assert (
            submit(sm, sid, p, context, EnterScene(B), "bad").error_code
            == "unsupported_spatial_transition"
        )
        assert (
            submit(
                sm, sid, p, context, PlaceObject("object.crate", B, "3,2", "3,1"), "remote"
            ).error_code
            == "invalid_target"
        )


@pytest.mark.parametrize("target_scope", ["public_interactions", "internal_only"])
def test_hidden_object_and_passage_references_are_indistinguishable(
    tmp_path, package_data, context, target_scope
):
    data = spatial_data(package_data)
    data["scenes"][0]["grid"]["wall_segments"] = [{"x1": 4, "y1": 0, "x2": 4, "y2": 5}]
    data["scenes"][0]["objects"][1]["initial_cell"] = "4,2"
    data["passages"][0]["endpoints"][0].update(standing_cell="4,3", threshold_cell="4,4")
    data["scenes"][1]["access_policy"] = target_scope
    p = _validate(data)
    with StateMachine(tmp_path / "hidden.db") as sm:
        sid = create(sm, p, context)
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        view = sm.get_spatial_view(sid, pc, p, context=context)
        assert "object.crate" not in str(view.objects) and view.exits == ()
        operations = [PlaceObject("object.crate", A, cell, "4,1") for cell in ("4,2", "2,2")] + [
            PlaceObject("object.unknown", A, "4,2", "4,1"),
            TraversePassage(PASSAGE, A, "2,1"),
            TraversePassage(PASSAGE, A, "2,2"),
            TraversePassage("passage.unknown", A, "2,1"),
            EnterScene(B),
            EnterScene("scene.unknown"),
            MoveActor("npc.caretaker", B),
            MoveActor("npc.caretaker", "scene.unknown"),
        ]
        for index, operation in enumerate(operations):
            actor = operation.actor_id if type(operation) is MoveActor else pc
            before = stored_rows(tmp_path / "hidden.db")
            query = sm.query_world_action(sid, actor, operation, p, context=context)
            assert query.reason == "invalid_target"
            assert stored_rows(tmp_path / "hidden.db") == before
            assert (
                submit(sm, sid, p, context, operation, f"hidden.{index}", actor).error_code
                == query.reason
            )


def test_target_scope_and_endpoint_occupancy_reject_atomically(tmp_path, package_data, context):
    for case in ("private", "occupied"):
        data = spatial_data(package_data)
        if case == "private":
            data["scenes"][1]["access_policy"] = "internal_only"
        else:
            data["actors"][1]["initial_cell"] = "1,1"
        p = _validate(data)
        db = tmp_path / f"{case}.db"
        with StateMachine(db) as sm:
            sid = create(sm, p, context)
            before = stored_rows(db)
            result = submit(sm, sid, p, context, EnterScene(B), "blocked")
            assert result.error_code == (
                "unsupported_interaction_scope" if case == "private" else "passage_unavailable"
            )
            after = stored_rows(db)
            assert {k: v for k, v in before.items() if k != "command_receipts"} == {
                k: v for k, v in after.items() if k != "command_receipts"
            }


def test_revisit_preserves_cells_and_counts_only_actual_visits(tmp_path, world, context):
    db = tmp_path / "visits.db"
    with StateMachine(db) as sm:
        sid = create(sm, world, context)
        initial = sm.read_session(sid, world, context=context)
        assert initial.scenes[A].visited_count == 1 and initial.scenes[B].visited_count == 0
        assert_ok(
            submit(sm, sid, world, context, PlaceObject("object.stool", A, "3,0", "2,0"), "place")
        )
        assert_ok(submit(sm, sid, world, context, EnterScene(B), "out"))
        assert sm.read_session(sid, world, context=context).scenes[B].visited_count == 1
    with StateMachine(db) as sm:
        assert_ok(submit(sm, sid, world, context, EnterScene(A), "back"))
        snapshot = sm.read_session(sid, world, context=context)
        assert snapshot.scenes[A].visited_count == 2
        assert snapshot.scenes[A].object_cells["object.stool"] == "2,0"
        assert snapshot.actors[NPC].cell == "1,2"


def test_additional_mechanical_scene_must_match_spatial_base(package_data):
    from modern_support import modern_loader

    data = combat_data(package_data, modern_loader())
    data["rule_bindings"]["noncombat_foundation"]["additional_scenes"][0]["grid"]["width"] = 4
    with pytest.raises(StateMachineError, match="base grid must agree"):
        _validate(data)


def test_npc_reliable_output_handoff_uses_same_passage_admission(tmp_path, context):
    import yaml
    from test_npc_evaluation_tasks import create_task
    from test_npc_state import SOURCE, grant

    from trpg_state_machine.npc_results import NPCActionIntent, NPCEvaluationResult

    data = spatial_data(yaml.safe_load(SOURCE.read_text("utf-8")))
    for actor in data["actors"]:
        actor["npc_state"]["runtime"].update(schedule=[], current_activity=None)
    p = _validate(data)
    db = tmp_path / "handoff.db"
    with StateMachine(db) as sm:
        sid = create(sm, p, context)
        npc = grant(sm, sid, p, context, NPC)
        assert_ok(submit(sm, sid, p, context, StepActor(B, "1,2", "1,1"), "approach", NPC))
        assert_ok(
            submit(sm, sid, p, context, PlaceObject("object.crate", A, "3,2", "3,1"), "block")
        )
        task = create_task(sm, sid, p, npc)
        version = sm.read_session(sid, p, context=context).world_version
        result = NPCEvaluationResult(action_intents=(NPCActionIntent("move_actor", A, version),))
        sm.submit_npc_evaluation_result(task.evaluation_id, result, p, context=npc)
        before = sm.read_session(sid, p, context=context)
        rejected = sm.dispatch_npc_output(task.evaluation_id, 0, p, context=context)
        assert rejected.command_receipt.error_code == "passage_unavailable"
        assert sm.read_session(sid, p, context=context) == before
        rows = stored_rows(db)
        assert sm.dispatch_npc_output(task.evaluation_id, 0, p, context=context) == rejected
        assert stored_rows(db) == rows
        assert_ok(
            submit(sm, sid, p, context, PlaceObject("object.crate", A, "3,1", "3,2"), "clear")
        )
        assert_ok(submit(sm, sid, p, context, StepActor(A, "2,1", "2,2"), "clear.pc"))
        npc_version = sm.get_npc_view(sid, NPC, p, context=npc).npc_state_version
        task2 = create_task(
            sm, sid, p, npc, key="task.second", expected_npc_state_version=npc_version
        )
        version = sm.read_session(sid, p, context=context).world_version
        sm.submit_npc_evaluation_result(
            task2.evaluation_id,
            NPCEvaluationResult(action_intents=(NPCActionIntent("move_actor", A, version),)),
            p,
            context=npc,
        )
    with StateMachine(db) as sm:
        committed = sm.dispatch_npc_output(task2.evaluation_id, 0, p, context=context)
        assert_ok(committed.command_receipt)
        assert sm.read_actor_position(sid, NPC, p, context=context).scene_id == A
        assert sm.read_actor_position(sid, NPC, p, context=context).cell == "2,1"
        assert sm.dispatch_npc_output(task.evaluation_id, 0, p, context=context) == rejected
        assert sm.dispatch_npc_output(task2.evaluation_id, 0, p, context=context) == committed


def test_actor_activity_and_closed_door_threshold_are_fail_closed(tmp_path, context, package_data):
    import yaml
    from test_npc_state import SOURCE

    p = _validate(spatial_data(yaml.safe_load(SOURCE.read_text("utf-8"))))
    with StateMachine(tmp_path / "activity.db") as sm:
        sid = create(sm, p, context)
        assert submit(sm, sid, p, context, StepActor(B, "1,2", "1,1"), "busy", NPC).error_code == (
            "unsupported_actor_activity"
        )
    p = _validate(spatial_data(package_data))
    with StateMachine(tmp_path / "door.db") as sm:
        sid = create(sm, p, context)
        # A door cannot close onto an actor occupying either linked threshold.
        assert_ok(submit(sm, sid, p, context, StepActor(B, "1,2", "0,1"), "threshold", NPC))
        before = sm.read_session(sid, p, context=context)
        assert (
            submit(
                sm,
                sid,
                p,
                context,
                SetObjectState(A, "object.platform_gate", ObjectState.CLOSED),
                "close",
            ).error_code
            == "passage_unavailable"
        )
        assert sm.read_session(sid, p, context=context) == before


def test_door_on_threshold_can_open_without_ignoring_other_obstacles(
    tmp_path, package_data, context
):
    data = spatial_data(package_data)
    data["scenes"][0]["objects"][0].update(initial_cell="3,1", state="closed")
    p = _validate(data)
    with StateMachine(tmp_path / "threshold-door.db") as sm:
        sid = create(sm, p, context)
        assert (
            submit(sm, sid, p, context, EnterScene(B), "closed").error_code == "passage_unavailable"
        )
        assert_ok(
            submit(
                sm,
                sid,
                p,
                context,
                SetObjectState(A, "object.platform_gate", ObjectState.OPEN),
                "open",
            )
        )
        assert_ok(submit(sm, sid, p, context, EnterScene(B), "cross"))
    # Another door on the same Cell must not be ignored while opening the first.
    data["scenes"][0]["objects"].append(
        {
            "object_id": "object.second_gate",
            "state": "closed",
            "movable": False,
            "blocks_passage": False,
            "initial_cell": "3,1",
        }
    )
    from copy import deepcopy

    second = deepcopy(data["passages"][0])
    second.update(passage_id="passage.second", door_object_id="object.second_gate")
    data["passages"].append(second)
    data["scenes"][1]["exits"][0]["passage_id"] = "passage.second"
    p = _validate(data)
    with StateMachine(tmp_path / "two-doors.db") as sm:
        sid = create(sm, p, context)
        before = sm.read_session(sid, p, context=context)
        assert (
            submit(
                sm,
                sid,
                p,
                context,
                SetObjectState(A, "object.platform_gate", ObjectState.OPEN),
                "open",
            ).error_code
            == "object_out_of_reach"
        )
        assert sm.read_session(sid, p, context=context) == before
