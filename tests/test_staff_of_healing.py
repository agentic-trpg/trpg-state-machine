"""Exercise the bound SRD Staff of Healing through public exploration commands."""

from copy import deepcopy
from dataclasses import asdict, replace
from random import Random

import pytest
from dnd5e_engine.evaluation_contracts import ExplorationItemAreaTarget
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from item_support import NPC, A, command, submit
from test_cure_wounds import cure_world
from test_exploration_charged_item import cast_command
from test_object_spatial_service import oriented_data
from test_scoped_runtime_validation import measure
from test_world_spatial import OBSTACLE

from trpg_state_machine import (
    AdvanceSessionTime,
    EquipWeapon,
    StateMachine,
    StateMachineError,
    TimeBinding,
)

STAFF, SPARE = "object.healing_staff", "object.spare_healing_staff"
CURE, MASS = "mkIIFw98V1d8uABu", "tfGfEtth3JKF7BXQ"
SLUG = "staff-of-healing"


def staff_world(
    tmp_path, context, *, charges=10, seed=0, wide_map=False, far_objects=0, obstacle=None
):
    """Load the unmodified bundled asset into one canonical Cleric 1 session."""

    def configure(data, loader):
        """Author two initially owned and independently charged staff instances."""
        data["time_binding"] = asdict(
            TimeBinding(day_length=120, start_day_tick=11, day_start=20, night_start=70)
        )
        del data["rule_bindings"]["combat_foundation"]
        pc = data["pc_templates"][0]
        owner = pc["pc_template_id"]
        pc["character"]["inventory"] = [SLUG]
        if wide_map:
            pc["character"]["hp"]["max"] = 50
            data["actors"][0]["initial_cell"] = "12,0"
            data["scenes"][0]["grid"].update(width=15, lighting={"7,0": "dark"})
            snapshot = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
            snapshot["character_states"][0]["hp_max"] = 50
            snapshot["scene_state"]["grid"].update(width=15, lighting={"7,0": "dark"})
            data["scenes"][0]["objects"].extend(
                dict(
                    object_id=f"object.unrelated_fixture_{index}",
                    state=None,
                    movable=False,
                    blocks_passage=False,
                    initial_cell="14,2",
                )
                for index in range(far_objects)
            )
        if obstacle is not None:
            opacity, cover = obstacle
            reference = oriented_data(anchor="9,0", footprint=[[0, 0]], blocking=False)
            placement = next(
                obj for obj in reference["scenes"][0]["objects"] if obj["object_id"] == OBSTACLE
            )
            definition = deepcopy(
                next(obj for obj in reference["world_objects"] if obj["object_id"] == OBSTACLE)
            )
            data["manifest"]["schema_version"] = reference["manifest"]["schema_version"]
            data["lockable_doors"] = []
            data["scenes"][0]["objects"].append(deepcopy(placement))
            capability = definition["capabilities"][0]
            capability["open_properties"] = {"cover": cover, "light_opaque": opacity}
            capability["closed_properties"] = {"cover": cover, "light_opaque": opacity}
            data.setdefault("world_objects", []).append(definition)
        for instance, balance in ((STAFF, charges), (SPARE, 10)):
            data["scenes"][0]["objects"].append(
                dict(
                    object_id=instance,
                    state=None,
                    movable=False,
                    blocks_passage=False,
                    initial_cell="0,1",
                )
            )
            data["item_transfers"].append(
                dict(
                    instance_id=instance,
                    item_slug=SLUG,
                    initial_owner_id=owner,
                    quantity=1,
                    charges_remaining_per_unit=balance,
                    policy="charge_exhaust_retain",
                    initially_attuned=instance == STAFF,
                )
            )
            for foundation in data["rule_bindings"].values():
                snapshot = foundation["snapshot"]
                snapshot["character_states"][0]["carried_item_slugs"] = [SLUG]
                snapshot["inventory_state"]["entries"].append(
                    dict(
                        instance_id=instance,
                        owner_id=owner,
                        item_slug=SLUG,
                        quantity=1,
                        charges_remaining_per_unit=balance,
                        accessible=True,
                    )
                )
        canonical = BundledAssetLoader()
        return MemoryAssetLoader(
            items=[*loader._items.values(), canonical.get_item(SLUG)],
            monsters=list(loader._monsters.values()),
            spells=list(loader._spells.values()),
            classes=list(loader._classes.values()),
            features=list(loader._features.values()),
        )

    return cure_world(
        tmp_path,
        context,
        extra_spells=("mass-cure-wounds",),
        exploration=True,
        slot_count=0,
        target_hp=2,
        rng_seed=seed,
        configure=configure,
    )


def use(
    sm, sid, package, context, key, *, activity=CURE, instance=STAFF, level=1, target=NPC, area=None
):
    """Build a public item command selecting the bound activity and actual cast level."""
    cmd = cast_command(
        sm, sid, package, context, key, target=target, activity=activity, instance=instance
    )
    return replace(
        cmd,
        payload=cmd.payload.model_copy(
            update={
                "cast_level": level,
                "area_target": area,
                "target_id": None if area else target,
            }
        ),
    )


def inventory(sm, sid, package, context):
    """Read the player's current inventory keyed by exact instance."""
    actor = sm.read_session(sid, package, context=context).pc_actor_id
    return {
        e.instance_id: e
        for e in sm.read_carried_items(sid, actor, package, context=context).entries
    }


def heal_events(sm, sid, package, context):
    """Read engine evidence emitted by the public item command."""
    return [
        e.payload["engine_event"]
        for e in sm.read_events(sid, package, context=context)
        if e.event_type == "world.item_used"
    ]


def test_staff_requires_exact_held_attunement_and_prices_levels(tmp_path, context):
    sm, sid, package, _, _ = staff_world(tmp_path, context)
    with sm:
        before = sm.read_rng_context(sid, package, context=context)
        cold = use(sm, sid, package, context, "not-held")
        assert sm.submit_command(cold, package, context=context).error_code == "item_not_held"
        assert (
            submit(sm, sid, package, context, EquipWeapon(SPARE), "hold-spare").status
            == "committed"
        )
        spare = use(sm, sid, package, context, "not-attuned", instance=SPARE)
        assert sm.submit_command(spare, package, context=context).error_code == "item_not_attuned"
        assert (
            submit(sm, sid, package, context, EquipWeapon(STAFF), "hold-staff").status
            == "committed"
        )
        wrong = use(sm, sid, package, context, "wrong-activity", activity="unknown")
        assert sm.submit_command(wrong, package, context=context).error_code == "activity_invalid"
        fifth = use(sm, sid, package, context, "level-five", level=5)
        assert sm.submit_command(fifth, package, context=context).error_code == "cast_level_invalid"
        assert sm.read_rng_context(sid, package, context=context) == before
        expected = before.state.restore()
        for level in (1, 4):
            cast = use(sm, sid, package, context, f"cure-{level}", level=level)
            receipt = sm.submit_command(cast, package, context=context)
            assert receipt.status == "committed", receipt
            assert sm.submit_command(cast, package, context=context) == receipt
            spell, heal = heal_events(sm, sid, package, context)[-2:]
            assert spell["spell_id"] == "cure-wounds"
            assert spell["slot_level"] is None and spell["item_cast_level"] == level
            assert heal["amount"] == sum(expected.randint(1, 8) for _ in range(level * 2)) + 3
        items = inventory(sm, sid, package, context)
        assert items[STAFF].charges_remaining_per_unit == 5
        assert items[SPARE].charges_remaining_per_unit == 10
        assert items[STAFF].attuned_actor_id == items[STAFF].owner_id
        assert items[SPARE].attuned_actor_id is None
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert after.resource_state.pools[0].current == 0


def test_staff_dawn_roll_partial_capped_save_retry_and_rollback(tmp_path, context):
    context = replace(context, can_advance_time=True)
    sm, sid, package, loader, db = staff_world(tmp_path, context, charges=1, seed=2)
    with sm:
        sm.save_session(sid, package, context=context)
    with StateMachine(db, dnd_loader=loader) as reopened:
        reopened.resume_session(sid, package, context=context)
        before = reopened.read_rng_context(sid, package, context=context)
        old = inventory(reopened, sid, package, context)
        dawn = command(reopened, sid, package, context, AdvanceSessionTime(10), "staff-dawn")
        reopened._conn.execute(
            "CREATE TEMP TRIGGER fail_staff_dawn BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='staff-dawn' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as exc:
            reopened.submit_command(dawn, package, context=context)
        assert exc.value.code == "storage_error"
        assert reopened.read_rng_context(sid, package, context=context) == before
        assert reopened.read_time_sync(sid, package, context=context).committed_tick == 0
        assert inventory(reopened, sid, package, context) == old
        assert reopened.get_command_receipt(sid, dawn.command_id, package, context=context) is None
        reopened._conn.execute("DROP TRIGGER fail_staff_dawn")
        result = reopened.submit_command(dawn, package, context=context)
        assert result.status == "committed", result
        assert reopened.submit_command(dawn, package, context=context) == result
        assert inventory(reopened, sid, package, context)[STAFF].charges_remaining_per_unit == 6
        later = submit(reopened, sid, package, context, AdvanceSessionTime(250), "two-more-days")
        assert later.status == "committed", later
        final = inventory(reopened, sid, package, context)
        assert final[STAFF].charges_remaining_per_unit == 10
        assert old[STAFF].charges_remaining_per_unit == 1
        assert final[SPARE] == old[SPARE]
        recoveries = [
            event.payload
            for event in reopened.read_events(sid, package, context=context)
            if event.event_type == "item.charges_recovered"
        ]
        assert [
            (e["boundary_tick"], e["dice"], e["modifier"], e["charges"]) for e in recoveries
        ] == [(9, [1], 4, 6), (129, [1], 4, 10)]
        assert (
            reopened.read_rng_context(sid, package, context=context).version == before.version + 2
        )


@pytest.mark.parametrize("destroyed", [False, True])
def test_staff_last_charge_roll_ties_equipment_and_absence_to_receipt(tmp_path, context, destroyed):
    rolls = {}
    for seed in range(50):
        rng = Random(seed)
        for _ in range(8):
            rng.randint(1, 8)
        roll = rng.randint(1, 20)
        rolls.setdefault(roll == 1, (seed, roll))
    seed, expected = rolls[destroyed]
    sm, sid, package, loader, db = staff_world(tmp_path, context, charges=4, seed=seed)
    with sm:
        assert (
            submit(sm, sid, package, context, EquipWeapon(STAFF), "hold-staff").status
            == "committed"
        )
        # All four charges are consumed by the canonical fourth-level Cast Activity.
        cast = use(sm, sid, package, context, "last-charge", level=4)
        before = sm.read_rng_context(sid, package, context=context)
        rng = before.state.restore()
        for _ in range(8):
            rng.randint(1, 8)
        expected = rng.randint(1, 20)
        assert (expected == 1) == destroyed
        receipt = sm.submit_command(cast, package, context=context)
        assert receipt.status == "committed", receipt
        assert heal_events(sm, sid, package, context)[-1]["roll"] == expected
        assert sm.submit_command(cast, package, context=context) == receipt
    with StateMachine(db, dnd_loader=loader) as reopened:
        reopened.resume_session(sid, package, context=context)
        items = inventory(reopened, sid, package, context)
        assert (STAFF not in items) == destroyed
        if not destroyed:
            assert items[STAFF].charges_remaining_per_unit == 0
        snapshot = reopened.read_noncombat_snapshot(sid, package, context=context)
        actor = next(a for a in snapshot.character_states if a.entity_type == "Character")
        assert actor.held_weapon_instance_id == (None if destroyed else STAFF)
        assert reopened.submit_command(cast, package, context=context) == receipt


def test_staff_casts_both_spells_at_their_prices_and_heals_the_chosen_sphere(tmp_path, context):
    sm, sid, package, _, _ = staff_world(tmp_path, context, wide_map=True)
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        held = submit(sm, sid, package, context, EquipWeapon(STAFF), "hold-for-mass")
        assert held.status == "committed", held
        cure = use(sm, sid, package, context, "cure-before-mass", target=pc)
        assert sm.submit_command(cure, package, context=context).status == "committed"
        assert inventory(sm, sid, package, context)[STAFF].charges_remaining_per_unit == 9
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        expected = rng.state.restore()
        for label, center in (
            ("out-of-sphere", "5,0"),
            ("unseen", "7,0"),
            ("out-of-range", "13,0"),
        ):
            affected = (pc, NPC) if label == "out-of-sphere" else (NPC,)
            invalid = use(
                sm,
                sid,
                package,
                context,
                label,
                activity=MASS,
                level=5,
                area=ExplorationItemAreaTarget(center_cell=center, target_ids=affected),
            )
            denied = sm.submit_command(invalid, package, context=context)
            assert denied.error_code == "target_invalid", denied
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        cast = use(
            sm,
            sid,
            package,
            context,
            "mass-charges",
            activity=MASS,
            level=5,
            area=ExplorationItemAreaTarget(center_cell="6,0", target_ids=(NPC, pc)),
        )
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_mass_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='mass-charges' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(cast, package, context=context)
        assert exc.value.code == "storage_error"
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.get_command_receipt(sid, cast.command_id, package, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_mass_receipt")
        receipt = sm.submit_command(cast, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(cast, package, context=context) == receipt
        intent, cast_event, *healing = heal_events(sm, sid, package, context)[-4:]
        assert intent["area_center_cell"] == "6,0"
        assert intent["area_target_ids"] == [NPC, pc]
        assert cast_event["spell_id"] == "mass-cure-wounds"
        assert cast_event["slot_level"] is None and cast_event["item_cast_level"] == 5
        assert [(event["target_id"], event["amount"]) for event in healing] == [
            (target, sum(expected.randint(1, 8) for _ in range(5)) + 3) for target in (NPC, pc)
        ]
        assert inventory(sm, sid, package, context)[STAFF].charges_remaining_per_unit == 4
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert after.resource_state == before.resource_state
        assert {actor.entity_id: actor.hp_current for actor in after.character_states} == {
            event["target_id"]: min(target.hp_max, target.hp_current + event["amount"])
            for event in healing
            for target in before.character_states
            if target.entity_id == event["target_id"]
        }
        stale = replace(cast, command_id="mass-stale")
        assert sm.submit_command(stale, package, context=context).status == "conflict"
        assert sm.read_noncombat_snapshot(sid, package, context=context) == after


def test_mass_cast_reads_local_rays_and_actors(tmp_path, context):
    observed = []
    for far_objects in (0, 25):
        directory = tmp_path / str(far_objects)
        directory.mkdir()
        sm, sid, package, _, _ = staff_world(
            directory, context, wide_map=True, far_objects=far_objects
        )
        with sm:
            actor = sm.read_session(sid, package, context=context).pc_actor_id
            held = submit(sm, sid, package, context, EquipWeapon(STAFF), "hold")
            assert held.status == "committed", held
            cast = use(
                sm,
                sid,
                package,
                context,
                "scope",
                activity=MASS,
                level=5,
                area=ExplorationItemAreaTarget(center_cell="6,0", target_ids=(actor, NPC)),
            )
            with measure(sm) as (counts, _):
                receipt = sm.submit_command(cast, package, context=context)
            assert receipt.status == "committed", receipt
            observed.append((counts["sql_selects"], counts["sql_rows"], counts["returned_bytes"]))
    assert observed[0] == observed[1], observed


@pytest.mark.parametrize(
    ("opacity", "cover", "expected"),
    [
        (False, "total", "rejected"),
        (True, "total", "rejected"),
        (False, "three_quarters", "committed"),
        (True, "none", "committed"),
    ],
)
def test_mass_healing_obeys_total_cover_independently_of_light(
    tmp_path, context, opacity, cover, expected
):
    sm, sid, package, _, _ = staff_world(
        tmp_path, context, wide_map=True, obstacle=(opacity, cover)
    )
    with sm:
        held = submit(sm, sid, package, context, EquipWeapon(STAFF), "hold")
        assert held.status == "committed", held
        assert sm._world.grid.read_scene(sid, A, package).cover_between("6,0", "12,0") == cover
        before = sm.read_rng_context(sid, package, context=context)
        cast = use(
            sm,
            sid,
            package,
            context,
            "area-cover",
            activity=MASS,
            level=5,
            area=ExplorationItemAreaTarget(center_cell="6,0", target_ids=(NPC,)),
        )
        result = sm.submit_command(cast, package, context=context)
        assert result.status == expected, result
        if expected == "rejected":
            assert result.error_code == "target_invalid"
            assert sm.read_rng_context(sid, package, context=context) == before
            assert inventory(sm, sid, package, context)[STAFF].charges_remaining_per_unit == 10
