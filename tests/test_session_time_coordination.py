"""Real command/store paths, controllable nanosecond clock and wake scheduler.

DeadlinePort is a persistent contract fixture, NOT a production Effects system.
Production mechanism and RE Combat integration are tested separately below.
"""

import sqlite3
from dataclasses import asdict, replace
from fractions import Fraction

import pytest

from trpg_state_machine import (
    AdvanceGameTime,
    AdvanceSessionTime,
    SetObjectState,
    SetTimeRunning,
    StateMachine,
    StateMachineError,
    TimeBinding,
    TypedCommand,
    world_command_from_data,
)
from trpg_state_machine.time_coordination import DueResult


class Clock:
    ns = 0

    def __call__(self):
        return self.ns

    def advance(self, seconds):
        self.ns += int(Fraction(seconds) * 1_000_000_000)


class Handle:
    def __init__(self, at, callback):
        self.at, self.callback, self.cancelled = at, callback, False

    def cancel(self):
        self.cancelled = True


class Scheduler:
    def __init__(self, clock):
        self.clock, self.handles = clock, []

    def call_later(self, delay, callback):
        handle = Handle(self.clock.ns + round(delay * 1_000_000_000), callback)
        self.handles.append(handle)
        return handle

    def fire(self):
        due = [h for h in self.handles if not h.cancelled and h.at <= self.clock.ns]
        for h in due:
            h.cancelled = True
            h.callback()

    def active(self):
        return [h for h in self.handles if not h.cancelled]


class DeadlinePort:
    def __init__(self, sm):
        self.sm = sm
        with sm._transaction(write=True):
            sm._conn.execute(
                "CREATE TABLE test_deadlines(sid TEXT,identity TEXT,tick INTEGER,status TEXT,"
                "PRIMARY KEY(sid,identity))"
            )

    def put(self, sid, identity, tick, status="ready"):
        with self.sm._transaction(write=True):
            self.sm._conn.execute(
                "INSERT OR REPLACE INTO test_deadlines VALUES (?,?,?,?)",
                (sid, identity, tick, status),
            )
        self.sm._session_runtime.refresh_time_drivers()

    def cancel(self, sid, identity):
        with self.sm._transaction(write=True):
            self.sm._conn.execute(
                "DELETE FROM test_deadlines WHERE sid=? AND identity=?", (sid, identity)
            )
        self.sm._session_runtime.refresh_time_drivers()

    def next_boundary(self, sid, package):
        return self.sm._conn.execute(
            "SELECT min(tick) FROM test_deadlines WHERE sid=?", (sid,)
        ).fetchone()[0]

    def settle_one(self, row, package, command_id):
        self.sm._store.require_write()
        sid = row["session_id"]
        item = self.sm._conn.execute(
            "SELECT * FROM test_deadlines WHERE sid=? ORDER BY tick,identity LIMIT 1", (sid,)
        ).fetchone()
        if item["status"] == "waiting":
            return DueResult(status="waiting", reason="test_external_choice")
        self.sm._conn.execute(
            "DELETE FROM test_deadlines WHERE sid=? AND identity=?", (sid, item["identity"])
        )
        event = self.sm._capture_event(
            sid,
            package,
            "test.deadline_settled",
            {
                "identity": item["identity"],
                "boundary_tick": item["tick"],
                "game_tick": self.sm._world_facts.time_state(sid)[1],
            },
            phase="test.after_deadline",
        )
        return DueResult((event,), "interrupted" if item["status"] == "interrupt" else "continue")


@pytest.fixture
def session(tmp_path, package, context, request_for):
    ctx = replace(context, can_advance_time=True)
    sm = StateMachine(tmp_path / "time.db")
    sid = sm.create_session(
        replace(request_for(), time_binding=TimeBinding()), package, context=ctx
    ).session_id
    clock = Clock()
    scheduler = Scheduler(clock)
    with sm:
        yield sm, sid, package, ctx, clock, scheduler


def command(sm, sid, p, ctx, op, key):
    state = sm.read_session(sid, p, context=ctx)
    kind = (
        "session.set_time_running"
        if type(op) is SetTimeRunning
        else "world.advance_time"
        if type(op) is AdvanceGameTime
        else "session.advance_time"
    )
    return TypedCommand(sid, key, state.pc_actor_id, kind, op, state.world_version)


def submit(s, op, key):
    sm, sid, p, ctx, *_ = s
    return sm.submit_command(command(sm, sid, p, ctx, op, key), p, context=ctx)


def start(s, numerator=1, denominator=1):
    sm, sid, p, ctx, clock, scheduler = s
    assert submit(s, SetTimeRunning("start", numerator, denominator), "start").status == "committed"
    return sm.attach_time_driver(sid, p, context=ctx, clock_ns=clock, scheduler=scheduler)


def sync(s):
    return s[0].read_time_sync(s[1], s[2], context=s[3])


def port(s):
    value = DeadlinePort(s[0])
    s[0]._session_runtime.time.consumers["fixture"] = value
    return value


def settled(s):
    return [
        e.payload
        for e in s[0].read_events(s[1], s[2], context=s[3])
        if e.event_type == "test.deadline_settled"
    ]


@pytest.mark.parametrize("rate", [(1, 1), (3, 2), (6, 1), (1, 3)])
def test_explicit_rates_fraction_and_readonly_display(session, rate):
    driver = start(session, *rate)
    sm, _, _, _, clock, scheduler = session
    writes = sm._conn.total_changes
    for _ in range(100):
        clock.advance(Fraction(1, 10))
        dto = sync(session)
        assert dto.committed_tick == 0
    assert driver.target(sm._session_runtime.time.run(session[1])) == Fraction(
        10 * rate[0], rate[1]
    )
    assert dto.estimated_target_tick == int(Fraction(10 * rate[0], rate[1]))
    assert sm._conn.total_changes == writes
    assert scheduler.active() == []


def test_no_default_rate_manual_still_works_and_wire_adapter(session):
    assert (
        submit(session, SetTimeRunning("start"), "no-rate").error_code == "time_rate_unconfigured"
    )
    assert submit(session, AdvanceSessionTime(10), "manual").status == "committed"
    data = asdict(command(*session[:4], AdvanceSessionTime(20), "wire"))
    data.pop("check_world_effects")
    assert world_command_from_data(data).payload == AdvanceSessionTime(20)
    assert sync(session).state == "paused"


def test_18_second_expiry_delayed_wake_and_successive_boundaries(session):
    deadlines = port(session)
    deadlines.put(session[1], "effect-18", 18)
    deadlines.put(session[1], "effect-24", 24)
    start(session)
    assert session[5].active()[0].at == 18_000_000_000
    session[4].advance(30)
    session[5].fire()
    assert [(p["identity"], p["game_tick"]) for p in settled(session)] == [
        ("effect-18", 18),
        ("effect-24", 24),
    ]
    assert sync(session).committed_tick == 30
    assert session[5].active() == []


def test_earlier_change_cancel_rearms_and_old_callback_is_inert(session):
    deadlines = port(session)
    deadlines.put(session[1], "later", 100)
    start(session)
    old = session[5].active()[0]
    deadlines.put(session[1], "earlier", 18)
    assert old.cancelled
    assert session[5].active()[0].at == 18_000_000_000
    deadlines.put(session[1], "earlier", 9)
    assert session[5].active()[0].at == 9_000_000_000
    deadlines.cancel(session[1], "earlier")
    assert session[5].active()[0].at == 100_000_000_000
    session[4].advance(200)
    old.callback()
    assert sync(session).committed_tick == 0


def test_pause_resume_rate_change_fraction_and_jump_rebase(session):
    start(session)
    session[4].advance(Fraction(3, 2))
    assert submit(session, SetTimeRunning("pause"), "pause").status == "committed"
    assert sync(session).committed_tick == 1
    session[4].advance(100)
    submit(session, SetTimeRunning("start"), "resume")
    session[4].advance(Fraction(1, 2))
    submit(session, SetTimeRunning("rate", 2, 1), "rate")
    assert sync(session).committed_tick == 2
    session[4].advance(Fraction(1, 2))
    assert sync(session).estimated_target_tick == 3
    submit(session, AdvanceSessionTime(1000, "activity"), "jump")
    assert sync(session).estimated_target_tick == 1000
    session[4].advance(1)
    assert sync(session).estimated_target_tick == 1002


def test_budget_equal_target_late_additions_and_continue(session):
    deadlines = port(session)
    for n in range(5):
        deadlines.put(session[1], str(n), 0)
    receipt = submit(session, AdvanceSessionTime(0, budget=2), "budget")
    assert receipt.status == "committed"
    assert sync(session).progress_status == "pending"
    assert len(settled(session)) == 2
    submit(session, AdvanceSessionTime(0, budget=8), "continue")
    assert len(settled(session)) == 5
    assert sync(session).progress_status == "completed"
    deadlines.put(session[1], "late", 0)
    submit(session, AdvanceSessionTime(0), "late")
    assert len(settled(session)) == 6


@pytest.mark.parametrize("status", ["waiting", "interrupt"])
def test_activity_stops_before_completion_and_continues_outside_transaction(session, status):
    deadlines = port(session)
    deadlines.put(session[1], "hazard", 18, status)
    deadlines.put(session[1], "completion", 60)
    start(session)
    submit(session, AdvanceSessionTime(60, "activity"), "activity")
    assert sync(session).committed_tick == 18
    assert sync(session).progress_status in ("waiting", "interrupted")
    assert not session[0]._conn.in_transaction
    assert all(p["identity"] != "completion" for p in settled(session))
    assert session[5].active() == []
    deadlines.cancel(session[1], "hazard")
    session[4].advance(100)
    submit(session, AdvanceSessionTime(60, "activity"), "continue")
    assert sync(session).committed_tick == sync(session).estimated_target_tick == 60
    assert settled(session)[-1]["identity"] == "completion"


def player_command(s, key="player"):
    sm, sid, p, ctx, *_ = s
    state = sm.read_session(sid, p, context=ctx)
    oid, value = next(iter(state.scenes[state.current_scene_id].objects.items()))
    from trpg_state_machine import ObjectState

    return TypedCommand(
        sid,
        key,
        state.pc_actor_id,
        "set_object_state",
        SetObjectState(state.current_scene_id, oid, ObjectState(value)),
        state.world_version,
    )


def test_player_presync_versions_replay_and_authorization(session):
    start(session)
    sm, _, p, ctx, clock, _ = session
    operation = player_command(session)
    clock.advance(18)
    receipt = sm.submit_command(operation, p, context=ctx)
    assert receipt.status == "committed"
    assert sync(session).committed_tick == 18
    assert receipt.world_version > operation.expected_world_version
    assert receipt.event_seqs
    clock.advance(100)
    assert sm.submit_command(operation, p, context=ctx) == receipt
    assert sync(session).committed_tick == 18
    stale = replace(operation, command_id="stale")
    assert sm.submit_command(stale, p, context=ctx).status == "conflict"
    assert sync(session).committed_tick == 18
    with pytest.raises(StateMachineError):
        sm.submit_command(
            replace(player_command(session), actor_id="npc.unauthorized"), p, context=ctx
        )
    assert sync(session).committed_tick == 18


def test_fixed_player_target_survives_failed_commit_and_does_not_mask_new_write(
    session, monkeypatch
):
    start(session)
    sm, sid, p, ctx, clock, _ = session
    operation = player_command(session)
    clock.advance(18)
    original = sm._event_persistence._append_events

    def fail(*args, **kwargs):
        raise StateMachineError("storage_error", "injected")

    monkeypatch.setattr(sm._event_persistence, "_append_events", fail)
    with pytest.raises(StateMachineError):
        sm.submit_command(operation, p, context=ctx)
    assert sync(session).committed_tick == 0
    monkeypatch.setattr(sm._event_persistence, "_append_events", original)
    clock.advance(100)
    assert sm.submit_command(operation, p, context=ctx).status == "committed"
    assert sync(session).committed_tick == 18
    admission = sm._conn.execute(
        "SELECT * FROM time_command_admissions WHERE session_id=?", (sid,)
    ).fetchone()
    assert admission["target_tick"] == 18


def test_stale_driver_ownership_session_isolation_and_release(session):
    driver = start(session)
    sm, sid, p, ctx, clock, scheduler = session
    with pytest.raises(StateMachineError) as error:
        sm.attach_time_driver(sid, p, context=ctx, clock_ns=clock, scheduler=scheduler)
    assert error.value.code == "time_driver_owned"
    with StateMachine(sm._conn.execute("PRAGMA database_list").fetchone()[2]) as other:
        with pytest.raises(StateMachineError) as error:
            other.attach_time_driver(sid, p, context=ctx, clock_ns=clock, scheduler=scheduler)
        assert error.value.code == "time_driver_owned"
    generation = sync(session).generation
    submit(session, SetTimeRunning("pause"), "pause")
    stale = AdvanceSessionTime(20, "realtime", generation, driver.token)
    assert submit(session, stale, "old").error_code == "stale_time_driver"
    driver.close()
    assert not scheduler.active()
    replacement = sm.attach_time_driver(sid, p, context=ctx, clock_ns=clock, scheduler=scheduler)
    replacement.close()


def test_restart_no_offline_time_and_saved_rate(session):
    start(session, 3, 2)
    sm, sid, p, ctx, clock, scheduler = session
    clock.advance(10)
    submit(session, SetTimeRunning("pause"), "pause")
    submit(session, SetTimeRunning("start"), "resume")
    path = sm._conn.execute("PRAGMA database_list").fetchone()[2]
    sm.close()
    clock.ns = 900_000_000_000_000  # unrelated process epoch
    with StateMachine(path) as restored:
        restored.resume_session(sid, p, context=ctx)
        dto = restored.read_time_sync(sid, p, context=ctx)
        assert dto.state == "detached" and dto.committed_tick == 15
        assert (dto.rate_numerator, dto.rate_denominator) == (3, 2)
        restored.attach_time_driver(sid, p, context=ctx, clock_ns=clock, scheduler=scheduler)
        assert restored.read_time_sync(sid, p, context=ctx).estimated_target_tick == 15


def test_schema36_migration_preserves_every_old_row_and_does_not_create_run_rows(session):
    from test_migrations import assert_original_rows_equal, stored_rows

    sm = session[0]
    path = sm._conn.execute("PRAGMA database_list").fetchone()[2]
    sm.close()
    with sqlite3.connect(path) as db:
        from test_migrations import remove_post39_derived_storage

        remove_post39_derived_storage(db)
        db.execute("DROP TABLE instance_configs")
        for table in (
            "session_time_waits",
            "session_time_runs",
            "session_time_progress",
            "time_command_admissions",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("DROP INDEX mechanism_time_boundary")
        db.execute("PRAGMA user_version=36")
    before = stored_rows(path)
    with StateMachine(path) as restored:
        assert restored._conn.execute("PRAGMA user_version").fetchone()[0] == 40
        assert_original_rows_equal(before, stored_rows(path))
        for table in (
            "session_time_waits",
            "session_time_runs",
            "session_time_progress",
            "time_command_admissions",
        ):
            assert restored._conn.execute(f"SELECT * FROM {table}").fetchall() == []


def test_real_mechanism_resets_once_at_18_before_endpoint(tmp_path, context):
    from lock_support import A
    from test_mechanism_service import extended_data
    from test_world_mechanisms import LEVER
    from world_support import setup
    from world_support import submit as world_submit

    from trpg_state_machine import ActivateMechanism

    data = extended_data(reset_policy="after_seconds", reset_delay_seconds=18)
    data["time_binding"] = asdict(TimeBinding())
    for actor in data["actors"]:
        actor.pop("npc_state", None)
    ctx = replace(context, can_advance_time=True)
    sm, sid, p, _ = setup(tmp_path, ctx, data)
    with sm:
        clock = Clock()
        scheduler = Scheduler(clock)
        s = (sm, sid, p, ctx, clock, scheduler)
        start(s)
        assert (
            world_submit(
                sm, sid, p, ctx, ActivateMechanism(A, LEVER, "0,0", "up"), "activate"
            ).status
            == "committed"
        )
        clock.advance(30)
        scheduler.fire()
        events = sm.read_events(sid, p, context=ctx)
        kinds = [e.event_type for e in events]
        resets = [e for e in events if e.event_type == "mechanism.reset"]
        assert len(resets) == 1 and resets[0].payload["boundary_tick"] == 18
        assert kinds[-3:] == ["game.time_advanced", "mechanism.reset", "game.time_advanced"]
        assert sm._world_facts.mechanism_state(sid, LEVER).reset_at_tick is None
        assert sync(s).committed_tick == 30
        assert (
            world_submit(sm, sid, p, ctx, AdvanceGameTime(30, 31), "manual").status == "committed"
        )
        assert (
            len(
                [
                    e
                    for e in sm.read_events(sid, p, context=ctx)
                    if e.event_type == "mechanism.reset"
                ]
            )
            == 1
        )


def test_real_combat_round_six_ticks_replay_and_exit_rebase(tmp_path, package_data, context):
    from dnd_support import create, initialize
    from modern_support import modern_data, modern_loader
    from test_combat_closure import close_command, pc_attack

    from trpg_state_machine.package import _validate

    assets = modern_loader()
    data = modern_data(package_data, assets, npc_hp=1)
    data["time_binding"] = asdict(TimeBinding())
    for actor in data["actors"]:
        actor.pop("npc_state", None)
    p = _validate(data)
    ctx = replace(context, can_advance_time=True)
    with StateMachine(tmp_path / "combat-time.db", dnd_loader=assets) as sm:
        sid = create(sm, p, ctx)
        clock = Clock()
        scheduler = Scheduler(clock)
        s = (sm, sid, p, ctx, clock, scheduler)
        start(s)
        clock.advance(10)
        assert initialize(sm, sid, p, ctx).status == "committed"
        assert sync(s).state == "combat" and sync(s).committed_tick == 10
        clock.advance(100)
        assert sync(s).estimated_target_tick == 10
        before = sm.read_combat_snapshot(sid, p, context=ctx).combat_state.round_number
        attack = pc_attack(sm, sid, p, ctx)
        receipt = sm.submit_command(attack, p, context=ctx)
        assert receipt.status == "committed"
        after = sm.read_combat_snapshot(sid, p, context=ctx).combat_state.round_number
        assert after == before + 1
        assert sync(s).committed_tick == 16
        assert sm.submit_command(attack, p, context=ctx) == receipt
        assert sync(s).committed_tick == 16
        assert (
            sm.submit_command(close_command(sm, sid, p, ctx), p, context=ctx).status == "committed"
        )
        assert sync(s).state == "running" and sync(s).estimated_target_tick == 16
        clock.advance(2)
        assert sync(s).estimated_target_tick == 18


def test_simultaneous_domain_choice_is_preserved_and_budget_bounds_past_recreation(session):
    deadlines = port(session)
    deadlines.put(session[1], "one", 18)
    # Two independent domain owners need a dependency/choice policy.
    session[0]._session_runtime.time.consumers["other"] = deadlines
    submit(session, AdvanceSessionTime(40), "simultaneous")
    assert sync(session).committed_tick == 18
    assert sync(session).progress_reason == "simultaneous_time_choice"
    assert settled(session) == []
    session[0]._session_runtime.time.resolve_simultaneous = lambda sid, names: names[0]
    submit(session, AdvanceSessionTime(40), "chosen")
    assert sync(session).committed_tick == 40
    assert len(settled(session)) == 1

    original = deadlines.settle_one

    def recreate(row, package, key):
        result = original(row, package, key)
        session[0]._conn.execute(
            "INSERT INTO test_deadlines VALUES (?,?,?,?)", (session[1], "past", 0, "ready")
        )
        return result

    deadlines.put(session[1], "past", 0)
    deadlines.settle_one = recreate
    submit(session, AdvanceSessionTime(40, budget=3), "bounded-past")
    assert sync(session).progress_status == "pending"
    assert len(settled(session)) == 4


def test_two_sessions_and_rollback_preserve_rng_events_work_and_receipts(
    session, request_for, monkeypatch
):
    from test_migrations import stored_rows

    sm, sid, p, ctx, *_ = session
    other = sm.create_session(
        replace(request_for(), command_id="other", time_binding=TimeBinding()), p, context=ctx
    ).session_id
    deadlines = port(session)
    deadlines.put(sid, "first", 18)
    deadlines.put(other, "other", 9)
    path = sm._conn.execute("PRAGMA database_list").fetchone()[2]
    original = sm._event_persistence._append_events
    before = stored_rows(path)

    def fail(*args, **kwargs):
        raise StateMachineError("storage_error", "injected publication fault")

    monkeypatch.setattr(sm._event_persistence, "_append_events", fail)
    cmd = command(sm, sid, p, ctx, AdvanceSessionTime(30), "retry")
    with pytest.raises(StateMachineError):
        sm.submit_command(cmd, p, context=ctx)
    assert stored_rows(path) == before
    monkeypatch.setattr(sm._event_persistence, "_append_events", original)
    receipt = sm.submit_command(cmd, p, context=ctx)
    assert receipt.status == "committed"
    committed = stored_rows(path)
    assert sm.submit_command(cmd, p, context=ctx) == receipt
    assert stored_rows(path) == committed
    assert sm.read_time_sync(other, p, context=ctx).committed_tick == 0
    assert deadlines.next_boundary(other, p) == 9


def test_control_retry_keeps_effective_target_and_fraction(session, monkeypatch):
    start(session)
    sm, sid, p, ctx, clock, _ = session
    clock.advance(Fraction(37, 2))
    pause = command(sm, sid, p, ctx, SetTimeRunning("pause"), "fixed-pause")
    original = sm._event_persistence._append_events

    def fail(*args, **kwargs):
        raise StateMachineError("storage_error", "injected")

    monkeypatch.setattr(sm._event_persistence, "_append_events", fail)
    with pytest.raises(StateMachineError):
        sm.submit_command(pause, p, context=ctx)
    monkeypatch.setattr(sm._event_persistence, "_append_events", original)
    clock.advance(100)
    receipt = sm.submit_command(pause, p, context=ctx)
    assert receipt.status == "committed"
    assert sync(session).committed_tick == 18 and sync(session).state == "paused"
    assert sm._session_runtime.time.run(sid)["remainder"] == "1/2"
    assert sm.submit_command(pause, p, context=ctx) == receipt


def test_player_sync_budget_retries_original_target_and_guards_timer(session):
    deadlines = port(session)
    for n in range(10):
        deadlines.put(session[1], str(n), 18)
    start(session)
    sm, _, p, ctx, clock, scheduler = session
    operation = player_command(session)
    clock.advance(30)
    with pytest.raises(StateMachineError) as error:
        sm.submit_command(operation, p, context=ctx)
    assert error.value.code == "time_advance_pending"
    assert sync(session).committed_tick == 18 and len(settled(session)) == 8
    assert scheduler.active() == []
    clock.advance(100)
    receipt = sm.submit_command(operation, p, context=ctx)
    assert receipt.status == "committed"
    assert sync(session).committed_tick == 30 and len(settled(session)) == 10
    assert len(receipt.event_seqs) >= 12


def test_later_unrelated_version_is_not_hidden_and_receipt_excludes_its_events(session):
    from trpg_state_machine import SetSceneFlag

    start(session)
    sm, sid, p, ctx, clock, _ = session
    operation = player_command(session)
    clock.advance(18)
    runtime = sm._session_runtime
    with sm._transaction():
        row = sm._session(sid, p, ctx)
        fingerprint = runtime.fingerprint(operation, row, ctx)
    runtime.prepare_timed_command(operation, p, ctx, fingerprint)
    state = sm.read_session(sid, p, context=ctx)
    scene = state.scenes[state.current_scene_id]
    flag, value = next(iter(scene.local_flags.items()))
    other = TypedCommand(
        sid,
        "other-write",
        state.pc_actor_id,
        "set_scene_flag",
        SetSceneFlag(state.current_scene_id, flag, not value),
        state.world_version,
    )
    assert sm.submit_command(other, p, context=ctx).status == "committed"
    receipt = sm.submit_command(operation, p, context=ctx)
    assert receipt.status == "conflict"
    events = sm.read_events(sid, p, context=ctx)
    assert receipt.event_seqs
    assert all(
        e.source_command_id == operation.command_id
        for e in events
        if e.event_seq in receipt.event_seqs
    )
    assert sync(session).committed_tick == 18


def test_foreign_writer_cannot_add_unnotified_earlier_boundary(session):
    start(session)
    sm, _, p, ctx, *_ = session
    cmd = player_command(session)
    with StateMachine(sm._conn.execute("PRAGMA database_list").fetchone()[2]) as other:
        assert other.read_session(session[1], p, context=ctx)
        with pytest.raises(StateMachineError) as error:
            other.submit_command(cmd, p, context=ctx)
        assert error.value.code == "time_driver_owned"


def test_realtime_external_wait_does_not_add_wait_processing_time(session):
    deadlines = port(session)
    deadlines.put(session[1], "choice", 18, "waiting")
    driver = start(session)
    session[4].advance(30)
    session[5].fire()
    assert sync(session).committed_tick == 18 and sync(session).progress_status == "waiting"
    session[4].advance(1000)
    deadlines.cancel(session[1], "choice")
    operation = AdvanceSessionTime(30, "realtime", sync(session).generation, driver.token)
    assert submit(session, operation, "continue-wait").status == "committed"
    assert sync(session).committed_tick == sync(session).estimated_target_tick == 30


def test_boundary_cascades_reference_actual_source_event_sequence(
    tmp_path, package_data, context, request_for
):
    from test_events import event_data

    from trpg_state_machine import SetSceneFlag
    from trpg_state_machine.package import _validate

    data = package_data
    scene = data["scenes"][0]
    scene["initial_state"]["local_flags"].update({"flag.expiry_a": False, "flag.expiry_b": False})
    data["events"] = [event_data("event.expiry_a"), event_data("event.expiry_b")]
    for event, flag in zip(data["events"], ("flag.expiry_a", "flag.expiry_b"), strict=True):
        event["condition"]["flag_id"] = flag
    scene["event_definition_ids"] = [event["event_definition_id"] for event in data["events"]]
    p = _validate(data)
    ctx = replace(context, can_advance_time=True)
    with StateMachine(tmp_path / "cascade.db") as sm:
        sid = sm.create_session(
            replace(request_for(), package_digest=p.package_digest, time_binding=TimeBinding()),
            p,
            context=ctx,
        ).session_id
        fixture = DeadlinePort(sm)
        sm._session_runtime.time.consumers["fixture"] = fixture
        fixture.put(sid, "a", 10)
        fixture.put(sid, "b", 20)
        original = fixture.settle_one

        def settle(row, package, key):
            result = original(row, package, key)
            identity = result.events[0].payload["identity"]
            events = sm._world.scenes.set_flag(
                row,
                package,
                SetSceneFlag(scene["scene_id"], "flag.expiry_" + identity, True),
                actor_id=row["pc_actor_id"],
            )
            return DueResult(tuple(events))

        fixture.settle_one = settle
        s = (sm, sid, p, ctx, Clock(), Scheduler(Clock()))
        assert submit(s, AdvanceSessionTime(30), "cascade").status == "committed"
        events = sm.read_events(sid, p, context=ctx)
        for suffix in ("a", "b"):
            source = next(
                e
                for e in events
                if e.event_type == "scene.flag_changed"
                and e.payload["flag_id"] == "flag.expiry_" + suffix
            )
            actual = sm._conn.execute(
                "SELECT source_event_seq FROM trigger_history WHERE session_id=? "
                "AND event_definition_id=?",
                (sid, "event.expiry_" + suffix),
            ).fetchone()[0]
            assert actual == source.event_seq


@pytest.mark.parametrize(
    "op",
    [
        SetTimeRunning("start", 0, 1),
        SetTimeRunning("rate", True, 1),
        SetTimeRunning("rate", 2, None),
        AdvanceSessionTime(True),
        AdvanceSessionTime(1, budget=0),
    ],
)
def test_invalid_time_request_has_no_writes(session, op):
    before = session[0]._conn.total_changes
    with pytest.raises(StateMachineError):
        submit(session, op, "invalid")
    assert session[0]._conn.total_changes == before


def test_time_grant_required_before_any_state_or_admission_write(session):
    sm, sid, p, ctx, *_ = session
    start(session)
    cmd = command(sm, sid, p, ctx, AdvanceSessionTime(30), "unauthorized-time")
    before = sm._conn.total_changes
    with pytest.raises(StateMachineError) as error:
        sm.submit_command(cmd, p, context=replace(ctx, can_advance_time=False))
    assert error.value.code == "unauthorized"
    assert sm._conn.total_changes == before


def test_combat_pending_budget_must_finish_before_close_and_cannot_be_cancelled(
    tmp_path, package_data, context
):
    from dnd_support import create, initialize
    from modern_support import modern_data, modern_loader
    from test_combat_closure import close_command, pc_attack

    from trpg_state_machine.package import _validate

    assets = modern_loader()
    data = modern_data(package_data, assets, npc_hp=1)
    data["time_binding"] = asdict(TimeBinding())
    for actor in data["actors"]:
        actor.pop("npc_state", None)
    p = _validate(data)
    ctx = replace(context, can_advance_time=True)
    with StateMachine(tmp_path / "combat-budget.db", dnd_loader=assets) as sm:
        sid = create(sm, p, ctx)
        fixture = DeadlinePort(sm)
        sm._session_runtime.time.consumers["fixture"] = fixture
        for n in range(10):
            fixture.put(sid, str(n), 1)
        assert initialize(sm, sid, p, ctx).status == "committed"
        attack = pc_attack(sm, sid, p, ctx)
        assert sm.submit_command(attack, p, context=ctx).status == "committed"
        s = (sm, sid, p, ctx, Clock(), Scheduler(Clock()))
        assert sync(s).committed_tick == 1 and sync(s).pending_target_tick == 6
        assert sync(s).progress_status == "pending"
        assert submit(s, SetTimeRunning("cancel"), "cancel").error_code == "time_mode_conflict"
        assert sync(s).pending_target_tick == 6 and sync(s).progress_status == "pending"
        assert (
            sm.submit_command(close_command(sm, sid, p, ctx), p, context=ctx).status == "committed"
        )
        assert sync(s).committed_tick == 6 and sync(s).progress_status == "completed"
        assert len(settled(s)) == 10


def test_cancelling_target_cannot_discard_unsettled_work_at_committed_time(session):
    fixture = port(session)
    fixture.put(session[1], "hazard", 18, "waiting")
    submit(session, AdvanceSessionTime(60, "activity"), "activity")
    assert sync(session).committed_tick == 18 and sync(session).progress_status == "waiting"
    assert (
        submit(session, SetTimeRunning("cancel"), "cancel-denied").error_code
        == "time_advance_pending"
    )
    assert sync(session).pending_target_tick == 60
    fixture.cancel(session[1], "hazard")  # the domain, not Runtime, cancels the actual work
    assert submit(session, SetTimeRunning("cancel"), "cancel").status == "committed"
    assert sync(session).pending_target_tick is None and sync(session).committed_tick == 18
