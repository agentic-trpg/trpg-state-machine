"""Public exploration casting with the selected slot and material instance."""

from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_contracts import ExplorationSpellCastPayload
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.spatial import GridTopology
from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.schema.common import ConsumptionBlock, ConsumptionTargetEntry
from dnd5e_srd_data.schema.spell import SpellMaterials
from item_support import ROPE, A, submit
from test_timed_spell_cast import PERCEPTION_SPELL, cast_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import PickupItem, StateMachineError, world_command_from_data

SECOND_ROPE = "object.spell_payment_spare_rope"


def _snapshot(session):
    sm, sid, package, context = session
    return sm.read_noncombat_snapshot(sid, package, context=context)


def _spell_balances(snapshot, actor_id):
    return {
        pool.pool_id: pool.current
        for pool in snapshot.resource_state.pools
        if pool.owner_id == actor_id and pool.kind in ("spell_slot", "pact_slot")
    }


def _warlock_slots(data):
    """Replace fixture balances with the bound level-three Warlock profile."""
    del data["rule_bindings"]["combat_foundation"]
    canonical = BundledAssetLoader()
    definitions = canonical.get_class("warlock"), canonical.get_subclass("fiend")
    grants = sorted(
        {
            grant.slug
            for definition in definitions
            for grant in definition.granted_features
            if grant.ref_type == "feature" and grant.level <= 3
        }
    )
    for binding in data["rule_bindings"].values():
        snapshot = binding["snapshot"]
        actor = snapshot["character_states"][0]
        actor.update(
            classes={"warlock": 3},
            character_level=3,
            granted_features=grants,
            spell_slots={},
            pact_slots={2: 2},
        )
        snapshot["resource_state"]["pools"] = [
            {
                "kind": "pact_slot",
                "owner_id": actor["entity_id"],
                "pool_id": "pact:2",
                "current": 2,
                "maximum": 2,
                "level": 2,
            }
        ]


@pytest.fixture
def material_world(timed_spell_world, request):
    """Author one familiar timed effect with a priced consumable ingredient."""

    def configure(data):
        scene = data["scenes"][0]
        rope = next(obj for obj in scene["objects"] if obj["object_id"] == ROPE)
        rope["initial_cell"] = "0,1"
        scene["objects"].append({**rope, "object_id": SECOND_ROPE})
        transfer = next(item for item in data["item_transfers"] if item["instance_id"] == ROPE)
        data["item_transfers"].append({**transfer, "instance_id": SECOND_ROPE})

    def material_spell(spell):
        (activity,) = spell.activities
        consumption = ConsumptionBlock(
            targets=[ConsumptionTargetEntry(type="material", target="rope")]
        )
        return spell.model_copy(
            update={
                "materials": SpellMaterials(value="a rope worth 1 GP", consumed=True, cost=1),
                "activities": [activity.model_copy(update={"consumption": consumption})],
            }
        )

    session = timed_spell_world(
        material=False,
        spell_id=PERCEPTION_SPELL,
        configure=configure,
        configure_spell=material_spell,
    )
    sm, sid, package, context = session
    for instance in (SECOND_ROPE, ROPE):
        if instance == getattr(request, "param", None):
            continue
        receipt = submit(
            sm, sid, package, context, PickupItem(instance, A, "0,1"), f"pick.{instance}"
        )
        assert receipt.status == "committed", receipt
    return session


def _material_command(session, instance, key):
    command = cast_command(session, spell_id=PERCEPTION_SPELL, command_id=key)
    return replace(
        command,
        payload=ExplorationSpellCastPayload.model_validate(
            {**command.payload.model_dump(), "material_instance_id": instance}
        ),
    )


def test_public_longstrider_pays_the_requested_higher_slot(timed_spell_world):
    session = timed_spell_world()
    sm, _, package, context = session
    with sm:
        command = cast_command(session, slot_level=2)
        before = _snapshot(session)
        balance = _spell_balances(before, command.actor_id)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = _snapshot(session)
        assert _spell_balances(after, command.actor_id) == {
            **balance,
            "spell:2": balance["spell:2"] - 1,
        }
        (created,) = [
            effect for effect in after.effect_states if effect not in before.effect_states
        ]
        assert (created.id, created.target_id) == ("effect:longstrider", command.actor_id)
        assert sm.submit_command(command, package, context=context) == receipt
        assert _snapshot(session) == after


def test_public_darkness_pays_the_warlocks_pact_slot(timed_spell_world):
    session = timed_spell_world(
        caster_class="warlock",
        caster_subclass="fiend",
        additional_spells=("darkness",),
        configure=_warlock_slots,
    )
    sm, sid, package, context = session
    with sm:
        state = sm.read_session(sid, package, context=context)
        command = world_command_from_data(
            {
                "schema_version": "sm-command/1",
                "session_id": sid,
                "command_id": "cast.pact-darkness",
                "actor_id": state.pc_actor_id,
                "command_type": "world.cast_spell",
                "expected_world_version": state.world_version,
                "payload": {
                    "kind": "world.cast_spell",
                    "spell_id": "darkness",
                    "spell_source_id": "class:warlock",
                    "slot_level": 2,
                    "target": {"kind": "point", "cell_id": "2,0"},
                },
            }
        )
        before = _snapshot(session)
        assert _spell_balances(before, command.actor_id) == {"pact:2": 2}
        assert not GridTopology(before.scene_state.grid).magical_darkness_on_cell("2,0")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = _snapshot(session)
        assert _spell_balances(after, command.actor_id) == {"pact:2": 1}
        assert GridTopology(after.scene_state.grid).magical_darkness_on_cell("2,0")
        assert sm.submit_command(command, package, context=context) == receipt
        assert _snapshot(session) == after


def test_spell_consumes_only_selected_material_instance_and_rolls_back(material_world):
    sm, sid, package, context = material_world
    with sm:
        before = _snapshot(material_world)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        command = _material_command(material_world, ROPE, "cast.consume-material")
        balance = _spell_balances(before, command.actor_id)
        owned = {entry.instance_id: entry for entry in before.inventory_state.entries}
        assert owned[ROPE].item_slug == owned[SECOND_ROPE].item_slug == "rope"

        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_material_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='cast.consume-material' "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as rejected:
            sm.submit_command(command, package, context=context)
        assert rejected.value.code == "storage_error"
        assert _snapshot(material_world) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_material_receipt")

        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = _snapshot(material_world)
        del owned[ROPE]
        assert {entry.instance_id: entry for entry in after.inventory_state.entries} == owned
        assert _spell_balances(after, command.actor_id) == {
            **balance,
            "spell:1": balance["spell:1"] - 1,
        }
        (created,) = [
            effect for effect in after.effect_states if effect not in before.effect_states
        ]
        assert created.target_id == command.actor_id
        assert sm.submit_command(command, package, context=context) == receipt
        assert _snapshot(material_world) == after


def test_material_spell_refuses_forged_duration_and_keeps_all_facts(material_world, monkeypatch):
    sm, sid, package, context = material_world
    with sm:
        before = _snapshot(material_world)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        command = _material_command(material_world, ROPE, "cast.forged-duration")
        evaluate = sm._rules._evaluate_dnd

        async def lengthen_effect(request):
            """Lengthen the proposed effect and its matching event by the same amount."""
            result = await evaluate(request)
            spend, material, create = result.state_delta.operations
            intent, cast, applied = result.proposed_events
            effect = applied.effect.model_copy(
                update={"duration": applied.effect.duration.model_copy(update={"seconds": 120})}
            )
            create = create.model_copy(
                update={"value": create.value.model_copy(update={"effect": effect_state(effect)})}
            )
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(
                        update={"operations": (spend, material, create)}
                    ),
                    "proposed_events": (
                        intent,
                        cast,
                        applied.model_copy(update={"effect": effect}),
                    ),
                }
            )

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", lengthen_effect)
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(command, package, context=context)
        assert exc.value.code == "invalid_rule_result"
        assert _snapshot(material_world) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None


@pytest.mark.parametrize("material_world", [SECOND_ROPE], indirect=True)
def test_unavailable_material_cannot_use_same_name_owned_instance(material_world):
    sm, sid, package, context = material_world
    with sm:
        before = _snapshot(material_world)
        rng = sm.read_rng_context(sid, package, context=context)
        for instance in (SECOND_ROPE, "object.missing_rope"):
            command = _material_command(material_world, instance, f"cast.unavailable.{instance}")
            receipt = sm.submit_command(command, package, context=context)
            assert (receipt.status, receipt.error_code) == ("rejected", "spell.material"), receipt
            assert receipt.event_seqs == ()
            assert _snapshot(material_world) == before
            assert sm.read_rng_context(sid, package, context=context) == rng
