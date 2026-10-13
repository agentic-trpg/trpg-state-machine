"""Boundary tests use the real shared store without constructing StateMachine."""

import ast
import asyncio
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from trpg_state_machine.errors import StateMachineError
from trpg_state_machine.storage import SqliteStore


def components(path, loader=None):
    """Explicit test wiring, deliberately never imports/constructs StateMachine."""
    from dnd5e_srd_data import BundledAssetLoader

    from trpg_state_machine.combat_handler import CombatHandler
    from trpg_state_machine.domain_facts import CharacterFacts, CombatRecords, WorldFacts
    from trpg_state_machine.event_capture import EventCapture
    from trpg_state_machine.event_persistence import EventPersistence
    from trpg_state_machine.npc_bootstrap import NPCBootstrap
    from trpg_state_machine.package_effects import PackageEffects
    from trpg_state_machine.rules_adapter import RulesAdapter
    from trpg_state_machine.session_access import SessionAccess
    from trpg_state_machine.spatial import WorldHandler

    store = SqliteStore(path)
    sessions = SessionAccess(store)
    facts, records = WorldFacts(store), CombatRecords(store)
    characters = CharacterFacts(store, facts)
    capture, persistence = EventCapture(store, characters, facts), EventPersistence(store)
    world = WorldHandler(store, sessions, characters, facts, records, capture, NPCBootstrap(store))
    effects = PackageEffects(store, world, capture)
    rules = RulesAdapter(store, loader or BundledAssetLoader(), characters, facts, records, world)
    combat = CombatHandler(
        store, sessions, characters, facts, records, world, rules, capture, effects, persistence
    )
    return SimpleNamespace(**locals())


def seed(c, package, context, sid="session.domain", pc="pc.domain"):
    from trpg_state_machine.package import SYNTHETIC_EVALUATION_RULESET

    with c.store.transaction(write=True):
        c.sessions.initialize(sid, package, context, package.pc_templates[0], pc, "rng." + sid)
        c.facts.initialize_world(sid, package)
        c.world._initialize_scene(sid, package.entry_scene_id, package)
        for scene in package.scenes:
            if scene.grid_json and scene.scene_id != package.entry_scene_id:
                c.world._initialize_scene(sid, scene.scene_id, package)
                c.facts.mark_unvisited(sid, scene.scene_id)
        c.characters.initialize_pc(sid, pc, package.entry_scene_id, package.pc_templates[0])
        c.sessions.initialize_rng(
            sid, "rng." + sid, synthetic=package.ruleset_binding == SYNTHETIC_EVALUATION_RULESET
        )
        row = c.sessions._session(sid, package, context)
        if package.noncombat_foundation_json:
            c.combat._initialize_noncombat(row, package)
    return sid, pc


def test_world_composes_without_facade_and_isolates_sessions(tmp_path, package_data, context):
    from spatial_support import A, B
    from test_gc1_doors_and_effects import DOOR, door_data

    from trpg_state_machine._operations import EnterScene, ObjectState, SetObjectState
    from trpg_state_machine.package import _validate

    package = _validate(door_data(package_data))
    c = components(tmp_path / "world.db")
    sid, pc = seed(c, package, context)
    seed(c, package, context, "session.other", "pc.other")
    for operation in (
        SetObjectState(A, DOOR, ObjectState.OPEN),
        EnterScene(B),
        SetObjectState(B, DOOR, ObjectState.CLOSED),
        SetObjectState(B, DOOR, ObjectState.OPEN),
        EnterScene(A),
    ):
        with c.store.transaction(write=True):
            row = c.sessions._session(sid, package, context)
            events = c.world._operate(row, package, operation, actor_id=pc)
            c.persistence._append_events(
                sid, 0, events, source_command_id="direct", package=package
            )
    assert c.characters.position(sid, pc).scene_id == A
    assert (
        c.store.connection.execute(
            "SELECT state FROM objects WHERE session_id=? AND object_id=?", ("session.other", DOOR)
        ).fetchone()[0]
        == "closed"
    )
    before = c.store.connection.total_changes
    assert c.world.get_spatial_view(sid, pc, package, context=context).doors[0].state == "open"
    assert c.store.connection.total_changes == before
    with pytest.raises(StateMachineError, match="active write"):
        c.characters.set_hp(sid, pc, 1)
    c.store.close()
    with pytest.raises(StateMachineError, match="closed"):
        c.facts.switch(sid, DOOR, "closed")


def test_real_combat_composes_without_facade_and_atomic_world_failure(
    tmp_path, package_data, context
):
    from dnd5e_engine.evaluation_contracts import CombatIntentPayload
    from dnd_support import NPC, WEAPON, loader, make_package

    from trpg_state_machine._execution import _WorldEffectKind, _WorldEffectOrigin
    from trpg_state_machine._operations import SetSceneFlag
    from trpg_state_machine.commands import TypedCommand

    assets = loader()
    package = make_package(package_data, assets)
    c = components(tmp_path / "combat.db", assets)
    sid, pc = seed(c, package, context)
    with c.store.transaction():
        row = dict(c.sessions._session(sid, package, context))
    import json

    from trpg_state_machine.combat import _remap

    initial = json.loads(package.combat_foundation_json)["snapshot"]
    with c.store.transaction(write=True):
        c.characters.initialize_rule_positions(
            row,
            _remap(initial["combat_state"]["actor_zone"], row["pc_template_id"], pc),
            initial["scene_state"]["scene_id"],
        )
    prepared = c.rules._prepare_combat(row, package)
    with c.store.transaction(write=True):
        c.combat._initialize_combat(row, package, prepared)
    command = TypedCommand(
        sid,
        "attack.domain",
        pc,
        "combat.intent",
        CombatIntentPayload(intent_type="attack", target_id=NPC, weapon_id=WEAPON),
        0,
    )
    receipt = asyncio.run(c.combat.submit_command_async(command, package, context=context))
    assert receipt.status == "committed"
    assert asyncio.run(c.combat.submit_command_async(command, package, context=context)) == receipt
    before = c.combat.read_combat_snapshot(sid, package, context=context)
    assert (
        c.store.connection.execute(
            "SELECT count(*) FROM events WHERE session_id=? AND event_type='combat.engine_event'",
            (sid,),
        ).fetchone()[0]
        > 0
    )
    # These are real SQLite failures after character/combat/world writes and capture.
    c.store.connection.execute(
        "CREATE TRIGGER fail_world_outbox BEFORE INSERT ON event_outbox "
        "BEGIN SELECT RAISE(ABORT,'world consequence fault'); END"
    )
    flags = c.store.connection.execute(
        "SELECT flags_json FROM scene_instances WHERE session_id=? AND scene_id=?",
        (sid, package.entry_scene_id),
    ).fetchone()[0]
    import json

    flag, value = next(iter(json.loads(flags).items()))
    with pytest.raises(StateMachineError) as failure, c.store.transaction(write=True):
        row = c.sessions._session(sid, package, context)
        c.characters.set_hp(sid, pc, 1)
        c.records.update_rule_state(sid, "round_number", 99)
        events = c.world._operate(
            row,
            package,
            SetSceneFlag(package.entry_scene_id, flag, not value),
            effect_origin=_WorldEffectOrigin(_WorldEffectKind.CHECK, "fault.domain"),
        )
        c.persistence._append_events(
            sid, row["world_version"], events, source_command_id="fault.domain", package=package
        )
    assert failure.value.code == "storage_error"
    assert c.combat.read_combat_snapshot(sid, package, context=context) == before
    assert (
        c.store.connection.execute(
            "SELECT flags_json FROM scene_instances WHERE session_id=? AND scene_id=?",
            (sid, package.entry_scene_id),
        ).fetchone()[0]
        == flags
    )
    assert not c.store.connection.execute(
        "SELECT 1 FROM event_records WHERE source_command_id='fault.domain'"
    ).fetchone()
    c.store.close()


def test_public_world_entry_executes_component_once(
    tmp_path, package, context, request_for, monkeypatch
):
    from trpg_state_machine import SetSceneFlag, StateMachine, TypedCommand

    with StateMachine(tmp_path / "once.db") as sm:
        sid = sm.create_session(request_for(), package, context=context).session_id
        state = sm.read_session(sid, package, context=context)
        flag, value = next(iter(state.scenes[state.current_scene_id].local_flags.items()))
        calls = []
        execute = sm._world._operate

        def tracked(*args, **kwargs):
            calls.append(kwargs)
            return execute(*args, **kwargs)

        monkeypatch.setattr(sm._world, "_operate", tracked)
        command = TypedCommand(
            sid,
            "once",
            state.pc_actor_id,
            "set_scene_flag",
            SetSceneFlag(state.current_scene_id, flag, not value),
            state.world_version,
        )
        assert sm.submit_command(command, package, context=context).status == "committed"
        assert len(calls) == 1 and calls[0]["actor_id"] == state.pc_actor_id
        sm.submit_command(command, package, context=context)
        assert len(calls) == 1


def test_combat_initialization_keeps_legacy_resource_rejection(tmp_path, package_data, context):
    from dnd_support import create, initialize, loader
    from dnd_support import package_data as combat_data

    from trpg_state_machine import StateMachine
    from trpg_state_machine.package import _validate

    assets = loader()
    data = combat_data(package_data, assets)
    data["pc_templates"][0]["character"]["resources"] = {"resource.focus": 1}
    package = _validate(data)
    with StateMachine(tmp_path / "legacy-resources.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        before = sm.read_session(sid, package, context=context)
        receipt = initialize(sm, sid, package, context)
        assert (receipt.status, receipt.error_code) == ("rejected", "unsupported_combat_foundation")
        assert sm.read_session(sid, package, context=context) == before
        assert sm._conn.execute("SELECT count(*) FROM mechanical_actors").fetchone()[0] == 0


def test_production_writers_and_dependency_direction():
    root = Path(__file__).parents[1] / "src" / "trpg_state_machine"
    owners = {
        "actors": "CharacterFacts",
        "characters": "CharacterFacts",
        "mechanical_actors": "CharacterFacts",
        "mechanical_components": "CharacterFacts",
        "objects": "WorldFacts",
        "door_locks": "WorldFacts",
        "door_credentials": "WorldFacts",
        "scene_instances": "WorldFacts",
        "world_states": "WorldFacts",
        "mechanical_scenes": "WorldFacts",
        "combats": "CombatRecords",
        "sessions": "SessionAccess",
        "rng_states": "SessionAccess",
        "command_receipts": "SessionAccess",
        "navigation_activities": "ActivityRecords",
    }
    components = {
        "storage.py",
        "domain_facts.py",
        "session_access.py",
        "spatial.py",
        "combat_handler.py",
        "rules_adapter.py",
        "event_capture.py",
        "event_persistence.py",
        "package_effects.py",
        "npc_bootstrap.py",
        "activity_handler.py",
        "activity_records.py",
        "navigation_plan.py",
        "navigation_driver.py",
        "navigation_package.py",
        "navigation_models.py",
        "item_package.py",
    }
    writers = set()
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        if path.name in components:
            for node in ast.walk(tree):
                assert not (isinstance(node, ast.ImportFrom) and node.module == "service")
                assert not (isinstance(node, ast.Name) and node.id == "StateMachine")
                assert not (isinstance(node, ast.FunctionDef) and node.name == "__getattr__")
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            parents = {
                child: parent for parent in ast.walk(cls) for child in ast.iter_child_nodes(parent)
            }
            for node in ast.walk(cls):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"execute", "executemany"}
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    continue
                sql = " ".join(node.args[0].value.split())
                match = re.match(r"(?:INSERT INTO|UPDATE|DELETE FROM) (\w+)\b", sql)
                if match and match[1] in owners:
                    table = match[1]
                    if table == "sessions" and sql.startswith("UPDATE sessions SET event_seq"):
                        assert cls.name == "EventPersistence"
                    else:
                        assert cls.name == owners[table], (path.name, cls.name, sql)
                    function = parents[node]
                    while not isinstance(function, ast.FunctionDef):
                        function = parents[function]
                    assert any(
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "require_write"
                        for call in ast.walk(function)
                    ), (cls.name, function.name)
                    writers.add(table)
    assert writers == owners.keys()


def test_store_scopes_rollback_savepoint_expire_and_never_commit_early(tmp_path):
    path = tmp_path / "scope.db"
    store = SqliteStore(path)
    with store.transaction(write=True) as scope:
        scope.execute("CREATE TABLE probe(value INTEGER)")
        scope.execute("INSERT INTO probe VALUES (1)")
    with store.transaction(write=True) as scope:
        escaped_write = scope.execute
        scope.execute("UPDATE probe SET value=2")
        for control in (
            "COMMIT",
            "END;",
            "ROLLBACK",
            "BEGIN",
            "SAVEPOINT bypass",
            "RELEASE x",
            "-- comment\nCOMMIT",
            "PRAGMA query_only=OFF",
        ):
            with pytest.raises(StateMachineError, match="outer scope"):
                scope.execute(control)
        assert store.connection.in_transaction
        with sqlite3.connect(path) as other:
            assert other.execute("SELECT value FROM probe").fetchone()[0] == 1
        with pytest.raises(ValueError), scope.savepoint():
            scope.execute("UPDATE probe SET value=3")
            raise ValueError("world consequence failed")
        assert scope.fetchone("SELECT value FROM probe")[0] == 2
        with pytest.raises(StateMachineError, match="savepoint"), store.transaction():
            pass
    with pytest.raises(StateMachineError, match="ended"):
        escaped_write("UPDATE probe SET value=9")
    with pytest.raises(StateMachineError, match="active write"):
        store.require_write()
    with pytest.raises(ValueError), store.transaction(write=True) as scope:
        scope.execute("UPDATE probe SET value=4")
        raise ValueError("outer failure")
    with store.transaction() as scope:
        assert scope.fetchone("SELECT value FROM probe")[0] == 2
        with pytest.raises(StateMachineError, match="active write"):
            store.require_write()
        with pytest.raises(StateMachineError, match="active write"):
            scope.execute("UPDATE probe SET value=9")
        with pytest.raises(StateMachineError, match="read query"):
            scope.fetchone("UPDATE probe SET value=9 RETURNING value")
    store.close()
    with pytest.raises(StateMachineError, match="closed"), store.transaction():
        pass


def test_store_rejects_other_thread_even_inside_active_scope(tmp_path):
    store = SqliteStore(tmp_path / "thread.db")
    with store.transaction(write=True), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.require_write)
        with pytest.raises(StateMachineError, match="another thread"):
            future.result()
    store.close()
