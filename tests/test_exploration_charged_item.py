"""Real retained item casting through the public exploration command."""

from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_contracts import (
    STAFF_CAST_SCHEMA_VERSION,
    ExplorationItemUsePayload,
    RuleEvaluationRequest,
)
from dnd5e_engine.evaluation_delta import InventorySpendCharge
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import (
    ActivationBlock,
    CastActivity,
    CastSpellBlock,
    ConsumptionBlock,
    ConsumptionTargetEntry,
)
from dnd5e_srd_data.schema.feature import RecoveryRule
from dnd5e_srd_data.schema.item import ItemUses
from item_support import NPC, A, command, create, item_data, item_loader, submit
from test_scoped_runtime_validation import measure

from trpg_state_machine import (
    AdvanceSessionTime,
    DropItem,
    PickupItem,
    StateMachine,
    StateMachineError,
    StepActor,
    TimeBinding,
    TypedCommand,
)
from trpg_state_machine.package import _validate

FIRST, SECOND = "object.healing_charm_first", "object.healing_charm_second"
SLUG, ACTIVITY = "test-healing-charm", "cast-cure-wounds"


def charged_world(
    tmp_path,
    context,
    *,
    cost=1,
    target_cell="1,0",
    pick_first=True,
    unrelated_items=0,
    dawn_recovery=False,
    initial_charges=None,
    configure=None,
):
    """Build a custom bound charm using the unmodified canonical Cure Wounds asset."""
    bundled, base = BundledAssetLoader(), item_loader()
    spell = bundled.get_spell("cure-wounds")
    item = base.get_item("wand-of-magic-missiles").model_copy(
        update={
            "slug": SLUG,
            "name": "Healing Charm",
            "description": "An authored charm that casts Cure Wounds using charges.",
            "uses": ItemUses(
                max="3", recovery=[RecoveryRule(period="dawn")] if dawn_recovery else []
            ),
            "activities": [
                CastActivity(
                    id=ACTIVITY,
                    activation=ActivationBlock(type="action", value=1),
                    consumption=ConsumptionBlock(
                        spell_slot=False,
                        targets=[ConsumptionTargetEntry(type="itemUses", value=str(cost))],
                    ),
                    spell=CastSpellBlock(
                        ability="wis",
                        level=1,
                        properties=[],
                        spellbook=False,
                        uuid=spell.foundry_uuid,
                    ),
                )
            ],
        }
    )
    loader = MemoryAssetLoader(
        items=[*base._items.values(), item],
        monsters=list(base._monsters.values()),
        spells=[spell],
    )
    data = item_data()
    if dawn_recovery:
        data["time_binding"] = asdict(
            TimeBinding(day_length=120, start_day_tick=11, day_start=20, night_start=70)
        )
        # Keep the mechanical recipient and use the supported second-based NPC shape.
        data["actors"][0].pop("npc_state", None)
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    data["actors"][0]["initial_cell"] = target_cell
    data["pc_templates"][0]["character"]["hp"]["current"] = 2
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        snapshot["snapshot_schema_version"] = "engine-snapshot/20"
        for actor in snapshot["character_states"]:
            assert not actor.pop("spells_known")
            actor["hp_current"] = 2
        snapshot["character_states"][0].update(weapon_in_hands=None, weapon_grip="none")
    items = [(FIRST, cost if initial_charges is None else initial_charges), (SECOND, 3)]
    items.extend((f"object.unrelated_charm_{i}", 3) for i in range(unrelated_items))
    for instance, charges in items:
        data["scenes"][0]["objects"].append(
            {
                "object_id": instance,
                "state": None,
                "movable": False,
                "blocks_passage": False,
                "initial_cell": "0,1",
            }
        )
        data["item_transfers"].append(
            {
                "instance_id": instance,
                "item_slug": SLUG,
                "initial_owner_id": None,
                "quantity": 1,
                "charges_remaining_per_unit": charges,
                "policy": "charge_exhaust_retain",
            }
        )
    if configure is not None:
        configure(data)
    package = _validate(data)
    db = tmp_path / "charged.sqlite"
    sm = StateMachine(db, dnd_loader=loader)
    sid = create(sm, package, context)
    for instance in (FIRST, SECOND) if pick_first else (SECOND,):
        receipt = submit(
            sm, sid, package, context, PickupItem(instance, A, "0,1"), "pick." + instance
        )
        assert receipt.status == "committed", receipt
    return sm, sid, package, loader, db


def cast_command(
    sm, sid, package, context, key="cast.charm", *, target=NPC, activity=ACTIVITY, instance=FIRST
):
    """Build one public item request using the current world version."""
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        key,
        state.pc_actor_id,
        "world.use_item",
        ExplorationItemUsePayload(
            kind="world.use_item", instance_id=instance, activity_id=activity, target_id=target
        ),
        state.world_version,
    )


def test_charge_and_heal_commit_once_and_persist(tmp_path, context):
    sm, sid, package, loader, db = charged_world(tmp_path, context, cost=2)
    with sm:
        command = cast_command(sm, sid, package, context)
        old = sm.read_noncombat_snapshot(sid, package, context=context)
        previous_rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        old_entries = {e.instance_id: e for e in old.inventory_state.entries}
        assert {e.instance_id: e for e in after.inventory_state.entries} == {
            **old_entries,
            FIRST: old_entries[FIRST].model_copy(update={"charges_remaining_per_unit": 0}),
        }
        assert old_entries[FIRST].charges_remaining_per_unit == 2
        assert after.resource_state == old.resource_state
        assert after.casting_state == old.casting_state
        assert next(a for a in after.character_states if a.entity_id == NPC).hp_current > 2
        assert (
            next(a for a in after.character_states if a.entity_id == command.actor_id).hp_current
            == 2
        )
        rng = sm.read_rng_context(sid, package, context=context)
        assert rng != previous_rng
        events = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.source_command_id == command.command_id
        ]
        assert [event.payload["engine_event"]["type"] for event in events] == [
            "intent_submitted",
            "spell_cast",
            "healing_applied",
        ]
        assert all(event.payload["item_instance_id"] == FIRST for event in events)
        assert tuple(event.event_seq for event in events) == receipt.event_seqs
        assert sm.submit_command(command, package, context=context) == receipt
        repeat = cast_command(sm, sid, package, context, "exhausted")
        exhausted = sm.submit_command(repeat, package, context=context)
        assert (exhausted.status, exhausted.error_code) == ("rejected", "item_exhausted")
        assert sm.read_noncombat_snapshot(sid, package, context=context) == after
        assert sm.read_rng_context(sid, package, context=context) == rng
    with StateMachine(db, dnd_loader=loader) as reopened:
        reopened.resume_session(sid, package, context=context)
        assert reopened.read_noncombat_snapshot(sid, package, context=context) == after
        assert (
            reopened.get_command_receipt(sid, command.command_id, package, context=context)
            == receipt
        )


@pytest.mark.parametrize(
    ("setup", "activity", "error"),
    [
        ({"target_cell": "2,0"}, ACTIVITY, "target_invalid"),
        ({}, "wrong-activity", "activity_invalid"),
    ],
)
def test_rejected_item_cast_preserves_hp_charge_rng_and_events(
    tmp_path, context, setup, activity, error
):
    sm, sid, package, _, _ = charged_world(tmp_path, context, **setup)
    with sm:
        command = cast_command(sm, sid, package, context, activity=activity)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code, receipt.event_seqs) == ("rejected", error, ())
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events


def test_unowned_item_has_same_public_behavior_as_missing_identity(tmp_path, context):
    sm, sid, package, _, _ = charged_world(tmp_path, context, pick_first=False)
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        state = sm.read_session(sid, package, context=context)
        for instance in (FIRST, "object.missing_charm"):
            command = cast_command(sm, sid, package, context, instance, instance=instance)
            with pytest.raises(StateMachineError) as exc:
                sm.submit_command(command, package, context=context)
            assert exc.value.code == "invalid_target"
            self_command = cast_command(
                sm, sid, package, context, instance, target=state.pc_actor_id, instance=instance
            )
            receipt = sm.submit_command(self_command, package, context=context)
            assert (receipt.status, receipt.error_code) == ("rejected", "item_missing")
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before


def test_charge_and_heal_roll_back_together_and_same_key_retries(tmp_path, context):
    sm, sid, package, _, _ = charged_world(tmp_path, context)
    with sm:
        command = cast_command(sm, sid, package, context)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_cast_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='cast.charm' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(command, package, context=context)
        assert exc.value.code == "storage_error"
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_cast_receipt")
        assert sm.submit_command(command, package, context=context).status == "committed"


@pytest.mark.parametrize("fault", ["wrong_instance", "wrong_cost"])
def test_item_result_must_pay_for_the_selected_activity_and_instance(
    tmp_path, context, monkeypatch, fault
):
    sm, sid, package, _, _ = charged_world(tmp_path, context, cost=2)
    with sm:
        command = cast_command(sm, sid, package, context)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        evaluate = sm._rules._evaluate_dnd

        async def malformed(request):
            """Substitute a well-formed charge proposal with the wrong instance or cost."""
            result = await evaluate(request)
            spend, *healing = result.state_delta.operations
            wrong = (
                next(
                    e
                    for e in request.state_snapshot.inventory_state.entries
                    if e.instance_id == SECOND
                )
                if fault == "wrong_instance"
                else spend.expected
            )
            forged = InventorySpendCharge(
                kind="inventory.spend_charge",
                expected=wrong,
                value=wrong.model_copy(
                    update={"charges_remaining_per_unit": wrong.charges_remaining_per_unit - 1}
                ),
                amount=1,
            )
            changed = result.state_delta.model_copy(update={"operations": (forged, *healing)})
            return result.model_copy(update={"state_delta": changed})

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", malformed)
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(command, package, context=context)
        assert exc.value.code == "invalid_rule_result"
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None


def test_cast_scope_is_independent_of_unrelated_ground_items(tmp_path, context):
    observed = []
    for extra_items in (0, 25):
        directory = tmp_path / str(extra_items)
        directory.mkdir()
        sm, sid, package, _, _ = charged_world(directory, context, unrelated_items=extra_items)
        with sm:
            command = cast_command(sm, sid, package, context)
            with measure(sm) as (counts, _):
                receipt = sm.submit_command(command, package, context=context)
            assert receipt.status == "committed", receipt
            observed.append((counts["sql_selects"], counts["sql_rows"], counts["returned_bytes"]))
    assert observed[0] == observed[1]


def test_dawn_refills_the_same_item_after_transfer_and_reopen(tmp_path, context):
    context = replace(context, can_advance_time=True)
    sm, sid, package, loader, db = charged_world(
        tmp_path, context, dawn_recovery=True, initial_charges=3, cost=2
    )
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        cast = cast_command(sm, sid, package, context)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        assert submit(sm, sid, package, context, DropItem(FIRST, A, "0,0"), "drop").status == (
            "committed"
        )
        step = submit(sm, sid, package, context, StepActor(A, "0,0", "0,1"), "step.clear")
        assert step.status == "committed", step
        pick = submit(sm, sid, package, context, PickupItem(FIRST, A, "0,0"), "npc.pick", NPC)
        assert pick.status == "committed", pick
        assert submit(sm, sid, package, context, AdvanceSessionTime(8), "before").status == (
            "committed"
        )
        sm.save_session(sid, package, context=context)
    with StateMachine(db, dnd_loader=loader) as reopened:
        reopened.resume_session(sid, package, context=context)
        old_inventory = reopened.read_carried_items(sid, NPC, package, context=context)
        old = next(entry for entry in old_inventory.entries if entry.instance_id == FIRST)
        assert old.charges_remaining_per_unit == 1
        sync = command(reopened, sid, package, context, AdvanceSessionTime(10), "dawn")
        receipt = reopened.submit_command(sync, package, context=context)
        assert receipt.status == "committed", receipt
        assert reopened.submit_command(sync, package, context=context) == receipt
        new_inventory = reopened.read_carried_items(sid, NPC, package, context=context)
        new = next(entry for entry in new_inventory.entries if entry.instance_id == FIRST)
        assert new.owner_id == NPC
        assert new.charges_remaining_per_unit == 3
        assert old.charges_remaining_per_unit == 1
        spare = reopened.read_carried_items(sid, actor, package, context=context).entries
        spare_charm = next(entry for entry in spare if entry.instance_id == SECOND)
        assert spare_charm.charges_remaining_per_unit == 3
        assert reopened.read_time_sync(sid, package, context=context).committed_tick == 10
        assert (
            submit(reopened, sid, package, context, AdvanceSessionTime(130), "another.dawn").status
            == "committed"
        )
        recoveries = [
            event
            for event in reopened.read_events(sid, package, context=context)
            if event.event_type == "item.charges_recovered"
        ]
        assert len(recoveries) == 1
        assert recoveries[0].payload == {
            "instance_id": FIRST,
            "previous_charges": 1,
            "charges": 3,
            "boundary_tick": 9,
        }


def test_dawn_balance_time_and_receipt_roll_back_together(tmp_path, context):
    context = replace(context, can_advance_time=True)
    sm, sid, package, _, _ = charged_world(tmp_path, context, dawn_recovery=True)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        old = sm.read_carried_items(sid, actor, package, context=context)
        events = sm.read_events(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        sync = command(sm, sid, package, context, AdvanceSessionTime(10), "dawn.failure")
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_dawn_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='dawn.failure' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(sync, package, context=context)
        assert exc.value.code == "storage_error"
        assert sm.read_carried_items(sid, actor, package, context=context) == old
        assert sm.read_events(sid, package, context=context) == events
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 0
        assert sm.get_command_receipt(sid, sync.command_id, package, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_dawn_receipt")
        receipt = sm.submit_command(sync, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(sync, package, context=context) == receipt
        new = sm.read_carried_items(sid, actor, package, context=context)
        new_first = next(entry for entry in new.entries if entry.instance_id == FIRST)
        old_first = next(entry for entry in old.entries if entry.instance_id == FIRST)
        assert new_first.charges_remaining_per_unit == 3
        assert old_first.charges_remaining_per_unit == 1
        assert sm.read_rng_context(sid, package, context=context) == rng


def test_dawn_refill_scope_is_independent_of_full_items(tmp_path, context):
    context = replace(context, can_advance_time=True)
    observed = []
    for extra_items in (0, 25):
        directory = tmp_path / str(extra_items)
        directory.mkdir()
        sm, sid, package, _, _ = charged_world(
            directory, context, dawn_recovery=True, unrelated_items=extra_items
        )
        with sm:
            sync = command(sm, sid, package, context, AdvanceSessionTime(10), "dawn")
            with measure(sm) as (counts, _):
                receipt = sm.submit_command(sync, package, context=context)
            assert receipt.status == "committed", receipt
            assert sm.read_time_sync(sid, package, context=context).committed_tick == 10
            recoveries = [
                event
                for event in sm.read_events(sid, package, context=context)
                if event.event_type == "item.charges_recovered"
            ]
            assert [event.payload["instance_id"] for event in recoveries] == [FIRST]
            observed.append((counts["sql_selects"], counts["sql_rows"], counts["returned_bytes"]))
    assert observed[0] == observed[1]


def test_fixed_charm_rejects_staff_cast_contract_without_draw_or_payment(
    tmp_path, context, monkeypatch
):
    sm, sid, package, _, _ = charged_world(tmp_path, context)
    with sm:
        original = sm._rules._evaluate_dnd
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        cast = cast_command(sm, sid, package, context, "charm-level")
        cast = replace(cast, payload=cast.payload.model_copy(update={"cast_level": 2}))
        result = sm.submit_command(cast, package, context=context)
        assert result.error_code == "item.capability"
        assert sm.submit_command(cast, package, context=context) == result
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        verified = []

        async def check_staff_contract(request):
            """Try the same legitimate charm through /41 before the ordinary /32 evaluation."""
            staff_request = RuleEvaluationRequest.model_validate(
                request.model_copy(update={"schema_version": STAFF_CAST_SCHEMA_VERSION})
            )
            refused = await original(staff_request)
            assert refused.status == "unsupported"
            assert refused.state_delta is None and refused.rng_transition is None
            verified.append(True)
            return await original(request)

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", check_staff_contract)
        ordinary = cast_command(sm, sid, package, context, "charm-normal")
        assert sm.submit_command(ordinary, package, context=context).status == "committed"
        assert verified == [True]
