"""Persistent test domain adapter, not a production Effects/rest integration.

Replies execute actual SetSceneFlag or pinned RE checks through submit_command;
only this adapter decides and persists its own choice/reward state.
"""

from dataclasses import asdict, replace

import pytest
from test_migrations import stored_rows
from test_session_time_coordination import (
    Clock,
    Scheduler,
    player_command,
    start,
    submit,
    sync,
)
from test_session_time_coordination import (
    session as session,
)

from trpg_state_machine import (
    AdvanceSessionTime,
    DomainTimeWait,
    SetSceneFlag,
    SetTimeRunning,
    StateMachine,
    StateMachineError,
    TimeBinding,
    TimeWaitReference,
    TimeWaitResolution,
    TypedCommand,
    world_command_from_data,
)
from trpg_state_machine.time_coordination import DueResult


class ChoiceDomain:
    def __init__(self, sm):
        self.sm = sm
        with sm._transaction(write=True):
            sm._conn.execute(
                "CREATE TABLE IF NOT EXISTS test_time_choices(sid TEXT,id TEXT,version INTEGER,"
                "status TEXT,tick INTEGER,outcome TEXT,handled_by TEXT,PRIMARY KEY(sid,id))"
            )
        sm._session_runtime.time.consumers["choices"] = self

    def put(self, sid, identity="choice", tick=18, status="waiting", outcome="continue"):
        with self.sm._transaction(write=True):
            self.sm._conn.execute(
                "INSERT OR REPLACE INTO test_time_choices VALUES (?,?,1,?,?,?,NULL)",
                (sid, identity, status, tick, outcome),
            )
        self.sm._session_runtime.refresh_time_drivers()

    def row(self, sid, identity="choice"):
        return self.sm._conn.execute(
            "SELECT * FROM test_time_choices WHERE sid=? AND id=?", (sid, identity)
        ).fetchone()

    def next_boundary(self, sid, package):
        return self.sm._conn.execute(
            "SELECT min(tick) FROM test_time_choices WHERE sid=? AND status IN ('waiting','ready')",
            (sid,),
        ).fetchone()[0]

    def read_time_wait(self, sid, wait_id, package):
        row = self.row(sid, wait_id)
        return None if row is None else DomainTimeWait(wait_id, row["version"], row["status"])

    def settle_one(self, row, package, key):
        item = self.sm._conn.execute(
            "SELECT * FROM test_time_choices WHERE sid=? AND status IN ('waiting','ready') "
            "ORDER BY tick,id LIMIT 1",
            (row["session_id"],),
        ).fetchone()
        if item["status"] == "waiting":
            return DueResult(
                status="waiting",
                reason="test_choice",
                wait=DomainTimeWait(item["id"], item["version"]),
            )
        self.sm._conn.execute(
            "UPDATE test_time_choices SET status='completed',version=version+1,handled_by=? "
            "WHERE sid=? AND id=?",
            (key, row["session_id"], item["id"]),
        )
        return DueResult(
            (
                self.sm._capture_event(
                    row["session_id"],
                    package,
                    "test.activity_completed",
                    {"id": item["id"]},
                    phase="test.after_completion",
                ),
            )
        )

    def authorize_time_wait_command(self, row, package, command, context, wait):
        if not context.can_adjudicate_checks:
            raise StateMachineError("unauthorized", "Test choice business grant is required")
        if command.command_type == "rules.check":
            return  # The same real Check admission, evaluator and RNG adoption still run.
        if (
            command.command_type != "set_scene_flag"
            or command.payload.scene_id != package.entry_scene_id
            or command.payload.flag_id != "flag.lamp_checked"
        ):
            raise StateMachineError(
                "unauthorized_time_wait_command", "Not a reply owned by this domain"
            )

    def resolve_time_wait(self, row, package, command, wait, *, events):
        self.sm._store.require_write()
        assert self.read_time_wait(row["session_id"], wait.wait_id, package) == wait
        if command.command_type == "rules.check":
            check = next(e for e in events if e.event_type == "rules.check_rolled")
            if not check.payload["engine_event"]["succeeded"]:
                raise StateMachineError("invalid_wait_choice", "The actual Engine check failed")
        elif not command.payload.value:
            raise StateMachineError("invalid_wait_choice", "This domain refuses that choice")
        item = self.row(row["session_id"], wait.wait_id)
        outcome = item["outcome"]
        status = {
            "continue": "resolved",
            "waiting": "waiting",
            "interrupt": "interrupted",
            "cancel": "cancelled",
        }[outcome]
        self.sm._conn.execute(
            "UPDATE test_time_choices SET status=?,version=version+1,handled_by=?,"
            "outcome='continue' "
            "WHERE sid=? AND id=?",
            (status, command.command_id, row["session_id"], wait.wait_id),
        )
        if outcome in ("interrupt", "cancel"):
            self.sm._conn.execute(
                "UPDATE test_time_choices SET status='cancelled',version=version+1 "
                "WHERE sid=? AND id='completion'",
                (row["session_id"],),
            )
        return TimeWaitResolution(outcome)


def setup_wait(s, outcome="continue", *, realtime=False):
    domain = ChoiceDomain(s[0])
    domain.put(s[1], outcome=outcome)
    domain.put(s[1], "completion", 60, "ready")
    start(s)
    original = None
    if realtime:
        original = player_command(s, "original-business")
        s[4].advance(60)
        with pytest.raises(StateMachineError) as error:
            s[0].submit_command(original, s[2], context=s[3])
        assert error.value.code == "time_advance_pending"
    else:
        assert submit(s, AdvanceSessionTime(60, "activity"), "jump").status == "committed"
    assert sync(s).committed_tick == 18 and sync(s).time_wait is not None
    return domain, original


def reply(s, key="reply", *, ref=None, **changes):
    sm, sid, p, ctx, *_ = s
    state = sm.read_session(sid, p, context=ctx)
    cmd = TypedCommand(
        sid,
        key,
        state.pc_actor_id,
        "set_scene_flag",
        SetSceneFlag(p.entry_scene_id, "flag.lamp_checked", True),
        state.world_version,
        time_wait=ref or sync(s).time_wait,
    )
    return replace(cmd, **changes)


def send(s, cmd, *, context=None):
    return s[0].submit_command(
        cmd, s[2], context=context or replace(s[3], can_adjudicate_checks=True)
    )


def resume(s, target=60, source="activity"):
    return submit(s, AdvanceSessionTime(target, source), "continue." + str(target))


def test_public_command_resolves_wait_then_continues_without_early_reward(session):
    domain, _ = setup_wait(session)
    session[4].advance(1000)
    cmd = reply(session)
    result = send(session, cmd)
    assert result.status == "committed"
    assert domain.row(session[1])["status"] == "resolved"
    assert domain.row(session[1])["handled_by"] == cmd.command_id
    assert domain.row(session[1], "completion")["status"] == "ready"
    assert sync(session).committed_tick == 18 and sync(session).time_wait is None
    assert resume(session).status == "committed"
    assert sync(session).committed_tick == sync(session).estimated_target_tick == 60
    assert domain.row(session[1], "completion")["status"] == "completed"
    before = stored_rows(session[0]._conn.execute("PRAGMA database_list").fetchone()[2])
    assert send(session, cmd) == result
    assert stored_rows(session[0]._conn.execute("PRAGMA database_list").fetchone()[2]) == before


@pytest.mark.parametrize("outcome", ["interrupt", "cancel"])
def test_domain_owns_interruption_and_cancellation(session, outcome):
    domain, _ = setup_wait(session, outcome)
    assert send(session, reply(session)).status == "committed"
    assert sync(session).committed_tick == 18
    assert domain.row(session[1], "completion")["status"] == "cancelled"
    if outcome == "interrupt":
        assert sync(session).progress_status == "interrupted"
        assert resume(session).error_code == "time_advance_interrupted"
    else:
        assert sync(session).pending_target_tick == 18
        assert resume(session, 18).status == "committed"
    assert sync(session).committed_tick == 18


def test_ordinary_operations_and_ungranted_or_unowned_replies_cannot_bypass_wait(session):
    domain, _ = setup_wait(session)
    with pytest.raises(StateMachineError, match="saved time work"):
        session[0].submit_command(player_command(session), session[2], context=session[3])
    for cmd, ctx in [
        (reply(session), session[3]),
        (
            reply(session),
            replace(session[3], player_id="not-the-session-player", can_adjudicate_checks=True),
        ),
        (
            reply(session, actor_id="npc.uncontrolled"),
            replace(session[3], can_adjudicate_checks=True),
        ),
        (
            replace(player_command(session), time_wait=sync(session).time_wait),
            replace(session[3], can_adjudicate_checks=True),
        ),
    ]:
        with pytest.raises(StateMachineError):
            send(session, cmd, context=ctx)
    assert submit(session, SetTimeRunning("cancel"), "cancel").error_code == "time_wait_unresolved"
    assert domain.row(session[1])["version"] == 1 and sync(session).committed_tick == 18
    assert send(session, reply(session)).status == "committed"
    with pytest.raises(StateMachineError) as error:
        send(session, reply(session, ref=TimeWaitReference("unknown", 1)))
    assert error.value.code == "invalid_time_wait"


def test_wrong_session_wait_version_and_old_replies_are_rejected(session, request_for):
    _, _ = setup_wait(session)
    ref = sync(session).time_wait
    for bad in (
        TimeWaitReference("not-this-wait", ref.version),
        replace(ref, version=ref.version + 1),
    ):
        with pytest.raises(StateMachineError):
            send(session, reply(session, ref=bad))
    other = (
        session[0]
        .create_session(
            replace(request_for(), command_id="other", time_binding=TimeBinding()),
            session[2],
            context=session[3],
        )
        .session_id
    )
    other_state = session[0].read_session(other, session[2], context=session[3])
    with pytest.raises(StateMachineError) as error:
        send(
            session,
            replace(
                reply(session),
                session_id=other,
                actor_id=other_state.pc_actor_id,
                expected_world_version=other_state.world_version,
            ),
        )
    assert error.value.code == "invalid_time_wait"
    cmd = reply(session)
    receipt = send(session, cmd)
    assert send(session, cmd) == receipt
    with pytest.raises(StateMachineError) as error:
        send(session, reply(session, "late", ref=ref))
    assert error.value.code == "stale_time_wait"
    with pytest.raises(StateMachineError) as error:
        send(session, cmd, context=session[3])
    assert error.value.code == "unauthorized"  # business permission is rechecked on replay


@pytest.mark.parametrize("phase", ["domain", "publication"])
def test_failed_reply_rolls_back_business_wait_events_rng_and_receipt(session, monkeypatch, phase):
    domain, _ = setup_wait(session)
    cmd = reply(session)
    path = session[0]._conn.execute("PRAGMA database_list").fetchone()[2]
    before = stored_rows(path)
    original = domain.resolve_time_wait

    def fault(*args, **kwargs):
        if phase == "domain":
            original(*args, **kwargs)
        raise StateMachineError("storage_error", "injected reply failure")

    target, name = (
        (domain, "resolve_time_wait")
        if phase == "domain"
        else (session[0]._event_persistence, "_append_events")
    )
    with monkeypatch.context() as patch:
        patch.setattr(target, name, fault)
        with pytest.raises(StateMachineError):
            send(session, cmd)
    assert stored_rows(path) == before
    assert send(session, cmd).status == "committed"


def test_real_version_conflict_and_domain_invalid_choice_leave_wait_intact(session):
    domain, _ = setup_wait(session)
    cmd = reply(session)
    result = send(session, replace(cmd, expected_world_version=cmd.expected_world_version - 1))
    assert result.status == "conflict"
    bad = reply(session, "bad", payload=replace(cmd.payload, value=False))
    result = send(session, bad)
    assert result.status == "rejected" and result.error_code == "invalid_wait_choice"
    assert domain.row(session[1])["version"] == 1 and sync(session).time_wait == cmd.time_wait
    assert send(session, reply(session, "good")).status == "committed"


def test_approved_replies_rebind_only_origin_admission_including_partial_reply(session):
    domain, original = setup_wait(session, "waiting", realtime=True)
    old_ref = sync(session).time_wait
    session[4].advance(1000)
    assert send(session, reply(session)).status == "committed"
    assert sync(session).time_wait.wait_id == old_ref.wait_id
    assert sync(session).time_wait.version == old_ref.version + 1
    with pytest.raises(StateMachineError):
        send(session, reply(session, "stale-partial", ref=old_ref))
    assert send(session, reply(session, "second-reply")).status == "committed"
    assert sync(session).committed_tick == 18
    result = session[0].submit_command(original, session[2], context=session[3])
    assert result.status == "committed"
    assert sync(session).committed_tick == sync(session).estimated_target_tick == 60
    assert domain.row(session[1], "completion")["status"] == "completed"
    assert session[0].submit_command(original, session[2], context=session[3]) == result


def test_unrelated_legal_write_is_not_absorbed_into_origin_version(session):
    _, original = setup_wait(session, realtime=True)
    unrelated = replace(reply(session, "gm-write"), time_wait=None)
    assert send(session, unrelated).status == "committed"
    assert send(session, reply(session)).status == "committed"
    result = session[0].submit_command(original, session[2], context=session[3])
    assert result.status == "conflict" and sync(session).committed_tick == 18
    with pytest.raises(StateMachineError):
        resume(session, source="realtime")  # realtime still needs driver identity
    driver = session[0]._session_runtime.time.drivers[session[1]]
    op = AdvanceSessionTime(60, "realtime", sync(session).generation, driver.token)
    assert submit(session, op, "fresh-time-continuation").status == "committed"
    assert (
        session[0]
        .submit_command(player_command(session, "fresh-business"), session[2], context=session[3])
        .status
        == "committed"
    )


def test_next_same_tick_wait_cannot_be_cleared_by_old_reply(session):
    domain, _ = setup_wait(session)
    domain.put(session[1], "second", 18)
    old = reply(session)
    assert send(session, old).status == "committed"
    assert resume(session).status == "committed"
    new_ref = sync(session).time_wait
    assert new_ref is not None and new_ref.wait_id != old.time_wait.wait_id
    assert sync(session).committed_tick == 18
    with pytest.raises(StateMachineError):
        send(
            session,
            replace(old, command_id="late-old", expected_world_version=sync(session).world_version),
        )
    assert sync(session).time_wait == new_ref
    assert send(session, reply(session, "new-reply")).status == "committed"


@pytest.mark.parametrize("realtime", [False, True])
def test_wait_reply_and_continuation_survive_restart(session, realtime):
    _, original = setup_wait(session, realtime=realtime)
    ref, cmd = sync(session).time_wait, reply(session)
    sm, sid, p, ctx, *_ = session
    path = sm._conn.execute("PRAGMA database_list").fetchone()[2]
    sm.close()
    with StateMachine(path) as restored:
        clock = Clock()
        clock.ns = 9_000_000_000_000_000
        scheduler = Scheduler(clock)
        restored.resume_session(sid, p, context=ctx)
        restored.attach_time_driver(sid, p, context=ctx, clock_ns=clock, scheduler=scheduler)
        s = (restored, sid, p, ctx, clock, scheduler)
        assert sync(s).time_wait == ref and scheduler.active() == []
        with pytest.raises(StateMachineError) as error:
            send(s, cmd)
        assert error.value.code == "unsupported_time_wait"
        domain = ChoiceDomain(restored)
        result = send(s, cmd)
        assert result.status == "committed" and sync(s).committed_tick == 18
        if realtime:
            assert restored.submit_command(original, p, context=ctx).status == "committed"
        else:
            assert resume(s).status == "committed"
        assert domain.row(sid, "completion")["status"] == "completed"
        assert send(s, cmd) == result


def test_wait_reference_in_codec_is_not_an_authorization_switch(session):
    setup_wait(session)
    cmd = replace(player_command(session), time_wait=sync(session).time_wait)
    wire = asdict(cmd)
    wire.pop("check_world_effects")
    assert world_command_from_data(wire) == cmd
    with pytest.raises(StateMachineError) as error:
        send(session, world_command_from_data(wire))
    assert error.value.code == "unauthorized_time_wait_command"
    for bad in (True, {"wait_id": cmd.time_wait.wait_id, "version": 1, "bypass": True}):
        with pytest.raises(StateMachineError):
            world_command_from_data({**wire, "time_wait": bad})
    with pytest.raises(StateMachineError):
        world_command_from_data({**wire, "skip_time_sync": True})


def test_claimed_release_without_domain_transition_rolls_back(session, monkeypatch):
    domain, _ = setup_wait(session)
    path = session[0]._conn.execute("PRAGMA database_list").fetchone()[2]
    before = stored_rows(path)
    monkeypatch.setattr(
        domain, "resolve_time_wait", lambda *a, **kw: TimeWaitResolution("continue")
    )
    with pytest.raises(StateMachineError) as error:
        send(session, reply(session))
    assert error.value.code == "invalid_time_wait_result"
    assert stored_rows(path) == before


def test_cancelled_target_still_processes_another_wait_at_same_boundary(session):
    domain, _ = setup_wait(session, "cancel")
    domain.put(session[1], "second", 18)
    assert send(session, reply(session)).status == "committed"
    assert resume(session, 18).status == "committed"
    assert sync(session).committed_tick == 18 and sync(session).time_wait is not None
    assert domain.row(session[1], "second")["status"] == "waiting"


@pytest.mark.parametrize("race", [False, True])
def test_wait_check_uses_real_re_outside_write_transaction(tmp_path, package_data, context, race):
    from dnd_support import create
    from modern_support import modern_data, modern_loader
    from test_real_checks import check_command

    from trpg_state_machine.package import _validate

    assets = modern_loader()
    data = modern_data(package_data, assets)
    data["time_binding"] = asdict(TimeBinding())
    p = _validate(data)
    ctx = replace(context, can_advance_time=True, can_adjudicate_checks=True)
    with StateMachine(tmp_path / "wait-check.db", dnd_loader=assets) as sm:
        sid = create(sm, p, ctx)
        clock = Clock()
        s = (sm, sid, p, ctx, clock, Scheduler(clock))
        domain, _ = setup_wait(s)
        cmd = replace(check_command(sm, sid, p, ctx, dc=1), time_wait=sync(s).time_wait)
        evaluate = sm._rules._evaluate_dnd
        rng = sm.read_rng_context(sid, p, context=ctx)

        async def evaluated(request):
            assert not sm._conn.in_transaction
            if race:
                assert (
                    send(s, replace(reply(s, "intervening"), time_wait=None)).status == "committed"
                )
            return await evaluate(request)

        sm._rules._evaluate_dnd = evaluated
        result = sm.submit_command(cmd, p, context=ctx)
        assert result.status == ("conflict" if race else "committed")
        assert sync(s).committed_tick == 18
        assert domain.row(sid)["status"] == ("waiting" if race else "resolved")
        assert (sm.read_rng_context(sid, p, context=ctx) == rng) == race
