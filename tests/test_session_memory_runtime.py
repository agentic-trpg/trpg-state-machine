"""Public paths plus the real durable boundary; no synthetic memory-only store."""

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Barrier, Event

import pytest
from lock_support import CRED, A
from spatial_support import command, create
from test_lockable_passages import acquire
from test_scoped_runtime_validation import grow, measure
from test_world_containers import BOX, container_data
from world_support import setup

from trpg_state_machine import PutItem, StateMachine, StateMachineError


def test_public_hot_reads_do_not_select_parse_or_build_again(tmp_path, context, monkeypatch):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        sm.get_object(sid, actor, BOX, package, context=context)
        sm.read_carried_items(sid, actor, package, context=context)
        model_parses = sm._store.memory.stats["model_parses"]
        counts = {"json": 0}
        original = json.loads

        def counted(*args, **kwargs):
            counts["json"] += 1
            return original(*args, **kwargs)

        # Snapshot assembly is cached as a detached parsed input. Public object
        # views retain disclosure work, while their fact/config parsing is reused.
        with measure(sm) as (sql, _), monkeypatch.context() as patch:
            patch.setattr(json, "loads", counted)
            for _ in range(4):
                sm.read_session(sid, package, context=context)
            for _ in range(4):
                sm.get_object(sid, actor, BOX, package, context=context)
                sm.read_carried_items(sid, actor, package, context=context)
        assert sql["sql_selects"] == sql["sql_rows"] == 0
        assert counts["json"] == 0
        assert sm._store.memory.stats["model_parses"] == model_parses


def test_fixed_snapshot_and_returned_nested_values_are_detached(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        old = sm.read_session(sid, package, context=context)
        old.world.flags["caller.changed"] = True
        assert "caller.changed" not in sm.read_session(sid, package, context=context).world.flags
        acquire(sm, sid, package, context)
        current = sm.read_session(sid, package, context=context)
        assert current.world_version > old.world_version
        assert current.character.inventory != old.character.inventory
        assert CRED not in old.character.inventory


def test_candidate_and_nested_rollback_never_mutate_published_rows(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        facts = sm._world_facts
        original = facts.object(sid, CRED)
        with sm._transaction(write=True):
            with pytest.raises(ValueError), sm._store.savepoint():
                sm._conn.execute(
                    "UPDATE objects SET cell='0,0' WHERE session_id=? AND object_id=?", (sid, CRED)
                )
                assert facts.object(sid, CRED)["cell"] == "0,0"
                assert original["cell"] != "0,0"
                with sm._store.savepoint():
                    sm._conn.execute(
                        "UPDATE objects SET cell='1,0' WHERE session_id=? AND object_id=?",
                        (sid, CRED),
                    )
                assert facts.object(sid, CRED)["cell"] == "1,0"
                raise ValueError("discard candidate")
            assert dict(facts.object(sid, CRED)) == dict(original)
        assert dict(facts.object(sid, CRED)) == dict(original)


@pytest.mark.parametrize("failure", ("before", "unknown", "publish", "partial"))
def test_real_put_commit_faults_reconcile_and_replay_once(tmp_path, context, monkeypatch, failure):
    sm, sid, package, database = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, package, context)
        operation = command(sm, sid, package, context, PutItem(A, CRED, BOX, "0,0"), "fault.put")
        before = sm.read_session(sid, package, context=context)
        commit = sm._store._commit
        publish = sm._store.memory.publish

        def fail_commit():
            if failure == "unknown":
                commit()
                raise OSError("lost commit acknowledgement")
            raise sqlite3.OperationalError("injected before commit")

        def fail_publish(scope):
            if failure == "partial":
                remember = sm._store.memory.remember

                def partial(key, value):
                    remember(key, value)
                    raise MemoryError("injected partway through publication")

                with monkeypatch.context() as patch:
                    patch.setattr(sm._store.memory, "remember", partial)
                    publish(scope)
            raise MemoryError("injected after durable commit")

        with monkeypatch.context() as patch:
            if failure in ("publish", "partial"):
                patch.setattr(sm._store.memory, "publish", fail_publish)
            else:
                patch.setattr(sm._store, "_commit", fail_commit)
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(operation, package, context=context)
        assert (
            error.value.code
            == {
                "before": "storage_error",
                "unknown": "commit_outcome_unknown",
                "publish": "committed_publication_pending",
                "partial": "committed_publication_pending",
            }[failure]
        )
        with sqlite3.connect(database) as disk:
            container = disk.execute(
                "SELECT container_id FROM objects WHERE session_id=? AND object_id=?", (sid, CRED)
            ).fetchone()[0]
            receipts = disk.execute(
                "SELECT count(*) FROM command_receipts WHERE session_id=? AND command_id=?",
                (sid, operation.command_id),
            ).fetchone()[0]
        if failure == "before":
            assert container is None and receipts == 0
            assert sm.read_session(sid, package, context=context) == before
        else:
            assert container == BOX and receipts == 1
            with pytest.raises(StateMachineError, match="reconciliation"):
                sm.read_session(sid, package, context=context)
            sm.resume_session(sid, package, context=context)
        receipt = sm.submit_command(operation, package, context=context)
        assert receipt.status == "committed"
        assert receipt == sm.submit_command(operation, package, context=context)
        assert (
            sm.read_session(sid, package, context=context).world_version == before.world_version + 1
        )
        assert sm._world_facts.object(sid, CRED)["container_id"] == BOX


def test_paused_session_does_not_block_other_session(tmp_path, context, monkeypatch):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        other = create(sm, package, context, "second.session")
        expected = sm.read_session(other, package, context=context)
        acquire(sm, sid, package, context)
        operation = command(sm, sid, package, context, PutItem(A, CRED, BOX, "0,0"), "pause.put")
        with monkeypatch.context() as patch:
            patch.setattr(
                sm._store.memory, "publish", lambda _: (_ for _ in ()).throw(MemoryError())
            )
            with pytest.raises(StateMachineError, match="committed"):
                sm.submit_command(operation, package, context=context)
        assert sm.read_session(other, package, context=context) == expected
        with pytest.raises(StateMachineError):
            sm.read_session(sid, package, context=context)
        sm.resume_session(sid, package, context=context)


def test_unload_cold_reload_and_independent_process_persistence(tmp_path, context):
    sm, sid, package, database = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, package, context)
        operation = command(sm, sid, package, context, PutItem(A, CRED, BOX, "0,0"), "restart.put")
        receipt = sm.submit_command(operation, package, context=context)
        old = sm.read_session(sid, package, context=context)
        sm.unload_session(sid, package, context=context)
        assert sm._store.memory.bytes == 0
        assert sm.read_session(sid, package, context=context) == old
    script = """
import sys
from trpg_state_machine import StateMachine
from trpg_state_machine.package import _validate
from lock_support import lock_loader
from test_world_containers import container_data
from trpg_state_machine.models import TrustedContext
with StateMachine(sys.argv[1], dnd_loader=lock_loader()) as sm:
    p = _validate(container_data())
    context = TrustedContext('authenticated:alice', 'player.alice')
    s = sm.resume_session(sys.argv[2], p, context=context)
    assert s.world_version == int(sys.argv[3])
    assert sm._world_facts.object(sys.argv[2], sys.argv[4])['container_id'] == sys.argv[5]
"""
    # Fixture imports and current source are explicit; no original-process RAM.
    import os
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "tests")))}
    completed = subprocess.run(
        [sys.executable, "-c", script, str(database), sid, str(receipt.world_version), CRED, BOX],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("growth", (0, 150))
def test_fixed_target_ram_work_does_not_scan_loaded_unrelated_rows(tmp_path, context, growth):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        if growth:
            grow(sm, sid, growth)
        sm.read_session(sid, package, context=context)  # a deliberately wide loaded reference
        before = dict(sm._store.memory.stats)
        with measure(sm) as (counts, _):
            for _ in range(5):
                assert sm._world_facts.object(sid, CRED)["object_id"] == CRED
        assert counts["sql_rows"] == counts["sql_selects"] == 0
        assert sm._store.memory.stats["ram_rows"] - before["ram_rows"] <= 5


def test_other_connection_commit_and_fixed_older_reader(tmp_path, context):
    sm, sid, package, database = setup(tmp_path, context, container_data())
    with sm, StateMachine(database, dnd_loader=sm._rules.loader) as writer:
        old = sm.read_session(sid, package, context=context)
        with sm._transaction():
            pinned = sm._snapshot(sm._session(sid, package, context), package)
            acquire(writer, sid, package, context)
            assert sm._snapshot(sm._session(sid, package, context), package) == pinned
        assert pinned == old
        assert sm.read_session(sid, package, context=context).world_version > old.world_version


def test_preparation_does_not_hold_sqlite_write_lock_and_savepoints_survive(tmp_path, context):
    sm, sid, package, database = setup(tmp_path, context, container_data())
    with sm, sqlite3.connect(database, isolation_level=None, timeout=0) as other:
        with sm._transaction(write=True) as scope:
            sm._session(sid, package, context)
            assert not scope.persisting
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
            with sm._store.savepoint():
                sm._conn.execute(
                    "UPDATE characters SET current_hp=current_hp-1 WHERE session_id=?", (sid,)
                )
                assert scope.persisting
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute("BEGIN IMMEDIATE")
        assert not sm._conn.in_transaction


def test_foreign_commit_cannot_mix_cold_data_into_prepared_ram(tmp_path, context):
    sm, sid, package, database = setup(tmp_path, context, container_data())
    with sm, sqlite3.connect(database, isolation_level=None) as other:
        old = sm.read_session(sid, package, context=context)
        with (
            pytest.raises(StateMachineError, match="changed before persistence"),
            sm._transaction(write=True),
            sm._store.savepoint(),
        ):
            sm._session(sid, package, context)
            other.execute(
                "UPDATE characters SET current_hp=current_hp-1 WHERE session_id=?", (sid,)
            )
            sm._conn.execute(
                "UPDATE world_states SET game_tick=game_tick+1 WHERE session_id=?", (sid,)
            )
        with pytest.raises(StateMachineError):
            sm.read_session(sid, package, context=context)
        rebuilt = sm.resume_session(sid, package, context=context)
        assert rebuilt.character.current_hp == old.character.current_hp - 1
        assert rebuilt.world.game_tick == old.world.game_tick


def test_five_synthetic_clients_compete_for_one_item_without_duplicate_transfer(tmp_path, context):
    sm, sid, package, database = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, package, context)
        first = command(sm, sid, package, context, PutItem(A, CRED, BOX, "0,0"), "race.0")
        loader = sm._rules.loader
        barrier = Barrier(5)

        def compete(number):
            with StateMachine(database, dnd_loader=loader) as client:
                client.read_session(sid, package, context=context)
                barrier.wait(timeout=30)
                from dataclasses import replace

                return client.submit_command(
                    replace(first, command_id=f"race.{number}"), package, context=context
                )

        with ThreadPoolExecutor(max_workers=5) as clients:
            receipts = list(clients.map(compete, range(5)))
        assert [r.status for r in receipts].count("committed") == 1
        assert [r.status for r in receipts].count("conflict") == 4
        current = sm.read_session(sid, package, context=context)
        assert current.world_version == first.expected_world_version + 1
        assert sm._world_facts.object(sid, CRED)["container_id"] == BOX


def test_hot_real_re_inputs_and_rng_are_not_reassembled(tmp_path, package_data, context):
    from test_real_checks import check_setup

    sm, sid, package, _, _ = check_setup(tmp_path, package_data, context)
    with sm:
        fixed = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        before = dict(sm._store.memory.stats)
        with measure(sm) as (counts, _):
            for _ in range(4):
                assert sm.read_noncombat_snapshot(sid, package, context=context) == fixed
                assert sm.read_rng_context(sid, package, context=context) == rng
        assert counts["sql_selects"] == counts["sql_rows"] == 0
        assert sm._store.memory.stats["snapshot_builds"] == before["snapshot_builds"]
        assert sm._store.memory.stats["json_parses"] == before["json_parses"]


def test_cross_command_weight_and_relationship_index_reuse(tmp_path, context, monkeypatch):
    from trpg_state_machine import TakeItem

    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        put = command(sm, sid, package, context, PutItem(A, CRED, BOX, "0,0"), "weight.put")
        sm.submit_command(put, package, context=context)
        from trpg_state_machine.world_containers import ContainerService

        weight = ContainerService.contents_weight.__wrapped__
        calls = []

        from trpg_state_machine.runtime_instances import instance_read

        @instance_read("object", "container", "contents_weight")
        def counted(self, *args, **kwargs):
            calls.append(args[0])
            return weight(self, *args, **kwargs)

        monkeypatch.setattr(ContainerService, "contents_weight", counted)
        first = sm.get_object(sid, actor, BOX, package, context=context)
        before = len(calls)
        assert first["contents"] == (CRED,) and before > 0
        assert sm.get_object(sid, actor, BOX, package, context=context) == first
        assert len(calls) == before
        take = command(sm, sid, package, context, TakeItem(A, CRED, BOX, "0,0"), "weight.take")
        assert sm.submit_command(take, package, context=context).status == "committed"
        empty = sm.get_object(sid, actor, BOX, package, context=context)
        assert empty["contents"] == () and empty["contents_weight_lb"] == 0
        assert first["contents"] == (CRED,)


def test_mechanism_trigger_index_survives_activation_state_change(tmp_path, context, monkeypatch):
    from lock_support import DOOR
    from test_world_mechanisms import LEVER, mechanism_data
    from world_support import submit

    from trpg_state_machine import ActivateMechanism, UnlockDoor
    from trpg_state_machine.scoped_reads import scoped_read
    from trpg_state_machine.world_mechanisms import MechanismService

    sm, sid, package, _ = setup(tmp_path, context, mechanism_data())
    with sm:
        acquire(sm, sid, package, context)
        submit(sm, sid, package, context, UnlockDoor(A, DOOR, CRED, "0,0"), "index.unlock")
        original, calls = MechanismService._build_index.__wrapped__, []

        @scoped_read(membership=("object_mechanisms",))
        def counted(self, session):
            calls.append(session)
            return original(self, session)

        monkeypatch.setattr(MechanismService, "_build_index", counted)
        index = sm._objects.mechanisms._build_index(sid)
        assert len(calls) == 1
        assert (
            submit(
                sm, sid, package, context, ActivateMechanism(A, LEVER, "0,0"), "index.activate"
            ).status
            == "committed"
        )
        assert sm._objects.mechanisms._build_index(sid) == index and len(calls) == 1


def test_capacity_eviction_and_close_release_owned_values(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    memory = sm._store.memory
    memory.capacity, memory.byte_limit = 8, 8192
    with sm:
        original = sm.read_session(sid, package, context=context)
        original_hp = original.character.current_hp
        for i in range(30):
            hp = 1 + i % original.character.max_hp
            with sm._transaction(write=True):
                sm._conn.execute("UPDATE characters SET current_hp=? WHERE session_id=?", (hp, sid))
            assert sm.read_session(sid, package, context=context).character.current_hp == hp
            assert len(memory.values) <= memory.capacity and memory.bytes <= memory.byte_limit
        assert original.character.current_hp == original_hp
    assert memory.bytes == 0 and not memory.values and not memory.versions


def test_unload_releases_environment_values_and_rebuilds_from_instances(tmp_path, context):
    from test_environment_service import seed
    from test_environment_snapshot import project
    from test_object_protection import large_data

    sm, sid, package, _ = setup(tmp_path, context, large_data())
    with sm:
        seed(sm, sid, package, context)
        old = project(sm, sid, package, "3,3")
        assert len(sm.environment._cells) > 0
        sm.unload_session(sid, package, context=context)
        assert len(sm.environment._cells) == len(sm.environment._projections) == 0
        assert project(sm, sid, package, "3,3") == old
    assert len(sm.environment._cells) == len(sm.environment._projections) == 0


def test_reconciliation_keeps_other_current_readers_out_until_acceptance(tmp_path, context):
    sm, sid, package, database = setup(tmp_path, context, container_data())
    ready, start, attempted = Event(), Event(), Event()
    with sm:
        expected = sm.read_session(sid, package, context=context)
        loader = sm._rules.loader

        def read_current():
            with StateMachine(database, dnd_loader=loader) as client:
                ready.set()
                assert start.wait(10)
                attempted.set()
                return client.read_session(sid, package, context=context)

        with ThreadPoolExecutor(max_workers=1) as clients:
            future = clients.submit(read_current)
            assert ready.wait(10)
            sm._store.memory.paused_sessions.add(sid)
            with sm._store.reconciliation(sid):
                start.set()
                assert attempted.wait(10)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.05)
                assert sm.save_session(sid, package, context=context) == expected
            assert future.result(timeout=10) == expected
        with pytest.raises(ValueError), sm._store.reconciliation(sid):
            raise ValueError("persisted validation failed")
        with pytest.raises(StateMachineError, match="reconciliation"):
            sm.read_session(sid, package, context=context)
