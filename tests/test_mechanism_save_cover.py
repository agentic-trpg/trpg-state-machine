"""Actual mechanism placement, cover and applicability through a public action."""

from dataclasses import replace

import pytest
from lock_support import CRED, DOOR, A, lock_loader
from test_faerie_fire import _grow_remote
from test_mechanism_effects import state_rows
from test_mechanism_save_gate import gated_data
from test_mechanism_service import trigger
from test_object_spatial_service import changed_grid, oriented_data
from test_real_saves import save_command
from test_scoped_runtime_validation import measure
from test_world_mechanisms import LEVER
from world_support import command, setup

from trpg_state_machine import (
    ActivateMechanism,
    ObjectState,
    SetLightEnabled,
    SetObjectState,
    StateMachine,
    StateMachineError,
    StepActor,
)
from trpg_state_machine.package import _validate


def cover_data(source_cell, degree, reach):
    """Use the actual mechanism as source and an independent object as cover."""
    data = gated_data(dc=100)
    source = next(obj for obj in data["scenes"][0]["objects"] if obj["object_id"] == LEVER)
    source["initial_cell"] = source_cell
    switch = next(obj for obj in data["scenes"][0]["objects"] if obj["object_id"] == CRED)
    switch["initial_cell"] = "0,0"
    gate = data["world_objects"][-1]["capabilities"][0]["operations"][0]["save_gate"]
    gate["effect_reach"] = reach
    cover = oriented_data(anchor="1,0", footprint=[[0, 0]], blocking=False)
    cover["world_objects"][-1]["capabilities"][0]["closed_properties"] = {"cover": degree}
    data["scenes"][0]["objects"].append(cover["scenes"][0]["objects"][-1])
    data["world_objects"].append(cover["world_objects"][-1])
    return data


@pytest.mark.parametrize(
    ("source_cell", "cover", "reach", "modifier", "miss"),
    [
        ("0,1", "half", {"kind": "direct"}, 0, None),
        ("2,0", "half", {"kind": "direct"}, 2, None),
        ("2,0", "three_quarters", {"kind": "direct"}, 5, None),
        ("2,0", "total", {"kind": "direct"}, None, "total_cover"),
        ("2,0", "total", {"kind": "radial_pulse", "radius_ft": 10}, 0, None),
        ("2,0", "total", {"kind": "radial_pulse", "radius_ft": 5}, None, "out_of_range"),
    ],
)
def test_bound_mechanism_cover_and_pulse(
    tmp_path, context, source_cell, cover, reach, modifier, miss
):
    sm, sid, package, db = setup(tmp_path, context, cover_data(source_cell, cover, reach))
    with sm:
        cmd = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "switch")
        before = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1
        assert sm._world_facts.light_state(sid, CRED)["enabled"]
        rolls = sm.get_command_rolls(sid, "switch", package, context=context)["rolls"]
        rng = sm.read_rng_context(sid, package, context=context)
        if miss:
            assert rolls == []
            assert rng == before
            assert sm._world_facts.object(sid, DOOR)["state"] == "closed"
            events = [sm._read_event(sid, seq) for seq in receipt.event_seqs]
            (event,) = [e for e in events if e.event_type == "mechanism.effect_not_applicable"]
            assert event.payload["engine_event"]["reason"] == miss
            assert event.payload["mechanism_object_id"] == LEVER
        else:
            (roll,) = rolls
            assert roll["mechanism_object_id"] == LEVER
            assert roll["source_kind"] == "world_object"
            assert roll["source_ref"] == "mechanism.fixture-ward"
            assert [
                m["value"] for m in roll["roll_detail"]["modifiers"] if m["source"] == "cover"
            ] == ([modifier] if modifier else [])
            assert not roll["succeeded"]
            assert rng.version == before.version + 1
            assert sm._world_facts.object(sid, DOOR)["state"] == "open"
        assert sm.submit_command(cmd, package, context=context) == receipt
    with StateMachine(db, dnd_loader=lock_loader()) as reopened:
        assert reopened.submit_command(cmd, package, context=context) == receipt
        assert reopened.get_command_rolls(sid, "switch", package, context=context)["rolls"] == rolls
        assert reopened.read_rng_context(sid, package, context=context) == rng


@pytest.mark.parametrize(("degree", "expected_bonus"), [("half", 2), ("total", None)])
def test_step_trigger_uses_object_geometry_at_actual_entry(
    tmp_path, context, degree, expected_bonus
):
    """Join region admission with covered and fully blocked effects from an actual object."""
    data = cover_data("3,0", degree, {"kind": "direct"})
    changed_grid(data, width=4)
    mechanism = next(item for item in data["world_objects"] if item["object_id"] == LEVER)
    mechanism["capabilities"][0]["triggers"] = [trigger("enter_region", cells=["1,0"])]
    data["scenes"][0]["objects"][-1]["initial_cell"] = "2,0"
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        cmd = command(sm, sid, package, context, StepActor(A, "0,0", "1,0"), "enter.covered")
        before = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.read_actor_position(sid, cmd.actor_id, package, context=context).cell == "1,0"
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1
        rolls = sm.get_command_rolls(sid, cmd.command_id, package, context=context)["rolls"]
        after = sm.read_rng_context(sid, package, context=context)
        if expected_bonus is None:
            assert rolls == []
            assert after == before
            assert sm._world_facts.object(sid, DOOR)["state"] == "closed"
            events = [sm._read_event(sid, seq) for seq in receipt.event_seqs]
            (miss,) = [
                event for event in events if event.event_type == "mechanism.effect_not_applicable"
            ]
            assert miss.payload["engine_event"]["reason"] == "total_cover"
        else:
            (roll,) = rolls
            assert roll["mechanism_object_id"] == LEVER
            assert [
                item["value"]
                for item in roll["roll_detail"]["modifiers"]
                if item["source"] == "cover"
            ] == [expected_bonus]
            assert after.version == before.version + 1
            assert sm._world_facts.object(sid, DOOR)["state"] == "open"
        assert sm.submit_command(cmd, package, context=context) == receipt


def test_source_requires_the_current_authorized_trigger(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, cover_data("0,0", "half", {"kind": "direct"}))
    with sm:
        _grow_remote(sm, sid, 1)
        before = sm.read_rng_context(sid, package, context=context)
        manual = command(
            sm, sid, package, context, ActivateMechanism(A, LEVER, "0,0", "up"), "manual"
        )
        receipt = sm.submit_command(manual, package, context=context)
        assert receipt.status == "rejected", receipt
        assert receipt.error_code == "unsupported_mechanism_operation"
        gm = replace(context, can_adjudicate_checks=True)
        for name, source, code in [
            ("object", LEVER, "invalid_actor"),
            ("missing", "object.missing", "invalid_actor"),
            ("remote", "npc.remote_0", "invalid_spatial_state"),
        ]:
            forged = save_command(
                sm, sid, package, gm, command_id=name, source_id=source, requires_cover=True
            )
            with pytest.raises(StateMachineError) as exc:
                sm.submit_command(forged, package, context=gm)
            assert exc.value.code == code
        assert sm.read_rng_context(sid, package, context=context) == before
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 0


def test_object_source_location_is_rechecked_at_adoption(tmp_path, context, monkeypatch):
    sm, sid, package, _ = setup(tmp_path, context, cover_data("2,0", "half", {"kind": "direct"}))
    with sm:
        service = sm._world.objects.mechanisms
        evaluate = service.evaluate_direct_saves

        def shift_source(*args, **kwargs):
            result = evaluate(*args, **kwargs)
            assert not sm._conn.in_transaction
            with sm._transaction(write=True):
                sm._world_facts.place(sid, LEVER, "0,1")
            return result

        monkeypatch.setattr(service, "evaluate_direct_saves", shift_source)
        cmd = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "shift")
        before = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "conflict" and receipt.error_code == "version_conflict"
        assert sm.read_rng_context(sid, package, context=context) == before
        assert not sm._world_facts.light_state(sid, CRED)["enabled"]
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 0


def test_open_trigger_uses_post_open_cover(tmp_path, context):
    """The opening fixture itself provides different cover in its admitted stage."""
    data = cover_data("2,0", "total", {"kind": "direct"})
    profile = data["world_objects"][-1]
    cover_id = profile["object_id"]
    data["scenes"][0]["objects"][-1]["state"] = "closed"
    profile["capabilities"][0]["open_properties"] = {"cover": "half"}
    profile["capabilities"].append({"kind": "openable"})
    data["world_objects"][-2]["capabilities"][0]["triggers"] = [
        trigger(
            "object_operation",
            target_object_id=cover_id,
            event_type="object.state_changed",
            value="open",
        )
    ]
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        cmd = command(
            sm, sid, package, context, SetObjectState(A, cover_id, ObjectState.OPEN), "open"
        )
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed", receipt
        (roll,) = sm.get_command_rolls(sid, "open", package, context=context)["rolls"]
        assert [m["value"] for m in roll["roll_detail"]["modifiers"] if m["source"] == "cover"] == [
            2
        ]
        assert sm._world_facts.object(sid, cover_id)["state"] == "open"


def test_mechanism_cover_locality(tmp_path, context, record_property):
    counts = []
    for size in (0, 25):
        case = tmp_path / f"cover-{size}"
        case.mkdir()
        sm, sid, package, _ = setup(case, context, cover_data("2,0", "half", {"kind": "direct"}))
        with sm:
            _grow_remote(sm, sid, size)
            cmd = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "cover")
            with measure(sm) as (work, _):
                assert sm.submit_command(cmd, package, context=context).status == "committed"
            counts.append((work["sql_selects"], work["sql_rows"]))
    record_property("sql_selects_and_rows_for_0_and_25_remote_pairs", str(counts))
    assert counts[0] == counts[1], counts


def test_mechanism_cover_late_receipt_rollback(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, cover_data("2,0", "half", {"kind": "direct"}))
    with sm:
        cmd = command(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "cover")
        before = state_rows(sm)
        version = sm.read_session(sid, package, context=context).world_version
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_cover_receipt BEFORE INSERT ON command_receipts "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(StateMachineError) as fault:
            sm.submit_command(cmd, package, context=context)
        assert fault.value.code == "storage_error"
        assert state_rows(sm) == before
        assert sm.read_session(sid, package, context=context).world_version == version
        assert sm.get_command_receipt(sid, "cover", package, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_cover_receipt")
        assert sm.submit_command(cmd, package, context=context).status == "committed"


@pytest.mark.parametrize(
    "reach", [{"kind": "unknown"}, {"kind": "radial_pulse", "radius_ft": True}]
)
def test_unreviewed_effect_reach_is_rejected_at_package_admission(reach):
    with pytest.raises(StateMachineError) as rejection:
        _validate(cover_data("2,0", "total", reach))
    assert rejection.value.code == "unsupported_mechanism"
