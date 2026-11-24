"""Ordinary and direct object saves over real exploration spell and region facts."""

from dataclasses import replace
from functools import partial

import pytest
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.rules.character import proficiency_grants
from dnd5e_srd_data import BundledAssetLoader
from item_support import NPC, A, B
from lock_support import CRED, DOOR
from spatial_support import command, submit
from test_bless_bane_cast import _cast, _session
from test_environment_region_effects import _toxic_fog
from test_faerie_fire import _grow_remote
from test_mechanism_save_gate import state_gated_data
from test_real_saves import save_command
from test_scoped_runtime_validation import measure
from test_timed_spell_cast import timed_spell_world as timed_spell_world
from test_world_mechanisms import LEVER

from trpg_state_machine import (
    AddEnvironmentSource,
    AdvanceSessionTime,
    ObjectState,
    RemoveEnvironmentSource,
    SetObjectState,
    StateMachine,
    StateMachineError,
)


def _configure_save(data, *, unreviewed_stock=False):
    """Use one canonical Cleric 1 and a direct authored state trigger."""
    cleric = BundledAssetLoader().get_class("cleric")
    fixed = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
    pc = fixed["character_states"][0]
    grants = proficiency_grants([(cleric, 1, "primary")])
    pc.update(
        class_slug="cleric",
        classes={"cleric": 1},
        subclass_slug=None,
        character_level=1,
        granted_features=sorted(
            grant.slug
            for grant in cleric.granted_features
            if grant.ref_type == "feature" and grant.level == 1
        ),
        spell_slots={1: 2},
        custom_counters={},
        save_proficiencies=sorted(grants.saves),
        weapon_proficiencies=sorted(grants.weapons),
        spellcasting_ability="wis",
    )
    (pool,) = (p for p in fixed["resource_state"]["pools"] if p.get("pool_id") == "spell:1")
    pool.update(current=2, maximum=2)
    fixed["resource_state"]["pools"] = [pool]
    gated = state_gated_data(dc=100)
    data["manifest"]["schema_version"] = "state-machine-foundation/10"
    data["lockable_doors"] = []
    data["world_objects"] = [
        item for item in gated["world_objects"] if item["object_id"] in {DOOR, LEVER, CRED}
    ]
    objects = data["scenes"][0]["objects"]
    if unreviewed_stock:
        instance = "object.cleric_dagger"
        data["pc_templates"][0]["character"]["inventory"].append("dagger")
        objects.append({**objects[-1], "object_id": instance})
        fixed["inventory_state"]["entries"].append(
            {
                **fixed["inventory_state"]["entries"][-1],
                "instance_id": instance,
                "item_slug": "dagger",
            }
        )
        data["item_transfers"].append(
            {**data["item_transfers"][-1], "instance_id": instance, "item_slug": "dagger"}
        )
    door = next(item for item in objects if item["object_id"] == DOOR)
    door.update(state="closed", initial_cell="0,1")
    objects.extend(
        item for item in gated["scenes"][0]["objects"] if item["object_id"] in {LEVER, CRED}
    )


def _warded_world(
    timed_spell_world, *, bless_pc=True, unreviewed_stock=False, database="timed-spell.sqlite"
):
    session = _session(
        timed_spell_world,
        "bless",
        seed=5,
        database=database,
        configure_save=partial(_configure_save, unreviewed_stock=unreviewed_stock),
    )
    sm, sid, package, context = session
    pc = sm.read_session(sid, package, context=context).pc_actor_id
    with pytest.raises(StateMachineError) as denied:
        sm.submit_command(save_command(sm, sid, package, context), package, context=context)
    assert denied.value.code == "unauthorized"
    context = replace(context, can_adjudicate_checks=True, can_manage_environment=True)
    cast, receipt = _cast(session, "bless", (pc, NPC) if bless_pc else (NPC,), command_id="ward")
    assert receipt.status == "committed", receipt
    return sm, sid, package, context, cast


def _save_roll(sm, sid, package, context, cmd, *, bonus_source=None):
    """Verify exact die provenance and RNG order through receipt-authorized evidence."""
    before = sm.read_rng_context(sid, package, context=context)
    receipt = sm.submit_command(cmd, package, context=context)
    assert receipt.status == "committed", receipt
    (roll,) = sm.get_command_rolls(sid, cmd.command_id, package, context=context)["rolls"]
    rng = before.state.restore()
    assert roll["roll_detail"]["d20"]["faces"] == [rng.randint(1, 20)]
    bonus = [item for item in roll["roll_detail"]["modifiers"] if item["kind"] == "die"]
    if bonus_source is None:
        assert bonus == []
    else:
        (die,) = bonus
        assert (die["source"], die["sides"], die["face"]) == (bonus_source, 4, rng.randint(1, 4))
    assert sm.read_rng_context(sid, package, context=context).state == RNGState.capture(rng)
    assert sm.submit_command(cmd, package, context=context) == receipt
    return receipt, roll


@pytest.mark.parametrize("bless_pc", [False, True])
def test_cast_concentration_and_both_public_save_paths(timed_spell_world, tmp_path, bless_pc):
    sm, sid, package, context, cast = _warded_world(timed_spell_world, bless_pc=bless_pc)
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        (caster,) = (a for a in before.character_states if a.entity_id == cast.actor_id)
        assert caster.concentration_effect_id == "effect:blessed"
        assert {effect.target_id for effect in before.effect_states} == (
            {cast.actor_id, NPC} if bless_pc else {NPC}
        )
        source = f"effect:blessed@cast:blessed:{cast.actor_id}" if bless_pc else None
        standalone = save_command(sm, sid, package, context, command_id="ward.save")
        first, first_roll = _save_roll(sm, sid, package, context, standalone, bonus_source=source)
        mechanism = command(
            sm, sid, package, context, SetObjectState(A, DOOR, ObjectState.OPEN), "ward.gate"
        )
        second, second_roll = _save_roll(sm, sid, package, context, mechanism, bonus_source=source)
        assert second_roll["succeeded"] is False
        assert sm._world_facts.light_state(sid, CRED)["enabled"]
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert after.character_states == before.character_states
        assert after.effect_states == before.effect_states
        assert after.resource_state == before.resource_state
        expiry = submit(sm, sid, package, context, AdvanceSessionTime(60), "ward.expire")
        assert expiry.status == "committed", expiry
        current = sm.read_noncombat_snapshot(sid, package, context=context)
        assert not current.effect_states
        assert all(not a.concentration_effect_id for a in current.character_states)
        _save_roll(
            sm,
            sid,
            package,
            context,
            save_command(sm, sid, package, context, command_id="ward.expired"),
        )
    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=sm._rules.loader) as reopened:
        for cmd, receipt, roll in (
            (standalone, first, first_roll),
            (mechanism, second, second_roll),
        ):
            assert reopened.submit_command(cmd, package, context=context) == receipt
            assert reopened.get_command_rolls(sid, cmd.command_id, package, context=context)[
                "rolls"
            ] == [roll]


def test_public_sourced_condition_and_invalid_lineage(timed_spell_world, monkeypatch):
    sm, sid, package, context, cast = _warded_world(timed_spell_world)
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        actor = next(a for a in before.character_states if a.entity_id == cast.actor_id)
        source = _toxic_fog("sourced.fog", "0,0")
        op = AddEnvironmentSource(A, source)
        added = submit(sm, sid, package, context, op, "condition.add")
        assert added.status == "committed", added
        current = sm.read_noncombat_snapshot(sid, package, context=context)
        (condition,) = next(
            a.conditions for a in current.character_states if a.entity_id == actor.entity_id
        )
        assert condition.condition == "poisoned" and condition.source_effect_id
        assert condition.duration_rounds is None and condition.scope == "session"
        bonus = f"effect:blessed@cast:blessed:{cast.actor_id}"
        cmd = save_command(sm, sid, package, context, command_id="sourced.active")
        _, active = _save_roll(sm, sid, package, context, cmd, bonus_source=bonus)
        assert active["advantage"] == "normal"

        evaluate = sm._rules._evaluate_dnd

        async def missing_effect(request):
            snapshot = request.state_snapshot
            effects = tuple(e for e in snapshot.effect_states if e.id != condition.source_effect_id)
            assert len(effects) == len(snapshot.effect_states) - 1
            altered = snapshot.model_copy(update={"effect_states": effects})
            return await evaluate(request.model_copy(update={"state_snapshot": altered}))

        rng = sm.read_rng_context(sid, package, context=context)
        with monkeypatch.context() as patch:
            patch.setattr(sm._rules, "_evaluate_dnd", missing_effect)
            patch.setattr(RNGState, "restore", lambda *_: pytest.fail("missing source drew RNG"))
            cmd = save_command(sm, sid, package, context, command_id="sourced.missing")
            refused = sm.submit_command(cmd, package, context=context)
            assert refused.status == "rejected"
            assert refused.error_code == "save.conditions"
        assert sm.read_rng_context(sid, package, context=context) == rng

        removed = submit(
            sm,
            sid,
            package,
            context,
            RemoveEnvironmentSource(A, source.source_id),
            "condition.remove",
        )
        assert removed.status == "committed", removed
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert after.effect_states == before.effect_states
        assert after.character_states == before.character_states
        cmd = save_command(sm, sid, package, context, command_id="sourced.ended")
        _, ended = _save_roll(sm, sid, package, context, cmd, bonus_source=bonus)
        assert ended["advantage"] == active["advantage"]


def test_mechanism_late_fault_preserves_ward_and_world(timed_spell_world):
    sm, sid, package, context, cast = _warded_world(timed_spell_world)
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        cmd = command(
            sm, sid, package, context, SetObjectState(A, DOOR, ObjectState.OPEN), "ward.late"
        )
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_ward_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='ward.late' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(StateMachineError) as fault:
            sm.submit_command(cmd, package, context=context)
        assert fault.value.code == "storage_error"
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.get_command_receipt(sid, cmd.command_id, package, context=context) is None
        assert sm._world_facts.object(sid, DOOR)["state"] == "closed"
        assert not sm._world_facts.light_state(sid, CRED)["enabled"]
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 0
        sm._conn.execute("DROP TRIGGER fail_ward_receipt")
        _save_roll(
            sm,
            sid,
            package,
            context,
            cmd,
            bonus_source=f"effect:blessed@cast:blessed:{cast.actor_id}",
        )


def test_mechanism_rechecks_changed_ward_effect_after_preparation(timed_spell_world, monkeypatch):
    sm, sid, package, context, cast = _warded_world(timed_spell_world)
    with sm:
        gates = sm._world.objects.mechanisms
        evaluate = gates.evaluate_direct_saves
        snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        (bless,) = (
            effect for effect in snapshot.effect_states if effect.target_id == cast.actor_id
        )

        def lose_ward(*args, **kwargs):
            result = evaluate(*args, **kwargs)
            assert not sm._conn.in_transaction
            with sm._transaction(write=True):
                sm._characters.effects.remove_effect(sid, bless.target_id, bless.id, bless.origin)
            return result

        monkeypatch.setattr(gates, "evaluate_direct_saves", lose_ward)
        cmd = command(
            sm, sid, package, context, SetObjectState(A, DOOR, ObjectState.OPEN), "ward.changed"
        )
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "conflict"
        assert receipt.error_code == "version_conflict"
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm._world_facts.object(sid, DOOR)["state"] == "closed"
        assert not sm._world_facts.light_state(sid, CRED)["enabled"]
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 0


def test_cleric_ward_keeps_unreviewed_stock_out_of_ordinary_saves(timed_spell_world, monkeypatch):
    sm, sid, package, context, _ = _warded_world(timed_spell_world, unreviewed_stock=True)
    with sm:
        monkeypatch.setattr(
            RNGState, "restore", lambda *_: pytest.fail("unreviewed stock drew RNG")
        )
        cmd = save_command(sm, sid, package, context, command_id="ward.unreviewed")
        refused = sm.submit_command(cmd, package, context=context)
        assert refused.status == "rejected"
        assert refused.error_code == "save.actor"


def test_active_ward_save_reads_stay_local_with_unrelated_world_growth(timed_spell_world):
    counts = []
    for scale in (0, 25):
        sm, sid, package, context, _ = _warded_world(timed_spell_world, database=f"ward-{scale}.db")
        with sm:
            with sm._store.transaction(write=True):
                sm.scene_lifecycle.ensure_scene(sid, B, package)
            _grow_remote(sm, sid, scale)
            cmd = save_command(sm, sid, package, context, command_id="ward.locality")
            with measure(sm) as (work, _):
                receipt = sm.submit_command(cmd, package, context=context)
            assert receipt.status == "committed", receipt
            counts.append((work["sql_selects"], work["sql_rows"]))
    print(f"active ward save SQL selects and rows by remote scale: {counts}")
    assert counts[0] == counts[1]
