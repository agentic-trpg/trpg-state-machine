"""Verify torch fuel through public World operations, time and saved-current reads."""

from dataclasses import asdict, replace

import pytest
from lock_support import CRED, A, lock_loader
from test_session_time_coordination import Clock, DeadlinePort, Scheduler
from test_world_lights import evidence, light_config, lighting_data
from world_support import setup, submit

from trpg_state_machine import (
    AdvanceGameTime,
    AdvanceSessionTime,
    SetLightEnabled,
    SetTimeRunning,
    StateMachine,
    StateMachineError,
    TimeBinding,
)

TORCH = "object.fuel_torch"


def torch_data(*, enabled=False, bind_time=True):
    data = lighting_data()
    if bind_time:
        data["time_binding"] = asdict(TimeBinding())
    for actor in data["actors"]:
        actor.pop("npc_state", None)
    data["scenes"][0]["objects"].append(
        {
            "object_id": TORCH,
            "state": None,
            "initial_cell": "0,0",
            "movable": False,
            "blocks_passage": False,
        }
    )
    data["world_objects"].append(
        {
            "object_id": TORCH,
            "definition_ref": "definition.torch",
            "display_name": "Torch",
            "description_ref": None,
            "material": "wood",
            "size_category": "tiny",
            "weight_lb": 1,
            "capabilities": [
                {
                    **light_config(),
                    "initial_enabled": enabled,
                    "fuel_policy": "world_time_metered",
                    "fuel_seconds": 3600,
                    "modes": [
                        {"mode_id": "normal", "bright_radius_ft": 20, "dim_outer_radius_ft": 40}
                    ],
                }
            ],
        }
    )
    return data


def illumination(sm, sid, actor, package, context):
    return sm.get_illumination_area(sid, actor, ("1,0",), package, context=context)[0]


def test_torch_pauses_and_resumes_remaining_fuel_across_save(tmp_path, context):
    context = replace(context, can_advance_time=True)
    sm, sid, package, database = setup(tmp_path, context, torch_data())
    on, off = SetLightEnabled(A, TORCH, "0,0", True), SetLightEnabled(A, TORCH, "0,0", False)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        assert illumination(sm, sid, actor, package, context)["illumination"] == "dark"
        assert submit(sm, sid, package, context, on, "light").status == "committed"
        assert (
            submit(sm, sid, package, context, AdvanceSessionTime(300), "burn").status == "committed"
        )
        assert submit(sm, sid, package, context, off, "pause").status == "committed"
        assert sm._world_facts.next_light_expiry(sid) is None
        assert (
            submit(sm, sid, package, context, AdvanceGameTime(300, 4000), "wait").status
            == "committed"
        )
        sm.save_session(sid, package, context=context)
    with StateMachine(database, dnd_loader=lock_loader()) as reopened:
        assert reopened.resume_session(sid, package, context=context).pc_actor_id == actor
        assert illumination(reopened, sid, actor, package, context)["illumination"] == "dark"
        assert submit(reopened, sid, package, context, on, "relight").status == "committed"
        assert reopened._world_facts.next_light_expiry(sid) == 7300
        assert (
            submit(reopened, sid, package, context, AdvanceSessionTime(7299), "before").status
            == "committed"
        )
        fixed = illumination(reopened, sid, actor, package, context)
        assert (fixed["illumination"], fixed["contributing_sources"]) == ("bright", (TORCH,))
        snapshot = reopened.read_noncombat_snapshot(sid, package, context=context)
        receipt = submit(reopened, sid, package, context, AdvanceGameTime(7299, 8000), "expire")
        assert receipt.status == "committed", receipt
        assert illumination(reopened, sid, actor, package, context)["illumination"] == "dark"
        assert fixed["illumination"] == snapshot.scene_state.grid.lighting["1,0"] == "bright"
        assert reopened._world_facts.next_light_expiry(sid) is None
        expires = [
            event
            for event in reopened.read_events(sid, package, context=context)
            if event.event_type == "object.light_expired"
        ]
        assert len(expires) == 2
        assert [e.payload["boundary_tick"] for e in expires] == [7300, 7300]
        before, after = (evidence(reopened, sid, e.event_seq) for e in expires)
        assert before["game_tick"] == after["game_tick"] == 7300
        assert before["scenes"][A]["lighting_sources"]["1,0"] == [TORCH]
        assert "1,0" not in after["scenes"][A]["lighting_sources"]
        availability = reopened.query_world_action(sid, actor, on, package, context=context)
        assert availability.reason == "light_fuel_exhausted"
        assert (
            submit(reopened, sid, package, context, on, "spent").error_code
            == "light_fuel_exhausted"
        )


def test_pre_lit_torch_recovers_and_expiry_isolated_on_failed_write(tmp_path, context, monkeypatch):
    context = replace(context, can_advance_time=True)
    sm, sid, package, database = setup(tmp_path, context, torch_data(enabled=True))
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        assert sm._world_facts.next_light_expiry(sid) == 3600
        sm.save_session(sid, package, context=context)
    with StateMachine(database, dnd_loader=lock_loader()) as reopened:
        reopened.resume_session(sid, package, context=context)
        lit = illumination(reopened, sid, actor, package, context)
        lights = reopened._objects.lights
        expire = lights.expire

        def fail_after_replacement(*args):
            expire(*args)
            assert reopened._world_facts.light_state(sid, TORCH)["enabled"] is False
            assert reopened._world_facts.next_light_expiry(sid) is None
            raise StateMachineError(
                "test_light_expiry_rollback", "Injected after light replacement"
            )

        with monkeypatch.context() as patch:
            patch.setattr(lights, "expire", fail_after_replacement)
            rejected = submit(reopened, sid, package, context, AdvanceSessionTime(3600), "fail")
            assert rejected.error_code == "test_light_expiry_rollback"
        assert reopened.read_time_sync(sid, package, context=context).committed_tick == 0
        assert reopened._world_facts.next_light_expiry(sid) == 3600
        assert illumination(reopened, sid, actor, package, context) == lit
        assert not any(
            event.event_type == "object.light_expired"
            for event in reopened.read_events(sid, package, context=context)
        )
        assert (
            submit(reopened, sid, package, context, AdvanceSessionTime(3600), "retry").status
            == "committed"
        )
        assert illumination(reopened, sid, actor, package, context)["illumination"] == "dark"
        assert lit["illumination"] == "bright"


def test_metered_profile_requires_world_time_and_unmetered_light_has_no_deadline(tmp_path, context):
    with pytest.raises(StateMachineError) as legacy:
        setup(tmp_path, context, torch_data(bind_time=False))
    assert legacy.value.code == "unsupported_time_protocol"
    context = replace(context, can_advance_time=True)
    sm, sid, package, _ = setup(tmp_path, context, torch_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        assert (
            submit(
                sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "unmetered"
            ).status
            == "committed"
        )
        assert (
            submit(sm, sid, package, context, AdvanceSessionTime(7200), "wait").status
            == "committed"
        )
        assert sm._world_facts.next_light_expiry(sid) is None
        assert illumination(sm, sid, actor, package, context)["contributing_sources"] == (CRED,)


def test_lighting_torch_arms_existing_time_driver(tmp_path, context):
    context = replace(context, can_advance_time=True)
    sm, sid, package, _ = setup(tmp_path, context, torch_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        assert (
            submit(sm, sid, package, context, SetTimeRunning("start", 1, 1), "run").status
            == "committed"
        )
        clock = Clock()
        scheduler = Scheduler(clock)
        sm.attach_time_driver(sid, package, context=context, clock_ns=clock, scheduler=scheduler)
        assert not scheduler.active()
        assert (
            submit(sm, sid, package, context, SetLightEnabled(A, TORCH, "0,0", True), "on").status
            == "committed"
        )
        assert [wake.at for wake in scheduler.active()] == [3_600_000_000_000]
        clock.advance(3600)
        scheduler.fire()
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 3600
        assert illumination(sm, sid, actor, package, context)["illumination"] == "dark"
        assert not scheduler.active()


@pytest.mark.parametrize("configured_policy", (False, True))
def test_exact_fuel_boundary_order_respects_configured_policy(tmp_path, context, configured_policy):
    context = replace(context, can_advance_time=True)
    sm, sid, package, _ = setup(tmp_path, context, torch_data(enabled=True))
    with sm:
        time = sm._session_runtime.time
        port = DeadlinePort(sm)
        time.consumers["fixture"] = port
        port.put(sid, "same-tick", 3600)
        if configured_policy:
            time.resolve_simultaneous = lambda sid, domains: "fixture"
        settle = port.settle_one

        def check_light_then_settle(row, package, command_id):
            assert sm._world_facts.light_state(sid, TORCH)["enabled"] is configured_policy
            return settle(row, package, command_id)

        port.settle_one = check_light_then_settle
        expiry = "object.light_expired"
        fixture = "test.deadline_settled"
        receipt = submit(sm, sid, package, context, AdvanceSessionTime(4000), "boundary")
        assert receipt.status == "committed", receipt
        events = [
            e
            for e in sm.read_events(sid, package, context=context)
            if e.event_type in (expiry, fixture)
        ]
        assert [e.event_type for e in events] == (
            [fixture, expiry, expiry] if configured_policy else [expiry, expiry, fixture]
        )
        assert [e.payload["boundary_tick"] for e in events] == [3600, 3600, 3600]
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 4000


def test_clock_consequence_cancels_due_lamp_without_stale_expiry(tmp_path, context, monkeypatch):
    context = replace(context, can_advance_time=True)
    sm, sid, package, _ = setup(tmp_path, context, torch_data(enabled=True))
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        effects = sm._session_runtime.time.effects
        expand = effects._expand_events
        off = SetLightEnabled(A, TORCH, "0,0", False)

        def switch_off(row, package, events, command_id):
            expanded = expand(row, package, events, command_id)
            if any(
                e.event_type == "game.time_advanced" and e.payload["game_tick"] == 3600
                for e in events
            ):
                expanded.extend(sm._objects.lights.operate(row, actor, off, package))
            return expanded

        monkeypatch.setattr(effects, "_expand_events", switch_off)
        receipt = submit(sm, sid, package, context, AdvanceSessionTime(4000), "cancel-at-boundary")
        assert receipt.status == "committed", receipt
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 4000
        assert sm._world_facts.next_light_expiry(sid) is None
        assert sm._world_facts.light_fuel(sid, TORCH).remaining_seconds == 0
        events = sm.read_events(sid, package, context=context)
        assert any(e.event_type == "object.light_changed" for e in events)
        assert not any(e.event_type == "object.light_expired" for e in events)
