"""Real public paths, deterministic SQL/row/build counts and transaction fences."""

import json
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace

import pytest
from lock_support import CRED, A
from test_lockable_passages import acquire
from test_npc_evaluation_tasks import evaluation_package as evaluation_package
from test_object_spatial_service import OBSTACLE, oriented_data
from test_observation_retention import ob_package as ob_package
from test_world_containers import BOX, container_data
from test_world_lights import lighting_data
from world_support import setup, submit

from trpg_state_machine import (
    ObjectState,
    PutItem,
    RepositionObject,
    SetObjectState,
    StateMachine,
    StateMachineError,
    StepActor,
    TakeItem,
    TypedCommand,
)
from trpg_state_machine.package import canonical_json


class CountCursor:
    def __init__(self, cursor, counts, sql):
        self.cursor, self.counts, self.sql = cursor, counts, sql

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def _row(self, row):
        if row is not None and self.sql.lstrip().upper().startswith(("SELECT", "WITH", "EXPLAIN")):
            self.counts["sql_rows"] += 1
            self.counts["returned_bytes"] += sum(len(v.encode()) for v in row if isinstance(v, str))
        return row

    def fetchone(self):
        return self._row(self.cursor.fetchone())

    def fetchall(self):
        return [self._row(r) for r in self.cursor.fetchall()]

    def __iter__(self):
        for row in self.cursor:
            yield self._row(row)


class CountConnection:
    def __init__(self, connection, counts, statements):
        self.connection, self.counts, self.statements = connection, counts, statements

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        self.statements.append(sql)
        operation = sql.lstrip().split()[0].upper()
        self.counts["sql_selects" if operation in ("SELECT", "WITH") else operation] += 1
        return CountCursor(self.connection.execute(sql, parameters), self.counts, sql)


@contextmanager
def measure(sm):
    counts, statements = Counter(), []
    original = sm._store._conn
    sm._store._conn = CountConnection(original, counts, statements)
    try:
        yield counts, statements
    finally:
        sm._store._conn = original


def observe(monkeypatch, obj, method, counts, label):
    original = getattr(obj, method)

    def counted(*args, **kwargs):
        counts[label] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(obj, method, counted)


def grow(sm, sid, n):
    """Trusted fixture adds persisted unrelated instances, no mocked read paths.

    Nonblocking overlapping objects are legal. Unrelated Actors use unique cells
    in an expanded remote map; all current configuration stays in instance tables.
    """
    f, conn = sm._world_facts, sm._conn
    remote = next(
        r[0]
        for r in conn.execute(
            "SELECT scene_id FROM scene_instances WHERE session_id=? AND scene_id<>?", (sid, A)
        )
    )
    template = f.instances.get(sid, "object", CRED)
    definition = json.loads(template["definition"])
    with sm._transaction(write=True):
        conn.execute(
            "UPDATE mechanical_scenes SET "
            "state_json=json_set(state_json,'$.grid.width',?) WHERE session_id=? AND scene_id=?",
            (n + 10, sid, remote),
        )
        for i in range(n):
            oid = f"object.unrelated_{i}"
            data = json.loads(json.dumps(definition))
            data["fields"]["object_id"] = oid
            data["fields"]["capabilities"] = {"tuple": []}
            config = {
                **template,
                "definition": canonical_json(data),
                "movable": False,
                "blocks_passage": False,
            }
            conn.execute(
                "INSERT INTO "
                "objects(session_id,object_id,scene_id,cell,orientation,item_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    sid,
                    oid,
                    A if i % 2 else remote,
                    "2,2",
                    "north",
                    canonical_json(
                        {
                            "item_slug": "amulet",
                            "quantity": 1,
                            "charges_remaining_per_unit": 0,
                            "accessible": True,
                        }
                    )
                    if i % 3 == 0
                    else None,
                ),
            )
            f.instances.put(sid, "object", oid, config)
            actor = f"npc.unrelated_{i}"
            conn.execute(
                "INSERT INTO "
                "actors(session_id,actor_id,kind,current_scene_id,cell) VALUES (?,?,'npc',?,?)",
                (sid, actor, remote, f"{i + 3},1"),
            )
            f.instances.put(
                sid,
                "actor",
                actor,
                {"actor_id": actor, "senses": (("visual", True), ("auditory", True))},
            )


def grow_scenes(sm, sid, count):
    """Add complete lazy Scene plans and unrelated ports to the persisted fixture."""
    from trpg_state_machine.instance_data import encode
    from trpg_state_machine.package import PassageDefinition, PassageEndpoint

    assert count % 2 == 0
    f, conn = sm._world_facts, sm._conn
    other = next(
        r[0]
        for r in conn.execute(
            "SELECT scene_id FROM scene_instances WHERE session_id=? AND scene_id<>?", (sid, A)
        )
    )
    template = f.instances.get(sid, "scene", other)
    planned_grid = canonical_json(f.mechanical_scene(sid, other)["grid"])
    session = f.instances.get(sid, "session", sid)
    names = tuple(f"scene.unrelated_{i}" for i in range(count))
    ports = []
    with sm._transaction(write=True):
        for i, name in enumerate(names):
            peer = names[i ^ 1]
            passage = f"passage.unrelated_{i // 2}"
            f.instances.put(
                sid,
                "scene",
                name,
                {
                    **template,
                    "scene_id": name,
                    "object_ids": (),
                    "actor_ids": (),
                    "actor_initial_ids": (),
                    "pending_objects": (),
                    "pending_definitions": (),
                    "pending_actors": (),
                    "has_grid": True,
                    "grid_json": planned_grid,
                    "exits": (peer,),
                    "exit_passages": ((peer, passage),),
                },
            )
            if i % 2 == 0:
                ports.append(
                    PassageDefinition(
                        passage,
                        (
                            PassageEndpoint(name, "0,0", "1,0"),
                            PassageEndpoint(peer, "0,0", "1,0"),
                        ),
                        None,
                    )
                )
        session["scene_ids"] += names
        session["passages"] += tuple(ports)
        conn.execute(
            "UPDATE instance_configs SET config_json=? WHERE session_id=? "
            "AND domain='session' AND instance_id=?",
            (canonical_json(encode(session)), sid, sid),
        )


@pytest.mark.parametrize("path", ("placement", "step"))
def test_public_placement_scope_is_independent_of_unrelated_growth(
    tmp_path, context, monkeypatch, path
):
    sm, sid, package, _ = setup(tmp_path, context, oriented_data())
    gm = replace(context, can_place_world_objects=True)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        calls = Counter()
        observe(monkeypatch, sm._objects.spatial, "_check_scene", calls, "full_scene_audits")
        observe(
            monkeypatch, sm._world_facts.instances, "object_definition", calls, "definition_reads"
        )
        op = (
            RepositionObject(A, OBSTACLE, "1,1", "1,1", "north", "east")
            if path == "placement"
            else StepActor(A, "0,0", "1,0")
        )
        sm._world_facts.maps.clear()
        sm._store.release_memory()
        calls.clear()
        with measure(sm) as (small, _):
            assert sm.query_world_action(sid, actor, op, package, context=gm).status == "available"
        initial = calls.copy()
        grow(sm, sid, 100)
        grow_scenes(sm, sid, 20)
        sm._world_facts.maps.clear()
        sm._store.release_memory()
        calls.clear()
        with measure(sm) as (large, sql):
            assert sm.query_world_action(sid, actor, op, package, context=gm).status == "available"
        assert calls == initial
        assert calls["full_scene_audits"] == 0
        assert large["sql_selects"] <= small["sql_selects"]
        assert large["sql_rows"] <= small["sql_rows"]
        assert large["returned_bytes"] <= small["returned_bytes"]
        assert any("object_spatial_cells" in s for s in sql)
        plan = sm._conn.execute(
            "EXPLAIN QUERY PLAN SELECT object_id FROM object_spatial_cells "
            "WHERE session_id=? AND scene_id=? AND cell=? AND blocks=1",
            (sid, A, "1,1"),
        ).fetchall()
        assert any("spatial_cell_lookup" in r[3] for r in plan)
        print("placement", dict(small), dict(large), dict(calls))


def test_container_closure_and_inventory_write_once(tmp_path, context, monkeypatch):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, package, context)
        grow(sm, sid, 100)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        op = PutItem(A, CRED, BOX, "0,0")
        calls = Counter()
        observe(monkeypatch, sm._characters, "sync_world_inventory", calls, "full_sync")
        observe(monkeypatch, sm._objects.spatial, "audit", calls, "full_audit")
        scope = sm._objects.containers._rows(sid, (CRED, BOX))
        assert set(scope) == {CRED, BOX}
        with measure(sm) as (counts, sql):
            result = submit(sm, sid, package, context, op, "scoped.put")
        assert result.status == "committed"
        assert calls == Counter()
        assert (
            sum(s.startswith("UPDATE mechanical_components SET inventory_json") for s in sql) == 1
        )
        assert sum(s.startswith("UPDATE characters SET inventory_json") for s in sql) == 1
        assert sm.get_object(sid, actor, BOX, package, context=context)["contents"] == (CRED,)
        print("container", dict(counts))


def test_missing_session_config_is_not_treated_as_empty_passages(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, oriented_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        sm._conn.execute(
            "DELETE FROM instance_configs WHERE session_id=? AND domain='session'", (sid,)
        )
        result = sm.query_world_action(
            sid,
            actor,
            RepositionObject(A, OBSTACLE, "1,1", "1,1", "north", "east"),
            package,
            context=replace(context, can_place_world_objects=True),
        )
        assert result.status == "unavailable" and result.reason == "incomplete_instance_config"
        with sm._transaction(write=True), pytest.raises(StateMachineError, match="configuration"):
            sm.scene_lifecycle.ensure_scene(sid, A, package)


@pytest.mark.parametrize("legacy", [True, False])
def test_deleted_lock_invalidates_scene_acceptance(tmp_path, context, legacy):
    from lock_support import DOOR
    from test_world_locks import multi_data
    from world_support import world_data

    sm, sid, package, _ = setup(tmp_path, context, world_data() if legacy else multi_data())
    with sm:
        lock = "lock." + DOOR if legacy else "lock.outer"
        sm._conn.execute(
            "DELETE FROM door_credentials WHERE session_id=? AND lock_id=?", (sid, lock)
        )
        sm._conn.execute("DELETE FROM door_locks WHERE session_id=? AND lock_id=?", (sid, lock))
        with sm._transaction(write=True), pytest.raises(StateMachineError, match="acceptance"):
            sm.scene_lifecycle.ensure_scene(sid, A, package)
        with pytest.raises(StateMachineError):
            sm.save_session(sid, package, context=context)


def test_loaded_cell_fields_ignore_unrelated_growth(tmp_path, context, monkeypatch):
    from trpg_state_machine.grid import GridReader
    from trpg_state_machine.runtime_instances import instance_read

    sm, sid, package, _ = setup(tmp_path, context, oriented_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        operation = StepActor(A, "0,0", "1,0")
        assert (
            sm.query_world_action(sid, actor, operation, package, context=context).status
            == "available"
        )
        calls = []
        original = GridReader.cell_occupants.__wrapped__
        # _read is the outer consistent-scope wrapper.
        original = original.__wrapped__

        @instance_read("scene", "spatial", "cells", capacity=128)
        def counted(self, *args):
            calls.append(args)
            return original(self, *args)

        monkeypatch.setattr(GridReader, "cell_occupants", counted)
        grow(sm, sid, 100)
        grow_scenes(sm, sid, 20)
        before = dict(sm._store.memory.stats)
        with measure(sm) as (counts, statements):
            for _ in range(3):
                assert (
                    sm.query_world_action(sid, actor, operation, package, context=context).status
                    == "available"
                )
        assert calls == []
        assert not any("FROM object_spatial_cells" in s for s in statements)
        assert sm._store.memory.stats["model_parses"] == before["model_parses"]
        owner = sm._store.memory.values[("instance", sid, "scene", A)][0]
        assert owner.spatial.cells
        assert counts["sql_rows"] < 20


def test_object_list_reuses_light_and_does_not_materialize_remote_objects(
    tmp_path, context, monkeypatch
):
    sm, sid, package, _ = setup(tmp_path, context, lighting_data())
    with sm:
        grow(sm, sid, 30)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        builds = Counter()
        # Observe the real function inside the memo wrapper, not bypass its work.
        from trpg_state_machine.world_lights import LightService

        method = LightService.sources.__wrapped__

        def counted(self, *args, **kwargs):
            builds["light_preparations"] += 1
            return method(self, *args, **kwargs)

        from trpg_state_machine.runtime_instances import instance_read

        monkeypatch.setattr(
            LightService,
            "sources",
            instance_read("scene", "lighting", "sources")(counted),
        )
        views = sm.list_scene_objects(sid, actor, package, context=context)
        # Adding capability-free objects leaves the already prepared light
        # association and sources valid, including through the public list path.
        assert builds["light_preparations"] == 0
        assert not {v["object_id"] for v in views} & {
            f"object.unrelated_{i}" for i in range(0, 30, 2)
        }


def test_savepoint_and_old_reader_keep_spatial_index_transactional(tmp_path, context):
    sm, sid, package, db = setup(tmp_path, context, oriented_data())
    with sm, StateMachine(db, dnd_loader=sm._rules.loader) as reader:
        old = tuple(
            sm._conn.execute(
                "SELECT cell,blocks FROM object_spatial_cells WHERE session_id=? "
                "AND object_id=? ORDER BY cell",
                (sid, OBSTACLE),
            )
        )
        with reader._transaction():
            assert [
                tuple(r)
                for r in reader._conn.execute(
                    "SELECT cell,blocks FROM object_spatial_cells WHERE session_id=? "
                    "AND object_id=? ORDER BY cell",
                    (sid, OBSTACLE),
                )
            ] == [tuple(r) for r in old]
            with sm._transaction(write=True):
                with pytest.raises(RuntimeError), sm._store.savepoint():
                    sm._world_facts.place(sid, OBSTACLE, "1,1", orientation="east")
                    assert sm._objects.spatial.project(sid, OBSTACLE, package).occupied_cells == (
                        "1,1",
                        "1,2",
                    )
                    raise RuntimeError("rollback candidate")
                assert sm._objects.spatial.project(sid, OBSTACLE, package).occupied_cells == (
                    "1,1",
                    "2,1",
                )
                sm._world_facts.place(sid, OBSTACLE, "1,1", orientation="east")
            assert [
                tuple(r)
                for r in reader._conn.execute(
                    "SELECT cell,blocks FROM object_spatial_cells WHERE session_id=? "
                    "AND object_id=? ORDER BY cell",
                    (sid, OBSTACLE),
                )
            ] == [tuple(r) for r in old]
        assert tuple(
            r[0]
            for r in reader._conn.execute(
                "SELECT cell FROM object_spatial_cells WHERE session_id=? AND "
                "object_id=? ORDER BY cell",
                (sid, OBSTACLE),
            )
        ) == ("1,1", "1,2")


def test_reentry_uses_certificate_and_rejects_deleted_dependency(tmp_path, context, monkeypatch):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        calls = Counter()
        observe(monkeypatch, sm.scene_lifecycle, "_validate_scene", calls, "scene_audits")
        with sm._transaction(write=True):
            assert sm.scene_lifecycle.ensure_scene(sid, A, package) is False
            assert calls == Counter()
            with pytest.raises(RuntimeError), sm._store.savepoint():
                sm._conn.execute(
                    "DELETE FROM object_openables WHERE session_id=? AND object_id=?",
                    (sid, BOX),
                )
                with pytest.raises(StateMachineError, match="acceptance"):
                    sm.scene_lifecycle.ensure_scene(sid, A, package)
                raise RuntimeError("restore dependency")
            assert sm.scene_lifecycle.ensure_scene(sid, A, package) is False
        sm._conn.execute(
            "DELETE FROM object_openables WHERE session_id=? AND object_id=?", (sid, BOX)
        )
        with sm._transaction(write=True), pytest.raises(StateMachineError):
            sm.scene_lifecycle.ensure_scene(sid, A, package)
        with pytest.raises(StateMachineError):
            sm.save_session(sid, package, context=context)


def test_unrelated_event_does_not_build_mechanism_or_inventory_index(
    tmp_path, context, monkeypatch
):
    from trpg_state_machine import SetSceneFlag

    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        calls = Counter()
        observe(monkeypatch, sm._objects.mechanisms, "_build_index", calls, "mechanism_builds")
        observe(monkeypatch, sm._characters, "sync_world_inventory", calls, "inventory_syncs")
        observe(monkeypatch, sm._objects.spatial, "audit", calls, "audits")
        flag = package.scenes[0].flags[0][0]
        old = dict(package.scenes[0].flags)[flag]
        assert (
            submit(sm, sid, package, context, SetSceneFlag(A, flag, not old), "flag").status
            == "committed"
        )
        assert calls == Counter()


def test_mechanism_index_is_once_per_real_cascade(tmp_path, context, monkeypatch):
    from test_mechanism_service import ordered_data
    from test_world_mechanisms import LEVER

    from trpg_state_machine import ActivateMechanism

    sm, sid, package, _ = setup(tmp_path, context, ordered_data())
    with sm:
        calls = Counter()
        from trpg_state_machine.runtime_instances import instance_read
        from trpg_state_machine.world_mechanisms import MechanismService

        original = MechanismService._scene_index.__wrapped__

        @instance_read("scene", "mechanisms", "index")
        def counted(self, *args):
            calls["builds"] += 1
            return original(self, *args)

        monkeypatch.setattr(MechanismService, "_scene_index", counted)
        assert (
            submit(sm, sid, package, context, ActivateMechanism(A, LEVER, "0,0"), "activate").status
            == "committed"
        )
        assert calls["builds"] == 1


def test_package_index_is_shared_by_dispatch_and_fired_signal_capture(
    tmp_path, package_data, context, monkeypatch
):
    from spatial_support import create
    from test_events import load_event_package

    from trpg_state_machine import SetSceneFlag
    from trpg_state_machine.package import AdventurePackage

    package = load_event_package(tmp_path, package_data).package
    calls = Counter()
    original = AdventurePackage.__getattribute__

    def observe_definitions(obj, name):
        if obj is package and name == "events":
            calls["definition_enumerations"] += 1
        return original(obj, name)

    monkeypatch.setattr(AdventurePackage, "__getattribute__", observe_definitions)
    with StateMachine(tmp_path / "program.db") as sm:
        sid = create(sm, package, context)
        assert calls["definition_enumerations"] == 1
        for i, value in enumerate((True, False, True)):
            assert (
                submit(
                    sm,
                    sid,
                    package,
                    context,
                    SetSceneFlag(A, "flag.lamp_checked", value),
                    f"flag.{i}",
                ).status
                == "committed"
            )
        assert calls["definition_enumerations"] == 1
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM trigger_history WHERE session_id=?", (sid,)
            ).fetchone()[0]
            == 1
        )


def test_close_updates_only_affected_accessibility(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, package, context)
        assert (
            submit(sm, sid, package, context, PutItem(A, CRED, BOX, "0,0"), "put").status
            == "committed"
        )
        with measure(sm) as (_, sql):
            assert (
                submit(
                    sm, sid, package, context, SetObjectState(A, BOX, ObjectState.CLOSED), "close"
                ).status
                == "committed"
            )
        assert (
            sum(s.startswith("UPDATE mechanical_components SET inventory_json") for s in sql) == 0
        )  # ground contents have no owner projection


def test_task_reads_only_fixed_grants_with_growing_ungranted_history(tmp_path, ob_package, context):
    from test_npc_evaluation_tasks import create_task, prepare
    from test_npc_modes import new_observation
    from test_observation_retention import advance

    with StateMachine(tmp_path / "history.db") as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        task = create_task(sm, sid, ob_package, npc, ids)
        advance(sm, sid, ob_package, context, 100)
        sm._store.release_memory()
        with measure(sm) as (small, _):
            original = sm.get_npc_evaluation_recovery_input(
                task.evaluation_id, ob_package, context=npc
            )
        for i in range(30):
            new_observation(sm, sid, ob_package, context, "scene.workshop" if i % 2 == 0 else A)
        advance(sm, sid, ob_package, context, 200)
        sm._store.release_memory()
        with measure(sm) as (large, sql):
            recovered = sm.get_npc_evaluation_recovery_input(
                task.evaluation_id, ob_package, context=npc
            )
        assert recovered == original
        assert large["sql_selects"] <= small["sql_selects"]
        assert large["sql_rows"] <= small["sql_rows"]
        assert any("o.content_json" in s and "json_each" in s for s in sql)
        with measure(sm) as (_, metadata_sql):
            sm.get_pending_npc_observation_work(sid, ob_package, context=npc)
        assert not any("o.content_json" in s for s in metadata_sql)
        print("task_recovery", dict(small), dict(large))


def test_npc_result_target_admission_does_not_enumerate_scenes(
    tmp_path, ob_package, context, monkeypatch
):
    from test_npc_evaluation_tasks import create_task, prepare

    from trpg_state_machine.npc_results import NPCActionIntent, NPCEvaluationResult

    with StateMachine(tmp_path / "target.db") as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        task = create_task(sm, sid, ob_package, npc, ids)
        calls = Counter()
        observe(monkeypatch, sm._world_facts.instances, "scenes", calls, "scene_enumerations")
        result = NPCEvaluationResult(
            action_intents=(NPCActionIntent("move_actor", "scene.workshop", 0),)
        )
        with measure(sm) as (_, sql):
            receipt = sm.submit_npc_evaluation_result(
                task.evaluation_id, result, ob_package, context=npc
            )
        assert receipt.status == "completed"
        assert calls == Counter()
        assert any("SELECT 1 FROM instance_configs" in s and "domain='scene'" in s for s in sql)


def test_real_schema39_migration_is_atomic_and_preserves_facts(tmp_path, context, monkeypatch):
    import io
    import os
    import sqlite3
    import subprocess
    import sys
    import zipfile
    from pathlib import Path

    from lock_support import lock_loader
    from test_migrations import assert_original_rows_equal, stored_rows

    from trpg_state_machine import runtime_integrity

    root = Path(__file__).resolve().parents[1]
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "7927a382738fc97e2420967c3e1c6280c717d1fb", "src"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    source = tmp_path / "schema39"
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        bundle.extractall(source)
    document = tmp_path / "package.json"
    document.write_text(json.dumps(oriented_data()), encoding="utf-8")
    code = """
import json,sys
from pathlib import Path
from world_support import setup
from trpg_state_machine import TrustedContext
sm,sid,p,db=setup(Path(sys.argv[1]),TrustedContext('authenticated:alice','player.alice'),json.load(open(sys.argv[2])))
assert sm._conn.execute('PRAGMA user_version').fetchone()[0]==39
print(sid)
sm.close()
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), str(document)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(source / "src"), str(root / "tests"))),
        },
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    sid = process.stdout.strip()
    db = tmp_path / "world.db"
    before = stored_rows(db)
    certify = runtime_integrity.certify_scene

    def fail(*args):
        raise RuntimeError("migration acceptance failpoint")

    monkeypatch.setattr(runtime_integrity, "certify_scene", fail)
    with pytest.raises(RuntimeError, match="failpoint"):
        StateMachine(db, dnd_loader=lock_loader())
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 39
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='object_spatial_cells'"
        ).fetchone()
    assert stored_rows(db) == before
    monkeypatch.setattr(runtime_integrity, "certify_scene", certify)
    with StateMachine(db, dnd_loader=lock_loader()) as sm:
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 40
        assert_original_rows_equal(before, stored_rows(db))
        assert tuple(
            r[0]
            for r in sm._conn.execute(
                "SELECT cell FROM object_spatial_cells WHERE session_id=? "
                "AND object_id=? ORDER BY cell",
                (sid, OBSTACLE),
            )
        ) == ("1,1", "2,1")
        assert (
            sm._conn.execute(
                "SELECT complete FROM scene_integrity WHERE session_id=? AND scene_id=?", (sid, A)
            ).fetchone()[0]
            == 1
        )
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER spatial_index_objects_update")
    with pytest.raises(StateMachineError) as error:
        StateMachine(db, dnd_loader=lock_loader())
    assert error.value.code == "unsupported_storage_schema"


def test_container_sql_vm_and_ram_work_are_fixed_with_unrelated_growth(tmp_path, context):
    from trpg_state_machine.commands import OPERATIONS

    results = []
    for growth in (0, 150):
        folder = tmp_path / str(growth)
        folder.mkdir()
        sm, sid, package, _ = setup(folder, context, container_data())
        with sm:
            acquire(sm, sid, package, context)
            if growth:
                grow(sm, sid, growth)
            state = sm.read_session(sid, package, context=context)
            version, serial = state.world_version, 0

            def cycle(sm=sm, sid=sid, state=state, package=package):
                nonlocal version, serial
                for payload in (PutItem(A, CRED, BOX, "0,0"), TakeItem(A, CRED, BOX, "0,0")):
                    serial += 1
                    kind = next(k for k, cls in OPERATIONS.items() if type(payload) is cls)
                    receipt = sm.submit_command(
                        TypedCommand(
                            sid, f"vm.{serial}", state.pc_actor_id, kind, payload, version
                        ),
                        package,
                        context=context,
                    )
                    assert receipt.status == "committed"
                    version = receipt.world_version

            cycle()
            cycle()
            vm_steps = 0

            def progress():
                nonlocal vm_steps
                vm_steps += 1
                return 0

            previous = dict(sm._store.memory.stats)
            sm._conn.set_progress_handler(progress, 1)
            try:
                with measure(sm) as (counts, _):
                    cycle()
                    cycle()
            finally:
                sm._conn.set_progress_handler(None, 0)
            runtime = {k: sm._store.memory.stats[k] - previous[k] for k in previous}
            results.append(
                (
                    vm_steps,
                    counts["sql_selects"],
                    counts["sql_rows"],
                    runtime["ram_rows"],
                    runtime["published_rows"],
                )
            )
            assert runtime["snapshot_builds"] == runtime["json_parses"] == 0
    assert results[0] == results[1]


def test_schema40_object_lookup_addendum_preserves_facts_and_old_read_scope(tmp_path, context):
    import sqlite3

    sm, sid, package, database = setup(tmp_path, context, container_data())
    before = sm.read_session(sid, package, context=context)
    sm.close()
    with sqlite3.connect(database) as disk:
        disk.execute("DROP INDEX spatial_object_lookup")
    with sqlite3.connect(database, isolation_level=None) as old:
        old.execute("BEGIN")
        fixed = old.execute("SELECT * FROM objects ORDER BY object_id").fetchall()
        with StateMachine(database, dnd_loader=sm._rules.loader) as reopened:
            assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == 40
            assert reopened._conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='spatial_object_lookup'"
            ).fetchone()[0] == (
                "CREATE INDEX spatial_object_lookup ON object_spatial_cells(session_id,object_id)"
            )
            assert reopened.read_session(sid, package, context=context) == before
        assert old.execute("SELECT * FROM objects ORDER BY object_id").fetchall() == fixed
        old.rollback()
