"""Scene boundaries through real commands and shared SQLite participants."""

import json
from dataclasses import replace

import pytest
from navigation_support import navigation_package, pump, start
from spatial_support import A, B, command, create, spatial_package, submit
from test_domain_runtime import components
from test_migrations import stored_rows
from test_npc_state import grant

from trpg_state_machine import (
    EnterScene,
    MoveActor,
    ObjectState,
    SceneLifecycleService,
    SetObjectState,
    SetSceneFlag,
    StateMachine,
    StateMachineError,
    StepActor,
)


def test_preinitialize_and_npc_crossing_do_not_record_pc_visits(tmp_path, package, context):
    # Authorized NPC movement may ensure a destination without any PC entering it.
    with StateMachine(tmp_path / "npc.db") as sm:
        sid = create(sm, package, context)
        before = sm.read_session(sid, package, context=context)
        assert B not in before.scenes
        cmd = command(
            sm, sid, package, context, MoveActor("npc.caretaker", B), "npc.cross", "npc.caretaker"
        )
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed"
        after = sm.read_session(sid, package, context=context)
        assert after.current_scene_id == before.current_scene_id == A
        assert after.actors[before.pc_actor_id] == before.actors[before.pc_actor_id]
        assert after.scenes[B].visited_count == 0
        assert after.scenes[A].visited_count == 1
        events = sm.read_events(sid, package, context=context, after_seq=before.event_seq)
        assert [(e.event_type, e.payload) for e in events] == [
            ("actor.departed", {"scene_id": A, "actor_id": "npc.caretaker", "from_cell": None}),
            ("actor.moved", {"actor_id": "npc.caretaker", "to_scene_id": B}),
        ]
        saved = stored_rows(tmp_path / "npc.db")
        assert sm.submit_command(cmd, package, context=context) == receipt
        assert stored_rows(tmp_path / "npc.db") == saved


def test_ensure_is_idempotent_pc_entry_revisit_and_restore(tmp_path, package, context):
    db = tmp_path / "entry.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        assert isinstance(sm.scene_lifecycle, SceneLifecycleService)
        assert sm.scene_lifecycle is sm._world.scenes
        assert create(sm, package, context) == sid
        assert sm.read_session(sid, package, context=context).scenes[A].visited_count == 1
        with sm._transaction(write=True):
            assert sm.scene_lifecycle.ensure_scene(sid, B, package)
        saved = stored_rows(db)
        with sm._transaction(write=True):
            assert not sm.scene_lifecycle.ensure_scene(sid, B, package)
        assert stored_rows(db) == saved
        assert (
            sm.query_world_action(
                sid,
                sm.read_session(sid, package, context=context).pc_actor_id,
                EnterScene(B),
                package,
                context=context,
            ).status
            == "available"
        )
        assert stored_rows(db) == saved
        cmd = command(sm, sid, package, context, EnterScene(B), "enter")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed"
        assert sm.read_session(sid, package, context=context).scenes[B].visited_count == 1
        assert sm.submit_command(cmd, package, context=context) == receipt
        assert submit(sm, sid, package, context, EnterScene(B), "same").event_seqs == ()
        assert (
            submit(
                sm,
                sid,
                package,
                context,
                SetObjectState(B, "object.workshop_door", ObjectState.OPEN),
                "open",
            ).status
            == "committed"
        )
        assert (
            submit(
                sm, sid, package, context, SetSceneFlag(B, "flag.workshop_checked", True), "flag"
            ).status
            == "committed"
        )
        assert submit(sm, sid, package, context, EnterScene(A), "leave").status == "committed"
        assert submit(sm, sid, package, context, EnterScene(B), "revisit").status == "committed"
        state = sm.read_session(sid, package, context=context)
        assert state.scenes[B].visited_count == 2 and state.scenes[A].visited_count == 2
        assert state.scenes[B].objects["object.workshop_door"] == "open"
        assert state.scenes[B].local_flags["flag.workshop_checked"] is True
        saved = stored_rows(db)
    with StateMachine(db) as sm:
        assert sm.resume_session(sid, package, context=context) == state
        assert stored_rows(db) == saved
        with sm._transaction(write=True):
            assert not sm.scene_lifecycle.ensure_scene(sid, B, package)
        assert stored_rows(db) == saved


def test_same_scene_steps_and_replay_do_not_record_entry(tmp_path, package_data, context):
    package = spatial_package(package_data)
    with StateMachine(tmp_path / "step.db") as sm:
        sid = create(sm, package, context)
        before = sm.read_session(sid, package, context=context)
        cmd = command(sm, sid, package, context, StepActor(A, "2,1", "2,2"), "step")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed"
        assert sm.submit_command(cmd, package, context=context) == receipt
        after = sm.read_session(sid, package, context=context)
        assert after.scenes[A].visited_count == before.scenes[A].visited_count == 1
        assert after.scenes[B].visited_count == 0


@pytest.mark.parametrize("npc", [False, True])
def test_navigation_uses_pc_only_visit_accounting(tmp_path, context, npc):
    package = navigation_package()
    with StateMachine(tmp_path / "navigation.db") as sm:
        sid = create(sm, package, context)
        actor = "npc.caretaker" if npc else None
        if npc:
            grant(sm, sid, package, context)
        assert start(sm, sid, package, context, actor=actor).status == "committed"
        assert pump(sm, sid, package, context, actor=actor).activity.status == "completed"
        state = sm.read_session(sid, package, context=context)
        assert state.scenes[B].visited_count == (0 if npc else 1)
        assert state.current_scene_id == (A if npc else B)
        assert pump(sm, sid, package, context, actor=actor).activity.status == "completed"
        assert sm.read_session(sid, package, context=context) == state


@pytest.mark.parametrize(
    "table,event",
    [
        ("scene_instances", "INSERT"),
        ("objects", "INSERT"),
        ("actors", "INSERT"),
        ("actors", "UPDATE"),
        ("sessions", "UPDATE OF current_scene_id"),
        ("scene_instances", "UPDATE OF visited_count"),
        ("event_evidence", "INSERT"),
        ("event_outbox", "INSERT"),
        ("command_receipts", "INSERT"),
    ],
)
def test_lazy_scene_transition_rolls_back_every_participant(
    tmp_path, package, context, table, event
):
    db = tmp_path / "rollback.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        cmd = command(sm, sid, package, context, EnterScene(B), "fault")
        before = stored_rows(db)
        sm._conn.execute(
            f"CREATE TEMP TRIGGER fail BEFORE {event} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'scene lifecycle fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(cmd, package, context=context)
        assert error.value.code == "storage_error"
        assert stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER fail")
        assert sm.submit_command(cmd, package, context=context).status == "committed"
        assert sm.read_session(sid, package, context=context).scenes[B].visited_count == 1


def test_preexisting_environment_and_capability_facts_survive_initialization(tmp_path, context):
    from dnd5e_engine.evaluation_state import SceneState
    from test_world_durability import durability_data

    from trpg_state_machine.package import _validate

    package = _validate(durability_data())
    c = components(tmp_path / "partial.db")
    sid = "session.partial"
    try:
        with c.store.transaction(write=True):
            c.world.runtime.initialize(
                sid, package, context, package.pc_templates[0], "pc.partial", "rng.partial"
            )
            c.facts.initialize_world(sid, package)
            # Separate participants may install facts before lifecycle in this
            # shared transaction. Existing FK dependencies are deferred to commit.
            c.store.connection.execute("PRAGMA defer_foreign_keys=ON")
            grid = json.loads(package.scene(A).grid_json)
            grid["default_lighting"] = "dark"
            c.facts.initialize_rule_scene(sid, SceneState(scene_id=A, grid=grid))
            oid = package.scenes[0].objects[0].object_id
            c.store.connection.execute(
                "INSERT INTO objects(session_id,object_id,scene_id,state,cell) "
                "VALUES (?,?,?,'open','4,4')",
                (sid, oid, A),
            )
            c.store.connection.execute("INSERT INTO object_durability VALUES (?,?,7)", (sid, oid))
            c.store.connection.execute("INSERT INTO object_openables VALUES (?,?,1,0)", (sid, oid))
            assert c.world.scenes.ensure_scene(sid, A, package)
            assert c.facts.object(sid, oid)["state"] == "open"
            assert c.facts.object(sid, oid)["cell"] == "4,4"
            assert c.facts.object_hp(sid, oid) == 7
            assert c.facts.openable_state(sid, oid) == {"jammed": True, "barred": False}
            row = c.store.connection.execute("SELECT state_json FROM mechanical_scenes").fetchone()
            assert json.loads(row[0])["grid"]["default_lighting"] == "dark"
            c.world.scenes.initialize_session(sid, "pc.partial", package.pc_templates[0], package)
            c.world.runtime.initialize_rng(sid, "rng.partial", synthetic=False)
        saved = stored_rows(tmp_path / "partial.db")
        with c.store.transaction(write=True):
            assert not c.world.scenes.ensure_scene(sid, A, package)
        assert stored_rows(tmp_path / "partial.db") == saved
    finally:
        c.store.close()


@pytest.mark.parametrize(
    "operation,code",
    [
        (SetSceneFlag(A, "flag.lamp_checked", 1), "invalid_operation"),
        (SetSceneFlag(A, "flag.door_locked", True), "invalid_target"),
        (SetSceneFlag(B, "flag.workshop_checked", True), "invalid_target"),
    ],
)
def test_flag_validation_preserves_state(tmp_path, package, context, operation, code):
    with StateMachine(tmp_path / "flags.db") as sm:
        sid = create(sm, package, context)
        before = sm.read_session(sid, package, context=context)
        if type(operation.value) is not bool:
            with pytest.raises(StateMachineError) as error:
                submit(sm, sid, package, context, operation, "invalid")
            assert error.value.code == code
        else:
            receipt = submit(sm, sid, package, context, operation, "invalid")
            assert receipt.error_code == code and receipt.event_seqs == ()
        assert sm.read_session(sid, package, context=context) == before


def test_legacy_counts_are_not_recomputed(tmp_path, package, context):
    db = tmp_path / "legacy.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        # Imported historical counter, deliberately not inferred from events.
        sm._conn.execute("UPDATE scene_instances SET visited_count=17 WHERE session_id=?", (sid,))
        saved = sm.read_session(sid, package, context=context)
    with StateMachine(db) as sm:
        assert sm.resume_session(sid, package, context=context) == saved
        with sm._transaction(write=True):
            assert not sm.scene_lifecycle.ensure_scene(sid, A, package)
        assert sm.read_session(sid, package, context=context) == saved
        assert submit(sm, sid, package, context, EnterScene(B), "leave").status == "committed"
        assert submit(sm, sid, package, context, EnterScene(A), "return").status == "committed"
        assert sm.read_session(sid, package, context=context).scenes[A].visited_count == 18


@pytest.mark.parametrize("broken", [False, True])
def test_mechanism_initialization_keeps_main_state_and_lifecycle_idempotency(
    tmp_path, context, broken
):
    from lock_support import DOOR
    from test_mechanism_service import extended_data
    from test_world_durability import durability_data
    from test_world_mechanisms import LEVER
    from world_support import setup

    data = extended_data(components=[DOOR], max_uses=3)
    durability = durability_data()["world_objects"][0]["capabilities"][-1]
    durability["initial_hp"] = 0 if broken else 20
    data["world_objects"][0]["capabilities"].append(durability)
    sm, sid, p, db = setup(tmp_path, context, data)
    with sm:
        state = sm._world_facts.mechanism_state(sid, LEVER)
        assert state.status == ("broken" if broken else "armed")
        assert (state.remaining_uses, state.activation_count) == (3, 0)
        with sm._transaction(write=True):
            saved = replace(state, status="disarmed", remaining_uses=2, activation_count=1)
            sm._world_facts.set_mechanism_state(sid, LEVER, saved)
            # The owner initialization participant must also preserve an already
            # changed seven-column row, independently of the scene existence guard.
            sm._world_facts.initialize_object_capabilities(sid, A, p)
            assert not sm.scene_lifecycle.ensure_scene(sid, A, p)
        assert sm._world_facts.mechanism_state(sid, LEVER) == saved
        rows = stored_rows(db)
    from lock_support import lock_loader

    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        restored.resume_session(sid, p, context=context)
        assert restored._world_facts.mechanism_state(sid, LEVER) == saved
        assert stored_rows(db) == rows
