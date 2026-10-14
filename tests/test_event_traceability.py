import sqlite3
from pathlib import Path

import pytest
from dnd_support import create, initialize, loader
from dnd_support import package_data as dnd_package_data
from test_commands import make_command
from test_dnd_runtime import attack, authority
from test_events import create_request, event_data, load_event_package
from test_migrations import historical_database, stored_rows

from trpg_state_machine import StateMachine, StateMachineError
from trpg_state_machine.package import _validate


def test_trigger_links_exact_source_event_in_same_command_chain(
    tmp_path, package_data, context, request_for
):
    first = event_data("event.first")
    second = event_data(
        "event.second", {"kind": "set_scene_flag", "flag_id": "flag.lamp_checked", "value": False}
    )
    second["trigger"]["event_type"] = "object.state_changed"
    package = load_event_package(tmp_path, package_data, [first, second]).package
    with StateMachine(tmp_path / "trace.db") as sm:
        created = sm.create_session(create_request(request_for, package), package, context=context)
        sid = created.session_id
        command = make_command(sm, created, package, context)
        receipt = sm.submit_command(command, package, context=context)
        rows = sm._conn.execute(
            "SELECT h.event_definition_id,h.source_event_seq,h.source_event_status,"
            "r.event_id,r.source_command_id FROM trigger_history h JOIN event_records r "
            "ON h.session_id=r.session_id AND h.source_event_seq=r.event_seq "
            "ORDER BY h.source_event_seq"
        ).fetchall()
        assert [r[1] for r in rows] == [receipt.event_seqs[0], receipt.event_seqs[0] + 1]
        assert all(r[2] == "linked" and r[4] == command.command_id for r in rows)
        events = {e.event_seq: e for e in sm.read_events(sid, package, context=context)}
        assert events[rows[0][1]].payload["value"] is True
        assert events[rows[1][1]].payload["state"] == "open"
        assert all(events[r[1]].event_id == r[3] for r in rows)
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm._conn.execute("SELECT count(*) FROM trigger_history").fetchone()[0] == 2
        assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("version", [3, 6, 7])
def test_real_old_history_is_audited_not_guessed(tmp_path, package_data, context, version):
    definition = event_data()
    definition["trigger"]["event_type"] = "scene.entered"
    definition["condition"]["value"] = False
    package = load_event_package(tmp_path, package_data, [definition]).package
    db, sessions = historical_database(tmp_path, version, tmp_path / "events.yaml")
    before = stored_rows(db)
    assert len(before["trigger_history"][1]) == 2
    with StateMachine(db) as sm:
        rows = sm._conn.execute(
            "SELECT source_event_seq,source_event_status FROM trigger_history"
        ).fetchall()
        assert [tuple(r) for r in rows] == [(None, "legacy_unavailable")] * 2
        assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for sid in sessions:
            sm.resume_session(sid, package, context=context)
    with StateMachine(db) as restarted:
        assert (
            restarted._conn.execute(
                "SELECT count(*) FROM trigger_history "
                "WHERE source_event_status='legacy_unavailable'"
            ).fetchone()[0]
            == 2
        )
    with sqlite3.connect(db) as conn:
        for table, (columns, oldrows) in before.items():
            assert oldrows <= set(
                conn.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
            )


def test_schema8_failure_rolls_back_all_migration_steps(tmp_path, package_data, monkeypatch):
    definition = event_data()
    definition["trigger"]["event_type"] = "scene.entered"
    definition["condition"]["value"] = False
    load_event_package(tmp_path, package_data, [definition])
    db, _ = historical_database(tmp_path, 7, tmp_path / "events.yaml")
    original = stored_rows(db)
    read = Path.read_text
    with monkeypatch.context() as patch:

        def broken(path, *args, **kwargs):
            text = read(path, *args, **kwargs)
            return (
                text.replace("PRAGMA user_version = 8;", "INVALID MIGRATION;")
                if path.name == "_migration_8.sql"
                else text
            )

        patch.setattr(Path, "read_text", broken)
        with pytest.raises(StateMachineError) as caught:
            StateMachine(db)
        assert caught.value.code == "storage_error"
    assert stored_rows(db) == original
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    with StateMachine(db) as sm:
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 31
        assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("fault", [None, "trigger_history", "command_receipts"])
def test_real_attack_world_cascade_rejection_rolls_back_and_replays(
    tmp_path, package_data, context, fault
):
    assets = loader()
    data = dnd_package_data(package_data, assets)
    definitions = []
    for i in range(33):
        flag = f"flag.cascade_{i}"
        data["scenes"][0]["initial_state"]["local_flags"][flag] = False
        definition = event_data(
            f"event.cascade_{i}", {"kind": "set_scene_flag", "flag_id": flag, "value": True}
        )
        definition["trigger"]["event_type"] = "combat.engine_event"
        definition["condition"] = {"kind": "flag_equals", "flag_id": flag, "value": False}
        definition["signals"] = []
        definitions.append(definition)
    data["events"] = definitions
    data["scenes"][0]["event_definition_ids"] = [d["event_definition_id"] for d in definitions]
    package = _validate(data)
    db = tmp_path / "cascade.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        assert initialize(sm, sid, package, context).status == "committed"
        command = attack(sm, sid, package, context)
        before = authority(sm, sid)
        world = sm.read_session(sid, package, context=context)
        calls = []
        real = sm._rules._evaluate_dnd

        async def tracked(request):
            calls.append(request.command_id)
            return await real(request)

        sm._rules._evaluate_dnd = tracked
        if fault:
            sm._conn.execute(
                f"CREATE TEMP TRIGGER fail BEFORE INSERT ON {fault} "
                "BEGIN SELECT RAISE(ABORT,'fault'); END"
            )
            with pytest.raises(StateMachineError) as caught:
                sm.submit_command(command, package, context=context)
            assert caught.value.code == "storage_error"
            assert authority(sm, sid) == before
            sm._conn.execute("DROP TRIGGER fail")
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "event_cascade_limit")
        assert receipt.event_seqs == ()
        assert sm.read_session(sid, package, context=context) == world
        assert {k: v for k, v in authority(sm, sid).items() if k != "command_receipts"} == {
            k: v for k, v in before.items() if k != "command_receipts"
        }
        assert sm._conn.execute("SELECT count(*) FROM trigger_history").fetchone()[0] == 0
        assert sm.submit_command(command, package, context=context) == receipt
        assert len(calls) == 1 + int(fault is not None)
    with StateMachine(db, dnd_loader=assets) as reopened:
        assert reopened.submit_command(command, package, context=context) == receipt
        assert (
            reopened.get_command_receipt(sid, command.command_id, package, context=context)
            == receipt
        )
        assert reopened.read_session(sid, package, context=context) == world
