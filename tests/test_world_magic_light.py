"""Public spatial spell casting and its durable Effects lifecycle."""

import pytest
from dnd5e_engine.evaluation_contracts import ExplorationEnvironmentalSpellCastPayload
from dnd5e_engine.evaluation_delta import TempHPSet
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.spatial import GridTopology
from item_support import DAGGER, NPC, SECOND, A
from spatial_support import command as world_command
from spatial_support import submit as submit_world
from test_mechanism_effects import FixturePort, install, rule_action, rule_data, state_rows
from test_ongoing_effect_storage import _effect
from test_scoped_runtime_validation import measure
from test_session_time_coordination import submit as submit_time
from test_timed_spell_cast import cast_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world
from test_world_mechanisms import LEVER

from trpg_state_machine import (
    ActivateMechanism,
    AdvanceSessionTime,
    DropWorldConcentration,
    EnvironmentArea,
    ObjectState,
    PickupItem,
    PutItem,
    SetObjectState,
    StateMachine,
    StateMachineError,
    StepActor,
    world_command_from_data,
)

ITEM = "object.pouch"
CASE = "object.dark_case"


@pytest.fixture
def magic_world(timed_spell_world, request):
    return _magic_world(timed_spell_world, opaque_case=getattr(request, "param", False))


def _magic_world(
    timed_spell_world,
    *,
    opaque_case=False,
    growth=0,
    additional_spells=(),
    truesight=False,
    high_slot=False,
    seed=0,
):
    """Make a fixed spatial cast scene with optional unrelated placed objects."""

    def configure(data):
        data["manifest"]["schema_version"] = "state-machine-foundation/6"
        data["lockable_doors"] = []
        data["world_objects"] = [
            {
                "object_id": ITEM,
                "definition_ref": "definition.component_pouch",
                "display_name": "Component pouch",
                "description_ref": None,
                "material": None,
                "size_category": "tiny",
                "weight_lb": 2,
                "capabilities": [],
            }
        ]
        data["scenes"][0]["objects"].extend(
            {
                "object_id": f"object.unrelated_{index}",
                "state": None,
                "movable": False,
                "blocks_passage": False,
                "initial_cell": "13,1",
            }
            for index in range(growth)
        )
        data["world_objects"].extend(
            {
                **data["world_objects"][0],
                "object_id": f"object.unrelated_{index}",
                "definition_ref": f"definition.unrelated_{index}",
            }
            for index in range(growth)
        )
        if opaque_case:
            data["world_objects"][0].update(
                volume_cu_ft=1, fit_profile="station.small", content_kind="station.unit"
            )
            data["world_objects"].append(
                {
                    **data["world_objects"][0],
                    "object_id": CASE,
                    "definition_ref": "definition.opaque_case",
                    "display_name": "Opaque case",
                    "capabilities": [
                        {"kind": "openable"},
                        {
                            "kind": "container",
                            "capacity_weight_lb": 10,
                            "capacity_volume_cu_ft": 10,
                            "opening_profiles": ["station.small"],
                            "allowed_content_kinds": ["station.unit"],
                            "nesting_limit": 2,
                            "light_transmission_open": "transmit",
                            "light_transmission_closed": "block",
                        },
                    ],
                }
            )
            data["world_objects"].append(
                {
                    **data["world_objects"][0],
                    "object_id": DAGGER,
                    "definition_ref": "definition.demo_dagger",
                    "display_name": "Dagger",
                }
            )
            dagger = next(obj for obj in data["scenes"][0]["objects"] if obj["object_id"] == DAGGER)
            dagger["initial_cell"] = "0,1"
            data["scenes"][0]["objects"].append(
                {
                    "object_id": CASE,
                    "state": "open",
                    "movable": False,
                    "blocks_passage": False,
                    "initial_cell": "0,1",
                }
            )
        for scene in data["scenes"]:
            scene["grid"]["width"] = 14
        if high_slot:
            data["rule_bindings"].pop("combat_foundation", None)
        for foundation in data["rule_bindings"].values():
            foundation["snapshot"]["scene_state"]["grid"]["width"] = 14
            if high_slot:
                fixed = foundation["snapshot"]
                caster = fixed["character_states"][0]
                caster["character_level"] = 7
                caster["classes"] = {"wizard": 7}
                caster["spell_slots"][4] = 2
                fixed["resource_state"]["pools"].append(
                    {
                        "kind": "spell_slot",
                        "owner_id": caster["entity_id"],
                        "pool_id": "spell:4",
                        "current": 2,
                        "maximum": 2,
                        "level": 4,
                    }
                )
            if truesight:
                foundation["snapshot"]["character_states"][0]["senses"]["truesight"] = 60
            for scene in foundation.get("additional_scenes", ()):
                scene["grid"]["width"] = 14

    return timed_spell_world(
        additional_spells=("light", "darkness", "dispel-magic", *additional_spells),
        database=f"magic-high-{seed}.sqlite"
        if high_slot
        else f"magic-{growth}.sqlite"
        if growth
        else "timed-spell.sqlite",
        seed=seed,
        configure=configure,
    )


def cast(session, command_id, spell, slot, target):
    """Submit an authored spatial spell using the current public Session version."""
    command = prepare_cast(session, command_id, spell, slot, target)
    sm, _, package, context = session
    return command, sm.submit_command(command, package, context=context)


def prepare_cast(session, command_id, spell, slot, target):
    """Construct a public cast so a failing submission can be retried exactly."""
    sm, sid, package, context = session
    state = sm.read_session(sid, package, context=context)
    payload = {
        "kind": "world.cast_spell",
        "spell_id": spell,
        "spell_source_id": "class:wizard",
        "slot_level": slot,
        "target": target,
    }
    command = world_command_from_data(
        {
            "schema_version": "sm-command/1",
            "session_id": sid,
            "command_id": command_id,
            "actor_id": state.pc_actor_id,
            "command_type": "world.cast_spell",
            "expected_world_version": state.world_version,
            "payload": payload,
        }
    )
    assert type(command.payload) is ExplorationEnvironmentalSpellCastPayload
    return command


def sources(session):
    sm, sid, package, _ = session
    with sm._store.transaction():
        return sm._world.magic.active_magic_sources(sid, A, package)


def test_light_is_slotless_follows_the_actual_carrier_replays_and_expires(magic_world):
    sm, sid, package, context = magic_world
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        command, receipt = cast(
            magic_world, "light-1", "light", 0, {"kind": "item", "item_id": ITEM}
        )
        assert receipt.status == "committed", receipt
        (old,) = sources(magic_world)
        assert (old.effect_id, old.source_id, old.item_id, old.cell) == (
            "spatial:light-1",
            command.actor_id,
            ITEM,
            "0,0",
        )
        assert old.spec.kind == "light" and old.cells and old.dim_cells
        assert sm.submit_command(command, package, context=context) == receipt
        assert (
            sm.read_noncombat_snapshot(sid, package, context=context).resource_state
            == before.resource_state
        )
        assert sm.read_rng_context(sid, package, context=context) == rng
        moved = submit_world(sm, sid, package, context, StepActor(A, "0,0", "0,1"), "move-pouch")
        assert moved.status == "committed", moved
        assert sm._world_facts.instances.session_value(sid, "evidence_schema") == "event-evidence/6"
        (current,) = sources(magic_world)
        assert (old.cell, current.cell, current.effect_id) == ("0,0", "0,1", old.effect_id)
        assert (
            submit_time(magic_world, AdvanceSessionTime(3600), "light-hour").status == "committed"
        )
        assert sources(magic_world) == ()
        assert old.cell == "0,0"


def test_darkness_dispels_a_paid_overlap_and_releases_its_own_concentration(magic_world):
    sm, sid, package, context = magic_world
    with sm:
        _, light = cast(magic_world, "light", "light", 0, {"kind": "item", "item_id": ITEM})
        assert light.status == "committed", light
        command, dark = cast(
            magic_world, "dark", "darkness", 2, {"kind": "point", "cell_id": "2,0"}
        )
        assert dark.status == "committed", dark
        (source,) = sources(magic_world)
        assert source.spec.kind == "magical_darkness"
        assert (source.effect_id, source.item_id, source.cell, source.spell_level) == (
            "spatial:dark",
            None,
            "2,0",
            2,
        )
        assert source.concentration_identity is not None
        ended = [
            event.payload
            for event in sm.read_events(sid, package, context=context)
            if event.event_type == "effect.spatial_light_ended"
        ]
        assert [(event["effect_id"], event["cause_effect_id"]) for event in ended] == [
            ("spatial:light", "spatial:dark")
        ]
        assert (
            sm._conn.execute(
                "SELECT current FROM character_resources WHERE session_id=? AND actor_id=? "
                "AND resource_id='spell:2'",
                (sid, command.actor_id),
            ).fetchone()[0]
            == 2
        )
        dropped = submit_world(sm, sid, package, context, DropWorldConcentration(), "drop")
        assert dropped.status == "committed", dropped
        assert sources(magic_world) == ()
        assert sm._conn.execute("SELECT count(*) FROM effect_concentrations").fetchone()[0] == 0


def test_public_dispel_ends_the_observed_darkness_and_keeps_unrelated_light(magic_world):
    """Spend once on an exact effect identified through committed cast evidence."""
    sm, sid, package, context = magic_world
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        cell = "12,0"

        def illumination():
            return sm.get_illumination_area(sid, actor, (cell,), package, context=context)[0]

        assert (
            cast(magic_world, "light", "light", 0, {"kind": "item", "item_id": ITEM})[1].status
            == "committed"
        )
        normal_light = illumination()
        _, darkness = cast(magic_world, "dark", "darkness", 2, {"kind": "point", "cell_id": cell})
        assert darkness.status == "committed", darkness
        old_light = illumination()
        assert old_light["magical_darkness"]
        cast_events = sm.read_events(sid, package, context=context)
        effect_id = next(
            e.payload["effect_id"] for e in cast_events if e.source_command_id == "dark"
        )
        assert effect_id == "spatial:dark"
        snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        resources = snapshot.resource_state
        rng = sm.read_rng_context(sid, package, context=context)
        old_sources = sources(magic_world)
        target = {"kind": "effect", "effect_id": effect_id}
        command, receipt = cast(magic_world, "dispel", "dispel-magic", 3, target)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(command, package, context=context) == receipt
        assert illumination() == normal_light
        assert old_light["magical_darkness"]
        assert {e.effect_id for e in old_sources} == {"spatial:dark", "spatial:light"}
        assert [e.effect_id for e in sources(magic_world)] == ["spatial:light"]
        assert sm._conn.execute("SELECT count(*) FROM effect_concentrations").fetchone()[0] == 0
        current = sm.read_noncombat_snapshot(sid, package, context=context)
        spend = {
            pool.pool_id: pool.current - prev.current
            for pool, prev in zip(current.resource_state.pools, resources.pools, strict=True)
            if pool.current != prev.current
        }
        assert spend == {"spell:3": -1}
        assert sm.read_rng_context(sid, package, context=context) == rng
        ended = [
            e
            for e in sm.read_events(sid, package, context=context)
            if e.event_type == "effect.spatial_light_ended"
        ]
        assert len(ended) == 1
        assert ended[0].payload["effect_id"] == effect_id
        assert ended[0].payload["reason"] == "dispelled"
        assert ended[0].source_command_id == command.command_id

        for absent in (effect_id, "spatial:unknown"):
            _, refused = cast(
                magic_world,
                f"dispel:{absent}",
                "dispel-magic",
                3,
                {"kind": "effect", "effect_id": absent},
            )
            assert (refused.status, refused.error_code) == ("rejected", "target_invalid")
        assert (
            sm.read_noncombat_snapshot(sid, package, context=context).resource_state
            == current.resource_state
        )

        _, light_end = cast(
            magic_world,
            "dispel:light",
            "dispel-magic",
            3,
            {"kind": "effect", "effect_id": "spatial:light"},
        )
        assert light_end.status == "committed", light_end
        assert sources(magic_world) == ()


def test_public_dispel_follows_the_selected_moving_darkness_item(timed_spell_world):
    """End the exact object spell after its unattended anchor becomes carried."""
    magic_world = _magic_world(timed_spell_world, opaque_case=True, truesight=True)
    sm, sid, package, context = magic_world
    with sm:
        _, dark = cast(magic_world, "item-dark", "darkness", 2, {"kind": "item", "item_id": DAGGER})
        assert dark.status == "committed", dark
        assert (
            submit_world(
                sm, sid, package, context, StepActor(A, "0,0", "0,1"), "approach-dark-anchor"
            ).status
            == "committed"
        )
        taken = submit_world(
            sm, sid, package, context, PickupItem(DAGGER, A, "0,1"), "take-dark-anchor"
        )
        assert taken.status == "committed", taken
        moved = submit_world(
            sm, sid, package, context, StepActor(A, "0,1", "1,1"), "move-dark-anchor"
        )
        assert moved.status == "committed", moved
        assert sources(magic_world)[0].cell == "1,1"
        _, ended = cast(
            magic_world,
            "dispel:item-dark",
            "dispel-magic",
            3,
            {"kind": "effect", "effect_id": "spatial:item-dark"},
        )
        assert ended.status == "committed", ended
        assert sources(magic_world) == ()


def test_public_three_ring_fixed_darkness_keeps_legacy_auto_end(magic_world):
    """Preserve the previously supported exact fixed-point level-three path."""
    sm, sid, package, context = magic_world
    with sm:
        _, dark = cast(
            magic_world, "dark-three", "darkness", 3, {"kind": "point", "cell_id": "5,0"}
        )
        assert dark.status == "committed", dark
        before = sm.read_rng_context(sid, package, context=context)
        _, ended = cast(
            magic_world,
            "dispel:dark-three",
            "dispel-magic",
            3,
            {"kind": "effect", "effect_id": "spatial:dark-three"},
        )
        assert ended.status == "committed", ended
        assert sources(magic_world) == ()
        assert sm.read_rng_context(sid, package, context=context) == before


def test_public_creature_dispel_ends_only_selected_actors_spell_applications(timed_spell_world):
    """One paid cast removes independent Longstrider identities on the visible NPC."""
    session = _magic_world(timed_spell_world)
    sm, sid, package, context = session
    with sm:
        for name, target in (("npc-one", NPC), ("npc-two", NPC), ("self", None)):
            command = cast_command(session, target_id=target, command_id=name)
            receipt = sm.submit_command(command, package, context=context)
            assert receipt.status == "committed", receipt
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        npc_effects = [effect for effect in before.effect_states if effect.target_id == NPC]
        assert len(npc_effects) == 2
        _, dispelled = cast(
            session, "dispel:npc", "dispel-magic", 3, {"kind": "creature", "target_id": NPC}
        )
        assert dispelled.status == "committed", dispelled
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert not any(effect.target_id == NPC for effect in after.effect_states)
        assert any(
            effect.target_id == sm.read_session(sid, package, context=context).pc_actor_id
            for effect in after.effect_states
        )


def test_public_creature_dispel_uses_two_distinct_canonical_spell_profiles(timed_spell_world):
    """A druid ends Longstrider and Barkskin by typed application facts."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")

    session = timed_spell_world(
        database="druid-dispel.sqlite",
        caster_class="druid",
        caster_subclass="land",
        additional_spells=("barkskin", "dispel-magic"),
        configure=configure,
    )
    sm, sid, package, context = session
    with sm:
        caster = sm.read_session(sid, package, context=context).pc_actor_id
        for spell_id, level in (("longstrider", 1), ("barkskin", 2)):
            command = cast_command(
                session,
                spell_id=spell_id,
                source_id="class:druid",
                target_id=caster,
                slot_level=level,
                command_id=f"cast:{spell_id}",
            )
            receipt = sm.submit_command(command, package, context=context)
            assert receipt.status == "committed", receipt
        assert {
            effect.id
            for effect in sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        } == {"effect:longstrider", "effect:bark-like_skin"}
        state = sm.read_session(sid, package, context=context)
        command = world_command_from_data(
            {
                "schema_version": "sm-command/1",
                "session_id": sid,
                "command_id": "dispel:two-profiles",
                "actor_id": caster,
                "command_type": "world.cast_spell",
                "expected_world_version": state.world_version,
                "payload": {
                    "kind": "world.cast_spell",
                    "spell_id": "dispel-magic",
                    "spell_source_id": "class:druid",
                    "slot_level": 3,
                    "target": {"kind": "creature", "target_id": caster},
                },
            }
        )
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.read_noncombat_snapshot(sid, package, context=context).effect_states == ()


def test_public_high_ring_dispel_pays_on_failure_and_auto_ends_with_adequate_slot(
    timed_spell_world,
):
    """A real level-four Darkness tests DC 14 at slot three and skips RNG at slot four."""
    outcomes = set()
    for seed in range(8):
        session = _magic_world(timed_spell_world, high_slot=True, seed=seed)
        sm, sid, package, context = session
        with sm:
            _, dark = cast(session, "dark-high", "darkness", 4, {"kind": "point", "cell_id": "5,0"})
            assert dark.status == "committed", dark
            before = sm.read_rng_context(sid, package, context=context)
            old_slot = next(
                pool.current
                for pool in sm.read_noncombat_snapshot(
                    sid, package, context=context
                ).resource_state.pools
                if pool.pool_id == "spell:3"
            )
            _, dispelled = cast(
                session,
                "dispel:high",
                "dispel-magic",
                3,
                {"kind": "effect", "effect_id": "spatial:dark-high"},
            )
            assert dispelled.status == "committed", dispelled
            assert sm.read_rng_context(sid, package, context=context) != before
            new_slot = next(
                pool.current
                for pool in sm.read_noncombat_snapshot(
                    sid, package, context=context
                ).resource_state.pools
                if pool.pool_id == "spell:3"
            )
            assert new_slot == old_slot - 1
            survived = bool(sources(session))
            outcomes.add(survived)
            if survived:
                auto_before = sm.read_rng_context(sid, package, context=context)
                _, auto = cast(
                    session,
                    "dispel:auto",
                    "dispel-magic",
                    4,
                    {"kind": "effect", "effect_id": "spatial:dark-high"},
                )
                assert auto.status == "committed", auto
                assert sources(session) == ()
                assert sm.read_rng_context(sid, package, context=context) == auto_before
        if outcomes == {True, False}:
            break
    assert outcomes == {True, False}


def test_dispel_receipt_failure_restores_spatial_effect_concentration_and_slot(magic_world):
    """Reject publication of the removal when committing its receipt fails."""
    sm, sid, package, context = magic_world
    with sm:
        _, darkness = cast(magic_world, "dark", "darkness", 2, {"kind": "point", "cell_id": "5,0"})
        assert darkness.status == "committed", darkness
        command = prepare_cast(
            magic_world,
            "dispel.rollback",
            "dispel-magic",
            3,
            {"kind": "effect", "effect_id": "spatial:dark"},
        )
        snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        old_sources = sources(magic_world)
        old_light = sm.get_illumination_area(
            sid, command.actor_id, ("5,0",), package, context=context
        )
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_dispel_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='dispel.rollback' AND NOT EXISTS ("
            "SELECT 1 FROM spatial_light_effects WHERE session_id=NEW.session_id "
            "AND effect_id='spatial:dark') AND EXISTS (SELECT 1 FROM character_resources "
            "WHERE session_id=NEW.session_id AND resource_id='spell:3' AND current=1) "
            "BEGIN SELECT RAISE(ABORT,'injected dispel receipt failure'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(command, package, context=context)
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert sm.read_noncombat_snapshot(sid, package, context=context) == snapshot
        assert sm.read_events(sid, package, context=context) == events
        assert sources(magic_world) == old_sources
        assert sm._conn.execute("SELECT count(*) FROM effect_concentrations").fetchone()[0] == 1
        assert (
            sm.get_illumination_area(sid, command.actor_id, ("5,0",), package, context=context)
            == old_light
        )
        sm._conn.execute("DROP TRIGGER fail_dispel_receipt")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(command, package, context=context) == receipt
        assert sources(magic_world) == ()


def test_public_dispel_scope_fixed_when_unrelated_objects_increase(timed_spell_world):
    """Count actual reads at the same public command with a fixed effect and approach."""
    observed = []
    for growth in (0, 25):
        session = _magic_world(timed_spell_world, growth=growth)
        sm, _, package, context = session
        with sm:
            _, darkness = cast(session, "dark", "darkness", 2, {"kind": "point", "cell_id": "5,0"})
            assert darkness.status == "committed", darkness
            command = prepare_cast(
                session,
                "dispel",
                "dispel-magic",
                3,
                {"kind": "effect", "effect_id": "spatial:dark"},
            )
            with measure(sm) as (counts, _):
                result = sm.submit_command(command, package, context=context)
            assert result.status == "committed", result
            observed.append(tuple(counts[k] for k in ("sql_selects", "sql_rows", "returned_bytes")))
    assert observed[0] == observed[1]


def test_public_creature_dispel_reads_fixed_target_when_unrelated_objects_grow(timed_spell_world):
    """Count the complete selected-creature path with its effect scope fixed."""
    observed = []
    for growth in (0, 25):
        session = _magic_world(timed_spell_world, growth=growth)
        sm, sid, package, context = session
        with sm:
            applied = cast_command(session, target_id=NPC, command_id="longstrider:npc")
            assert sm.submit_command(applied, package, context=context).status == "committed"
            command = prepare_cast(
                session,
                "dispel:npc",
                "dispel-magic",
                3,
                {"kind": "creature", "target_id": NPC},
            )
            with measure(sm) as (counts, _):
                result = sm.submit_command(command, package, context=context)
            assert result.status == "committed", result
            observed.append(tuple(counts[k] for k in ("sql_selects", "sql_rows", "returned_bytes")))
    assert observed[0] == observed[1]


def test_observer_cannot_see_lit_cell_across_committed_magical_darkness(magic_world):
    sm, sid, package, context = magic_world
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        target = "9,0"

        def observe():
            return sm.environment.query_area(
                sid, pc, A, EnvironmentArea((target,)), package, context=context
            )

        assert observe()[0].cell == target
        _, darkness = cast(
            magic_world, "intercept", "darkness", 2, {"kind": "point", "cell_id": "5,0"}
        )
        assert darkness.status == "committed", darkness
        (light,) = sm.get_illumination_area(sid, pc, (target,), package, context=context)
        assert light["illumination"] == "bright"
        assert "magical_darkness" not in light
        with pytest.raises(StateMachineError) as hidden:
            observe()
        assert hidden.value.code == "unsupported_target_visibility"

        dropped = submit_world(sm, sid, package, context, DropWorldConcentration(), "drop")
        assert dropped.status == "committed", dropped
        assert observe()[0].cell == target


def test_public_views_apply_darkness_to_each_observers_actual_sight_line(timed_spell_world):
    from test_object_spatial_service import changed_grid

    from trpg_state_machine.perception import GameViewContext

    marker = "object.darkness_view_marker"

    def configure(data):
        """Place an ordinary visible marker beyond the spell and near the NPC."""
        changed_grid(data, width=14, default_lighting="bright")
        data["manifest"]["schema_version"] = "state-machine-foundation/6"
        data["lockable_doors"] = []
        next(actor for actor in data["actors"] if actor["actor_id"] == NPC)["initial_cell"] = "11,2"
        data["scenes"][0]["objects"].append(
            {
                "object_id": marker,
                "state": None,
                "movable": False,
                "blocks_passage": False,
                "initial_cell": "9,0",
            }
        )
        data.setdefault("world_objects", []).append(
            {
                "object_id": marker,
                "definition_ref": "definition.darkness_view_marker",
                "display_name": "Stone marker",
                "description_ref": None,
                "material": None,
                "size_category": "small",
                "weight_lb": 1,
                "capabilities": [],
            }
        )

    session = timed_spell_world(additional_spells=("darkness",), configure=configure)
    sm, sid, package, context = session
    with sm:
        viewer = GameViewContext(context.principal_id, sid, context.player_id, "player")

        def current_player_view():
            """Read the player's authorized scene projection."""
            return sm.get_scene_view(sid, package, context=viewer)

        def npc_sees_marker():
            """Read the other actor's public spatial view under its control grant."""
            return marker in {
                obj[0] for obj in sm.get_spatial_view(sid, NPC, package, context=context).objects
            }

        before = current_player_view()
        assert marker in dict(before.visible_objects)
        assert current_player_view() == before and npc_sees_marker()

        _, receipt = cast(
            session, "darkness-view", "darkness", 2, {"kind": "point", "cell_id": "5,0"}
        )
        assert receipt.status == "committed", receipt
        covered = current_player_view()
        assert covered.view_world_version > before.view_world_version
        assert marker not in dict(covered.visible_objects) and npc_sees_marker()
        assert current_player_view() == covered and marker in dict(before.visible_objects)

        receipt = submit_world(sm, sid, package, context, DropWorldConcentration(), "restore-view")
        assert receipt.status == "committed", receipt
        restored = current_player_view()
        assert restored.view_world_version > covered.view_world_version
        assert marker in dict(restored.visible_objects) and npc_sees_marker()


def test_owned_item_target_and_failed_adoption_preserve_state(magic_world, monkeypatch):
    sm, sid, package, context = magic_world
    with sm:
        _, illegal = cast(
            magic_world, "owned-dark", "darkness", 2, {"kind": "item", "item_id": ITEM}
        )
        assert (illegal.status, illegal.error_code) == ("rejected", "target_invalid")
        baseline = sm.read_noncombat_snapshot(sid, package, context=context)

        def abort(*_):
            raise RuntimeError("failure during spatial rule adoption")

        monkeypatch.setattr(sm._world.magic, "reconcile_scene", abort)
        with pytest.raises(RuntimeError, match="spatial rule adoption"):
            cast(magic_world, "aborted", "darkness", 2, {"kind": "point", "cell_id": "2,0"})
        assert sources(magic_world) == ()
        assert sm.read_noncombat_snapshot(sid, package, context=context) == baseline
        assert sm._conn.execute("SELECT count(*) FROM effect_concentrations").fetchone()[0] == 0


def test_movement_into_dim_radius_reconciles_without_recasting(magic_world):
    sm, sid, package, context = magic_world
    with sm:
        _, light = cast(magic_world, "moving-light", "light", 0, {"kind": "item", "item_id": ITEM})
        assert light.status == "committed", light
        _, dark = cast(
            magic_world, "fixed-dark", "darkness", 2, {"kind": "point", "cell_id": "12,0"}
        )
        assert dark.status == "committed", dark
        assert {source.effect_id for source in sources(magic_world)} == {
            "spatial:moving-light",
            "spatial:fixed-dark",
        }
        first = submit_world(sm, sid, package, context, StepActor(A, "0,0", "0,1"), "north")
        assert first.status == "committed", first
        moved = submit_world(sm, sid, package, context, StepActor(A, "0,1", "1,1"), "overlap")
        assert moved.status == "committed", moved
        assert [source.effect_id for source in sources(magic_world)] == ["spatial:fixed-dark"]
        (dispel,) = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_type == "effect.spatial_light_ended"
        ]
        assert dispel.source_command_id == "overlap" and dispel.payload["reason"] == "dispelled"


@pytest.mark.parametrize("magic_world", [True], indirect=True)
def test_opaque_container_suppresses_effect_and_cannot_be_targeted_by_hidden_id(
    magic_world, tmp_path
):
    sm, sid, package, context = magic_world
    with sm:
        picked = submit_world(
            sm, sid, package, context, PickupItem(DAGGER, A, "0,1"), "take-dagger"
        )
        assert picked.status == "committed", picked
        _, light = cast(magic_world, "open-light", "light", 0, {"kind": "item", "item_id": DAGGER})
        assert light.status == "committed", light
        put = submit_world(sm, sid, package, context, PutItem(A, DAGGER, CASE, "0,0"), "stow")
        assert put.status == "committed", put
        (source,) = sources(magic_world)
        assert source.cell == "0,1" and source.effect_id == "spatial:open-light"
        closed = submit_world(
            sm, sid, package, context, SetObjectState(A, CASE, ObjectState.CLOSED), "cover"
        )
        assert closed.status == "committed", closed
        assert sources(magic_world) == ()
        _, denied = cast(
            magic_world, "hidden-light", "light", 0, {"kind": "item", "item_id": DAGGER}
        )
        assert (denied.status, denied.error_code) == ("rejected", "spell.target")
        opened = submit_world(
            sm, sid, package, context, SetObjectState(A, CASE, ObjectState.OPEN), "uncover"
        )
        assert opened.status == "committed", opened
        assert sources(magic_world) == (source,)
        loader = sm._rules.loader

    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as recovered:
        assert sources((recovered, sid, package, context)) == (source,)
        # Inspect the null-safe cold-load lookup SessionMemory actually uses.
        for table, column, value, index in (
            ("magic_light_anchors", "scene_id", A, "magic_light_anchors_on_scene"),
            ("spatial_light_effects", "fixed_scene_id", A, "spatial_light_effects_on_scene"),
            ("spatial_light_effects", "object_id", DAGGER, "spatial_light_effects_on_object"),
        ):
            plan = recovered._conn.execute(
                f'EXPLAIN QUERY PLAN SELECT * FROM "{table}" '
                f'WHERE "{column}" IS ? AND "session_id" IS ?',
                (value, sid),
            ).fetchall()
            assert any(index in row[3] and f"{column}=?" in row[3] for row in plan)


@pytest.fixture
def concentration_trap(timed_spell_world):
    def create(*, seed, temp_hp=0, actions=None):
        """Build a public trigger-actor mechanism outside the Darkness footprint."""

        def configure(data):
            trap = rule_data(max_uses=1, actions=actions)
            data["manifest"]["schema_version"] = "state-machine-foundation/10"
            data["lockable_doors"] = []
            data["world_objects"] = [
                {**entry, "size_category": "small"}
                for entry in trap["world_objects"]
                if entry["object_id"] == LEVER
            ]
            data["scenes"][0]["objects"].extend(
                entry for entry in trap["scenes"][0]["objects"] if entry["object_id"] == LEVER
            )
            for foundation in data["rule_bindings"].values():
                actor, npc = foundation["snapshot"]["character_states"]
                actor.update(constitution=12, temp_hp=temp_hp)
                npc["granted_features"] = []

        return timed_spell_world(
            additional_spells=("darkness", "light"), configure=configure, walking=True, seed=seed
        )

    return create


def darkened_concentration(session):
    """Establish Darkness through the public cast before testing mechanism damage."""
    _, dark = cast(session, "dark", "darkness", 2, {"kind": "point", "cell_id": "8,0"})
    assert dark.status == "committed", dark
    (concentration,) = sources(session)
    return concentration


def concentration_light(session):
    """Read the same authorized cell before and after the command."""
    sm, sid, package, context = session
    actor = sm.read_session(sid, package, context=context).pc_actor_id
    (cell,) = sm.get_illumination_area(sid, actor, ("8,0",), package, context=context)
    return cell


def concentration_rows(sm):
    """Read durable Effects rows alongside HP, RNG, activation and event facts."""
    return state_rows(sm) | {
        table: [tuple(row) for row in sm._conn.execute(f"SELECT * FROM {table}")]
        for table in ("effect_concentrations", "ongoing_effects", "spatial_light_effects")
    }


@pytest.mark.parametrize("seed,temp_hp,retained", [(0, 0, True), (2, 0, False), (2, 5, False)])
def test_mechanism_damage_settles_spatial_concentration(
    concentration_trap, seed, temp_hp, retained
):
    def absorb_with_temporary_hp(request, result):
        damage = result.state_delta[0]
        return result.model_copy(
            update={
                "state_delta": (
                    TempHPSet(
                        kind="actor.temp_hp_set",
                        target_id=damage.target_id,
                        expected_temp_hp=temp_hp,
                        temp_hp=temp_hp - 2,
                    ),
                )
            }
        )

    session = concentration_trap(seed=seed, temp_hp=temp_hp)
    sm, sid, package, context = session
    with sm:
        normal_light = concentration_light(session)
        concentration = darkened_concentration(session)
        old_light = concentration_light(session)
        old_snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        assert old_light["magical_darkness"]
        hp = sm.read_session(sid, package, context=context).character.current_hp
        rng = sm.read_rng_context(sid, package, context=context)
        expected_rng = rng.state.restore()
        expected_rng.getrandbits(32)
        port_rng = RNGState.capture(expected_rng)
        expected_roll = expected_rng.randint(1, 20)
        port = install(sm, FixturePort(mutate=absorb_with_temporary_hp if temp_hp else None))
        cmd = world_command(
            sm, sid, package, context, ActivateMechanism(A, LEVER, "0,0", "up"), "hit"
        )
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(cmd, package, context=context) == receipt
        assert len(port.requests) == 1
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        actor = next(actor for actor in after.character_states if actor.entity_id == cmd.actor_id)
        assert (actor.hp_current, actor.temp_hp) == ((hp, 3) if temp_hp else (hp - 2, 0))
        final_rng = sm.read_rng_context(sid, package, context=context)
        assert (final_rng.version, final_rng.state) == (
            rng.version + 1,
            RNGState.capture(expected_rng),
        )
        events = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.source_command_id == "hit"
        ]
        activation = next(event for event in events if event.event_type == "mechanism.activated")
        (audit,) = activation.payload["rule_results"]
        (check,) = audit["concentration_checks"]
        assert audit["rng_transition"]["next_state"] == port_rng.model_dump(mode="json")
        assert audit["adopted_rng_transition"]["input_state"] == rng.state.model_dump(mode="json")
        assert audit["adopted_rng_transition"]["next_state"] == final_rng.state.model_dump(
            mode="json"
        )
        assert (
            check["damage_instance_id"] == f"{audit['source']['consequence_id']}:{actor.entity_id}"
        )
        assert check["concentration_id"] == concentration.concentration_identity[1]
        assert check["outcome"] == ("retained" if retained else "broken")
        assert check["check"]["natural"] == expected_roll
        assert check["check"]["dc"] == 10
        ended = [event for event in events if event.event_type == "effect.spatial_light_ended"]
        assert len(ended) == (0 if retained else 1)
        if ended:
            assert ended[0].payload["effect_id"] == concentration.effect_id
            assert ended[0].payload["reason"] == "concentration_ended"
        assert sources(session) == ((concentration,) if retained else ())
        assert concentration_light(session) == (old_light if retained else normal_light)
        assert old_light["magical_darkness"]
        assert GridTopology(old_snapshot.scene_state.grid).magical_darkness_on_cell("8,0")
        assert GridTopology(after.scene_state.grid).magical_darkness_on_cell("8,0") is retained


def test_later_mechanism_refusal_rolls_back_spatial_concentration(concentration_trap, tmp_path):
    session = concentration_trap(seed=2, actions=[rule_action(), rule_action()])
    sm, sid, package, context = session
    with sm:
        concentration = darkened_concentration(session)
        before = concentration_rows(sm)
        old_snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        old_light = concentration_light(session)
        port = install(sm, FixturePort(status="rejected", fail_at=2))
        cmd = world_command(
            sm, sid, package, context, ActivateMechanism(A, LEVER, "0,0", "up"), "refuse"
        )
        receipt = sm.submit_command(cmd, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "mechanism_rule_rejected")
        assert sm.submit_command(cmd, package, context=context) == receipt
        _, second = port.requests
        assert not GridTopology(second.state_snapshot.scene_state.grid).magical_darkness_on_cell(
            "8,0"
        )
        assert concentration_rows(sm) == before
        assert sm.read_noncombat_snapshot(sid, package, context=context) == old_snapshot
        assert sources(session) == (concentration,)
        assert concentration_light(session) == old_light
        assert not receipt.event_seqs
        loader = sm._rules.loader
    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as reopened:
        assert concentration_rows(reopened) == before
        assert sources((reopened, sid, package, context)) == (concentration,)
        assert reopened.submit_command(cmd, package, context=context) == receipt


def test_zero_mechanism_damage_keeps_spatial_concentration_without_a_save(concentration_trap):
    def reduce_to_zero(request, result):
        (packet,) = result.proposed_events
        return result.model_copy(
            update={
                "state_delta": (),
                "proposed_events": (packet.model_copy(update={"amount": 0}),),
            }
        )

    session = concentration_trap(seed=2)
    sm, sid, package, context = session
    with sm:
        concentration = darkened_concentration(session)
        rng = sm.read_rng_context(sid, package, context=context)
        expected_rng = rng.state.restore()
        expected_rng.getrandbits(32)
        install(sm, FixturePort(mutate=reduce_to_zero))
        receipt = submit_world(
            sm, sid, package, context, ActivateMechanism(A, LEVER, "0,0", "up"), "zero"
        )
        assert receipt.status == "committed", receipt
        assert sources(session) == (concentration,)
        assert sm.read_rng_context(sid, package, context=context).state == RNGState.capture(
            expected_rng
        )


@pytest.mark.parametrize("packet_fault", ["omitted", "duplicate", "overstated"])
def test_incomplete_mechanism_damage_packet_cannot_skip_concentration(
    concentration_trap, packet_fault
):
    def invalid_packet(request, result):
        (packet,) = result.proposed_events
        events = {
            "omitted": (),
            "duplicate": (packet, packet),
            "overstated": (packet.model_copy(update={"amount": 30}),),
        }[packet_fault]
        return result.model_copy(update={"proposed_events": events})

    session = concentration_trap(seed=2)
    sm, sid, package, context = session
    with sm:
        concentration = darkened_concentration(session)
        before = concentration_rows(sm)
        install(sm, FixturePort(mutate=invalid_packet))
        with pytest.raises(
            StateMachineError, match="damage audit disagrees|multiple damage packets"
        ) as error:
            submit_world(
                sm,
                sid,
                package,
                context,
                ActivateMechanism(A, LEVER, "0,0", "up"),
                "no-damage-audit",
            )
        assert error.value.code == "invalid_rule_result"
        assert concentration_rows(sm) == before
        assert sources(session) == (concentration,)


def test_mechanism_damage_ends_actor_members_of_the_same_group(concentration_trap):
    session = concentration_trap(
        seed=2, actions=[rule_action(scope="area", cells=["11,2"], actor_kind="npc")]
    )
    sm, sid, package, context = session
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        _, lit = cast(session, "independent-light", "light", 0, {"kind": "item", "item_id": LEVER})
        assert lit.status == "committed", lit
        (light,) = sources(session)
        with sm._store.transaction(write=True):
            sm._characters.effects.create_concentration(
                sid,
                light.effect_id,
                NPC,
                "effect.one",
                3,
                [
                    {"effect": {**_effect(actor, effect_id), "statuses": []}}
                    for effect_id in ("effect.one", "effect.two")
                ],
            )
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        assert len(before.effect_states) == 2
        assert sm._characters.effects.concentration_reference(sid, NPC) == "effect.one"
        install(sm)
        receipt = submit_world(
            sm, sid, package, context, ActivateMechanism(A, LEVER, "0,0", "up"), "actor-group-hit"
        )
        assert receipt.status == "committed", receipt
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert not after.effect_states
        assert sm._characters.effects.concentration_reference(sid, NPC) is None
        assert len(before.effect_states) == 2
        assert sources(session) == (light,)


def test_item_cast_preserves_disclosure_for_unseen_and_foreign_targets(timed_spell_world):
    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        next(item for item in data["item_transfers"] if item["instance_id"] == SECOND)[
            "initial_owner_id"
        ] = NPC
        data["scenes"][0]["grid"]["default_lighting"] = "dark"
        for foundation in data["rule_bindings"].values():
            snapshot = foundation["snapshot"]
            snapshot["scene_state"]["grid"]["default_lighting"] = "dark"
            next(actor for actor in snapshot["character_states"] if actor["entity_id"] == NPC)[
                "carried_item_slugs"
            ] = ["dagger"]
            snapshot["inventory_state"]["entries"].append(
                {
                    "instance_id": SECOND,
                    "owner_id": NPC,
                    "item_slug": "dagger",
                    "quantity": 1,
                    "charges_remaining_per_unit": 0,
                    "accessible": True,
                }
            )

    session = timed_spell_world(additional_spells=("darkness",), configure=configure)
    sm, sid, package, context = session
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        assert sm._world_facts.placement(sid, SECOND)["carrier_id"] == NPC
        assert sm._world_facts.placement(sid, DAGGER)["scene_id"] == A
        for target in (SECOND, DAGGER, "object.unrecognized"):
            _, denied = cast(
                session, f"cast.{target}", "darkness", 2, {"kind": "item", "item_id": target}
            )
            assert (denied.status, denied.error_code) == ("rejected", "spell.target")
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sources(session) == ()
