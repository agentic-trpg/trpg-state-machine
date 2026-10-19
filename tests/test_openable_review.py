"""Regression coverage for the Openable review through public APIs and real rules."""

from dataclasses import asdict, replace

import pytest
import yaml
from lock_support import CRED, DOOR, TWIN, A
from spatial_support import create
from test_dnd_ledger_scope import stored_state
from test_lockable_passages import acquire
from test_openable_checks import check_data, setup_check
from test_world_locks import multi_data
from world_support import command, submit

from trpg_state_machine import (
    ClearJam,
    ForceOpen,
    PickLock,
    StateMachine,
    StateMachineError,
    load_adventure_package,
)
from trpg_state_machine.package import _validate


@pytest.mark.parametrize("entry", ["", " null", " text", " 42", " []", " [one, two]"])
def test_yaml_lock_nonmapping_returns_structured_error(tmp_path, entry):
    data = multi_data()
    data["world_objects"][0]["capabilities"][1]["locks"] = ["LOCK_ENTRY"]
    source = yaml.safe_dump(data, sort_keys=False).replace("- LOCK_ENTRY", "-" + entry)
    path = tmp_path / "invalid.yaml"
    path.write_text(source, encoding="utf-8")
    result = load_adventure_package(path)
    assert result.status == "invalid" and result.package is None
    assert len(result.errors) == 1
    issue = result.errors[0]
    assert issue.code == "schema_error"
    assert issue.path.endswith(".locks[0]")
    assert issue.message == "Lock must be a mapping"


@pytest.mark.parametrize("count", [1, 2])
def test_yaml_legacy_valid_locks_preserve_digest_and_defaults(tmp_path, count):
    data = multi_data()
    # This exact /36 source is the historical digest fixture, never a new
    # Session under /37. Preserve its input identity and both original hashes.
    data["manifest"]["ruleset_binding"]["evaluator_version"] = "dnd5e-evaluation/36"
    data["world_objects"][0]["capabilities"][1]["locks"] = data["world_objects"][0]["capabilities"][
        1
    ]["locks"][:count]
    expected = _validate(data)
    path = tmp_path / "valid.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = load_adventure_package(path)
    assert result.status == "valid"
    assert result.package == expected
    # Digests calculated with validate_world_objects from review baseline 018ea8cc.
    assert (
        result.package.package_digest
        == {
            1: "sha256:e976800fae1ed21fb80f762c07ec62f066b012d88a81896a51f6d946978764bc",
            2: "sha256:4299648ea73c666b2b5878e9b968b2454a5b60cdcdf9b242c6acec0874dacd2f",
        }[count]
    )
    locks = result.package.world_objects[0].capability("locks").locks
    assert len(locks) == count
    assert all(lock.pick_policy == "unconfigured" and lock.pick_dc is None for lock in locks)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"unknown": True}, "unsupported_field"),
        ({"credential_instance_ids": ["object.missing"]}, "invalid_reference"),
        ({"policy": "arbitrary"}, "schema_error"),
    ],
)
def test_yaml_lock_constraints_still_apply(tmp_path, change, code):
    data = multi_data()
    data["world_objects"][0]["capabilities"][1]["locks"][0].update(change)
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = load_adventure_package(path)
    assert result.status == "invalid" and result.errors[0].code == code


def without_receipts(state):
    return {key: value for key, value in state.items() if key != "command_receipts"}


@pytest.mark.parametrize("tool", [CRED, TWIN, "object.missing"])
def test_unlocked_query_and_execution_ignore_tools_and_replay(tmp_path, context, monkeypatch, tool):
    import trpg_state_machine.rule_execution as execution

    data, loader = check_data()
    # Even an unconfigured picking policy cannot make an unlocked lock require a check.
    data["world_objects"][0]["capabilities"][1]["locks"][0].update(
        initial_locked=False, pick_policy="unconfigured", pick_dc=None
    )
    package = _validate(data)
    database = tmp_path / "unlocked.db"
    with StateMachine(database, dnd_loader=loader) as sm:
        sid = create(sm, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        acquire(sm, sid, package, context, iid=CRED, key="correct-tool")
        acquire(sm, sid, package, context, iid=TWIN, key="wrong-tool")
        operation = PickLock(A, DOOR, "lock.outer", tool, "0,0")

        async def forbidden(*args, **kwargs):
            pytest.fail("unlocked target must not evaluate")

        monkeypatch.setattr(execution, "evaluate_openable", forbidden)

        def forbidden_request(*args, **kwargs):
            pytest.fail("unlocked target must not request rules")

        monkeypatch.setattr(sm._rules, "request", forbidden_request)
        before = stored_state(database)
        query = sm.query_world_action(sid, actor, operation, package, context=context)
        assert (query.status, query.reason) == ("available", "already_unlocked")
        assert stored_state(database) == before
        cmd = command(sm, sid, package, context, operation, "noop")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed" and receipt.event_seqs == ()
        after = stored_state(database)
        assert without_receipts(after) == without_receipts(before)
        assert sm.submit_command(cmd, package, context=context) == receipt
        assert stored_state(database) == after
        # A stale new command still conflicts, including for an unlocked target.
        stale = replace(cmd, command_id="stale", expected_world_version=0)
        assert sm.submit_command(stale, package, context=context).error_code == "version_conflict"
        assert without_receipts(stored_state(database)) == without_receipts(before)
    with StateMachine(database, dnd_loader=loader) as restored:
        before_replay = stored_state(database)
        assert restored.submit_command(cmd, package, context=context) == receipt
        assert stored_state(database) == before_replay
        assert (
            restored.get_object(sid, actor, DOOR, package, context=context)["open_state"]
            == "closed"
        )
        assert dict(
            (lock[0], bool(lock[1])) for lock in restored._world_facts.locks_for_object(sid, DOOR)
        ) == {"lock.outer": False, "lock.inner": True}


@pytest.mark.parametrize("tool,allowed", [(CRED, True), (TWIN, False)])
def test_two_locks_query_and_execution_select_exact_target(tmp_path, context, tool, allowed):
    data, loader = check_data()
    locks = data["world_objects"][0]["capabilities"][1]["locks"]
    locks[0]["initial_locked"] = False
    locks[1].update(pick_policy="scenario", pick_dc=0)
    package = _validate(data)
    database = tmp_path / "two.db"
    with StateMachine(database, dnd_loader=loader) as sm:
        sid = create(sm, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        acquire(sm, sid, package, context, iid=tool, key="tool")
        for lid in ("lock.outer", "lock.inner"):
            op = PickLock(A, DOOR, lid, tool, "0,0")
            before = stored_state(database)
            query = sm.query_world_action(sid, actor, op, package, context=context)
            assert stored_state(database) == before
            expected = (
                "already_unlocked"
                if lid == "lock.outer"
                else ("available" if allowed else "tool_unavailable")
            )
            assert query.reason == expected
            result = submit(sm, sid, package, context, op, lid)
            assert result.status == ("committed" if lid == "lock.outer" or allowed else "rejected")
            if lid == "lock.outer" or not allowed:
                assert without_receipts(stored_state(database)) == without_receipts(before)
        actual = dict(
            (lock[0], bool(lock[1])) for lock in sm._world_facts.locks_for_object(sid, DOOR)
        )
        assert actual == {"lock.outer": False, "lock.inner": not allowed}
        assert sm.get_object(sid, actor, DOOR, package, context=context)["open_state"] == "closed"


@pytest.mark.parametrize("boundary", ["lock", "scene", "reach", "side", "permission"])
def test_unlocked_state_never_bypasses_identity_or_access(tmp_path, context, boundary):
    data, loader = check_data()
    locks = data["world_objects"][0]["capabilities"][1]["locks"]
    locks[0]["initial_locked"] = False
    if boundary == "side":
        locks[0]["operable_sides"] = ["scene.workshop"]
    if boundary == "reach":
        for pc in data["pc_templates"]:
            pc["initial_cell"] = "2,0"
    package = _validate(data)
    database = tmp_path / "access.db"
    with StateMachine(database, dnd_loader=loader) as sm:
        sid = create(sm, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        op = PickLock(
            A, DOOR, "lock.outer", "object.missing", "2,0" if boundary == "reach" else "0,0"
        )
        if boundary == "lock":
            op = replace(op, lock_id="lock.unknown")
        if boundary == "scene":
            op = replace(op, scene_id="scene.workshop")
        caller = (
            replace(context, principal_id="stranger", player_id="player.stranger")
            if boundary == "permission"
            else context
        )
        before = stored_state(database)
        cmd = command(sm, sid, package, context, op, "denied")
        if boundary == "permission":
            for invoke in (
                lambda: sm.query_world_action(sid, actor, op, package, context=caller),
                lambda: sm.submit_command(cmd, package, context=caller),
            ):
                with pytest.raises(StateMachineError) as failure:
                    invoke()
                assert failure.value.code == "unauthorized"
            assert stored_state(database) == before
        else:
            query = sm.query_world_action(sid, actor, op, package, context=caller)
            assert query.status == "unavailable" and query.reason != "tool_unavailable"
            result = sm.submit_command(cmd, package, context=caller)
            assert result.status == "rejected" and result.error_code == query.reason
            assert without_receipts(stored_state(database)) == without_receipts(before)


@pytest.mark.parametrize("operation", ["pick", "force", "jam"])
@pytest.mark.parametrize("dc,success", [(0, True), (100, False)])
def test_real_openable_audit_survives_failure_replay_and_restart(
    tmp_path, context, operation, dc, success
):
    sm, sid, package, loader = setup_check(tmp_path, context, dc=dc, jammed=True)
    payload = {
        "pick": PickLock(A, DOOR, "lock.outer", CRED, "0,0"),
        "force": ForceOpen(A, DOOR, "break-outer", None, "0,0"),
        "jam": ClearJam(A, DOOR, "free-jam", None, "0,0"),
    }[operation]

    def assert_audit(machine):
        events = [
            e
            for e in machine.read_events(sid, package, context=context)
            if e.event_seq in receipt.event_seqs
        ]
        checks = [e for e in events if e.event_type == "rules.check_rolled"]
        assert len(checks) == 1
        event = checks[0]
        assert event.source_command_id == cmd.command_id
        audit = event.payload["adjudication"]
        assert audit["openable_operation"] == {
            "command_type": cmd.command_type,
            "payload": asdict(payload),
        }
        assert audit["payload"]["ability"] == ("dex" if operation == "pick" else "str")
        assert event.payload["engine_event"]["succeeded"] is success
        assert event.signals == ()
        assert all("openable_operation" not in repr(e.signals) for e in events)
        return events

    with sm:
        cmd = command(sm, sid, package, context, payload, "audited")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed"
        events = assert_audit(sm)
        before = stored_state(tmp_path / "check.db")
        assert sm.submit_command(cmd, package, context=context) == receipt
        assert stored_state(tmp_path / "check.db") == before
    with StateMachine(tmp_path / "check.db", dnd_loader=loader) as restored:
        assert assert_audit(restored) == events
        assert restored.submit_command(cmd, package, context=context) == receipt
        assert stored_state(tmp_path / "check.db") == before


@pytest.mark.parametrize("operation", ["pick", "force", "jam"])
@pytest.mark.parametrize("dc", [0, 100])
def test_audit_rolls_back_with_failed_transaction(tmp_path, context, operation, dc):
    sm, sid, package, _ = setup_check(tmp_path, context, jammed=True, dc=dc)
    with sm:
        payload = {
            "pick": PickLock(A, DOOR, "lock.outer", CRED, "0,0"),
            "force": ForceOpen(A, DOOR, "break-outer", None, "0,0"),
            "jam": ClearJam(A, DOOR, "free-jam", None, "0,0"),
        }[operation]
        before = stored_state(tmp_path / "check.db")
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_audit BEFORE INSERT ON command_receipts "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        cmd = command(sm, sid, package, context, payload, "rollback")
        with pytest.raises(StateMachineError):
            sm.submit_command(cmd, package, context=context)
        assert stored_state(tmp_path / "check.db") == before
        assert sm.get_command_receipt(sid, "rollback", package, context=context) is None
