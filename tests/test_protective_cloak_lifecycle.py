"""Exact cloak wear and short-rest attunement through public command and save paths."""

import json
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_contracts import SavePayload
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_wearables import attune_cloak_reason
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from spatial_support import command, create
from test_long_rest import rest_data

from trpg_state_machine import (
    EquipWearable,
    ShortRest,
    StateMachine,
    TypedCommand,
    UnequipWearable,
    world_command_from_data,
)
from trpg_state_machine.package import _validate
from trpg_state_machine.time_coordination import DueResult

CLOAK = "object.protective_cloak"
SLUG = "cloak-of-protection"


def _world(tmp_path, context, *, copies=1):
    """Create a timed /20 world with exact canonical cloaks directly owned by its PC."""
    canonical = BundledAssetLoader()
    data, base = rest_data()
    loader = MemoryAssetLoader(
        items=[*base._items.values(), canonical.get_item(SLUG)],
        monsters=list(base._monsters.values()),
        classes=list(base._classes.values()),
        features=list(base._features.values()),
        spells=list(base._spells.values()),
    )
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    owner = data["pc_templates"][0]["pc_template_id"]
    data["pc_templates"][0]["character"]["inventory"].append(SLUG)
    for index in range(copies):
        iid = CLOAK if index == 0 else f"{CLOAK}_{index}"
        data["scenes"][0]["objects"].append(
            dict(object_id=iid, state=None, movable=False, blocks_passage=False, initial_cell="2,1")
        )
        data["item_transfers"].append(
            dict(
                instance_id=iid,
                item_slug=SLUG,
                initial_owner_id=owner,
                quantity=1,
                charges_remaining_per_unit=0,
                policy="ordinary_public",
            )
        )
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        snapshot["character_states"][0]["carried_item_slugs"].append(SLUG)
        for index in range(copies):
            iid = CLOAK if index == 0 else f"{CLOAK}_{index}"
            snapshot["inventory_state"]["entries"].append(
                dict(
                    instance_id=iid,
                    owner_id=owner,
                    item_slug=SLUG,
                    quantity=1,
                    charges_remaining_per_unit=0,
                    accessible=True,
                )
            )
    package = _validate(data)
    sm = StateMachine(tmp_path / "cloak.sqlite", dnd_loader=loader)
    sid = create(sm, package, context)
    return sm, sid, package, loader


def _json(value):
    data = asdict(value)
    data.pop("check_world_effects")
    if hasattr(value.payload, "model_dump"):
        data["payload"] = value.payload.model_dump(mode="json")
    return world_command_from_data(json.loads(json.dumps(data)))


def _save(sm, sid, package, context, key):
    state = sm.read_session(sid, package, context=context)
    return _json(
        TypedCommand(
            sid,
            key,
            state.pc_actor_id,
            "rules.save",
            SavePayload(
                kind="rules.save",
                target_id=state.pc_actor_id,
                ability="dex",
                dc=14,
                advantage=(),
                disadvantage=(),
                is_magical=False,
            ),
            state.world_version,
        )
    )


def _cloak_save_modifier(sm, sid, package, context, key):
    """Commit a public save and report whether the exact cloak source applied."""
    save = _save(sm, sid, package, context, key)
    receipt = sm.submit_command(save, package, context=context)
    assert receipt.status == "committed", receipt
    roll = sm.get_command_rolls(sid, save.command_id, package, context=context)["rolls"][0]
    return {"kind": "fixed", "source": f"equipment:{CLOAK}:{SLUG}", "value": 1} in roll[
        "roll_detail"
    ]["modifiers"]


def test_cloak_requires_both_wear_and_attunement(tmp_path, context):
    """One exact bond survives removal and cold reopen; voluntary release removes both bonuses."""
    host = replace(context, can_authorize_short_rest=True, can_adjudicate_checks=True)
    sm, sid, package, loader = _world(tmp_path, host)
    with sm:
        actor = sm.read_session(sid, package, context=host).pc_actor_id
        base_ac = sm.read_character_ac(sid, actor, package, context=host).effective
        started_at = sm.read_time_sync(sid, package, context=host).committed_tick
        premature = _json(
            command(sm, sid, package, host, ShortRest(unattune_instance_id=CLOAK), "premature")
        )
        assert sm.submit_command(premature, package, context=host).status == "rejected"
        assert sm.read_time_sync(sid, package, context=host).committed_tick == started_at
        wear = _json(command(sm, sid, package, host, EquipWearable(CLOAK), "wear"))
        assert sm.submit_command(wear, package, context=host).status == "committed"
        assert sm.read_character_ac(sid, actor, package, context=host).effective == base_ac
        rest = _json(command(sm, sid, package, host, ShortRest(attune_instance_id=CLOAK), "attune"))
        receipt = sm.submit_command(rest, package, context=host)
        assert receipt.status == "committed", receipt
        assert sm.read_time_sync(sid, package, context=host).committed_tick == started_at + 3600
        assert sm.read_character_ac(sid, actor, package, context=host).effective == base_ac + 1
        repeated = _json(
            command(sm, sid, package, host, ShortRest(attune_instance_id=CLOAK), "repeat-attune")
        )
        assert sm.submit_command(repeated, package, context=host).status == "rejected"
        assert sm.read_time_sync(sid, package, context=host).committed_tick == started_at + 3600
        assert _cloak_save_modifier(sm, sid, package, host, "save-worn")
        off = _json(command(sm, sid, package, host, UnequipWearable(CLOAK), "off"))
        assert sm.submit_command(off, package, context=host).status == "committed"
        assert sm.read_character_ac(sid, actor, package, context=host).effective == base_ac
        assert not _cloak_save_modifier(sm, sid, package, host, "save-off")
        on = _json(command(sm, sid, package, host, EquipWearable(CLOAK), "on"))
        assert sm.submit_command(on, package, context=host).status == "committed"
        assert _cloak_save_modifier(sm, sid, package, host, "save-on")
        release = _json(
            command(sm, sid, package, host, ShortRest(unattune_instance_id=CLOAK), "release")
        )
        assert sm.submit_command(release, package, context=host).status == "committed"
        assert sm.read_time_sync(sid, package, context=host).committed_tick == started_at + 7200
        assert sm.read_character_ac(sid, actor, package, context=host).effective == base_ac
        assert not _cloak_save_modifier(sm, sid, package, host, "save-released")
    with StateMachine(tmp_path / "cloak.sqlite", dnd_loader=loader) as reopened:
        assert reopened.submit_command(rest, package, context=host).status == "committed"
        assert reopened.submit_command(release, package, context=host).status == "committed"
        assert reopened.read_character_ac(sid, actor, package, context=host).effective == base_ac


def test_attunement_contact_interruption_and_late_rollback(tmp_path, context, monkeypatch):
    """Contact loss and a late receipt fault leave no bond or elapsed rest."""
    host = replace(context, can_authorize_short_rest=True)
    sm, sid, package, loader = _world(tmp_path, host)
    with sm:
        rest = _json(command(sm, sid, package, host, ShortRest(attune_instance_id=CLOAK), "rest"))
        start = sm.read_time_sync(sid, package, context=host).committed_tick

        class ContactLoss:
            def next_boundary(self, sid, package):
                return 60 if sm._world_facts.time_state(sid)[1] < 60 else None

            def settle_one(self, row, package, command_id):
                sm._store.connection.execute(
                    "UPDATE objects SET item_accessible=0 WHERE session_id=? AND object_id=?",
                    (sid, CLOAK),
                )
                return DueResult(status="continue")

        sm._session_runtime.time.consumers["cloak_contact_loss"] = ContactLoss()
        rejected = sm.submit_command(rest, package, context=host)
        assert rejected.status == "rejected" and rejected.error_code == "rest_interrupted"
        assert sm.read_time_sync(sid, package, context=host).committed_tick == start
        assert sm._world_facts.object(sid, CLOAK)["item_attuned_actor_id"] is None
        assert sm._world_facts.object(sid, CLOAK)["item_accessible"] == 1
        del sm._session_runtime.time.consumers["cloak_contact_loss"]

        retry = _json(command(sm, sid, package, host, ShortRest(attune_instance_id=CLOAK), "retry"))
        finish = sm._session_runtime.finish

        def fail_after_receipt(*args, **kwargs):
            finish(*args, **kwargs)
            assert sm._world_facts.object(sid, CLOAK)["item_attuned_actor_id"] == retry.actor_id
            raise RuntimeError("late receipt fault")

        with monkeypatch.context() as patch:
            patch.setattr(sm._session_runtime, "finish", fail_after_receipt)
            with pytest.raises(RuntimeError, match="late receipt fault"):
                sm.submit_command(retry, package, context=host)
        assert sm.read_time_sync(sid, package, context=host).committed_tick == start
        assert sm._world_facts.object(sid, CLOAK)["item_attuned_actor_id"] is None
        assert sm.get_command_receipt(sid, retry.command_id, package, context=host) is None
    with StateMachine(tmp_path / "cloak.sqlite", dnd_loader=loader) as reopened:
        assert reopened.submit_command(retry, package, context=host).status == "committed"
        assert reopened.read_time_sync(sid, package, context=host).committed_tick == start + 3600


def test_attuned_worn_cloak_reopens_with_same_ac_and_save_receipt(tmp_path, context):
    """Cold loading preserves the active bond, worn ID and authorized save evidence."""
    host = replace(context, can_authorize_short_rest=True, can_adjudicate_checks=True)
    sm, sid, package, loader = _world(tmp_path, host)
    with sm:
        actor = sm.read_session(sid, package, context=host).pc_actor_id
        base = sm.read_character_ac(sid, actor, package, context=host).effective
        wear = _json(command(sm, sid, package, host, EquipWearable(CLOAK), "cold-wear"))
        assert sm.submit_command(wear, package, context=host).status == "committed"
        rest = _json(
            command(sm, sid, package, host, ShortRest(attune_instance_id=CLOAK), "cold-attune")
        )
        assert sm.submit_command(rest, package, context=host).status == "committed"
        save = _save(sm, sid, package, host, "cold-save")
        receipt = sm.submit_command(save, package, context=host)
        assert receipt.status == "committed"
        rolls = sm.get_command_rolls(sid, save.command_id, package, context=host)
    with StateMachine(tmp_path / "cloak.sqlite", dnd_loader=loader) as reopened:
        assert reopened.read_character_ac(sid, actor, package, context=host).effective == base + 1
        assert reopened.submit_command(save, package, context=host) == receipt
        assert reopened.get_command_rolls(sid, save.command_id, package, context=host) == rolls


def test_same_type_second_cloak_cannot_attune(tmp_path, context):
    """The exact second instance is refused before an additional hour passes."""
    host = replace(context, can_authorize_short_rest=True)
    sm, sid, package, _ = _world(tmp_path, host, copies=2)
    with sm:
        first = _json(command(sm, sid, package, host, ShortRest(attune_instance_id=CLOAK), "one"))
        assert sm.submit_command(first, package, context=host).status == "committed"
        tick = sm.read_time_sync(sid, package, context=host).committed_tick
        second = _json(
            command(
                sm,
                sid,
                package,
                host,
                ShortRest(attune_instance_id=f"{CLOAK}_1"),
                "second",
            )
        )
        refused = sm.submit_command(second, package, context=host)
        assert refused.status == "rejected" and refused.error_code == "item_attunement_same_type"
        assert sm.read_time_sync(sid, package, context=host).committed_tick == tick


def test_bound_attunement_limit_uses_re_rule(tmp_path, context):
    """The RE limit refuses a fourth distinct item from fixed actor facts."""
    sm, sid, package, loader = _world(tmp_path, context)
    with sm:
        actor = sm.read_noncombat_snapshot(sid, package, context=context).character_states[0]
        assert (
            attune_cloak_reason(
                actor,
                loader.get_item(SLUG),
                ("ring-of-protection", "amulet-of-health", "boots-of-speed"),
                loader,
            )
            == "item_attunement_limit"
        )
