"""Trusted fixed-one item charge writes and active containment identity."""

from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_srd_data import MemoryAssetLoader
from lock_support import CRED, TWIN
from test_lockable_passages import acquire
from test_world_containers import BOX, container_data
from world_support import command, submit

from trpg_state_machine import PickupItem, PutItem, SpendItemCharge, StateMachine, StateMachineError
from trpg_state_machine.package import _validate


def charge_world(tmp_path, context):
    """Build an explicitly retained synthetic item with no cast or potion action."""
    from lock_support import lock_loader

    original = lock_loader()
    asset = original.get_item("wand-of-magic-missiles").model_copy(
        update={
            "slug": "test-charge-cell",
            "name": "Test charge cell",
            "description": "Synthetic retained charge test item.",
            "activities": [],
        }
    )
    loader = MemoryAssetLoader(
        items=[*original._items.values(), asset], monsters=list(original._monsters.values())
    )
    data = container_data()
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    for item in data["item_transfers"]:
        if item["instance_id"] in (CRED, TWIN):
            item.update(
                item_slug="test-charge-cell",
                charges_remaining_per_unit=2,
                policy="charge_exhaust_retain"
                if item["instance_id"] == CRED
                else "ordinary_public",
            )
    package = _validate(data)
    from spatial_support import create

    sm = StateMachine(tmp_path / "charge.db", dnd_loader=loader)
    sid = create(sm, package, context)
    return sm, sid, package, loader


def test_charge_spend_keeps_contained_identity_and_fixed_snapshot(tmp_path, context):
    sm, sid, package, loader = charge_world(tmp_path, context)
    trusted = replace(context, can_spend_item_charge=True)
    with sm:
        acquire(sm, sid, package, context)
        acquire(sm, sid, package, context, iid=TWIN, key="acquire.twin")
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        assert (
            submit(
                sm, sid, package, context, PickupItem(BOX, package.entry_scene_id, "1,0"), "box"
            ).status
            == "committed"
        )
        assert (
            submit(
                sm, sid, package, context, PutItem(package.entry_scene_id, CRED, BOX, "0,0"), "put"
            ).status
            == "committed"
        )
        assert (
            submit(
                sm,
                sid,
                package,
                context,
                PutItem(package.entry_scene_id, TWIN, BOX, "0,0"),
                "put.twin",
            ).status
            == "committed"
        )
        assert sm.get_object(sid, actor, BOX, package, context=context)["contents"] == (
            CRED,
            TWIN,
        )
        with sm._store.transaction():
            contents = sm._world_facts.active_contents(sid, BOX)
            before = next(item for item in contents if item.object_id == CRED)
            spare = next(item for item in contents if item.object_id == TWIN)
            assert before.object_id == CRED and before.charges == 2
            assert spare is not before and spare.charges == 2
        snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        old = next(e for e in snapshot.inventory_state.entries if e.instance_id == CRED)
        op = SpendItemCharge(CRED)
        assert (
            sm.query_world_action(sid, actor, op, package, context=context).reason == "unauthorized"
        )
        assert sm.query_world_action(sid, actor, op, package, context=trusted).status == "available"
        with pytest.raises(StateMachineError) as denied:
            submit(sm, sid, package, context, op, "denied")
        assert denied.value.code == "unauthorized"
        first = command(sm, sid, package, trusted, op, "spend.1")
        receipt = sm.submit_command(first, package, context=trusted)
        assert receipt.status == "committed"
        assert sm.submit_command(first, package, context=trusted) == receipt
        with sm._store.transaction():
            after = next(
                item for item in sm._world_facts.active_contents(sid, BOX) if item.object_id == CRED
            )
            assert after is before and after.charges == 1
            assert spare.charges == 2
        assert old.charges_remaining_per_unit == 2
        assert submit(sm, sid, package, trusted, op, "spend.2").status == "committed"
        exhausted = submit(sm, sid, package, trusted, op, "spend.3")
        assert exhausted.error_code == "item_charges_exhausted"
        assert (
            sm.query_world_action(sid, actor, op, package, context=trusted).reason
            == "item_charges_exhausted"
        )
        with sm._store.transaction():
            assert (
                next(
                    item
                    for item in sm._world_facts.active_contents(sid, BOX)
                    if item.object_id == CRED
                )
                is before
            )
            assert before.charges == 0
            assert spare.charges == 2
            assert sm._world_facts.object(sid, CRED)["item_charges"] == 0
    with StateMachine(tmp_path / "charge.db", dnd_loader=loader) as reopened:
        current = reopened.read_noncombat_snapshot(sid, package, context=context)
        assert (
            next(
                e for e in current.inventory_state.entries if e.instance_id == CRED
            ).charges_remaining_per_unit
            == 0
        )


def test_charge_commit_boundary_rollback_and_hot_container_read(tmp_path, context, monkeypatch):
    sm, sid, package, _ = charge_world(tmp_path, context)
    trusted = replace(context, can_spend_item_charge=True)
    with sm:
        acquire(sm, sid, package, context)
        assert (
            submit(
                sm, sid, package, context, PickupItem(BOX, package.entry_scene_id, "1,0"), "box"
            ).status
            == "committed"
        )
        assert (
            submit(
                sm, sid, package, context, PutItem(package.entry_scene_id, CRED, BOX, "0,0"), "put"
            ).status
            == "committed"
        )
        with sm._store.transaction():
            item = sm._world_facts.active_contents(sid, BOX)[0]
            assert item.charges == 2
        with pytest.raises(RuntimeError), sm._store.transaction(write=True):
            sm._conn.execute(
                "UPDATE objects SET item_charges=1 WHERE session_id=? AND object_id=?",
                (sid, CRED),
            )
            assert item._payload[2] == 2
            raise RuntimeError("roll back candidate")
        with sm._store.transaction():
            assert sm._world_facts.active_contents(sid, BOX)[0] is item
            assert item.charges == 2
        original_commit = sm._store._commit

        def checked_commit():
            assert item._payload[2] == 2
            return original_commit()

        monkeypatch.setattr(sm._store, "_commit", checked_commit)
        assert (
            submit(sm, sid, package, trusted, SpendItemCharge(CRED), "spend").status == "committed"
        )
        with sm._store.transaction():
            assert sm._world_facts.active_contents(sid, BOX)[0] is item
            assert item.charges == 1
        before = sm._store.memory.stats["cold_queries"]
        statements = []
        sm._store._conn.set_trace_callback(statements.append)
        try:
            with sm._store.transaction():
                assert sm._world_facts.active_contents(sid, BOX)[0].charges == 1
                assert sm._world_facts.active_contents(sid, BOX)[0] is item
        finally:
            sm._store._conn.set_trace_callback(None)
        assert sm._store.memory.stats["cold_queries"] == before
        assert not any("FROM objects" in sql for sql in statements)
