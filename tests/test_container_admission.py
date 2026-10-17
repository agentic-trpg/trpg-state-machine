"""Scoped containment admission through public queries and commands."""

import json
from copy import deepcopy

import pytest
from lock_support import CRED, NPC, TWIN, A
from test_lockable_passages import acquire
from test_world_containers import BAG, BOX, container_data
from world_support import command, setup, submit

from trpg_state_machine import (
    ObjectState,
    PickupItem,
    PutItem,
    SetObjectState,
    StateMachineError,
    TakeItem,
)


def profile(data, oid):
    return next(p for p in data["world_objects"] if p["object_id"] == oid)


def config(data, oid):
    return profile(data, oid)["capabilities"][1]


def third_container(data):
    profile(data, TWIN).update(
        weight_lb=2, capabilities=deepcopy(profile(data, BAG)["capabilities"])
    )
    next(o for o in data["scenes"][0]["objects"] if o["object_id"] == TWIN).update(
        state="open", initial_cell="1,0"
    )


def authority(sm):
    """All durable tables except the permitted rejection receipt."""
    tables = [r[0] for r in sm._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {
        t: tuple(tuple(r) for r in sm._conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid'))
        for t in tables
        if t != "command_receipts"
    }


def assert_admission(sm, sid, p, context, op, key, error=None, actor=None):
    actor = actor or sm.read_session(sid, p, context=context).pc_actor_id
    before = authority(sm)
    query = sm.query_world_action(sid, actor, op, p, context=context)
    assert authority(sm) == before
    cmd = command(sm, sid, p, context, op, key, actor)
    receipt = sm.submit_command(cmd, p, context=context)
    assert receipt.error_code == error, receipt
    assert query.status == ("unavailable" if error else "available")
    if error:
        assert query.reason == error
        assert receipt.status == "rejected"
        assert authority(sm) == before
    else:
        assert receipt.status == "committed"
        assert authority(sm)["rng_states"] == before["rng_states"]
    replay_state = authority(sm)
    assert sm.submit_command(cmd, p, context=context) == receipt
    assert authority(sm) == replay_state
    return receipt


@pytest.mark.parametrize("problem", ["unknown", "overweight"])
def test_unrelated_container_does_not_block_put_or_take(tmp_path, context, problem):
    d = container_data()
    config(d, BAG)["capacity_weight_lb"] = "unknown" if problem == "unknown" else 0
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        acquire(sm, sid, p, context)
        if problem == "overweight":
            # Historical local corruption; no command needs to traverse this branch.
            sm._conn.execute(
                "UPDATE objects SET container_id=?,scene_id=NULL,cell=NULL WHERE object_id=?",
                (BAG, TWIN),
            )
        assert_admission(sm, sid, p, context, PutItem(A, CRED, BOX, "0,0"), "put")
        assert_admission(sm, sid, p, context, TakeItem(A, CRED, BOX, "0,0"), "take")


@pytest.mark.parametrize("move_subtree", [False, True])
@pytest.mark.parametrize("legacy_limit", [1, 2, 3, 8])
def test_third_container_rejected_even_when_moving_loaded_container(
    tmp_path, context, move_subtree, legacy_limit
):
    d = container_data()
    third_container(d)
    for oid in (BOX, BAG, TWIN):
        config(d, oid)["nesting_limit"] = legacy_limit
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        acquire(sm, sid, p, context)
        assert_admission(sm, sid, p, context, PutItem(A, BAG, BOX, "0,0"), "nest")
        assert_admission(sm, sid, p, context, PutItem(A, CRED, BAG, "0,0"), "ordinary")
        op = PutItem(A, BOX, TWIN, "0,0") if move_subtree else PutItem(A, TWIN, BAG, "0,0")
        assert_admission(sm, sid, p, context, op, "third", "container_nesting_limit")


@pytest.mark.parametrize("which", ["target", "ancestor", "source", "source_ancestor"])
@pytest.mark.parametrize("dimension", ["capacity_weight_lb", "capacity_volume_cu_ft"])
def test_relevant_unknown_capacity_rejects(tmp_path, context, which, dimension):
    d = container_data()
    oid = BOX if "ancestor" in which else BAG
    config(d, oid)[dimension] = "unknown"
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        acquire(sm, sid, p, context)
        # Seed legacy containment with unknown measurements, without changing the
        # bound Package. All tested admission still uses public commands.
        with sm._world._transaction(write=True):
            sm._conn.execute(
                "UPDATE objects SET container_id=?,scene_id=NULL,cell=NULL WHERE object_id=?",
                (BOX, BAG),
            )
            if which.startswith("source"):
                sm._conn.execute(
                    "UPDATE objects SET container_id=?,carrier_id=NULL WHERE object_id=?",
                    (BAG, CRED),
                )
            sm._characters.sync_world_inventory(sid)
        op = (
            TakeItem(A, CRED, BAG, "0,0")
            if which.startswith("source")
            else PutItem(A, CRED, BAG, "0,0")
        )
        assert_admission(sm, sid, p, context, op, "unknown", "unknown_container_capacity")


@pytest.mark.parametrize("dimension", ["capacity_weight_lb", "capacity_volume_cu_ft"])
@pytest.mark.parametrize("nested", [False, True])
def test_capacity_includes_ancestor_other_contents(tmp_path, context, dimension, nested):
    d = container_data()
    config(d, BOX)[dimension] = 2 if dimension == "capacity_weight_lb" else 1
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        acquire(sm, sid, p, context)
        if nested:
            assert_admission(sm, sid, p, context, PutItem(A, BAG, BOX, "0,0"), "nest")
            op = PutItem(A, CRED, BAG, "0,0")
            if dimension == "capacity_volume_cu_ft":
                # External volume of a bag stays fixed; filling it does not expand the box.
                assert_admission(sm, sid, p, context, op, "fixed.volume")
                return
        else:
            assert_admission(sm, sid, p, context, PutItem(A, BAG, BOX, "0,0"), "existing")
            op = PutItem(A, CRED, BOX, "0,0")
        assert_admission(sm, sid, p, context, op, "over", "container_capacity_exceeded")


def test_loaded_container_weight_and_two_levels_allow_ordinary_contents(tmp_path, context):
    d = container_data()
    config(d, BOX)["capacity_weight_lb"] = 2
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        acquire(sm, sid, p, context)
        assert_admission(sm, sid, p, context, PutItem(A, CRED, BAG, "0,0"), "fill")
        assert_admission(
            sm, sid, p, context, PutItem(A, BAG, BOX, "0,0"), "heavy", "container_capacity_exceeded"
        )
        assert_admission(sm, sid, p, context, TakeItem(A, CRED, BAG, "0,0"), "empty")
        assert_admission(sm, sid, p, context, PutItem(A, BAG, BOX, "0,0"), "nest")


@pytest.mark.parametrize("indirect", [False, True])
def test_cycles_are_rejected_atomically(tmp_path, context, indirect):
    sm, sid, p, _ = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, p, context)
        if indirect:
            assert_admission(sm, sid, p, context, PutItem(A, BAG, BOX, "0,0"), "nest")
        assert_admission(
            sm,
            sid,
            p,
            context,
            PutItem(A, BOX, BAG if indirect else BOX, "0,0"),
            "cycle",
            "container_cycle",
        )


def test_outer_access_query_and_execution_recover_after_open(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, p, context)
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        assert_admission(sm, sid, p, context, PutItem(A, CRED, BAG, "0,0"), "fill")
        assert_admission(sm, sid, p, context, PutItem(A, BAG, BOX, "0,0"), "nest")
        assert (
            submit(sm, sid, p, context, SetObjectState(A, BOX, ObjectState.CLOSED), "close").status
            == "committed"
        )
        before = authority(sm)
        assert sm.get_object(sid, actor, BOX, p, context=context)["contents"] is None
        with pytest.raises(StateMachineError, match="No authorized local object"):
            sm.get_object(sid, actor, BAG, p, context=context)
        visible = sm.list_scene_objects(sid, actor, p, context=context)
        assert not {BAG, CRED} & {x["object_id"] for x in visible}
        assert authority(sm) == before
        assert_admission(
            sm, sid, p, context, TakeItem(A, CRED, BAG, "0,0"), "closed", "container_inaccessible"
        )
        assert (
            submit(sm, sid, p, context, SetObjectState(A, BOX, ObjectState.OPEN), "open").status
            == "committed"
        )
        assert sm.get_object(sid, actor, BAG, p, context=context)["contents"] == (CRED,)
        assert_admission(sm, sid, p, context, TakeItem(A, CRED, BAG, "0,0"), "take")


@pytest.mark.parametrize("owned", [False, True])
def test_other_hand_ambiguity_only_restricts_owned_items(tmp_path, context, owned):
    d = container_data()
    state = d["rule_bindings"]["noncombat_foundation"]["snapshot"]["character_states"][0]
    state["other_hand_occupied"] = True
    del d["rule_bindings"]["combat_foundation"]
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        acquire(sm, sid, p, context)
        # CRED is owned after acquire; BAG remains a separate ground instance.
        op = PutItem(A, CRED if owned else BAG, BOX, "0,0")
        assert_admission(
            sm, sid, p, context, op, "hand", "equipped_item_unsupported" if owned else None
        )


@pytest.mark.parametrize("related", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
def test_effect_guards_only_active_operating_actor(tmp_path, context, related, disabled):
    sm, sid, p, _ = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, p, context)
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        effect = {
            "id": "effect.test",
            "name": "explicit guard fixture",
            "origin": "test",
            "target_id": actor if related else NPC,
            "disabled": disabled,
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
        assert_admission(
            sm,
            sid,
            p,
            context,
            PutItem(A, CRED, BOX, "0,0"),
            "effect",
            "unsupported_actor_state" if related and not disabled else None,
        )


@pytest.mark.parametrize("stage", ["projection", "after_event"])
def test_transfer_fault_rolls_back_all_facts(tmp_path, context, monkeypatch, stage):
    sm, sid, p, _ = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, p, context)
        if stage == "projection":
            original = sm._characters.project_item_transfer

            def fail(*args, **kwargs):
                original(*args, **kwargs)
                raise StateMachineError("injected_failure", "after projection")

            monkeypatch.setattr(sm._characters, "project_item_transfer", fail)
        else:
            original = sm._world._capture_event

            def fail(*args, **kwargs):
                result = original(*args, **kwargs)
                if kwargs.get("phase") == "world.after_container_transfer":
                    raise StateMachineError("injected_failure", "after transfer evidence")
                return result

            monkeypatch.setattr(sm._world, "_capture_event", fail)
        # Query is preflight only; injected storage failures happen during execution.
        before = authority(sm)
        cmd = command(sm, sid, p, context, PutItem(A, CRED, BOX, "0,0"), "fault")
        receipt = sm.submit_command(cmd, p, context=context)
        assert receipt.error_code == "injected_failure"
        assert authority(sm) == before
        assert sm.submit_command(cmd, p, context=context) == receipt


def test_authorized_npc_put_take_uses_same_admission(tmp_path, context):
    from item_support import navigate

    d = container_data()
    config(d, BAG)["capacity_weight_lb"] = "unknown"
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        sm.grant_npc_access(sid, NPC, "agent:containers", p, context=context)
        acquire(sm, sid, p, context)
        navigate(sm, sid, p, context, (A, "1,1"), "near", NPC)
        assert (
            submit(sm, sid, p, context, PickupItem(BOX, A, "1,0"), "npc.pick", NPC).status
            == "committed"
        )
        assert_admission(sm, sid, p, context, PutItem(A, TWIN, BOX, "1,1"), "npc.put", actor=NPC)
        assert_admission(sm, sid, p, context, TakeItem(A, TWIN, BOX, "1,1"), "npc.take", actor=NPC)


@pytest.mark.parametrize("descendant", [False, True])
@pytest.mark.parametrize("related", [False, True])
def test_casting_binding_tracks_exact_moved_instance_and_subtree(
    tmp_path, context, descendant, related
):
    sm, sid, p, _ = setup(tmp_path, context, container_data())
    with sm:
        acquire(sm, sid, p, context)
        acquire(sm, sid, p, context, iid=TWIN, key="twin")
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        if descendant:
            assert_admission(sm, sid, p, context, PutItem(A, CRED, BAG, "0,0"), "fill")
        casting = {
            "actors": [
                {
                    "actor_id": actor,
                    "verbal_available": True,
                    "sources": [
                        {
                            "source_id": "class.wizard",
                            "class_slug": "wizard",
                            "ability": "int",
                            "known_spells": ["hold-person"],
                            "prepared_spells": ["hold-person"],
                        }
                    ],
                    "materials": [
                        {
                            "spell_slug": "hold-person",
                            "source_id": "class.wizard",
                            "inventory_instance_id": CRED if related else TWIN,
                            "available": True,
                        }
                    ],
                }
            ]
        }
        sm._conn.execute(
            "UPDATE mechanical_components SET casting_json=? WHERE session_id=?",
            (json.dumps(casting), sid),
        )
        op = PutItem(A, BAG if descendant else CRED, BOX, "0,0")
        assert_admission(
            sm, sid, p, context, op, "casting", "unsupported_actor_activity" if related else None
        )


@pytest.mark.parametrize("owned", [False, True])
def test_equipped_slug_does_not_bind_a_separate_ground_instance(tmp_path, context, owned):
    d = container_data()
    # The PC already wields a rapier; a separate scenario instance shares its slug.
    next(i for i in d["item_transfers"] if i["instance_id"] == BAG)["item_slug"] = "rapier"
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        acquire(sm, sid, p, context)
        if owned:
            assert (
                submit(sm, sid, p, context, PickupItem(BAG, A, "1,0"), "pick").status == "committed"
            )
        assert_admission(
            sm,
            sid,
            p,
            context,
            PutItem(A, BAG, BOX, "0,0"),
            "weapon",
            "equipped_item_unsupported" if owned else None,
        )


@pytest.mark.parametrize("closed", [False, True])
def test_jam_controls_transition_not_access_to_an_open_container(tmp_path, context, closed):
    d = container_data()
    profile(d, BOX)["capabilities"][0]["initial_jammed"] = True
    next(o for o in d["scenes"][0]["objects"] if o["object_id"] == BOX)["state"] = (
        "closed" if closed else "open"
    )
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        acquire(sm, sid, p, context)
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        assert sm.get_object(sid, actor, BOX, p, context=context)["contents"] == (
            None if closed else ()
        )
        assert_admission(
            sm,
            sid,
            p,
            context,
            PutItem(A, CRED, BOX, "0,0"),
            "jam",
            "container_inaccessible" if closed else None,
        )


@pytest.mark.parametrize("related", [False, True])
def test_attunement_lifecycle_guard_only_for_moved_subtree(tmp_path, context, related):
    from dnd5e_engine.evaluation_ruleset import ruleset_binding
    from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
    from lock_support import lock_loader
    from spatial_support import create

    from trpg_state_machine import StateMachine
    from trpg_state_machine.package import _validate

    base = lock_loader()
    bundled = BundledAssetLoader()
    assets = MemoryAssetLoader(
        items=[
            *[base.get_item(s) for s in base.list_slugs("items")],
            bundled.get_item("ring-of-protection"),
        ],
        monsters=[bundled.get_monster("bandit")],
    )
    d = container_data()
    d["manifest"]["ruleset_binding"] = ruleset_binding(assets).model_dump(mode="json")
    next(i for i in d["item_transfers"] if i["instance_id"] == TWIN)["item_slug"] = (
        "ring-of-protection"
    )
    p = _validate(d)
    with StateMachine(tmp_path / "binding.db", dnd_loader=assets) as sm:
        sid = create(sm, p, context)
        acquire(sm, sid, p, context)
        if related:
            # Historical contained magical instance; no lifecycle is silently changed.
            sm._conn.execute(
                "UPDATE objects SET container_id=?,scene_id=NULL,cell=NULL WHERE object_id=?",
                (BAG, TWIN),
            )
        assert_admission(
            sm,
            sid,
            p,
            context,
            PutItem(A, BAG if related else CRED, BOX, "0,0"),
            "attunement",
            "unsupported_item_binding" if related else None,
        )


def test_legacy_third_level_can_be_removed_without_rewriting_other_locations(tmp_path, context):
    d = container_data()
    third_container(d)
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        acquire(sm, sid, p, context)
        with sm._world._transaction(write=True):
            for child, parent in ((BAG, BOX), (TWIN, BAG), (CRED, TWIN)):
                sm._conn.execute(
                    "UPDATE objects SET container_id=?,carrier_id=NULL,scene_id=NULL,cell=NULL "
                    "WHERE object_id=?",
                    (parent, child),
                )
            sm._characters.sync_world_inventory(sid)
        assert_admission(sm, sid, p, context, TakeItem(A, BAG, BOX, "0,0"), "recover")
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        assert sm.get_object(sid, actor, CRED, p, context=context)["location"]["ancestry"] == (
            TWIN,
            BAG,
        )


@pytest.mark.parametrize("limited", [False, True])
def test_unknown_sibling_weight_is_required_only_by_enabled_ancestor_limit(
    tmp_path, context, limited
):
    d = container_data()
    config(d, BOX)["capacity_weight_lb"] = 10 if limited else None
    profile(d, TWIN)["weight_lb"] = None
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        acquire(sm, sid, p, context)
        assert_admission(sm, sid, p, context, PutItem(A, BAG, BOX, "0,0"), "nest")
        # Existing sibling is an aggregation dependency, even though it is not moved.
        sm._conn.execute(
            "UPDATE objects SET container_id=?,scene_id=NULL,cell=NULL WHERE object_id=?",
            (BOX, TWIN),
        )
        assert_admission(
            sm,
            sid,
            p,
            context,
            PutItem(A, CRED, BAG, "0,0"),
            "measurement",
            "unknown_item_capacity" if limited else None,
        )


@pytest.mark.parametrize("related", [False, True])
def test_worn_body_armor_does_not_bind_an_unequipped_shield(tmp_path, context, related):
    d = container_data()
    del d["rule_bindings"]["combat_foundation"]
    snapshot = d["rule_bindings"]["noncombat_foundation"]["snapshot"]
    state = snapshot["character_states"][0]
    state["worn_armor"] = "light"
    state["carried_item_slugs"].append("leather-armor")
    d["pc_templates"][0]["character"]["inventory"].append("leather-armor")
    snapshot["inventory_state"]["entries"].append(
        {
            "instance_id": "inventory.worn",
            "owner_id": state["entity_id"],
            "item_slug": "leather-armor",
            "quantity": 1,
            "charges_remaining_per_unit": 0,
            "accessible": True,
        }
    )
    next(i for i in d["item_transfers"] if i["instance_id"] == BAG)["item_slug"] = (
        "leather-armor" if related else "shield"
    )
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        acquire(sm, sid, p, context)
        assert submit(sm, sid, p, context, PickupItem(BAG, A, "1,0"), "pick").status == "committed"
        assert_admission(
            sm,
            sid,
            p,
            context,
            PutItem(A, BAG, BOX, "0,0"),
            "armor",
            "equipped_item_unsupported" if related else None,
        )
