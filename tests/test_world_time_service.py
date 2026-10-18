"""Real Session, public commands, immutable bindings and bounded time queries."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from dataclasses import asdict, replace
from fractions import Fraction
from pathlib import Path

import pytest
from test_game_time import tick, tick_command
from test_migrations import assert_original_rows_equal, stored_rows
from test_npc_evaluation_tasks import evaluation_package as evaluation_package

from trpg_state_machine import (
    LegacyTimeBinding,
    StateMachine,
    StateMachineError,
    TimeBinding,
    TimeBoundary,
    WorldTimeService,
    time_binding_from_data,
    world_command_from_data,
)
from trpg_state_machine.package import _validate


@pytest.mark.parametrize(
    "fields",
    [
        {"day_length": 0},
        {"day_length": -1},
        {"day_length": True},
        {"day_length": "60"},
        {"day_length": 2147483648},
        {"start_day": False},
        {"start_day": 0},
        {"start_day_tick": 86400},
        {"start_day_tick": True},
        {"start_day_tick": -1},
        {"day_start": 86400},
        {"night_start": None},
        {"day_start": False},
        {"night_start": 21600},
        {"cycle": "none"},
        {"cycle": False},
        {"cycle": "always_day"},
        {"schema_version": "world-time/2"},
    ],
)
def test_configuration_errors_are_structured(fields):
    with pytest.raises(StateMachineError) as error:
        TimeBinding(**fields)
    assert error.value.code == "invalid_time_binding"
    assert error.value.issue.path


def test_package_default_override_pin_and_creation_retry(
    tmp_path, package_data, context, request_for
):
    binding = TimeBinding()
    package_data["time_binding"] = asdict(binding)
    p = _validate(package_data)
    ctx = replace(context, can_advance_time=True)
    request = replace(request_for(), package_digest=p.package_digest)
    with StateMachine(tmp_path / "time.db") as sm:
        result = sm.create_session(request, p, context=ctx)
        sid = result.session_id
        assert sm.create_session(request, p, context=ctx) == result
        assert sm.world_time is sm._world.time
        assert type(sm.world_time) is WorldTimeService
        assert sm.read_session(sid, p, context=ctx).world.time_binding == binding
        changed = replace(p, time_binding=TimeBinding(start_day=9))
        assert sm.world_time.read(sid, changed, context=ctx).binding == binding
        override = TimeBinding(cycle="none", day_start=None, night_start=None)
        other = sm.create_session(
            replace(request, command_id="override", time_binding=override), p, context=ctx
        ).session_id
        assert sm.world_time.read(other, p, context=ctx).binding == override
        with pytest.raises(StateMachineError, match="different"):
            sm.create_session(replace(request, time_binding=override), p, context=ctx)
        tick(sm, sid, p, ctx, 3600)
        assert sm.world_time.read(other, p, context=ctx).game_tick == 0
        changed_data = {**package_data, "time_binding": asdict(override)}
        with pytest.raises(StateMachineError) as pin:
            sm.read_session(sid, _validate(changed_data), context=ctx)
        assert pin.value.code == "package_digest_mismatch"
        with pytest.raises(StateMachineError) as unauthorized:
            sm.world_time.read(sid, p, context=replace(ctx, player_id="other"))
        assert unauthorized.value.code == "unauthorized"


@pytest.mark.parametrize(
    "binding,at,expected",
    [
        (TimeBinding(), 0, (1, 0, "night", 21600)),
        (TimeBinding(), 21600, (1, 21600, "day", 64800)),
        (TimeBinding(), 64800, (1, 64800, "night", 108000)),
        (TimeBinding(), 86400, (2, 0, "night", 108000)),
        (
            TimeBinding(
                day_length=100, start_day=7, start_day_tick=90, day_start=20, night_start=80
            ),
            30,
            (8, 20, "day", 90),
        ),
        (TimeBinding(day_length=100, day_start=80, night_start=20), 20, (1, 20, "night", 80)),
        (TimeBinding(day_length=100, day_start=80, night_start=20), 80, (1, 80, "day", 120)),
        (
            TimeBinding(day_length=1, cycle="none", day_start=None, night_start=None),
            2147483647,
            (2147483648, 0, None, None),
        ),
    ],
)
def test_real_public_advance_calendar_and_exact_boundaries(
    tmp_path, package, context, request_for, binding, at, expected
):
    ctx = replace(context, can_advance_time=True)
    db = tmp_path / "time.db"
    with StateMachine(db) as sm:
        sid = sm.create_session(
            replace(request_for(), time_binding=binding), package, context=ctx
        ).session_id
        command = tick_command(sm, sid, package, ctx, at)
        data = asdict(command)
        data.pop("check_world_effects")
        assert world_command_from_data(data) == command
        before = sm.read_session(sid, package, context=ctx)
        receipt = sm.submit_command(command, package, context=ctx)
        assert receipt.status == "committed"
        assert sm.submit_command(command, package, context=ctx) == receipt
        snapshot = sm.world_time.read(sid, package, context=ctx)
        assert (
            snapshot.current_day,
            snapshot.day_tick,
            snapshot.phase,
            snapshot.next_phase_boundary.tick if snapshot.next_phase_boundary else None,
        ) == expected
        assert snapshot.elapsed.seconds == at
        assert snapshot.elapsed.minutes == Fraction(at, 60)
        assert snapshot.elapsed.hours == Fraction(at, 3600)
        assert snapshot.elapsed.days == Fraction(at, binding.day_length)
        assert sm.world_time.to_ticks(sid, 2, "minutes", package, context=ctx) == 120
        assert sm.world_time.to_ticks(sid, 2, "hours", package, context=ctx) == 7200
        assert sm.world_time.to_ticks(sid, 1, "days", package, context=ctx) == binding.day_length
        assert sm.world_time.elapsed(sid, package, context=ctx, since_tick=at).ticks == 0
        assert sm.world_time.remaining(sid, at, package, context=ctx).ticks == 0
        assert sm.world_time.reached(sid, at, package, context=ctx)
        if at:
            assert sm.world_time.reached(sid, at - 1, package, context=ctx)
            event = sm.read_events(sid, package, context=ctx)[-1]
            assert event.payload == {
                "previous_tick": 0,
                "game_tick": at,
                "time_protocol": "world-time/1",
            }
            assert receipt.event_seqs == (event.event_seq,)
            assert sm.read_session(sid, package, context=ctx).rng == before.rng
        state = stored_rows(db)
        sm.world_time.boundaries(sid, 0, at, package, context=ctx)
        assert stored_rows(db) == state  # All queries are read-only.


def test_boundary_pages_no_gaps_duplicates_or_unbounded_expansion(
    tmp_path, package, context, request_for
):
    binding = TimeBinding(day_length=10, day_start=0, night_start=4)
    with StateMachine(tmp_path / "time.db") as sm:
        sid = sm.create_session(
            replace(request_for(), time_binding=binding), package, context=context
        ).session_id

        def page(start=0, end=30, **kw):
            return sm.world_time.boundaries(sid, start, end, package, context=context, **kw)

        expected = (
            TimeBoundary(4, ("night_started",)),
            TimeBoundary(10, ("day_rolled", "day_started")),
            TimeBoundary(14, ("night_started",)),
            TimeBoundary(20, ("day_rolled", "day_started")),
            TimeBoundary(24, ("night_started",)),
            TimeBoundary(30, ("day_rolled", "day_started")),
        )
        assert page().boundaries == expected
        first = page(limit=2)
        second = page(limit=2, after_tick=first.next_cursor)
        third = page(limit=2, after_tick=second.next_cursor)
        assert first.boundaries + second.boundaries + third.boundaries == expected
        assert third.next_cursor is None
        assert page(4, 10).boundaries == expected[1:2]
        assert not page(10, 10).boundaries
        huge = page(0, 2147483647, limit=3)
        assert huge.boundaries == expected[:3] and huge.next_cursor == 14
        assert sm.world_time.read(sid, package, context=context).phase == "day"
        for kwargs in ({"limit": True}, {"limit": 1001}, {"after_tick": 31}):
            with pytest.raises(StateMachineError):
                page(**kwargs)


def test_no_cycle_calendar_only_and_legacy_has_no_invented_scale(
    tmp_path, package, context, request_for
):
    with StateMachine(tmp_path / "time.db") as sm:
        legacy = sm.create_session(request_for(), package, context=context).session_id
        view = sm.world_time.read(legacy, package, context=context)
        assert view.binding == LegacyTimeBinding()
        assert view.phase is view.current_day is view.elapsed.seconds is None
        for query in (
            lambda: sm.world_time.to_ticks(legacy, 1, "seconds", package, context=context),
            lambda: sm.world_time.boundaries(legacy, 0, 1, package, context=context),
        ):
            with pytest.raises(StateMachineError) as unsupported:
                query()
            assert unsupported.value.code == "unsupported_time_protocol"
        binding = TimeBinding(
            day_length=10, start_day_tick=9, cycle="none", day_start=None, night_start=None
        )
        sid = sm.create_session(
            replace(request_for("second"), time_binding=binding), package, context=context
        ).session_id
        view = sm.world_time.read(sid, package, context=context)
        assert view.phase is view.next_phase_boundary is None
        assert view.next_calendar_boundary == 1
        assert sm.world_time.boundaries(sid, 0, 21, package, context=context).boundaries == (
            TimeBoundary(1, ("day_rolled",)),
            TimeBoundary(11, ("day_rolled",)),
            TimeBoundary(21, ("day_rolled",)),
        )


@pytest.mark.parametrize(
    "table,action",
    [
        ("world_states", "UPDATE"),
        ("events", "INSERT"),
        ("event_outbox", "INSERT"),
        ("command_receipts", "INSERT"),
        ("sessions", "UPDATE"),
    ],
)
def test_seconds_command_fault_rolls_back_every_fact_and_retries(
    tmp_path, package, context, request_for, table, action
):
    ctx = replace(context, can_advance_time=True)
    db = tmp_path / "time.db"
    with StateMachine(db) as sm:
        sid = sm.create_session(
            replace(request_for(), time_binding=TimeBinding()), package, context=ctx
        ).session_id
        command = tick_command(sm, sid, package, ctx, 10)
        before = stored_rows(db)
        sm._conn.execute(
            f"CREATE TEMP TRIGGER fault BEFORE {action} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=ctx)
        assert error.value.code == "storage_error"
        assert stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER fault")
        assert sm.submit_command(command, package, context=ctx).status == "committed"


def test_seconds_authority_conflicts_noop_and_immutable_configuration(
    tmp_path, package, context, request_for
):
    ctx = replace(context, can_advance_time=True)
    db = tmp_path / "time.db"
    with StateMachine(db) as sm:
        sid = sm.create_session(
            replace(request_for(), time_binding=TimeBinding()), package, context=ctx
        ).session_id
        before = sm.read_session(sid, package, context=ctx)
        command = tick_command(sm, sid, package, ctx, 10)
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(command, package, context=context)
        assert denied.value.code == "unauthorized"
        assert (
            tick(sm, sid, package, ctx, 10, expected_tick=1, key="bad.precondition").error_code
            == "game_time_conflict"
        )
        stale = replace(command, command_id="stale", expected_world_version=99)
        assert sm.submit_command(stale, package, context=ctx).status == "conflict"
        assert sm.read_session(sid, package, context=ctx) == before
        noop = tick_command(sm, sid, package, ctx, 0)
        assert sm.submit_command(noop, package, context=ctx).event_seqs == ()
        assert sm.read_session(sid, package, context=ctx) == before
        assert sm.submit_command(command, package, context=ctx).status == "committed"
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(command, package, context=context)
        assert denied.value.code == "unauthorized"
        for query in (
            lambda: sm.world_time.elapsed(sid, package, context=ctx, since_tick=11),
            lambda: sm.world_time.remaining(sid, True, package, context=ctx),
            lambda: sm.world_time.to_ticks(sid, 1, "unknown", package, context=ctx),
        ):
            with pytest.raises(StateMachineError):
                query()
        for sql in (
            "UPDATE world_time_bindings SET binding_json='{}' WHERE session_id=?",
            "DELETE FROM world_time_bindings WHERE session_id=?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                sm._conn.execute(sql, (sid,))
        with pytest.raises(StateMachineError) as unsupported:
            sm.grant_npc_access(sid, "npc.caretaker", "agent:npc", package, context=ctx)
        assert unsupported.value.code == "unsupported_time_consumer"
        before_projection = stored_rows(db)
        with pytest.raises(StateMachineError) as unsupported:
            sm.project_event(sid, 2, package, context=ctx)
        assert unsupported.value.code == "unsupported_time_consumer"
        assert stored_rows(db) == before_projection


def test_seconds_combat_manual_advance_still_rejected(tmp_path, package_data, context, request_for):
    from dnd_support import initialize, loader, make_package

    assets = loader()
    package = make_package(package_data, assets)
    ctx = replace(context, can_advance_time=True)
    request = replace(
        request_for(), package_digest=package.package_digest, time_binding=TimeBinding()
    )
    with StateMachine(tmp_path / "combat.db", dnd_loader=assets) as sm:
        sid = sm.create_session(request, package, context=ctx).session_id
        assert initialize(sm, sid, package, ctx).status == "committed"
        before = sm.read_session(sid, package, context=ctx)
        command = tick_command(sm, sid, package, ctx, 6)
        receipt = sm.submit_command(command, package, context=ctx)
        assert receipt.error_code == "unsupported_combat_world_operation"
        assert sm.read_session(sid, package, context=ctx) == before
        assert sm.submit_command(command, package, context=ctx) == receipt


def test_worldhandler_composes_real_clock_without_facade(tmp_path, package, context):
    from test_domain_runtime import components, seed

    from trpg_state_machine import AdvanceGameTime

    c = components(tmp_path / "components.db")
    sid, pc = seed(c, package, context)
    try:
        assert type(c.world.time) is WorldTimeService
        with c.store.transaction(write=True):
            # Same immutable initialization seam used inside create_session.
            c.world.time.initialize(sid, TimeBinding())
            row = c.sessions._session(sid, package, context)
            events = c.world._operate(row, package, AdvanceGameTime(0, 6), actor_id=pc)
            assert events[0].payload["game_tick"] == 6
        assert c.world.time.read(sid, package, context=context).elapsed.seconds == 6
    finally:
        c.store.close()


def test_configured_legacy_consumers_fail_before_seconds_session_creation(
    tmp_path, evaluation_package, context, request_for
):
    request = replace(
        request_for(),
        adventure_id=evaluation_package.adventure_id,
        package_version=evaluation_package.package_version,
        package_digest=evaluation_package.package_digest,
        time_binding=TimeBinding(),
    )
    db = tmp_path / "time.db"
    with StateMachine(db) as sm:
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as unsupported:
            sm.create_session(request, evaluation_package, context=context)
        assert unsupported.value.code == "unsupported_time_consumer"
        assert stored_rows(db) == before


def test_new_process_recovers_binding_tick_and_original_receipt(
    tmp_path, package, context, request_for
):
    ctx = replace(context, can_advance_time=True)
    db = tmp_path / "time.db"
    binding = TimeBinding(start_day=3, start_day_tick=86399)
    with StateMachine(db) as sm:
        sid = sm.create_session(
            replace(request_for(), time_binding=binding), package, context=ctx
        ).session_id
        command = tick_command(sm, sid, package, ctx, 1)
        receipt = sm.submit_command(command, package, context=ctx)
        sm.save_session(sid, package, context=ctx)
    before = stored_rows(db)
    code = """
import json,sys
from trpg_state_machine import (StateMachine,TrustedContext,load_adventure_package,
                                world_command_from_data)
p=load_adventure_package(sys.argv[2]).package
context_data=json.loads(sys.argv[3])
context_data["controlled_npc_ids"]=tuple(context_data["controlled_npc_ids"])
c=TrustedContext(**context_data)
data=json.loads(sys.argv[4]); data.pop("check_world_effects")
cmd=world_command_from_data(data)
with StateMachine(sys.argv[1]) as sm:
    view=sm.resume_session(cmd.session_id,p,context=c)
    receipt=sm.submit_command(cmd,p,context=c)
    time=sm.world_time.read(cmd.session_id,p,context=c)
    print(json.dumps([time.game_tick,time.current_day,time.day_tick,receipt.status,
                      view.world.time_binding.start_day]))
"""
    root = Path(__file__).resolve().parents[1]
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(db),
            str(root / "tests/fixtures/lantern_station.yaml"),
            json.dumps(asdict(ctx)),
            json.dumps(asdict(command)),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "tests")))},
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(process.stdout) == [1, 4, 0, receipt.status, 3]
    assert stored_rows(db) == before


def test_real_schema31_upgrade_atomicity_and_historical_command_replay(
    tmp_path, package, context, request_for, monkeypatch
):
    root = Path(__file__).resolve().parents[1]
    old = tmp_path / "old"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "7aa9ad1", "src"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as contents:
        contents.extractall(old)
    db = tmp_path / "old.db"
    ctx = replace(context, can_advance_time=True)
    code = """
import json,sys
from trpg_state_machine import *
p=load_adventure_package(sys.argv[2]).package
c=TrustedContext('authenticated:alice','player.alice',can_advance_time=True)
r=CreateSessionRequest('old',p.adventure_id,p.package_version,p.package_digest,
                       (PlayerSelection(c.player_id,'pc_template.observer'),))
with StateMachine(sys.argv[1]) as sm:
    sid=sm.create_session(r,p,context=c).session_id
    actor=sm.read_session(sid,p,context=c).pc_actor_id
    receipt=sm.submit_command(TypedCommand(sid,'old.tick',actor,'world.advance_time',
                                          AdvanceGameTime(0,10),0),p,context=c)
    print(json.dumps([sid,actor]))
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(db), str(root / "tests/fixtures/lantern_station.yaml")],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(old / "src")},
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    sid, actor = json.loads(process.stdout)
    original = stored_rows(db)
    read = Path.read_text

    def broken(path, *args, **kwargs):
        text = read(path, *args, **kwargs)
        return (
            text.replace("PRAGMA user_version = 32;", "INVALID MIGRATION;")
            if (path.name == "_migration_32.sql")
            else text
        )

    monkeypatch.setattr(Path, "read_text", broken)
    with pytest.raises(StateMachineError) as failure:
        StateMachine(db)
    assert failure.value.code == "storage_error"
    assert stored_rows(db) == original
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 31
    monkeypatch.undo()
    from trpg_state_machine import AdvanceGameTime, TypedCommand

    replay = TypedCommand(sid, "old.tick", actor, "world.advance_time", AdvanceGameTime(0, 10), 0)
    replay_context = replace(ctx, controlled_npc_ids=())
    for _ in range(2):
        with StateMachine(db) as sm:
            assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 34
            assert sm.world_time.read(sid, package, context=ctx).binding == LegacyTimeBinding()
            assert sm.submit_command(replay, package, context=replay_context).status == "committed"
            assert sm.read_session(sid, package, context=ctx).world.game_tick == 10
            assert sm._conn.execute("SELECT * FROM world_time_bindings").fetchall() == []
        assert_original_rows_equal(original, stored_rows(db))


def test_closed_binding_data_schema():
    assert time_binding_from_data(asdict(TimeBinding())) == TimeBinding()
    for data in ({}, {**asdict(TimeBinding()), "rate": 6}, {"schema_version": "game-clock/1"}):
        with pytest.raises(StateMachineError) as error:
            time_binding_from_data(data)
        assert error.value.code == "invalid_time_binding"


def test_seconds_binding_creation_failure_is_atomic_and_legacy_cannot_be_converted(
    tmp_path, package, context, request_for
):
    db = tmp_path / "time.db"
    with StateMachine(db) as sm:
        initial = stored_rows(db)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fault BEFORE INSERT ON world_time_bindings "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        request = replace(request_for(), time_binding=TimeBinding())
        with pytest.raises(StateMachineError) as failure:
            sm.create_session(request, package, context=context)
        assert failure.value.code == "storage_error"
        assert stored_rows(db) == initial
        sm._conn.execute("DROP TRIGGER fault")
        assert sm.create_session(request, package, context=context).session_id
        legacy = sm.create_session(request_for("legacy"), package, context=context).session_id
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as failure, sm._transaction(write=True):
            sm.world_time.initialize(legacy, TimeBinding())
        assert failure.value.code == "storage_error"
        assert stored_rows(db) == before
