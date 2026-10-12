import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from trpg_state_machine import (
    EnterScene,
    MoveActor,
    ObjectState,
    SetObjectState,
    SetSceneFlag,
    StateMachine,
    StateMachineError,
    TrustedContext,
    TypedCommand,
)


def make_command(sm, created, package, context, command_id="cmd.flag"):
    snapshot = sm.read_session(created.session_id, package, context=context)
    return TypedCommand(
        created.session_id,
        command_id,
        snapshot.pc_actor_id,
        "set_scene_flag",
        SetSceneFlag("scene.platform", "flag.lamp_checked", True),
        snapshot.world_version,
    )


def test_durable_retry_and_receipt(tmp_path, package, context, request_for):
    database = tmp_path / "state.db"
    with StateMachine(database) as sm:
        created = sm.create_session(request_for(), package, context=context)
        command = make_command(sm, created, package, context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed" and receipt.world_version == 1
    with StateMachine(database) as restarted:
        assert restarted.submit_command(command, package, context=context) == receipt
        assert (
            restarted.get_command_receipt(
                created.session_id, command.command_id, package, context=context
            )
            == receipt
        )
        assert (
            restarted.get_command_receipt(created.session_id, "unknown", package, context=context)
            is None
        )
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0] == 3


def test_fingerprint_conflict(tmp_path, package, context, request_for):
    with StateMachine(tmp_path / "state.db") as sm:
        created = sm.create_session(request_for(), package, context=context)
        command = make_command(sm, created, package, context)
        sm.submit_command(command, package, context=context)
        with pytest.raises(StateMachineError, match="different content") as error:
            sm.submit_command(replace(command, expected_world_version=1), package, context=context)
        assert error.value.code == "idempotency_conflict"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"actor_id": "npc.caretaker"}, "invalid_actor"),
        ({"schema_version": "future"}, "invalid_command"),
        ({"command_type": "sql"}, "unsupported_operation"),
        ({"payload": {"patch": True}}, "unsupported_operation"),
        ({"expected_world_version": True}, "invalid_operation"),
    ],
)
def test_invalid_commands(tmp_path, package, context, request_for, change, code):
    with StateMachine(tmp_path / "state.db") as sm:
        created = sm.create_session(request_for(), package, context=context)
        command = replace(make_command(sm, created, package, context), **change)
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == code
        assert sm.read_session(created.session_id, package, context=context).world_version == 0


def test_authorization_and_revoked_grants(tmp_path, package, context, request_for):
    with StateMachine(tmp_path / "state.db") as sm:
        created = sm.create_session(request_for(), package, context=context)
        command = TypedCommand(
            created.session_id,
            "npc.noop",
            "npc.caretaker",
            "move_actor",
            MoveActor("npc.caretaker", "scene.platform"),
            0,
        )
        unprivileged = TrustedContext(context.principal_id, context.player_id)
        for entry in (sm.submit_command,):
            with pytest.raises(StateMachineError) as error:
                entry(command, package, context=unprivileged)
            assert error.value.code == "unauthorized"
        sm.submit_command(command, package, context=context)
        with pytest.raises(StateMachineError):
            sm.submit_command(command, package, context=unprivileged)
        with pytest.raises(StateMachineError):
            sm.get_command_receipt(
                created.session_id, command.command_id, package, context=unprivileged
            )
        with pytest.raises(StateMachineError):
            sm.get_command_receipt(
                created.session_id,
                command.command_id,
                package,
                context=TrustedContext("intruder", context.player_id),
            )


def test_rejections_and_version_conflicts_are_durable(tmp_path, package, context, request_for):
    with StateMachine(tmp_path / "state.db") as sm:
        created = sm.create_session(request_for(), package, context=context)
        command = make_command(sm, created, package, context)
        rejected = replace(command, payload=SetSceneFlag("scene.platform", "missing", True))
        result = sm.submit_command(rejected, package, context=context)
        assert result.status == "rejected" and result.error_code == "invalid_target"
        assert sm.submit_command(rejected, package, context=context) == result
        conflict = replace(command, command_id="stale", expected_world_version=99)
        result = sm.submit_command(conflict, package, context=context)
        assert result.status == "conflict" and result.event_seqs == ()
        assert sm.submit_command(conflict, package, context=context) == result


def test_atomic_receipt_failure(tmp_path, package, context, request_for):
    database = tmp_path / "state.db"
    with StateMachine(database) as sm:
        created = sm.create_session(request_for(), package, context=context)
        command = make_command(sm, created, package, context)
        sm._conn.execute(
            "CREATE TRIGGER fail_receipt BEFORE INSERT ON command_receipts "
            "BEGIN SELECT RAISE(ABORT, 'fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "storage_error"
        sm._conn.execute("DROP TRIGGER fail_receipt")
        snapshot = sm.read_session(created.session_id, package, context=context)
        assert snapshot.world_version == 0 and snapshot.event_seq == 2
        assert not snapshot.scenes["scene.platform"].local_flags["flag.lamp_checked"]
        assert (
            sm.get_command_receipt(created.session_id, command.command_id, package, context=context)
            is None
        )
        assert sm.submit_command(command, package, context=context).world_version == 1


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_writers(tmp_path, package, context, request_for, same_key):
    database = tmp_path / "state.db"
    with StateMachine(database) as sm:
        created = sm.create_session(request_for(), package, context=context)
        first = make_command(sm, created, package, context)
    second = first if same_key else replace(first, command_id="second")

    def submit(command):
        with StateMachine(database) as sm:
            return sm.submit_command(command, package, context=context)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, (first, second)))
    if same_key:
        assert results[0] == results[1]
    else:
        assert sorted(r.status for r in results) == ["committed", "conflict"]
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT world_version FROM sessions").fetchone()[0] == 1


def test_all_world_commands(tmp_path, package, context, request_for):
    with StateMachine(tmp_path / "state.db") as sm:
        created = sm.create_session(request_for(), package, context=context)
        actor = sm.read_session(created.session_id, package, context=context).pc_actor_id
        for index, (kind, payload) in enumerate(
            [
                ("enter_scene", EnterScene("scene.workshop")),
                (
                    "set_object_state",
                    SetObjectState("scene.workshop", "object.workshop_door", ObjectState.OPEN),
                ),
                ("move_actor", MoveActor("npc.caretaker", "scene.workshop")),
            ]
        ):
            command = TypedCommand(
                created.session_id,
                str(index),
                payload.actor_id if isinstance(payload, MoveActor) else actor,
                kind,
                payload,
                index,
            )
            assert sm.submit_command(command, package, context=context).status == "committed"


def test_upgrade_batch1_preserves_real_database(tmp_path, package, context, request_for):
    database = tmp_path / "state.db"
    with StateMachine(database) as sm:
        created = sm.create_session(request_for(), package, context=context)
        before = sm.read_session(created.session_id, package, context=context)
    # Copy matching authority columns into the original Batch-1 schema, then upgrade it.
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as conn:
        schema = Path("src/trpg_state_machine/_schema.sql").read_text("utf-8")
        conn.executescript(schema)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("ATTACH DATABASE ? AS source", (str(database),))
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            columns = ",".join(r[1] for r in conn.execute(f"PRAGMA table_info({table})"))
            conn.execute(f"INSERT INTO {table}({columns}) SELECT {columns} FROM source.{table}")
    database = legacy
    with StateMachine(database) as sm:
        assert sm.resume_session(created.session_id, package, context=context) == before
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 21
        assert sm._conn.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0] == 0
        assert (
            sm.submit_command(
                make_command(sm, created, package, context), package, context=context
            ).status
            == "committed"
        )
    assert Path(database).exists()
