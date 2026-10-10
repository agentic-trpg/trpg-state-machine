import json
import sqlite3
from copy import deepcopy
from dataclasses import replace

import pytest
import yaml
from test_commands import make_command

from trpg_state_machine import StateMachine, StateMachineError, load_adventure_package
from trpg_state_machine._operations import EnterScene, MoveActor


def event_data(event_id="event.open_gate", effect=None):
    return {
        "event_definition_id": event_id,
        "trigger": {"event_type": "scene.flag_changed", "scene_id": "scene.platform"},
        "condition": {"kind": "flag_equals", "flag_id": "flag.lamp_checked", "value": True},
        "effect": effect
        or {"kind": "set_object_state", "object_id": "object.platform_gate", "state": "open"},
        "signals": [
            {
                "signal_id": "signal.chime",
                "channel": "auditory",
                "content": "A chime rings.",
                "scope": {"kind": "scene", "scene_id": "scene.platform", "visibility": "clear"},
            }
        ],
        "once": "per_session",
    }


def load_event_package(tmp_path, data, definitions=None):
    data = deepcopy(data)
    data["events"] = definitions if definitions is not None else [event_data()]
    data["scenes"][0]["event_definition_ids"] = [d["event_definition_id"] for d in data["events"]]
    path = tmp_path / "events.yaml"
    path.write_text(yaml.safe_dump(data), "utf-8")
    return load_adventure_package(path)


def create_request(request_for, package, key="create.events"):
    return replace(request_for(key), package_digest=package.package_digest)


def test_atomic_trigger_signals_and_restart(tmp_path, package_data, context, request_for):
    package = load_event_package(tmp_path, package_data).package
    assert package is not None
    database = tmp_path / "events.db"
    with StateMachine(database) as sm:
        created = sm.create_session(create_request(request_for, package), package, context=context)
        command = make_command(sm, created, package, context)
        receipt = sm.submit_command(command, package, context=context)
        events = sm.read_events(created.session_id, package, context=context)
        assert [e.event_seq for e in events] == list(range(1, 6))
        assert len({e.event_id for e in events}) == 5
        assert events[-1].source_command_id == command.command_id
        assert events[-1].signals[0].content == "A chime rings."
        assert receipt.event_seqs == (3, 4, 5)
        assert sm.read_session(created.session_id, package, context=context).world_version == 1
        assert sm.submit_command(command, package, context=context) == receipt
    with StateMachine(database) as sm:
        assert sm.read_events(created.session_id, package, context=context) == events
        assert (
            sm.read_events(created.session_id, package, context=context, after_seq=4) == events[4:]
        )
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute(
                "SELECT state FROM objects WHERE object_id='object.platform_gate'"
            ).fetchone()[0]
            == "open"
        )
        assert conn.execute("SELECT COUNT(*) FROM trigger_history").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM event_outbox WHERE consumer='delivery'").fetchone()[
                0
            ]
            == 5
        )


def test_once_and_cycle_protection(tmp_path, package_data, context, request_for):
    first = event_data(
        effect={"kind": "set_scene_flag", "flag_id": "flag.lamp_checked", "value": False}
    )
    second = event_data(
        "event.back", {"kind": "set_scene_flag", "flag_id": "flag.lamp_checked", "value": True}
    )
    second["condition"]["value"] = False
    package = load_event_package(tmp_path, package_data, [first, second]).package
    assert package is not None
    with StateMachine(tmp_path / "state.db") as sm:
        created = sm.create_session(create_request(request_for, package), package, context=context)
        command = make_command(sm, created, package, context)
        assert sm.submit_command(command, package, context=context).status == "committed"
        assert sm._conn.execute("SELECT COUNT(*) FROM trigger_history").fetchone()[0] == 2
        # A new command cannot re-award/re-fire once-per-session definitions.
        other = replace(
            command,
            command_id="again",
            expected_world_version=1,
            payload=replace(command.payload, value=False),
        )
        sm.submit_command(other, package, context=context)
        assert sm._conn.execute("SELECT COUNT(*) FROM trigger_history").fetchone()[0] == 2


def test_cascade_limit_rolls_back(tmp_path, package_data, context, request_for):
    definitions = [event_data(f"event.reward_{i}") for i in range(33)]
    package = load_event_package(tmp_path, package_data, definitions).package
    with StateMachine(tmp_path / "state.db") as sm:
        created = sm.create_session(create_request(request_for, package), package, context=context)
        receipt = sm.submit_command(
            make_command(sm, created, package, context), package, context=context
        )
        assert receipt.status == "rejected" and receipt.error_code == "event_cascade_limit"
        assert sm._conn.execute("SELECT COUNT(*) FROM trigger_history").fetchone()[0] == 0
        assert sm.read_session(created.session_id, package, context=context).event_seq == 2


def test_outbox_insert_failure_rolls_back_everything(tmp_path, package_data, context, request_for):
    package = load_event_package(tmp_path, package_data).package
    with StateMachine(tmp_path / "state.db") as sm:
        created = sm.create_session(create_request(request_for, package), package, context=context)
        sm._conn.execute(
            "CREATE TRIGGER fail_work BEFORE INSERT ON event_outbox "
            "BEGIN SELECT RAISE(ABORT, 'fault'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(make_command(sm, created, package, context), package, context=context)
        assert sm.read_session(created.session_id, package, context=context).world_version == 0
        for table, count in [
            ("events", 2),
            ("event_records", 2),
            ("event_evidence", 2),
            ("event_outbox", 4),
            ("trigger_history", 0),
            ("command_receipts", 0),
        ]:
            assert sm._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count


def test_delivery_retry_gap_and_isolation(tmp_path, package, context, request_for):
    database = tmp_path / "state.db"
    received = set()

    def lost_response(event):
        received.add(event.event_id)
        raise TimeoutError("external response lost")

    with StateMachine(database) as sm:
        created = sm.create_session(request_for(), package, context=context)
        other = sm.create_session(request_for("other"), package, context=context)
        assert sm.deliver_event(
            created.session_id,
            2,
            package,
            context=context,
            sink=lambda event: received.add(event.event_id),
        )
        assert sm.delivery_cursor(created.session_id, package, context=context) == 0
        with pytest.raises(TimeoutError):
            sm.deliver_event(created.session_id, 1, package, context=context, sink=lost_response)
    with StateMachine(database) as sm:
        assert sm.deliver_event(
            created.session_id,
            1,
            package,
            context=context,
            sink=lambda event: received.add(event.event_id),
        )
        assert sm.delivery_cursor(created.session_id, package, context=context) == 2
        assert not sm.deliver_event(
            created.session_id,
            1,
            package,
            context=context,
            sink=lambda event: pytest.fail("already delivered"),
        )
        assert sm.delivery_cursor(other.session_id, package, context=context) == 0
        assert len(received) == 2
        attempts = sm._conn.execute(
            "SELECT attempts FROM event_outbox WHERE session_id=? AND event_seq=1",
            (created.session_id,),
        ).fetchone()[0]
        assert attempts == 2


def test_evidence_is_event_time_not_current_state(tmp_path, package, context, request_for):
    with StateMachine(tmp_path / "state.db") as sm:
        created = sm.create_session(request_for(), package, context=context)
        sm._apply(
            created.session_id,
            package,
            EnterScene("scene.workshop"),
            expected_world_version=0,
            context=context,
        )
        sm._apply(
            created.session_id,
            package,
            MoveActor("npc.caretaker", "scene.workshop"),
            expected_world_version=1,
            context=context,
        )
        evidence = json.loads(
            sm._conn.execute(
                "SELECT evidence_json FROM event_evidence WHERE event_seq=2"
            ).fetchone()[0]
        )
        assert (
            next(a for a in evidence["actors"] if a["actor_id"] == "npc.caretaker")[
                "current_scene_id"
            ]
            == "scene.platform"
        )


@pytest.mark.parametrize(
    "change",
    [
        {"once": "repeat"},
        {"effect": {"kind": "rule_command"}},
        {"condition": {"kind": "eval", "flag_id": "flag.lamp_checked", "value": True}},
        {
            "signals": [
                {
                    "signal_id": "signal.bad",
                    "channel": "telepathy",
                    "content": "secret",
                    "scope": {"kind": "world", "scene_id": "scene.platform", "visibility": "clear"},
                }
            ]
        },
    ],
)
def test_unsupported_event_mechanisms(tmp_path, package_data, change):
    definition = event_data()
    definition.update(change)
    result = load_event_package(tmp_path, package_data, [definition])
    assert result.status == "invalid" and result.package is None


def test_initial_entry_triggers(tmp_path, package_data, context, request_for):
    definition = event_data()
    definition["trigger"]["event_type"] = "scene.entered"
    definition["condition"]["value"] = False
    package = load_event_package(tmp_path, package_data, [definition]).package
    with StateMachine(tmp_path / "state.db") as sm:
        created = sm.create_session(create_request(request_for, package), package, context=context)
        assert created.event_seq == 4 and created.world_version == 0
        assert (
            sm.read_session(created.session_id, package, context=context)
            .scenes["scene.platform"]
            .objects["object.platform_gate"]
            == "open"
        )
