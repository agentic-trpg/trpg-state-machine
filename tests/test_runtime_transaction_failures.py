"""Caught SQLite failures cannot publish partial trigger-captured candidates."""

import sqlite3
from contextlib import nullcontext, suppress

import pytest
from test_world_containers import BAG, BOX, container_data
from world_support import setup

from trpg_state_machine import StateMachineError


@pytest.mark.parametrize("mode", ("ABORT", "FAIL", "ROLLBACK"))
@pytest.mark.parametrize("recover", (False, True))
def test_caught_multirow_statement_poison_and_savepoint_recovery(tmp_path, context, mode, recover):
    sm, sid, package, db = setup(tmp_path, context, container_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        # A later row's trigger can choose SQLite's three distinct error policies.
        sm._conn.execute(
            f"CREATE TEMP TRIGGER fail_second BEFORE UPDATE OF state ON objects "
            f"WHEN NEW.object_id='{BAG}' AND NEW.state='closed' "
            f"BEGIN SELECT RAISE({mode},'second row'); END"
        )
        sm.get_object(sid, actor, BOX, package, context=context)
        abort = not recover or mode == "ROLLBACK"
        outcome = pytest.raises(StateMachineError) if abort else nullcontext()
        with outcome as outer, sm._transaction(write=True):
            sm._conn.execute(
                "UPDATE objects SET cell='0,1' WHERE session_id=? AND object_id=?",
                (sid, BOX),
            )
            if recover:
                sm._conn.execute("SAVEPOINT recover_statement")
            with pytest.raises(sqlite3.IntegrityError):
                sm._conn.execute(
                    "UPDATE objects SET state='closed' WHERE session_id=? AND object_id IN (?,?)",
                    (sid, BOX, BAG),
                )
            for access in (
                lambda: sm._world_facts.object(sid, BOX),
                lambda: sm._objects.containers.contents_weight(sid, BOX),
                lambda: sm._conn.execute(
                    "UPDATE objects SET state='open' WHERE session_id=?", (sid,)
                ),
            ):
                with pytest.raises(StateMachineError) as error:
                    access()
                assert error.value.code == "transaction_failed"
            if recover and mode != "ROLLBACK":
                sm._conn.execute("ROLLBACK TO recover_statement")
                sm._conn.execute("RELEASE recover_statement")
                assert sm._world_facts.object(sid, BOX)["state"] == "open"
                assert sm._world_facts.object(sid, BOX)["cell"] == "0,1"
            # Catching the SQL error must still refuse normal outer COMMIT.
        if abort:
            assert outer.value.code == "transaction_failed"
        assert sm.get_object(sid, actor, BOX, package, context=context)["open_state"] == "open"
        with sqlite3.connect(db) as raw:
            assert raw.execute(
                "SELECT state,cell FROM objects WHERE session_id=? AND object_id=?",
                (sid, BOX),
            ).fetchone() == ("open", "1,0" if abort else "0,1")


def test_original_check_constraint_caught_then_commit(tmp_path, context):
    sm, sid, package, db = setup(tmp_path, context, container_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        old = sm.get_object(sid, actor, BOX, package, context=context)
        failed = False
        try:
            with sm._transaction(write=True), suppress(sqlite3.IntegrityError):
                sm._conn.execute(
                    "UPDATE objects SET state=CASE WHEN object_id=? "
                    "THEN 'closed' ELSE 'invalid' END "
                    "WHERE session_id=? AND object_id IN (?,?)",
                    (BOX, sid, BOX, BAG),
                )
        except StateMachineError as error:
            assert error.code == "transaction_failed"
            failed = True
        current = sm.get_object(sid, actor, BOX, package, context=context)
        with sqlite3.connect(db) as raw:
            sql_state = raw.execute(
                "SELECT state FROM objects WHERE object_id=?", (BOX,)
            ).fetchone()[0]
        print(
            "ATOMIC", {"ram": current["open_state"], "sqlite": sql_state, "commit_refused": failed}
        )
        assert current["open_state"] == sql_state == "open"
        assert current == old and failed


def test_executemany_caught_failure_restores_existing_savepoint(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm, sm._transaction(write=True):
        sm._conn.execute("UPDATE objects SET cell='0,1' WHERE object_id=?", (BOX,))
        with pytest.raises(sqlite3.IntegrityError), sm._store.savepoint():
            sm._conn.executemany(
                "UPDATE objects SET state=? WHERE session_id=? AND object_id=?",
                (("closed", sid, BOX), ("invalid", sid, BAG)),
            )
        assert sm._world_facts.object(sid, BOX)["state"] == "open"
        assert sm._world_facts.object(sid, BOX)["cell"] == "0,1"


@pytest.mark.parametrize("mode", ("ABORT", "FAIL", "ROLLBACK"))
def test_failed_delete_with_real_fk_cascades_discards_candidates(tmp_path, context, mode):
    sm, sid, package, db = setup(tmp_path, context, container_data())
    with sm:
        sm._conn.execute(
            f"CREATE TEMP TRIGGER fail_delete BEFORE DELETE ON objects "
            f"WHEN OLD.object_id='{BAG}' BEGIN SELECT RAISE({mode},'delete'); END"
        )
        with (
            pytest.raises(StateMachineError),
            sm._transaction(write=True),
            suppress(sqlite3.IntegrityError),
        ):
            sm._conn.execute(
                "DELETE FROM objects WHERE session_id=? AND object_id IN (?,?)", (sid, BOX, BAG)
            )
        assert sm._world_facts.object(sid, BOX) is not None
        assert sm._world_facts.openable_state(sid, BOX) is not None
        with sqlite3.connect(db) as raw:
            assert (
                raw.execute(
                    "SELECT count(*) FROM object_openables "
                    "WHERE session_id=? AND object_id IN (?,?)",
                    (sid, BOX, BAG),
                ).fetchone()[0]
                == 2
            )


def test_caught_failure_inside_savepoint_is_rolled_back_on_context_exit(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm, sm._transaction(write=True):
        sm._world_facts.switch(sid, BOX, "closed")
        with (
            pytest.raises(StateMachineError),
            sm._store.savepoint(),
            suppress(sqlite3.IntegrityError),
        ):
            sm._conn.executemany(
                "UPDATE objects SET state=? WHERE session_id=? AND object_id=?",
                (("open", sid, BOX), ("invalid", sid, BAG)),
            )
        assert sm._world_facts.object(sid, BOX)["state"] == "closed"
        sm._world_facts.switch(sid, BAG, "closed")


def test_executemany_parameter_failure_poison_and_recover(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, container_data())

    def parameters():
        yield ("closed", sid, BOX)
        raise ValueError("parameter source failed after one SQL write")

    with sm, sm._transaction(write=True):
        with pytest.raises(ValueError), sm._store.savepoint():
            sm._conn.executemany(
                "UPDATE objects SET state=? WHERE session_id=? AND object_id=?", parameters()
            )
        assert sm._world_facts.object(sid, BOX)["state"] == "open"
        sm._world_facts.switch(sid, BAG, "closed")


def test_releasing_failed_savepoint_is_cleanup_not_recovery(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        with pytest.raises(StateMachineError) as outer, sm._transaction(write=True):
            sm._conn.execute("SAVEPOINT participant")
            try:
                with suppress(sqlite3.IntegrityError):
                    sm._conn.execute("UPDATE objects SET state='invalid' WHERE object_id=?", (BAG,))
            finally:
                sm._conn.execute("RELEASE participant")
            with pytest.raises(StateMachineError) as read:
                sm._world_facts.object(sid, BOX)
            assert read.value.code == "transaction_failed"
        assert outer.value.code == "transaction_failed"
        assert sm._world_facts.object(sid, BOX)["state"] == "open"


@pytest.mark.parametrize("failed_sql", ("BEGIN IMMEDIATE", "PRAGMA schema_version"))
def test_caught_persistence_upgrade_failure_refuses_candidates_and_commit(
    tmp_path, context, monkeypatch, failed_sql
):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        old = sm.get_object(sid, actor, BOX, package, context=context)
        connection = sm._store._conn

        class FaultConnection:
            def __getattr__(self, name):
                return getattr(connection, name)

            def execute(self, sql, parameters=()):
                if sql == failed_sql:
                    raise sqlite3.OperationalError("injected upgrade/pin failure")
                return connection.execute(sql, parameters)

        with pytest.raises(StateMachineError) as outer, sm._transaction(write=True):
            # Install only after the initial fixed reading scope was established.
            with monkeypatch.context() as patch:
                patch.setattr(sm._store, "_conn", FaultConnection())
                with pytest.raises(sqlite3.OperationalError):
                    sm._world_facts.switch(sid, BOX, "closed")
            with pytest.raises(StateMachineError) as read:
                sm._world_facts.object(sid, BOX)
            assert read.value.code == "transaction_failed"
        assert outer.value.code == "transaction_failed"
        assert sm.get_object(sid, actor, BOX, package, context=context) == old
