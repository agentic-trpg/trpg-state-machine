"""Public same-tick Effects and World mechanism advancement."""

from dataclasses import replace

import pytest
from lock_support import CRED, A
from spatial_support import command, submit
from test_exploration_charged_item import FIRST, charged_world
from test_exploration_charged_item import cast_command as charged_cast_command
from test_mechanism_service import extended_data
from test_timed_spell_cast import cast_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world
from test_world_mechanisms import LEVER

from trpg_state_machine import (
    ActivateMechanism,
    AdvanceSessionTime,
    SetSceneFlag,
    StateMachineError,
)
from trpg_state_machine.package import EventDefinition


def _same_tick_world(timed_spell_world, *, package_callbacks=False):
    """Author a real 3600-second lever reset alongside the published spell fixture."""

    def configure(data):
        lever = next(item for item in data["world_objects"] if item["object_id"] == LEVER)
        mechanism = lever["capabilities"][0]
        mechanism.update(reset_policy="after_seconds", reset_delay_seconds=3600, triggers=[])
        placement = next(
            item for item in data["scenes"][0]["objects"] if item["object_id"] == LEVER
        )
        x, y = data["pc_templates"][0]["initial_cell"].split(",")
        placement["initial_cell"] = f"{x},{int(y) + 1}"
        if package_callbacks:
            data["scenes"][0]["initial_state"]["local_flags"].update(
                {"flag.reset_taken": False, "flag.expired_before_reset": False}
            )

    sm, sid, package, context = timed_spell_world(region_mechanism=True, configure=configure)
    if package_callbacks:
        # In-memory typed Packages reach the dispatcher without YAML trigger admission.
        package = replace(
            package,
            events=(
                EventDefinition(
                    "event.reset_trigger",
                    "mechanism.reset",
                    A,
                    "flag.reset_taken",
                    False,
                    SetSceneFlag(A, "flag.reset_taken", True),
                    (),
                ),
                EventDefinition(
                    "event.expiry_trigger",
                    "effect.expired",
                    A,
                    "flag.reset_taken",
                    False,
                    SetSceneFlag(A, "flag.expired_before_reset", True),
                    (),
                ),
            ),
        )
    return sm, sid, package, context


def _arm_both(session):
    """Cast and activate through their normal authorized commands at tick zero."""
    sm, sid, package, context = session
    cast = cast_command(session)
    applied = sm.submit_command(cast, package, context=context)
    assert applied.status == "committed", applied
    position = sm.read_actor_position(sid, cast.actor_id, package, context=context)
    activated = submit(
        sm, sid, package, context, ActivateMechanism(A, LEVER, position.cell, "up"), "arm-reset"
    )
    assert activated.status == "committed", activated
    assert sm.read_time_sync(sid, package, context=context).committed_tick == 0


@pytest.mark.parametrize("fault", [False, True], ids=["normal", "receipt-fault"])
def test_independent_longstrider_expiry_and_mechanism_reset_settle_once(timed_spell_world, fault):
    """Settle both real production due items and continue through the public clock."""
    session = _same_tick_world(timed_spell_world)
    sm, sid, package, context = session
    with sm:
        _arm_both(session)

        advance = command(sm, sid, package, context, AdvanceSessionTime(3601), "cross-both")
        if fault:
            before = sm.read_noncombat_snapshot(sid, package, context=context)
            old_events = sm.read_events(sid, package, context=context)
            sm._conn.execute(
                "CREATE TEMP TRIGGER fail_same_tick BEFORE INSERT ON command_receipts "
                "WHEN NEW.command_id='cross-both' AND EXISTS ("
                "SELECT 1 FROM object_mechanisms WHERE session_id=NEW.session_id "
                "AND object_id='object.lever' AND reset_at_tick IS NULL"
                ") AND NOT EXISTS (SELECT 1 FROM ongoing_effects WHERE session_id=NEW.session_id) "
                "BEGIN SELECT RAISE(ABORT,'injected failure after both expiries'); END"
            )
            with pytest.raises(StateMachineError):
                sm.submit_command(advance, package, context=context)
            assert sm.read_time_sync(sid, package, context=context).committed_tick == 0
            assert sm.read_noncombat_snapshot(sid, package, context=context) == before
            assert sm.read_events(sid, package, context=context) == old_events
            assert sm.get_command_receipt(sid, "cross-both", package, context=context) is None
            assert sm._world_facts.mechanism_state(sid, LEVER).reset_at_tick == 3600
            sm._conn.execute("DROP TRIGGER fail_same_tick")

        receipt = sm.submit_command(advance, package, context=context)
        assert receipt.status == "committed", receipt
        time = sm.read_time_sync(sid, package, context=context)
        assert (time.committed_tick, time.progress_status, time.progress_reason) == (
            3601,
            "completed",
            None,
        )
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == ()
        assert sm.submit_command(advance, package, context=context) == receipt
        events = sm.read_events(sid, package, context=context)
        due = [
            event for event in events if event.event_type in {"mechanism.reset", "effect.expired"}
        ]
        assert [event.event_type for event in due] == ["mechanism.reset", "effect.expired"]
        for event in due:
            assert event.event_seq in receipt.event_seqs
            assert event.payload["boundary_tick"] == 3600


def test_host_typed_package_callback_keeps_same_tick_choice(timed_spell_world):
    """Keep the decision when either boundary can affect the other's Package consequence."""
    session = _same_tick_world(timed_spell_world, package_callbacks=True)
    sm, sid, package, context = session
    with sm:
        _arm_both(session)
        assert submit(sm, sid, package, context, AdvanceSessionTime(3601), "pause").status == (
            "committed"
        )
        time = sm.read_time_sync(sid, package, context=context)
        assert (time.committed_tick, time.progress_status, time.progress_reason) == (
            3600,
            "waiting",
            "simultaneous_time_choice",
        )
        assert len(sm.read_noncombat_snapshot(sid, package, context=context).effect_states) == 1
        sm._session_runtime.time.resolve_simultaneous = lambda _sid, _due: "effects"
        receipt = submit(sm, sid, package, context, AdvanceSessionTime(3601), "choose-expiry")
        assert receipt.status == "committed", receipt
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 3601
        adopted = [
            event.payload["event_definition_id"]
            for event in sm.read_events(sid, package, context=context)
            if event.event_type == "package.event_fired"
        ]
        assert adopted == ["event.expiry_trigger", "event.reset_trigger"]


def test_same_instance_mechanism_reset_and_charge_recovery_at_dawn(tmp_path, context):
    """Reset and refill disjoint capabilities of one real portable instance."""

    def configure(data):
        fixture = extended_data(reset_policy="after_seconds", reset_delay_seconds=9)
        data["manifest"]["schema_version"] = "state-machine-foundation/10"
        data["lockable_doors"] = []
        data["scenes"][0]["objects"].extend(
            item for item in fixture["scenes"][0]["objects"] if item["object_id"] == CRED
        )
        definitions = {item["object_id"]: item for item in fixture["world_objects"]}
        data["world_objects"] = [
            definitions[CRED],
            {
                **definitions[LEVER],
                "object_id": FIRST,
                "definition_ref": "definition.resetting_healing_charm",
            },
        ]

    context = replace(context, can_advance_time=True)
    sm, sid, package, _, _ = charged_world(
        tmp_path, context, dawn_recovery=True, configure=configure
    )
    with sm:
        cast = charged_cast_command(sm, sid, package, context)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        position = sm.read_actor_position(sid, cast.actor_id, package, context=context)
        activated = submit(
            sm,
            sid,
            package,
            context,
            ActivateMechanism(A, FIRST, position.cell, "up"),
            "activate-charm-switch",
        )
        assert activated.status == "committed", activated
        advance = command(sm, sid, package, context, AdvanceSessionTime(10), "cross-dawn")
        receipt = sm.submit_command(advance, package, context=context)
        assert receipt.status == "committed", receipt
        time = sm.read_time_sync(sid, package, context=context)
        assert (time.committed_tick, time.progress_status, time.progress_reason) == (
            10,
            "completed",
            None,
        )
        (reset, refill) = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_type in {"mechanism.reset", "item.charges_recovered"}
        ]
        assert (reset.event_type, reset.payload["object_id"], reset.payload["boundary_tick"]) == (
            "mechanism.reset",
            FIRST,
            9,
        )
        assert refill.event_type == "item.charges_recovered"
        assert refill.payload == {
            "instance_id": FIRST,
            "previous_charges": 0,
            "charges": 3,
            "boundary_tick": 9,
        }
        assert {reset.event_seq, refill.event_seq} <= set(receipt.event_seqs)
        inventory = sm.read_carried_items(sid, cast.actor_id, package, context=context)
        assert (
            next(
                item for item in inventory.entries if item.instance_id == FIRST
            ).charges_remaining_per_unit
            == 3
        )
        assert sm.submit_command(advance, package, context=context) == receipt
