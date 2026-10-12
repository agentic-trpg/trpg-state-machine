"""GC-2 public-command composition and real SQLite progress/authority tests."""

from dataclasses import asdict, replace

import pytest
from navigation_support import (
    NPC,
    A,
    B,
    advance,
    advance_command,
    create,
    navigation_data,
    navigation_package,
    pump,
    read,
    start,
    submit,
)
from test_npc_state import grant

from trpg_state_machine import (
    AdvanceNavigation,
    CancelNavigation,
    ResumeNavigation,
    StateMachine,
    StateMachineError,
    StepActor,
    world_command_from_data,
)
from trpg_state_machine.package import _validate


@pytest.mark.parametrize("target", [(B, "4,2"), (A, "0,4")])
def test_one_destination_drives_multiple_real_steps_and_optional_door(tmp_path, context, target):
    package = navigation_package()
    with StateMachine(tmp_path / "nav.db") as sm:
        sid = create(sm, package, context)
        before = sm.read_session(sid, package, context=context)
        assert start(sm, sid, package, context, target=target).status == "committed"
        activity = read(sm, sid, package, context)
        assert activity.status == "active" and activity.last_result.code == "started"
        assert activity.cursor == 0 and activity.plan_length > 1
        assert (
            sm.read_actor_position(sid, before.pc_actor_id, package, context=context).cell == "0,0"
        )
        result = pump(sm, sid, package, context)
        assert (
            result.activity.status == "completed" and result.activity.last_result.code == "arrived"
        )
        position = sm.read_actor_position(sid, before.pc_actor_id, package, context=context)
        assert (position.scene_id, position.cell) == target
        after = sm.read_session(sid, package, context=context)
        assert after.rng == before.rng and after.world.game_tick == before.world.game_tick
        events = sm.read_events(sid, package, context=context)
        assert sum(e.event_type == "object.state_changed" for e in events) == int(target[0] == B)
        assert all(not e.signals for e in events if e.event_type.startswith("activity."))
        version = after.world_version
        assert pump(sm, sid, package, context).activity.status == "completed"
        assert sm.read_session(sid, package, context=context).world_version == version


def test_zero_distance_finishes_without_claiming_a_move_at_start(tmp_path, context):
    package = navigation_package()
    with StateMachine(tmp_path / "zero.db") as sm:
        sid = create(sm, package, context)
        initial_seq = sm.read_session(sid, package, context=context).event_seq
        assert start(sm, sid, package, context, target=(A, "0,0")).status == "committed"
        assert read(sm, sid, package, context).status == "active"
        result = pump(sm, sid, package, context, budget=1)
        assert result.activity.status == "completed" and result.activity.plan_length == 0
        assert not any(
            e.signals for e in sm.read_events(sid, package, context=context, after_seq=initial_seq)
        )


def test_authorized_npc_owns_current_activity_and_only_its_steps_execute(tmp_path, context):
    package = navigation_package()
    with StateMachine(tmp_path / "npc.db") as sm:
        sid = create(sm, package, context)
        npc_context = grant(sm, sid, package, context)
        before = sm.get_npc_view(sid, NPC, package, context=npc_context)
        assert start(sm, sid, package, context, actor=NPC).status == "committed"
        busy = sm.get_npc_view(sid, NPC, package, context=npc_context)
        assert busy.runtime.current_activity == "navigation:activity.route"
        position = sm.read_actor_position(sid, NPC, package, context=context)
        manual = submit(
            sm, sid, package, context, StepActor(A, position.cell, "1,2"), "manual", NPC
        )
        assert manual.error_code == "unsupported_actor_activity"
        result = pump(sm, sid, package, context, actor=NPC)
        assert result.activity.status == "completed"
        after = sm.get_npc_view(sid, NPC, package, context=npc_context)
        assert after.runtime.current_activity is None
        assert after.npc_state_version == before.npc_state_version + 2
        assert (
            after.memories == before.memories
            and after.goals == before.goals
            and after.plans == before.plans
        )
        with pytest.raises(StateMachineError, match="control"):
            sm.read_navigation_activity(
                sid, NPC, "activity.route", package, context=replace(context, controlled_npc_ids=())
            )


def test_duplicate_keys_cursor_world_occ_and_terminal_handles(tmp_path, context):
    package = navigation_package()
    with StateMachine(tmp_path / "retries.db") as sm:
        sid = create(sm, package, context)
        from spatial_support import command

        from trpg_state_machine import StartNavigation

        cmd = command(
            sm, sid, package, context, StartNavigation("activity.route", B, "4,2"), "start"
        )
        receipt = sm.submit_command(cmd, package, context=context)
        assert sm.submit_command(cmd, package, context=context) == receipt
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(
                replace(cmd, payload=replace(cmd.payload, target_cell="3,3")),
                package,
                context=context,
            )
        assert exc.value.code == "idempotency_conflict"
        old = advance_command(sm, sid, package, context, "step")
        receipt = sm.submit_command(old, package, context=context)
        state = sm.read_session(sid, package, context=context)
        assert sm.submit_command(old, package, context=context) == receipt
        assert (
            sm.submit_command(
                replace(old, command_id="stale.world"), package, context=context
            ).status
            == "conflict"
        )
        stale = replace(old, command_id="stale.cursor", expected_world_version=state.world_version)
        assert (
            sm.submit_command(stale, package, context=context).error_code
            == "activity_version_conflict"
        )
        activity = read(sm, sid, package, context)
        cancel = command(
            sm,
            sid,
            package,
            context,
            CancelNavigation(activity.activity_id, activity.activity_version),
            "cancel",
        )
        receipt = sm.submit_command(cancel, package, context=context)
        assert sm.submit_command(cancel, package, context=context) == receipt
        assert advance(sm, sid, package, context, "after.cancel").error_code == "activity_inactive"
        assert start(sm, sid, package, context, key="old.id").error_code == "activity_exists"
        assert (
            start(sm, sid, package, context, aid="activity.new", key="new.id").status == "committed"
        )


def test_dynamic_obstacle_stops_without_false_observation_and_requires_explicit_resume(
    tmp_path, context
):
    package = navigation_package()
    with StateMachine(tmp_path / "blocked.db") as sm:
        sid = create(sm, package, context)
        start(sm, sid, package, context)
        assert advance(sm, sid, package, context, "first").status == "committed"
        activity = read(sm, sid, package, context)
        next_cell = activity.plan[activity.cursor]["to_cell"]
        # External-world fault injection after planning; core execution is never mocked.
        with sm._store.transaction(write=True):
            sm._world_facts.place(sid, "object.crate", next_cell)
        before = sm.read_session(sid, package, context=context)
        blocked = advance(sm, sid, package, context, "blocked")
        stopped = read(sm, sid, package, context)
        assert stopped.status == "blocked" and stopped.cursor == activity.cursor
        assert sm.read_session(sid, package, context=context).actors == before.actors
        events = sm.read_events(sid, package, context=context)
        assert all(not e.signals for e in events if e.event_seq in blocked.event_seqs)
        assert advance(sm, sid, package, context, "no.auto.retry").error_code == "activity_inactive"
        with sm._store.transaction(write=True):
            sm._world_facts.place(sid, "object.crate", "3,2")
        receipt = submit(
            sm,
            sid,
            package,
            context,
            ResumeNavigation(stopped.activity_id, stopped.activity_version),
            "resume",
        )
        assert receipt.status == "committed"
        assert (
            pump(sm, sid, package, context, request_id="after.resume").activity.status
            == "completed"
        )


def test_remote_dynamic_obstacle_never_changes_plan_or_earlier_steps(tmp_path, context):
    data = navigation_data()
    data["scenes"][1]["objects"][0].update(state=None, blocks_passage=True, movable=True)
    package = _validate(data)
    observed = []
    outcomes = []
    for blocked in (False, True):
        with StateMachine(tmp_path / f"remote-{blocked}.db") as sm:
            sid = create(sm, package, context)
            with sm._store.transaction(write=True):
                sm._world._initialize_scene(sid, B, package)
                if blocked:
                    sm._world_facts.place(sid, "object.workshop_door", "0,1")
            start(sm, sid, package, context)
            activity = read(sm, sid, package, context)
            for index in range(2):
                advance(sm, sid, package, context, f"early.{index}")
            observed.append((activity.plan, read(sm, sid, package, context).cursor))
            outcomes.append(pump(sm, sid, package, context).activity.status)
    assert observed[0] == observed[1]
    assert outcomes == ["completed", "blocked"]


@pytest.mark.parametrize("missing", ["knowledge", "actor", "route"])
def test_ungranted_knowledge_and_unlisted_connections_are_not_used(tmp_path, context, missing):
    data = navigation_data()
    if missing == "knowledge":
        data["manifest"]["schema_version"] = "state-machine-foundation/2"
        del data["navigation"]
    elif missing == "actor":
        data["navigation"]["pc_template_grants"] = []
    else:
        data["navigation"]["regions"][0]["passage_ids"] = []
    package = _validate(data)
    with StateMachine(tmp_path / "knowledge.db") as sm:
        sid = create(sm, package, context)
        receipt = start(sm, sid, package, context)
        assert receipt.status == "rejected"
        assert receipt.error_code in {
            "navigation_knowledge_unavailable",
            "navigation_route_unavailable",
        }
        assert sm._conn.execute("SELECT count(*) FROM navigation_activities").fetchone()[0] == 0


def test_closed_data_adapter_rejects_trust_scripts_and_untyped_versions(tmp_path, context):
    package = navigation_package()
    with StateMachine(tmp_path / "data.db") as sm:
        sid = create(sm, package, context)
        from spatial_support import command

        from trpg_state_machine import StartNavigation

        cmd = command(
            sm, sid, package, context, StartNavigation("activity.route", B, "4,2"), "start"
        )
        data = asdict(cmd)
        del data["check_world_effects"]
        assert world_command_from_data(data) == cmd
        for extra in ({"trusted": True}, {"source": "package"}, {"script": "move()"}):
            with pytest.raises(StateMachineError):
                world_command_from_data({**data, "payload": {**data["payload"], **extra}})
        with pytest.raises(StateMachineError):
            sm.submit_command(
                replace(
                    cmd,
                    command_type="activity.advance_navigation",
                    payload=AdvanceNavigation("activity.route", True),
                ),
                package,
                context=context,
            )
