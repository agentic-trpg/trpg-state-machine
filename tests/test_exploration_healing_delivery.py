"""Public ranged exploration healing and explicit higher-slot payment."""

from copy import deepcopy
from random import Random

import pytest
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from item_support import NPC, A, submit
from test_cure_wounds import HEALING_WORD, actor, cure_loader, cure_world, pool
from test_exploration_cure_wounds import cast_command
from test_object_spatial_service import changed_grid
from test_world_spatial import spatial_world_data

from trpg_state_machine import StateMachine, StateMachineError, StepActor
from trpg_state_machine.perception import GameViewContext


def healing_word_world(tmp_path, context, *, target_cell="12,0", darkvision=None, **grid_fields):
    """Bind the exact spell and a room with cells on both sides of the 60-foot limit."""

    def configure(data, loader):
        changed_grid(data, width=24, **grid_fields)
        if darkvision is not None:
            snapshot = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
            snapshot["character_states"][0]["senses"]["darkvision"] = darkvision

    return cure_world(
        tmp_path,
        context,
        exploration=True,
        extra_spells=(HEALING_WORD,),
        target_cell=target_cell,
        configure=configure,
    )


def player_view(sm, sid, package, context):
    """Read the genuine player disclosure of actors in the current scene."""
    game = GameViewContext(context.principal_id, sid, context.player_id, "player")
    return sm.get_scene_view(sid, package, context=game)


@pytest.mark.parametrize("darkvision", [None, 60])
def test_healing_word_at_sixty_feet_commits_and_recovers(tmp_path, context, darkvision):
    light = "dark" if darkvision else "bright"
    sm, sid, package, loader, db = healing_word_world(
        tmp_path, context, darkvision=darkvision, lighting={"12,0": light}
    )
    with sm:
        view = player_view(sm, sid, package, context)
        assert NPC in view.visible_actor_ids
        command = cast_command(sm, sid, package, context, NPC, spell=HEALING_WORD)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        input_rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        rng = Random(0)
        amount = sum(rng.randint(1, 4) for _ in range(2)) + 3
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert actor(after, NPC).hp_current == actor(before, NPC).hp_current + amount
        assert pool(after).current == pool(before).current - 1
        successor = sm.read_rng_context(sid, package, context=context)
        assert successor.version == input_rng.version + 1
        assert successor.state == RNGState.capture(rng)
        events = [
            event.payload["engine_event"]
            for event in sm.read_events(sid, package, context=context)
            if event.source_command_id == command.command_id
        ]
        assert [event["type"] for event in events] == [
            "intent_submitted",
            "spell_cast",
            "healing_applied",
        ]
        assert events[-1]["target_id"] == NPC and events[-1]["amount"] == amount
        assert sm.submit_command(command, package, context=context) == receipt
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_noncombat_snapshot(sid, package, context=context) == after
        assert reopened.read_rng_context(sid, package, context=context) == successor


@pytest.mark.parametrize(
    "target_cell,grid,target_id,disclosed",
    [
        ("12,0", {"lighting": {"12,0": "dark"}}, NPC, False),
        ("12,0", {"wall_segments": [{"x1": 9, "y1": 0, "x2": 9, "y2": 1}]}, NPC, False),
        ("12,0", {"cover_cells": {"12,0": "total"}}, NPC, False),
        ("13,0", {}, NPC, True),
        ("12,0", {}, "npc.guessed", False),
    ],
    ids=("dark", "wall", "total-cover", "beyond-sixty", "guessed"),
)
def test_healing_word_refusal_discloses_no_target_facts(
    tmp_path, context, monkeypatch, target_cell, grid, target_id, disclosed
):
    sm, sid, package, _, _ = healing_word_world(tmp_path, context, target_cell=target_cell, **grid)
    with sm:
        assert (target_id in player_view(sm, sid, package, context).visible_actor_ids) == disclosed
        command = cast_command(sm, sid, package, context, target_id, spell=HEALING_WORD)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)

        def forbidden(*args, **kwargs):
            pytest.fail("Undisclosed and out-of-range targets must not reach the evaluator")

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", forbidden)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code, receipt.event_seqs) == (
            "rejected",
            "target_invalid",
            (),
        )
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events


def test_healing_word_old_disclosure_cannot_be_replayed_at_new_version(
    tmp_path, context, monkeypatch
):
    sm, sid, package, _, _ = healing_word_world(
        tmp_path, context, wall_segments=[{"x1": 1, "y1": 1, "x2": 1, "y2": 2}]
    )
    with sm:
        assert NPC in player_view(sm, sid, package, context).visible_actor_ids
        command = cast_command(sm, sid, package, context, NPC, spell=HEALING_WORD)
        moved = submit(sm, sid, package, context, StepActor(A, "0,0", "0,1"), "caster.step")
        assert moved.status == "committed", moved
        assert NPC not in player_view(sm, sid, package, context).visible_actor_ids
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)

        def forbidden(*args, **kwargs):
            pytest.fail("Stale or currently hidden targets must not reach the evaluator")

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", forbidden)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code, receipt.event_seqs) == (
            "conflict",
            "version_conflict",
            (),
        )
        current = cast_command(
            sm, sid, package, context, NPC, spell=HEALING_WORD, key="current.healing_word"
        )
        refusal = sm.submit_command(current, package, context=context)
        assert (refusal.status, refusal.error_code, refusal.event_seqs) == (
            "rejected",
            "target_invalid",
            (),
        )
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events


def test_healing_word_rolls_back_with_receipt_failure(tmp_path, context):
    sm, sid, package, loader, db = healing_word_world(tmp_path, context)
    with sm:
        command = cast_command(sm, sid, package, context, NPC, spell=HEALING_WORD)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_healing_word_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='exploration.cure' AND EXISTS (SELECT 1 FROM characters "
            "WHERE session_id=NEW.session_id AND actor_id='npc.caretaker' AND current_hp=13) "
            "AND EXISTS (SELECT 1 FROM character_resources WHERE session_id=NEW.session_id "
            "AND resource_id='spell:1' AND current=1) "
            "BEGIN SELECT RAISE(ABORT,'injected Healing Word receipt failure'); END"
        )
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(command, package, context=context)
        assert exc.value.code == "storage_error"
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_noncombat_snapshot(sid, package, context=context) == before
        assert reopened.read_rng_context(sid, package, context=context) == rng
        assert reopened.submit_command(command, package, context=context).status == "committed"


def test_cure_wounds_slot_two_rolls_four_dice_and_debits_only_selected_pool(tmp_path, context):
    canonical = BundledAssetLoader()
    base = cure_loader()
    channel_slug = "channel-divinity-cleric"
    cleric = canonical.get_class("cleric")
    life = canonical.get_subclass("life")
    grants = [
        grant.slug
        for asset in (cleric, life)
        for grant in asset.granted_features
        if grant.ref_type == "feature" and grant.level <= 3
    ]
    loader = MemoryAssetLoader(
        classes=[cleric],
        subclasses=[life],
        features=[canonical.get_feature(slug) for slug in grants],
        spells=[canonical.get_spell("cure-wounds")],
        items=list(base._items.values()),
        monsters=list(base._monsters.values()),
    )

    def configure(data, loader):
        data["rule_bindings"].pop("combat_foundation")
        snapshot = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
        caster = snapshot["character_states"][0]
        caster.update(
            classes={"cleric": 3},
            character_level=3,
            subclass_slug="life",
            granted_features=grants,
            spell_slots={1: 4, 2: 2},
            custom_counters={f"feature_use:{channel_slug}": {"spent": 0}},
        )
        pools = snapshot["resource_state"]["pools"]
        pools[0].update(current=4, maximum=4)
        pools.extend(
            [
                dict(
                    kind="spell_slot",
                    owner_id=caster["entity_id"],
                    pool_id="spell:2",
                    current=2,
                    maximum=2,
                    level=2,
                ),
                dict(
                    kind="feature_uses",
                    owner_id=caster["entity_id"],
                    pool_id=f"feature:{channel_slug}",
                    current=2,
                    maximum=2,
                    feature_slug=channel_slug,
                ),
            ]
        )

    sm, sid, package, loader, db = cure_world(
        tmp_path, context, exploration=True, loader=loader, configure=configure
    )
    with sm:
        command = cast_command(sm, sid, package, context, NPC, slot_level=2)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = Random(0)
        expected = sum(rng.randint(1, 8) for _ in range(4)) + 3 + 4
        assert actor(after, NPC).hp_current == actor(before, NPC).hp_current + expected
        assert actor(after, command.actor_id).spell_slots == {1: 4, 2: 1}
        before_pools = {p.pool_id: p for p in before.resource_state.pools}
        after_pools = {p.pool_id: p for p in after.resource_state.pools}
        assert after_pools == {
            **before_pools,
            "spell:2": before_pools["spell:2"].model_copy(update={"current": 1}),
        }
        assert sm.read_rng_context(sid, package, context=context).state == RNGState.capture(rng)
        assert sm.submit_command(command, package, context=context) == receipt
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_noncombat_snapshot(sid, package, context=context) == after
        assert (
            reopened.get_command_receipt(sid, command.command_id, package, context=context)
            == receipt
        )


@pytest.mark.parametrize("subject,condition", [("caster", "blinded"), ("target", "invisible")])
def test_healing_word_preserves_unknown_condition_visibility(tmp_path, context, subject, condition):
    def configure(data, loader):
        changed_grid(data, width=24)
        snapshot = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
        index = 0 if subject == "caster" else 1
        snapshot["character_states"][index]["conditions"] = [
            ActiveCondition(
                condition=condition, source_entity_id="implied:fixture", scope="session"
            ).model_dump(mode="json")
        ]

    sm, sid, package, _, _ = cure_world(
        tmp_path,
        context,
        exploration=True,
        extra_spells=(HEALING_WORD,),
        target_cell="12,0",
        configure=configure,
    )
    with sm:
        assert NPC not in player_view(sm, sid, package, context).visible_actor_ids
        command = cast_command(sm, sid, package, context, NPC, spell=HEALING_WORD)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code, receipt.event_seqs) == (
            "rejected",
            "target_invalid",
            (),
        )
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng


def install_fixture(data, oid, cell, **properties):
    """Install explicit occurrence and cover/visual properties for a local fixture."""
    template = spatial_world_data()
    occurrence = deepcopy(template["scenes"][0]["objects"][-1])
    occurrence.update(object_id=oid, initial_cell=cell)
    data["scenes"][0]["objects"].append(occurrence)
    definition = deepcopy(template["world_objects"][-1])
    definition.update(object_id=oid, definition_ref=oid.replace("object.", "definition."))
    definition["capabilities"][0].update(footprint=[[0, 0]], closed_properties=properties)
    data.setdefault("world_objects", []).append(definition)
    data.setdefault("lockable_doors", [])
    data["manifest"]["schema_version"] = "state-machine-foundation/8"


@pytest.mark.parametrize(
    "properties",
    [{"vision_occlusion": True}, {"cover": "total"}],
    ids=("visual-occlusion", "total-cover"),
)
def test_healing_word_rejects_selected_rays_with_live_fixture_occlusion(
    tmp_path, context, properties
):
    def configure(data, loader):
        changed_grid(data, width=24)
        install_fixture(data, "object.healing_barrier", "9,0", **properties)

    sm, sid, package, _, _ = cure_world(
        tmp_path,
        context,
        exploration=True,
        extra_spells=(HEALING_WORD,),
        target_cell="12,0",
        configure=configure,
    )
    with sm:
        assert NPC not in player_view(sm, sid, package, context).visible_actor_ids
        command = cast_command(sm, sid, package, context, NPC, spell=HEALING_WORD)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code, receipt.event_seqs) == (
            "rejected",
            "target_invalid",
            (),
        )
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng


@pytest.mark.parametrize("unrelated", [0, 16])
def test_public_healing_word_keeps_actor_and_ray_inputs_scoped(
    tmp_path, context, monkeypatch, unrelated
):
    def configure(data, loader):
        changed_grid(data, width=24, height=8)
        npc = data["actors"][0]
        for index in range(unrelated):
            oid = f"object.outside_heal_{index}"
            cell = f"{index},6"
            install_fixture(data, oid, cell)
            aid = f"npc.outside_heal_{index}"
            actor = {**deepcopy(npc), "actor_id": aid, "initial_cell": f"{index},7"}
            data["actors"].append(actor)
            data["scenes"][0]["actor_ids"].append(aid)

    sm, sid, package, _, _ = cure_world(
        tmp_path,
        context,
        exploration=True,
        extra_spells=(HEALING_WORD,),
        target_cell="12,0",
        configure=configure,
    )
    with sm:
        command = cast_command(sm, sid, package, context, NPC, spell=HEALING_WORD)
        region_reads, actor_builds = [], []
        read_region = sm._world.grid.reader.region_occupants
        build_actors = sm._characters.rule_actors

        def region(sid, scene, cells, **options):
            actors, occupants = read_region(sid, scene, cells, **options)
            objects = tuple(occupants)
            region_reads.append((frozenset(cells), len(objects), len(tuple(actors))))
            return actors, objects

        def build(sid, actor_ids=None):
            result = build_actors(sid, actor_ids)
            actor_builds.append((frozenset(actor_ids), len(result)))
            return result

        def forbidden(*args, **kwargs):
            pytest.fail("A selected spell target cannot build a complete scene view")

        monkeypatch.setattr(sm, "_fine_scene_disclosure", forbidden)
        monkeypatch.setattr(sm._world.grid.reader, "region_occupants", region)
        monkeypatch.setattr(sm._characters, "rule_actors", build)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        expected_ray = frozenset(f"{index},0" for index in range(13))
        assert len(region_reads) == 2
        assert all(
            r == expected_ray and objects <= 3 and actors == 0
            for r, objects, actors in region_reads
        )
        assert actor_builds == [(frozenset((command.actor_id, NPC)), 2)]
