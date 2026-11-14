"""Public transfers protect only actual instance-bound equipment on snapshot /19."""

import json
from pathlib import Path
from runpy import run_path

import pytest
from dnd_support import initialize
from item_support import DAGGER, ROPE, item_data, item_loader
from lock_support import CRED, TWIN, A, lock_loader
from spatial_support import create
from test_container_admission import assert_admission, authority
from test_item_transfers import ready
from test_item_transfers import setup as item_setup
from test_scoped_runtime_validation import grow, measure
from test_world_containers import BOX, container_data
from world_support import command, setup

from trpg_state_machine import (
    DropItem,
    PickupItem,
    PutItem,
    StateMachine,
    StateMachineError,
    TakeItem,
    TypedCommand,
)
from trpg_state_machine.package import _validate

author_fixture = run_path(
    str(Path(__file__).resolve().parents[1] / "examples" / "real_cross_phase.py")
)["author_fixture"]


def combat_equipment_data():
    """Supply a held rapier and paired armor/shield instances for a /19 encounter."""
    data = item_data()
    owner = data["pc_templates"][0]["pc_template_id"]
    data["actors"][0]["initial_cell"] = "1,0"
    data["rule_bindings"]["combat_foundation"]["snapshot"]["combat_state"]["actor_zone"][
        "npc.caretaker"
    ] = "1,0"
    next(t for t in data["item_transfers"] if t["instance_id"] == DAGGER).update(
        item_slug="rapier", initial_owner_id=owner
    )
    for slug, name in (("leather-armor", "armor"), ("shield", "shield")):
        data["pc_templates"][0]["character"]["inventory"].append(slug)
        for suffix in ("equipped", "spare"):
            iid = f"object.{name}_{suffix}"
            data["scenes"][0]["objects"].append(
                {
                    "object_id": iid,
                    "state": None,
                    "movable": False,
                    "blocks_passage": False,
                    "initial_cell": "2,1",
                }
            )
            data["item_transfers"].append(
                {
                    "instance_id": iid,
                    "item_slug": slug,
                    "initial_owner_id": owner,
                    "quantity": 1,
                    "charges_remaining_per_unit": 0,
                    "policy": "ordinary_public",
                }
            )
    data["manifest"]["ruleset_binding"]["evaluator_version"] = "dnd5e-evaluation/39"
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        snapshot["snapshot_schema_version"] = "engine-snapshot/19"
        next(e for e in snapshot["inventory_state"]["entries"] if e["item_slug"] == "rapier")[
            "instance_id"
        ] = DAGGER
        actor = snapshot["character_states"][0]
        actor.update(
            weapon_in_hands="rapier",
            weapon_grip="one_handed",
            held_weapon_instance_id=DAGGER,
            worn_armor="light",
            worn_armor_instance_id="object.armor_equipped",
            shield_equipped=True,
            other_hand_occupied=True,
            equipped_shield_instance_id="object.shield_equipped",
        )
        actor["carried_item_slugs"].extend(("leather-armor", "shield"))
        for iid, slug in (
            ("object.armor_equipped", "leather-armor"),
            ("object.armor_spare", "leather-armor"),
            ("object.shield_equipped", "shield"),
            ("object.shield_spare", "shield"),
        ):
            snapshot["inventory_state"]["entries"].append(
                {
                    "instance_id": iid,
                    "owner_id": owner,
                    "item_slug": slug,
                    "quantity": 1,
                    "charges_remaining_per_unit": 0,
                    "accessible": True,
                }
            )
    return data


def ingress_equipment_data(kind, *, combat_only=False):
    """Use the ordinary /2 ingress inventory without portable declarations."""
    data = author_fixture(item_loader())
    owner = data["pc_templates"][0]["pc_template_id"]
    slug = {"armor": "leather-armor", "shield": "shield", "offhand": "rope"}[kind]
    identity = f"inventory.{kind}_bound"
    data["pc_templates"][0]["character"]["inventory"].append(slug)
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        actor = snapshot["character_states"][0]
        actor["carried_item_slugs"].append(slug)
        if kind == "armor":
            actor.update(worn_armor="light", worn_armor_instance_id=identity)
        elif kind == "shield":
            actor.update(
                shield_equipped=True, other_hand_occupied=True, equipped_shield_instance_id=identity
            )
        else:
            actor.update(other_hand_occupied=True, offhand_item_instance_id=identity)
        snapshot["inventory_state"]["entries"].append(
            {
                "instance_id": identity,
                "owner_id": owner,
                "item_slug": slug,
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
        )
    if combat_only:
        del data["rule_bindings"]["noncombat_foundation"]
        combat = data["rule_bindings"]["combat_foundation"]["snapshot"]
        npc = next(a for a in combat["character_states"] if a["entity_id"] == "npc.caretaker")
        npc["carried_item_slugs"].append("rope")
        combat["inventory_state"]["entries"].append(
            {
                "instance_id": "inventory.npc_rope",
                "owner_id": "npc.caretaker",
                "item_slug": "rope",
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
        )
    return data


def equipment_data(kind):
    """Build two same-type owned instances with only the first equipped."""
    data = container_data()
    owner = data["pc_templates"][0]["pc_template_id"]
    slug = {"armor": "leather-armor", "shield": "shield", "offhand": "rope"}[kind]
    for iid in (CRED, TWIN):
        transfer = next(t for t in data["item_transfers"] if t["instance_id"] == iid)
        transfer.update(item_slug=slug, initial_owner_id=owner)
    data["pc_templates"][0]["character"]["inventory"].append(slug)
    data["manifest"]["ruleset_binding"]["evaluator_version"] = "dnd5e-evaluation/39"
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        snapshot["snapshot_schema_version"] = "engine-snapshot/19"
        actor = snapshot["character_states"][0]
        actor.update(weapon_in_hands=None, weapon_grip="none")
        actor["carried_item_slugs"].append(slug)
        if kind == "armor":
            actor.update(worn_armor="light", worn_armor_instance_id=CRED)
        elif kind == "shield":
            actor.update(
                shield_equipped=True, other_hand_occupied=True, equipped_shield_instance_id=CRED
            )
        else:
            actor.update(other_hand_occupied=True, offhand_item_instance_id=CRED)
        for iid in (CRED, TWIN):
            snapshot["inventory_state"]["entries"].append(
                {
                    "instance_id": iid,
                    "owner_id": owner,
                    "item_slug": slug,
                    "quantity": 1,
                    "charges_remaining_per_unit": 0,
                    "accessible": True,
                }
            )
    return data


@pytest.mark.parametrize("kind", ["armor", "shield", "offhand"])
@pytest.mark.parametrize("operation", ["drop", "put"])
def test_bound_instance_rejected_while_same_type_spare_moves(tmp_path, context, kind, operation):
    """Query and command share the exact bound-instance refusal and preserve facts."""
    sm, sid, package, _ = setup(tmp_path, context, equipment_data(kind))
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        equipped = next(a for a in snapshot.character_states if a.entity_id == actor)
        assert (
            getattr(
                equipped,
                {
                    "armor": "worn_armor_instance_id",
                    "shield": "equipped_shield_instance_id",
                    "offhand": "offhand_item_instance_id",
                }[kind],
            )
            == CRED
        )
        raw = json.loads(
            sm._conn.execute(
                "SELECT state_json FROM mechanical_actors WHERE session_id=? AND actor_id=?",
                (sid, actor),
            ).fetchone()[0]
        )
        assert not {"worn_armor", "shield_equipped", "other_hand_occupied"} & raw.keys()
        assert (
            set(
                (
                    "worn_armor_instance_id",
                    "equipped_shield_instance_id",
                    "offhand_item_instance_id",
                )
            )
            <= raw.keys()
        )
        bound = DropItem(CRED, A, "0,0") if operation == "drop" else PutItem(A, CRED, BOX, "0,0")
        spare = DropItem(TWIN, A, "0,0") if operation == "drop" else PutItem(A, TWIN, BOX, "0,0")
        assert_admission(sm, sid, package, context, bound, "bound", "equipped_item_unsupported")
        assert_admission(sm, sid, package, context, spare, "spare")
        if operation == "put":
            assert_admission(sm, sid, package, context, TakeItem(A, TWIN, BOX, "0,0"), "take")


def test_equipment_binding_survives_save_and_reopen(tmp_path, context):
    """A reopened current-schema Session resolves the same shield instance."""
    sm, sid, package, db = setup(tmp_path, context, equipment_data("shield"))
    with sm:
        sm.save_session(sid, package, context=context)
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 45
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        restored.resume_session(sid, package, context=context)
        actor = restored.read_session(sid, package, context=context).pc_actor_id
        assert (
            next(
                a
                for a in restored.read_noncombat_snapshot(
                    sid, package, context=context
                ).character_states
                if a.entity_id == actor
            ).equipped_shield_instance_id
            == CRED
        )
        before = authority(restored)
        assert_admission(
            restored,
            sid,
            package,
            context,
            DropItem(CRED, A, "0,0"),
            "reopen.bound",
            "equipped_item_unsupported",
        )
        assert authority(restored) == before


def test_wrong_initial_equipment_kind_rolls_back_creation(tmp_path, context):
    """An ID that names the wrong materialized kind cannot enter live actor facts."""
    data = equipment_data("armor")
    next(t for t in data["item_transfers"] if t["instance_id"] == CRED)["item_slug"] = "rope"
    data["pc_templates"][0]["character"]["inventory"].append("rope")
    for foundation in data["rule_bindings"].values():
        foundation["snapshot"]["character_states"][0]["carried_item_slugs"].append("rope")
        next(
            e
            for e in foundation["snapshot"]["inventory_state"]["entries"]
            if e["instance_id"] == CRED
        )["item_slug"] = "rope"
    package = _validate(data)
    with StateMachine(tmp_path / "bad.db", dnd_loader=lock_loader()) as sm:
        with pytest.raises(StateMachineError, match="Worn armor identity differs"):
            create(sm, package, context)
        assert sm._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["armor", "shield", "offhand"])
@pytest.mark.parametrize("combat_only", [False, True])
def test_ingress_inventory_equipment_resolves_after_materialization(
    tmp_path, context, kind, combat_only
):
    """The standard /2 inventory path can bind gear before and at combat start."""
    package = _validate(ingress_equipment_data(kind, combat_only=combat_only))
    with StateMachine(tmp_path / "ingress.db", dnd_loader=item_loader()) as sm:
        sid = create(sm, package, context)
        if combat_only:
            assert initialize(sm, sid, package, context).status == "committed"
            snapshot = sm.read_combat_snapshot(sid, package, context=context)
            assert "inventory.npc_rope" in {e.instance_id for e in snapshot.inventory_state.entries}
        else:
            snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        pc = next(a for a in snapshot.character_states if a.entity_id == actor)
        field = {
            "armor": "worn_armor_instance_id",
            "shield": "equipped_shield_instance_id",
            "offhand": "offhand_item_instance_id",
        }[kind]
        assert getattr(pc, field) == f"inventory.{kind}_bound"
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 45


@pytest.mark.parametrize("fault", ["missing_actor", "active_effect"])
def test_pickup_preserves_typed_actor_and_effect_boundary(tmp_path, context, fault):
    """Preview and command refuse Pickup when its original actor guard fails."""
    sm, sid, package, _ = item_setup(tmp_path, context, item_data())
    with sm:
        ready(sm, sid, package, context)
        state = sm.read_session(sid, package, context=context)
        actor, version = state.pc_actor_id, state.world_version
        if fault == "missing_actor":
            sm._conn.execute(
                "DELETE FROM mechanical_actors WHERE session_id=? AND actor_id=?", (sid, actor)
            )
        else:
            effect = {
                "id": "effect.test",
                "name": "active pickup guard",
                "origin": "test",
                "target_id": actor,
                "disabled": False,
                "transfer": False,
                "duration": {
                    k: None for k in ("rounds", "turns", "seconds", "start_round", "start_turn")
                },
                "changes": [],
                "action_policy": None,
                "statuses": [],
                "flags": {},
                "lifecycle": None,
                "end_effects": [],
            }
            sm._conn.execute(
                "UPDATE mechanical_components SET effects_json=? WHERE session_id=?",
                (json.dumps([effect]), sid),
            )
        before = authority(sm)
        operation = PickupItem(DAGGER, A, "2,1")
        preview = sm.query_world_action(sid, actor, operation, package, context=context)
        assert (preview.status, preview.reason) == ("unavailable", "unsupported_actor_state")
        assert authority(sm) == before
        cmd = TypedCommand(sid, "pickup.guard", actor, "world.pickup_item", operation, version)
        receipt = sm.submit_command(cmd, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "unsupported_actor_state")
        assert authority(sm) == before


def test_combat_availability_uses_bound_equipment_and_spares(tmp_path, context):
    """The public availability path passes the /19 armor, shield and weapon IDs to RE."""
    from dnd5e_engine.evaluation_contracts import CombatIntentPayload

    package = _validate(combat_equipment_data())
    with StateMachine(tmp_path / "combat.db", dnd_loader=item_loader()) as sm:
        sid = create(sm, package, context)
        assert initialize(sm, sid, package, context).status == "committed"
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        fixed = sm.read_combat_snapshot(sid, package, context=context)
        pc = next(a for a in fixed.character_states if a.entity_id == actor)
        assert pc.worn_armor_instance_id == "object.armor_equipped"
        assert pc.equipped_shield_instance_id == "object.shield_equipped"
        assert {"object.armor_spare", "object.shield_spare"} <= {
            e.instance_id for e in fixed.inventory_state.entries
        }
        payload = CombatIntentPayload(
            intent_type="attack",
            source_id=actor,
            target_id="npc.caretaker",
            weapon_instance_id=DAGGER,
        )
        result = sm.query_action_availability(sid, actor, payload, package, context=context)
        assert result.status == "available", result
        version = sm.read_session(sid, package, context=context).world_version
        attack = TypedCommand(sid, "equipment.attack", actor, "combat.intent", payload, version)
        assert sm.submit_command(attack, package, context=context).status == "committed"


def test_combat_snapshot_shares_inventory_projection(tmp_path, context, monkeypatch):
    """The public read derives carried slugs and full inventory from one item bundle."""
    data = combat_equipment_data()
    next(item for item in data["item_transfers"] if item["instance_id"] == ROPE)[
        "initial_owner_id"
    ] = "npc.caretaker"
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        npc = next(
            actor for actor in snapshot["character_states"] if actor["entity_id"] == "npc.caretaker"
        )
        npc["carried_item_slugs"].append("rope")
        snapshot["inventory_state"]["entries"].append(
            {
                "instance_id": ROPE,
                "owner_id": "npc.caretaker",
                "item_slug": "rope",
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
        )
    package = _validate(data)
    with StateMachine(tmp_path / "shared.db", dnd_loader=item_loader()) as sm:
        sid = create(sm, package, context)
        assert initialize(sm, sid, package, context).status == "committed"
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        original = sm._world_facts.portable_inventory
        project_actors = sm._characters.combat_rule_actors
        calls = []
        actor_projections = []

        def counted(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        def counted_actors(*args):
            actor_projections.append(args)
            return project_actors(*args)

        monkeypatch.setattr(sm._world_facts, "portable_inventory", counted)
        monkeypatch.setattr(sm._characters, "combat_rule_actors", counted_actors)
        snapshot = sm.read_combat_snapshot(sid, package, context=context)
        assert len(actor_projections) == 1
        assert len(calls) <= 1
        if calls:
            assert calls[0][1]["owner_ids"] == set(snapshot.combat_state.initiative_ids)
        _, entries = original(sid, owner_ids=set(snapshot.combat_state.initiative_ids))
        assert [
            entry.model_dump(mode="json") for entry in snapshot.inventory_state.entries
        ] == list(entries.values())
        for projected in snapshot.character_states:
            assert projected.carried_item_slugs == tuple(
                dict.fromkeys(
                    entry["item_slug"]
                    for entry in entries.values()
                    if entry["owner_id"] == projected.entity_id
                )
            )
        pc = next(value for value in snapshot.character_states if value.entity_id == actor)
        assert pc.carried_item_slugs.count("shield") == 1
        npc = next(value for value in snapshot.character_states if value.entity_id != actor)
        assert "rope" in npc.carried_item_slugs


def test_bound_transfer_query_cost_is_independent_of_unrelated_items(
    tmp_path, context, monkeypatch
):
    """A fixed bound target reads the same SQL rows as unrelated instances grow."""
    measured = []
    for growth in (0, 80):
        root = tmp_path / str(growth)
        root.mkdir()
        sm, sid, package, _ = setup(root, context, equipment_data("shield"))
        with sm:
            grow(sm, sid, growth)
            actor = sm.read_session(sid, package, context=context).pc_actor_id
            operation = PutItem(A, CRED, BOX, "0,0")
            # Warm immutable map/instance fields before measuring the ordinary query.
            assert sm.query_world_action(
                sid, actor, operation, package, context=context
            ).reason == ("equipped_item_unsupported")
            parsed = 0
            original = sm._store.memory.json

            def counted(value, source=original):
                nonlocal parsed
                parsed += 1
                return source(value)

            with monkeypatch.context() as patch:
                patch.setattr(sm._store.memory, "json", counted)
                with measure(sm) as (counts, _):
                    result = sm.query_world_action(sid, actor, operation, package, context=context)
            assert result.reason == "equipped_item_unsupported"
            preview = (counts["sql_selects"], counts["sql_rows"], parsed)
            spare = command(sm, sid, package, context, PutItem(A, TWIN, BOX, "0,0"), "spare")
            parsed = 0
            with monkeypatch.context() as patch:
                patch.setattr(sm._store.memory, "json", counted)
                with measure(sm) as (counts, _):
                    receipt = sm.submit_command(spare, package, context=context)
            assert receipt.status == "committed"
            measured.append((preview, (counts["sql_selects"], counts["sql_rows"], parsed)))
    print("EQUIPMENT_LOCALITY", measured)
    assert measured[0] == measured[1]
