"""Real fixed-RE checks and authoritative object consequences, no mock successes."""

from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from lock_support import CRED, DOOR, A, lock_loader
from spatial_support import create
from test_lockable_passages import acquire
from test_world_locks import multi_data
from world_support import command, submit

from trpg_state_machine import (
    ClearJam,
    ForceOpen,
    ObjectState,
    PickLock,
    SetObjectState,
    StateMachine,
    StateMachineError,
)
from trpg_state_machine.package import _validate


def check_data(*, dc=0, jammed=False, barred=False):
    d = multi_data()
    d["manifest"]["schema_version"] = "state-machine-foundation/7"
    tools = BundledAssetLoader()
    old = lock_loader()
    loader = MemoryAssetLoader(
        items=[old.get_item(s) for s in old.list_slugs("items")]
        + [tools.get_item(s) for s in ("thieves-tools", "crowbar", "smiths-tools")],
        monsters=[old.get_monster(s) for s in old.list_slugs("monsters")],
    )
    d["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    next(i for i in d["item_transfers"] if i["instance_id"] == CRED)["item_slug"] = "thieves-tools"
    c = d["world_objects"][0]["capabilities"]
    c[0].update(
        initial_jammed=jammed,
        initial_barred=barred,
        force_methods=[
            {
                "method_id": "free-jam",
                "ability": "str",
                "skill": "athletics",
                "dc": dc,
                "required_tool": None,
                "leverage_applicable": False,
                "unlock_lock_id": None,
                "clear_bar": False,
                "clear_jam": True,
                "open_after": False,
            },
            {
                "method_id": "break-outer",
                "ability": "str",
                "skill": "athletics",
                "dc": dc,
                "required_tool": None,
                "leverage_applicable": False,
                "unlock_lock_id": "lock.outer",
                "clear_bar": False,
                "clear_jam": False,
                "open_after": False,
            },
        ],
    )
    c[1]["locks"][0].update(pick_policy="scenario", pick_dc=dc)
    return d, loader


def setup_check(tmp_path, context, **kwargs):
    d, loader = check_data(**kwargs)
    p = _validate(d)
    sm = StateMachine(tmp_path / "check.db", dnd_loader=loader)
    sid = create(sm, p, context)
    acquire(sm, sid, p, context, iid=CRED, key="tools")
    return sm, sid, p, loader


@pytest.mark.parametrize("dc,success", [(0, True), (100, False)])
def test_pick_success_and_valid_failure_only_affect_selected_lock(tmp_path, context, dc, success):
    sm, sid, p, loader = setup_check(tmp_path, context, dc=dc, barred=True)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        op = PickLock(A, DOOR, "lock.outer", CRED, "0,0")
        before_rng = sm.read_session(sid, p, context=context).rng
        changes = sm._conn.total_changes
        assert sm.query_world_action(sid, actor, op, p, context=context).status == "available"
        assert sm._conn.total_changes == changes
        cmd = command(sm, sid, p, context, op, "pick")
        result = sm.submit_command(cmd, p, context=context)
        assert result.status == "committed", result
        view = sm.get_object(sid, actor, DOOR, p, context=context)
        locks = {entry["lock_id"]: entry["locked"] for entry in view["locks_for_object"]}
        assert locks == {"lock.outer": not success, "lock.inner": True}
        assert view["open_state"] == "closed" and view["openable"]["barred"]
        after = sm.read_noncombat_snapshot(sid, p, context=context)
        assert (
            sm.read_session(sid, p, context=context).rng.rng_version == before_rng.rng_version + 1
        )
        assert sm.submit_command(cmd, p, context=context) == result
        assert sm.read_noncombat_snapshot(sid, p, context=context) == after
        if success:
            noop = submit(sm, sid, p, context, op, "repeat")
            assert noop.status == "committed" and not noop.event_seqs
            assert sm.read_noncombat_snapshot(sid, p, context=context) == after
    with StateMachine(tmp_path / "check.db", dnd_loader=loader) as restored:
        assert restored.submit_command(cmd, p, context=context) == result


@pytest.mark.parametrize("invalid", ["foreign-lock", "wrong-tool", "reach"])
def test_invalid_pick_has_no_rng_or_world_side_effects(tmp_path, context, invalid):
    sm, sid, p, _ = setup_check(tmp_path, context)
    with sm:
        op = PickLock(A, DOOR, "lock.outer", CRED, "0,0")
        op = replace(
            op,
            **{
                "foreign-lock": {"lock_id": "lock.secret"},
                "wrong-tool": {"tool_instance_id": "object.amulet_b"},
                "reach": {"expected_cell": "2,2"},
            }[invalid],
        )
        before = sm.read_noncombat_snapshot(sid, p, context=context)
        result = submit(sm, sid, p, context, op, "bad")
        assert result.status == "rejected" and not result.event_seqs
        assert sm.read_noncombat_snapshot(sid, p, context=context) == before


@pytest.mark.parametrize("dc,success", [(0, True), (100, False)])
def test_force_and_clear_jam_respect_explicit_consequences(tmp_path, context, dc, success):
    sm, sid, p, _ = setup_check(tmp_path, context, dc=dc, jammed=True, barred=True)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        assert (
            submit(
                sm, sid, p, context, ForceOpen(A, DOOR, "break-outer", None, "0,0"), "force"
            ).status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, ClearJam(A, DOOR, "free-jam", None, "0,0"), "jam").status
            == "committed"
        )
        view = sm.get_object(sid, actor, DOOR, p, context=context)
        assert view["openable"]["jammed"] is not success
        assert view["openable"]["barred"] is True
        assert view["open_state"] == "closed"
        assert submit(
            sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.OPEN), "open"
        ).error_code == ("object_barred" if success else "object_jammed")


def test_sql_failure_rolls_back_check_rng_world_and_receipt(tmp_path, context):
    sm, sid, p, _ = setup_check(tmp_path, context)
    with sm:
        before = sm.read_noncombat_snapshot(sid, p, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_commit BEFORE INSERT ON command_receipts "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        cmd = command(sm, sid, p, context, PickLock(A, DOOR, "lock.outer", CRED, "0,0"), "pick")
        with pytest.raises(StateMachineError):
            sm.submit_command(cmd, p, context=context)
        assert sm.read_noncombat_snapshot(sid, p, context=context) == before
        assert bool(sm._world_facts.locks_for_object(sid, DOOR)[1][1])
        assert sm.get_command_receipt(sid, "pick", p, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_commit")
        assert sm.submit_command(cmd, p, context=context).status == "committed"


@pytest.mark.parametrize("leverage,mode", [(True, "advantage"), (False, "normal")])
def test_crowbar_only_grants_advantage_for_declared_leverage(tmp_path, context, leverage, mode):
    d, loader = check_data(jammed=True)
    next(i for i in d["item_transfers"] if i["instance_id"] == CRED)["item_slug"] = "crowbar"
    method = d["world_objects"][0]["capabilities"][0]["force_methods"][0]
    method.update(required_tool="crowbar", leverage_applicable=leverage)
    p = _validate(d)
    with StateMachine(tmp_path / "crowbar.db", dnd_loader=loader) as sm:
        sid = create(sm, p, context)
        acquire(sm, sid, p, context, iid=CRED, key="tool")
        receipt = submit(sm, sid, p, context, ClearJam(A, DOOR, "free-jam", CRED, "0,0"), "jam")
        assert receipt.status == "committed", receipt
        event = next(
            e
            for e in sm.read_events(sid, p, context=context)
            if e.event_seq in receipt.event_seqs and e.event_type == "rules.check_rolled"
        )
        assert event.payload["engine_event"]["advantage"] == mode


def test_standard_srd_lock_uses_dc15_and_wrong_owned_tool_is_unavailable(tmp_path, context):
    d, loader = check_data()
    d["world_objects"][0]["capabilities"][1]["locks"][0].update(
        pick_policy="srd_lock", pick_dc=None
    )
    p = _validate(d)
    with StateMachine(tmp_path / "ordinary.db", dnd_loader=loader) as sm:
        sid = create(sm, p, context)
        acquire(sm, sid, p, context, iid=CRED, key="tool")
        op = PickLock(A, DOOR, "lock.outer", CRED, "0,0")
        receipt = submit(sm, sid, p, context, op, "pick")
        assert receipt.status == "committed"
        event = next(
            e
            for e in sm.read_events(sid, p, context=context)
            if e.event_seq in receipt.event_seqs and e.event_type == "rules.check_rolled"
        )
        assert event.payload["engine_event"]["dc"] == 15
        from lock_support import TWIN

        acquire(sm, sid, p, context, iid=TWIN, key="wrong-tool")
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        wrong = replace(op, tool_instance_id=TWIN, lock_id="lock.inner")
        assert sm.query_world_action(sid, actor, wrong, p, context=context).status == "unavailable"


def test_version_conflict_never_evaluates_or_pays(tmp_path, context, monkeypatch):
    sm, sid, p, _ = setup_check(tmp_path, context)
    with sm:
        import trpg_state_machine.rule_execution as execution

        async def forbidden(*args, **kwargs):
            pytest.fail("stale object command must not evaluate")

        monkeypatch.setattr(execution, "evaluate_openable", forbidden)
        before = sm.read_session(sid, p, context=context)
        cmd = command(sm, sid, p, context, PickLock(A, DOOR, "lock.outer", CRED, "0,0"), "stale")
        receipt = sm.submit_command(replace(cmd, expected_world_version=0), p, context=context)
        assert receipt.status == "conflict" and receipt.error_code == "version_conflict"
        assert sm.read_session(sid, p, context=context) == before


@pytest.mark.parametrize(
    "policy,start,target,blocked",
    [
        ("prevent_opening", "closed", ObjectState.OPEN, True),
        ("prevent_opening", "open", ObjectState.CLOSED, False),
        ("prevent_closing", "closed", ObjectState.OPEN, False),
        ("prevent_closing", "open", ObjectState.CLOSED, True),
    ],
)
def test_directional_jam_policy_preserves_legacy_default(
    tmp_path, context, policy, start, target, blocked
):
    from world_support import setup, world_data

    d = world_data()
    d["manifest"]["schema_version"] = "state-machine-foundation/7"
    d["lockable_doors"] = []
    d["world_objects"][0]["capabilities"][0].update(initial_jammed=True, jam_policy=policy)
    d["scenes"][0]["objects"][0]["state"] = start
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        receipt = submit(sm, sid, p, context, SetObjectState(A, DOOR, target), "transition")
        assert receipt.error_code == ("object_jammed" if blocked else None)


def test_successful_jam_clear_allows_later_open_and_close(tmp_path, context):
    d, loader = check_data(jammed=True)
    d["world_objects"][0]["capabilities"] = d["world_objects"][0]["capabilities"][:1]
    d["world_objects"][0]["capabilities"][0]["force_methods"] = d["world_objects"][0][
        "capabilities"
    ][0]["force_methods"][:1]
    p = _validate(d)
    with StateMachine(tmp_path / "unjam.db", dnd_loader=loader) as sm:
        sid = create(sm, p, context)
        assert (
            submit(sm, sid, p, context, ClearJam(A, DOOR, "free-jam", None, "0,0"), "jam").status
            == "committed"
        )
        for i, state in enumerate((ObjectState.OPEN, ObjectState.CLOSED, ObjectState.OPEN)):
            assert (
                submit(sm, sid, p, context, SetObjectState(A, DOOR, state), f"switch.{i}").status
                == "committed"
            )


def test_force_cannot_silently_clear_other_obstacles(tmp_path, context):
    d, loader = check_data()
    d["world_objects"][0]["capabilities"][0]["force_methods"][1]["open_after"] = True
    p = _validate(d)
    with StateMachine(tmp_path / "obstacles.db", dnd_loader=loader) as sm:
        sid = create(sm, p, context)
        before = sm.read_session(sid, p, context=context)
        result = submit(
            sm, sid, p, context, ForceOpen(A, DOOR, "break-outer", None, "0,0"), "force"
        )
        assert result.error_code == "object_obstructed"
        assert sm.read_session(sid, p, context=context) == before


def test_clear_jam_has_real_authorized_observation_path(tmp_path, context):
    from lock_support import NPC

    from trpg_state_machine.perception import JudgmentResult, NPCContext, SignalJudgment

    sm, sid, p, _ = setup_check(tmp_path, context, jammed=True)
    with sm:
        sm.grant_npc_access(sid, NPC, "agent:openable", p, context=context)
        receipt = submit(sm, sid, p, context, ClearJam(A, DOOR, "free-jam", None, "0,0"), "jam")
        assert receipt.status == "committed"
        event = next(
            e
            for e in sm.read_events(sid, p, context=context)
            if e.event_seq in receipt.event_seqs and e.event_type == "object.jam_cleared"
        )
        assert len(event.signals) == 1

        def notice(request):
            return JudgmentResult(
                tuple(SignalJudgment(signal.signal_id, True) for signal in request.signals)
            )

        for committed in sm.read_events(sid, p, context=context):
            assert (
                sm.project_event(sid, committed.event_seq, p, context=context, judgment=notice)
                == "resolved"
            )
        assert sm.read_observations(sid, p, context=NPCContext("agent:openable", sid, NPC))
