"""Recovery regressions through real Session commands and deterministic clocks."""

from fractions import Fraction

import pytest
from test_session_time_coordination import (
    Clock,
    Scheduler,
    command,
    player_command,
    port,
    settled,
    start,
    submit,
    sync,
)
from test_session_time_coordination import (
    session as session,
)

from trpg_state_machine import AdvanceSessionTime, SetTimeRunning, StateMachine, StateMachineError


def reopen(s, deadlines):
    sm, sid, package, context, *_ = s
    path = sm._conn.execute("PRAGMA database_list").fetchone()[2]
    sm.close()
    restored = StateMachine(path)
    restored.resume_session(sid, package, context=context)
    deadlines.sm = restored
    restored._session_runtime.time.consumers["fixture"] = deadlines
    clock = Clock()
    clock.ns = 9_000_000_000_000_000
    scheduler = Scheduler(clock)
    driver = restored.attach_time_driver(
        sid, package, context=context, clock_ns=clock, scheduler=scheduler
    )
    return (restored, sid, package, context, clock, scheduler), driver


def test_restarted_player_admission_rejoins_driver_and_future_wake(session, monkeypatch):
    deadlines = port(session)
    deadlines.put(session[1], "next", 23)
    start(session)
    sm, _, package, context, clock, _ = session
    original = player_command(session, "fixed-before-restart")
    clock.advance(18)

    def fault(*args, **kwargs):
        raise StateMachineError("storage_error", "injected after admission")

    monkeypatch.setattr(sm._event_persistence, "_append_events", fault)
    with pytest.raises(StateMachineError):
        sm.submit_command(original, package, context=context)
    assert sync(session).committed_tick == 0
    restored, driver = reopen(session, deadlines)
    with restored[0] as sm:
        result = sm.submit_command(original, package, context=context)
        assert result.status == "committed"
        # Observe the real epoch/schedule before any display query can repair it.
        assert driver.base_tick == 18
        assert restored[5].active()[0].at == restored[4].ns + 5_000_000_000
        assert sync(restored).committed_tick == sync(restored).estimated_target_tick == 18
        assert sm.submit_command(original, package, context=context) == result
        assert sync(restored).committed_tick == 18
        restored[4].advance(Fraction(5, 2))
        assert (
            sm.submit_command(
                player_command(restored, "next-player"), package, context=context
            ).status
            == "committed"
        )
        assert sync(restored).committed_tick == 20
        assert Fraction(
            sync(restored).display_fraction_numerator, sync(restored).display_fraction_denominator
        ) == Fraction(1, 2)
        restored[4].advance(Fraction(5, 2))
        restored[5].fire()
        assert sync(restored).committed_tick == 23
        assert settled(restored)[-1]["identity"] == "next"


def test_restarted_budgeted_realtime_target_rejoins_driver(session):
    deadlines = port(session)
    for i in range(10):
        deadlines.put(session[1], str(i), 18)
    deadlines.put(session[1], "later", 40)
    start(session)
    session[4].advance(30)
    session[5].fire()
    assert sync(session).committed_tick == 18 and sync(session).progress_status == "pending"
    restored, driver = reopen(session, deadlines)
    with restored[0]:
        restored[5].fire()
        assert driver.base_tick == 30  # budget continuation also rejoins before a DTO read
        assert sync(restored).committed_tick == sync(restored).estimated_target_tick == 30
        assert restored[5].active()[0].at == restored[4].ns + 10_000_000_000
        assert (
            restored[0]
            .submit_command(player_command(restored), restored[2], context=restored[3])
            .status
            == "committed"
        )
        restored[4].advance(10)
        restored[5].fire()
        assert sync(restored).committed_tick == 40


@pytest.mark.parametrize("completed", [False, True])
def test_cancel_without_unfinished_target_preserves_elapsed_and_deadline(session, completed):
    deadlines = port(session)
    deadlines.put(session[1], "expiry", 18)
    driver = start(session)
    if completed:
        session[0].submit_command(player_command(session), session[2], context=session[3])
    session[4].advance(Fraction(21, 2))
    before = sync(session)
    base = driver.base_ns, driver.base_tick
    cancel = command(*session[:4], SetTimeRunning("cancel"), "cancel")
    result = session[0].submit_command(cancel, session[2], context=session[3])
    assert result.status == "committed"
    assert sync(session).generation == before.generation
    assert (driver.base_ns, driver.base_tick) == base
    assert driver.target(session[0]._session_runtime.time.run(session[1])) == Fraction(21, 2)
    assert session[5].active()[0].at == 18_000_000_000
    assert session[0].submit_command(cancel, session[2], context=session[3]) == result
    session[4].advance(Fraction(15, 2))
    session[5].fire()
    assert sync(session).committed_tick == 18 and len(settled(session)) == 1


def test_paused_display_keeps_fraction_and_resumes_continuously(session):
    start(session)
    session[4].advance(Fraction(3, 2))
    submit(session, SetTimeRunning("pause"), "pause")
    paused = sync(session)
    assert paused.state == "paused" and paused.committed_tick == paused.display_base_tick == 1
    assert Fraction(
        paused.display_fraction_numerator, paused.display_fraction_denominator
    ) == Fraction(1, 2)
    session[4].advance(100)
    assert sync(session) == paused
    submit(session, SetTimeRunning("start"), "resume")
    resumed = sync(session)
    assert resumed.display_base_tick == paused.display_base_tick
    assert resumed.display_fraction_numerator == paused.display_fraction_numerator
    session[4].advance(Fraction(1, 2))
    assert sync(session).estimated_target_tick == 2


def test_ordinary_small_commits_keep_original_epoch_and_fraction(session):
    driver = start(session, 3, 2)
    origin = driver.base_ns, driver.base_tick
    for i in range(1, 21):
        session[4].advance(Fraction(1, 10))
        result = session[0].submit_command(
            player_command(session, "step." + str(i)), session[2], context=session[3]
        )
        assert result.status == "committed"
        assert (driver.base_ns, driver.base_tick) == origin
        assert driver.target(session[0]._session_runtime.time.run(session[1])) == Fraction(
            3 * i, 20
        )
    assert sync(session).committed_tick == 3


def test_realtime_budget_cancel_is_refused_without_discarding_elapsed(session):
    deadlines = port(session)
    for i in range(10):
        deadlines.put(session[1], str(i), 18)
    driver = start(session)
    session[4].advance(30)
    session[5].fire()
    assert sync(session).progress_status == "pending"
    before = driver.base_ns, driver.base_tick, sync(session).generation
    cancel = command(*session[:4], SetTimeRunning("cancel"), "cancel-pending-realtime")
    receipt = session[0].submit_command(cancel, session[2], context=session[3])
    assert receipt.error_code == "time_advance_pending"
    assert (driver.base_ns, driver.base_tick, sync(session).generation) == before
    assert session[0].submit_command(cancel, session[2], context=session[3]) == receipt
    session[5].fire()
    assert sync(session).committed_tick == sync(session).estimated_target_tick == 30


@pytest.mark.parametrize("source", ["activity", "manual"])
def test_jump_budget_cancel_stays_at_legal_boundary_and_keeps_domain_deadlines(session, source):
    deadlines = port(session)
    deadlines.put(session[1], "boundary", 18)
    deadlines.put(session[1], "future", 60)
    start(session)
    submit(session, AdvanceSessionTime(60, source, budget=1), "partial-jump")
    assert sync(session).committed_tick == 18 and sync(session).progress_status == "pending"
    session[4].advance(100)
    assert submit(session, SetTimeRunning("cancel"), "cancel-jump").status == "committed"
    assert sync(session).committed_tick == sync(session).estimated_target_tick == 18
    assert deadlines.next_boundary(session[1], session[2]) == 60
    assert session[5].active()[0].at == session[4].ns + 42_000_000_000


def test_interrupted_jump_cancel_does_not_add_waiting_wall_time(session):
    deadlines = port(session)
    deadlines.put(session[1], "interrupt", 18, "interrupt")
    start(session)
    submit(session, AdvanceSessionTime(60, "activity"), "interrupted")
    assert sync(session).progress_status == "interrupted"
    session[4].advance(1000)
    assert submit(session, SetTimeRunning("cancel"), "cancel").status == "committed"
    assert sync(session).committed_tick == sync(session).estimated_target_tick == 18
