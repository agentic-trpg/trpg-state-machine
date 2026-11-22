"""Public spatial spell casting and its durable Effects lifecycle."""

import json
from dataclasses import replace
from unittest.mock import patch

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
from test_timed_spell_cast import PERCEPTION_SPELL, cast_command
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
    natural_fog=False,
    database=None,
    grid_extent=None,
    configure_extra=None,
):
    """Make a fixed spatial cast scene with optional unrelated placed objects."""

    def configure(data):
        data["manifest"]["schema_version"] = "state-machine-foundation/6"
        data["lockable_doors"] = []
        if natural_fog or high_slot:
            data["rule_bindings"].pop("combat_foundation", None)
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
            scene["grid"]["width"] = grid_extent or 14
            if grid_extent is not None:
                scene["grid"]["height"] = grid_extent
            if natural_fog:
                scene["grid"]["obscurement_cells"]["13,1"] = "heavy"
        for binding, foundation in data["rule_bindings"].items():
            foundation["snapshot"]["scene_state"]["grid"]["width"] = grid_extent or 14
            if grid_extent is not None:
                foundation["snapshot"]["scene_state"]["grid"]["height"] = grid_extent
            if natural_fog and binding == "noncombat_foundation":
                foundation["snapshot"]["scene_state"]["grid"]["obscurement_cells"]["13,1"] = "heavy"
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
                scene["grid"]["width"] = grid_extent or 14
                if grid_extent is not None:
                    scene["grid"]["height"] = grid_extent
                if natural_fog:
                    scene["grid"]["obscurement_cells"]["13,1"] = "heavy"
        if configure_extra is not None:
            configure_extra(data)

    if database is None:
        name = f"magic-{growth}.sqlite" if growth else "timed-spell.sqlite"
        database = f"magic-high-{seed}.sqlite" if high_slot else name
    return timed_spell_world(
        additional_spells=("light", "darkness", "dispel-magic", *additional_spells),
        database=database,
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
    wire = {
        "schema_version": "sm-command/1",
        "session_id": sid,
        "command_id": command_id,
        "actor_id": state.pc_actor_id,
        "command_type": "world.cast_spell",
        "expected_world_version": state.world_version,
        "payload": payload,
    }
    command = world_command_from_data(json.loads(json.dumps(wire)))
    assert type(command.payload) is ExplorationEnvironmentalSpellCastPayload
    return command


def sources(session):
    sm, sid, package, _ = session
    with sm._store.transaction():
        return sm._world.magic.active_magic_sources(sid, A, package)


def test_fog_cloud_public_cast_obscures_authorized_sight_and_preserves_natural_fog(
    timed_spell_world,
):
    """The spell's fixed area affects vision; ending it retains a separate map source."""
    session = _magic_world(timed_spell_world, additional_spells=("fog-cloud",), natural_fog=True)
    sm, sid, package, context = session
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        initial = sm.read_noncombat_snapshot(sid, package, context=context)
        before = sm.get_spatial_view(sid, actor, package, context=context)
        old_topology = sm._world.grid.base_topology(sid, A, package)
        assert old_topology.can_see("0,0", "2,0")
        assert before.objects
        command, receipt = cast(session, "fog", "fog-cloud", 1, {"kind": "point", "cell_id": "2,0"})
        assert receipt.status == "committed", receipt
        (source,) = sources(session)
        assert source.spec.kind == "fog"
        assert source.origin == "2,0" and source.spec.obscurement == "heavy"
        during = sm.read_noncombat_snapshot(sid, package, context=context)
        assert during.scene_state.grid.obscurement_cells["2,0"] == "heavy"
        assert during.scene_state.grid.obscurement_cells["13,1"] == "heavy"
        obscured = sm.get_spatial_view(sid, actor, package, context=context)
        assert not obscured.objects
        assert not sm._world.grid.base_topology(sid, A, package).can_see("0,0", "2,0")
        assert sm.submit_command(command, package, context=context) == receipt
        ended = submit_world(sm, sid, package, context, DropWorldConcentration(), "end-fog")
        assert ended.status == "committed", ended
        assert sources(session) == ()
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert "2,0" not in after.scene_state.grid.obscurement_cells
        assert after.scene_state.grid.obscurement_cells["13,1"] == "heavy"
        assert sm.get_spatial_view(sid, actor, package, context=context).objects == before.objects
        assert sm._world.grid.base_topology(sid, A, package).can_see("0,0", "2,0")
        assert old_topology.can_see("0,0", "2,0")
        assert "2,0" not in initial.scene_state.grid.obscurement_cells


def test_fog_cloud_expiry_reopens_without_obscuring_area(timed_spell_world, tmp_path):
    """The existing Effects deadline removes the spell across cold reconstruction."""
    session = _magic_world(timed_spell_world, additional_spells=("fog-cloud",))
    sm, sid, package, context = session
    with sm:
        _, receipt = cast(
            session, "expiring-fog", "fog-cloud", 1, {"kind": "point", "cell_id": "2,0"}
        )
        assert receipt.status == "committed", receipt
        old = sm.read_noncombat_snapshot(sid, package, context=context)
        assert old.scene_state.grid.obscurement_cells["2,0"] == "heavy"
        loader = sm._rules.loader
    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as reopened:
        assert (
            reopened.read_noncombat_snapshot(
                sid, package, context=context
            ).scene_state.grid.obscurement_cells["2,0"]
            == "heavy"
        )
        ended = submit_time((reopened, sid, package, context), AdvanceSessionTime(3600), "fog-hour")
        assert ended.status == "committed", ended
        assert sources((reopened, sid, package, context)) == ()
        assert (
            "2,0"
            not in reopened.read_noncombat_snapshot(
                sid, package, context=context
            ).scene_state.grid.obscurement_cells
        )
        assert old.scene_state.grid.obscurement_cells["2,0"] == "heavy"


def strong_wind_command(session, command_id, cells, *, scene=A):
    """Create a bounded, JSON-decoded Environment attestation for the public entry point."""
    sm, sid, package, context = session
    state = sm.read_session(sid, package, context=context)
    wire = {
        "schema_version": "sm-command/1",
        "session_id": sid,
        "actor_id": state.pc_actor_id,
        "command_id": command_id,
        "command_type": "world.apply_strong_wind",
        "expected_world_version": state.world_version,
        "payload": {"scene_id": scene, "source_id": "wind.test-gust", "cells": cells},
    }
    return world_command_from_data(json.loads(json.dumps(wire)))


def test_authorized_wind_ends_only_hit_fog_and_reopens(timed_spell_world, tmp_path):
    """Publish early expiry, its group teardown and view update in the same command."""
    session = _magic_world(timed_spell_world, additional_spells=("fog-cloud",), natural_fog=True)
    sm, sid, package, context = session
    gm = replace(context, can_manage_environment=True)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        _, light = cast(session, "other-spell", "light", 0, {"kind": "item", "item_id": ITEM})
        assert light.status == "committed", light
        baseline = sm.get_spatial_view(sid, actor, package, context=context)
        _, fog = cast(session, "wind-fog", "fog-cloud", 1, {"kind": "point", "cell_id": "2,0"})
        assert fog.status == "committed", fog
        old = sm.read_noncombat_snapshot(sid, package, context=context)
        old_topology = sm._world.grid.base_topology(sid, A, package)
        rng = sm.read_rng_context(sid, package, context=context)
        assert not old_topology.can_see("0,0", "2,0")
        assert not sm.get_spatial_view(sid, actor, package, context=context).objects
        assert sm._characters.effects.has_world_concentration(sid, actor)

        miss = strong_wind_command(session, "wind-miss", ["13,1"])
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(miss, package, context=context)
        assert denied.value.code == "unauthorized"
        missed = sm.submit_command(miss, package, context=gm)
        assert missed.status == "committed", missed
        assert not missed.event_seqs
        assert {source.effect_id for source in sources(session)} == {
            "spatial:wind-fog",
            "spatial:other-spell",
        }

        bad_scene = strong_wind_command(session, "wrong-scene", ["2,0"], scene="scene.other")
        refused = sm.submit_command(bad_scene, package, context=gm)
        assert (refused.status, refused.error_code) == ("rejected", "invalid_target")
        with pytest.raises(StateMachineError):
            strong_wind_command(session, "duplicate-cell", ["2,0", "2,0"])
        with pytest.raises(StateMachineError):
            strong_wind_command(session, "noncanonical-cell", ["02,0"])
        outside = strong_wind_command(session, "outside-grid", ["30,0"])
        refused = sm.submit_command(outside, package, context=gm)
        assert (refused.status, refused.error_code) == ("rejected", "invalid_cell")

        failing = strong_wind_command(session, "wind-rolled-back", ["3,0"])
        end_event = sm._world.magic.end_event

        def fail_after_removal(*args, **kwargs):
            end_event(*args, **kwargs)
            raise StateMachineError("test_candidate_fault", "Reject after deleting Fog Cloud")

        with patch.object(sm._world.magic, "end_event", side_effect=fail_after_removal):
            rolled_back = sm.submit_command(failing, package, context=gm)
        assert (rolled_back.status, rolled_back.error_code) == ("rejected", "test_candidate_fault")
        assert sm._characters.effects.has_world_concentration(sid, actor)
        assert (
            sm.read_noncombat_snapshot(sid, package, context=context).scene_state == old.scene_state
        )
        assert sm.submit_command(failing, package, context=gm) == rolled_back

        hit = strong_wind_command(session, "wind-hit", ["3,0"])
        dispersed = sm.submit_command(hit, package, context=gm)
        assert dispersed.status == "committed", dispersed
        assert sm.submit_command(hit, package, context=gm) == dispersed
        assert [source.effect_id for source in sources(session)] == ["spatial:other-spell"]
        assert not sm._characters.effects.has_world_concentration(sid, actor)
        assert sm.read_rng_context(sid, package, context=context) == rng
        (event,) = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.source_command_id == "wind-hit"
        ]
        assert event.event_type == "effect.spatial_light_ended"
        assert (
            event.payload["effect_id"],
            event.payload["reason"],
            event.payload["wind_source_id"],
        ) == (
            "spatial:wind-fog",
            "strong_wind",
            "wind.test-gust",
        )
        assert not old_topology.can_see("0,0", "2,0")
        assert sm.get_spatial_view(sid, actor, package, context=context).objects == baseline.objects
        assert old.scene_state.grid.obscurement_cells["2,0"] == "heavy"
        loader = sm._rules.loader

    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as reopened:
        assert reopened.submit_command(hit, package, context=gm) == dispersed
        assert reopened.submit_command(failing, package, context=gm) == rolled_back
        current = reopened.read_noncombat_snapshot(sid, package, context=context)
        assert current.scene_state.grid.obscurement_cells == {"13,1": "heavy"}
        assert not reopened._characters.effects.has_world_concentration(sid, actor)
        assert [source.effect_id for source in sources((reopened, sid, package, context))] == [
            "spatial:other-spell"
        ]


def test_fog_record_stage_reuse_invalidates_on_write_and_savepoint_rollback(timed_spell_world):
    """An in-flight source edit cannot leave a prior stage's spell facts in use."""
    session = _magic_world(timed_spell_world, additional_spells=("fog-cloud",))
    sm, sid, package, _ = session
    with sm:
        _, receipt = cast(session, "fog-stage", "fog-cloud", 1, {"kind": "point", "cell_id": "2,0"})
        assert receipt.status == "committed", receipt
        magic = sm._world.magic
        with sm._transaction(write=True):
            assert magic._records(sid, A)[0]["fixed_cell"] == "2,0"
            assert magic._records(sid, A)[0]["fixed_cell"] == "2,0"
            with pytest.raises(ValueError), sm._store.savepoint():
                sm._conn.execute(
                    "UPDATE spatial_light_effects SET fixed_cell=? "
                    "WHERE session_id=? AND effect_id=?",
                    ("3,0", sid, "spatial:fog-stage"),
                )
                assert magic._records(sid, A)[0]["fixed_cell"] == "3,0"
                raise ValueError("rollback the candidate anchor")
            assert magic._records(sid, A)[0]["fixed_cell"] == "2,0"


def test_fog_cloud_fixed_area_reads_do_not_follow_unrelated_objects(timed_spell_world):
    """The same spell area reads a bounded source and geometry as the scene grows."""
    observations = []
    for growth in (0, 25):
        session = _magic_world(timed_spell_world, growth=growth, additional_spells=("fog-cloud",))
        sm, sid, package, context = session
        with sm:
            _, receipt = cast(
                session, "fog-scope", "fog-cloud", 1, {"kind": "point", "cell_id": "2,0"}
            )
            assert receipt.status == "committed", receipt
            with sm._store.transaction(), measure(sm) as (counts, _):
                cells = sm._world.magic.obscuring_cells(sid, A, package, ("2,0", "13,1"))
            assert cells == {"2,0": "heavy"}
            observations.append((counts["sql_selects"], counts["sql_rows"]))
    assert observations[1] == observations[0]


def _seed_distant_fog_sources(sm, sid, actor, source, count, *, cell="13,1", high=False):
    """Grow valid keyed Effects sources with distinct remote casters for locality tests."""
    if not count:
        return
    conn = sm._conn
    remote = conn.execute(
        "SELECT scene_id FROM scene_instances WHERE session_id=? AND scene_id<>? LIMIT 1",
        (sid, A),
    ).fetchone()[0]
    actor_row = dict(
        conn.execute(
            "SELECT * FROM actors WHERE session_id=? AND actor_id=?", (sid, actor)
        ).fetchone()
    )
    character_row = dict(
        conn.execute(
            "SELECT * FROM characters WHERE session_id=? AND actor_id=?", (sid, actor)
        ).fetchone()
    )
    mechanical_row = dict(
        conn.execute(
            "SELECT * FROM mechanical_actors WHERE session_id=? AND actor_id=?", (sid, actor)
        ).fetchone()
    )

    def insert(table, values):
        columns = tuple(values)
        conn.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(values[column] for column in columns),
        )

    with sm._transaction(write=True):
        for index in range(count):
            caster = f"npc.distant_fog_{index}"
            identity = f"spatial:distant-fog-{index}"
            insert(
                "actors",
                {**actor_row, "actor_id": caster, "kind": "npc", "current_scene_id": remote},
            )
            insert("characters", {**character_row, "actor_id": caster})
            state = json.loads(mechanical_row["state_json"])
            state["entity_id"] = caster
            insert(
                "mechanical_actors",
                {**mechanical_row, "actor_id": caster, "state_json": json.dumps(state)},
            )
            sm._world_facts.instances.put(
                sid,
                "actor",
                caster,
                {"actor_id": caster, "senses": (("visual", True), ("auditory", True))},
            )
            insert(
                "effect_concentrations",
                {
                    "session_id": sid,
                    "concentration_id": identity,
                    "caster_id": caster,
                    "effect_id": identity,
                    "rounds_remaining": 0,
                },
            )
            insert(
                "spatial_light_effects",
                {
                    **source,
                    "effect_id": identity,
                    "caster_id": caster,
                    "fixed_cell": cell,
                    "slot_level": 9 if high and index == 0 else source["slot_level"],
                    "radius_ft": 180 if high and index == 0 else source["radius_ft"],
                    "concentration_id": identity,
                },
            )


def test_wind_command_keeps_local_fog_work_fixed_as_distant_sources_grow(timed_spell_world):
    """A fixed attestation reaches the same Effects rows with 25 additional areas."""
    observed = []
    for growth in (0, 25):
        session = _magic_world(
            timed_spell_world,
            additional_spells=("fog-cloud",),
            database=f"wind-growth-{growth}.sqlite",
        )
        sm, sid, package, context = session
        gm = replace(context, can_manage_environment=True)
        with sm:
            _, fog = cast(session, "near-fog", "fog-cloud", 1, {"kind": "point", "cell_id": "2,0"})
            assert fog.status == "committed", fog
            magic = sm._world.magic
            source = dict(magic.owner.get(sid, "spatial:near-fog"))
            actor = sm.read_session(sid, package, context=context).pc_actor_id
            _seed_distant_fog_sources(sm, sid, actor, source, growth)
            command = strong_wind_command(session, "close-exposure", ["3,0"])
            with (
                patch.object(magic, "_source", wraps=magic._source) as projected,
                patch.object(magic.owner, "spec", wraps=magic.owner.spec) as parsed,
                measure(sm) as (counts, statements),
            ):
                receipt = sm.submit_command(command, package, context=gm)
            assert receipt.status == "committed", receipt
            assert projected.call_count == parsed.call_count == 1
            assert any("AND fixed_cell IN" in sql for sql in statements)
            assert len(sources(session)) == growth
            observed.append((counts["sql_selects"], counts["sql_rows"]))
    assert observed[0] == observed[1]


def test_fixed_sight_skips_distant_fog_areas_and_refreshes_after_public_cast(
    timed_spell_world, tmp_path
):
    """Far same-scene Fog sources do not enter one nearby ray's rule projection."""
    observed = []
    for growth, extent, center, high in (
        (0, None, "13,1", False),
        (25, None, "13,1", False),
        (1, 80, "60,60", True),
        (25, 80, "60,60", True),
    ):
        database = f"fog-growth-{growth}-{extent}.sqlite"
        session = _magic_world(
            timed_spell_world,
            additional_spells=("fog-cloud",),
            database=database,
            grid_extent=extent,
        )
        sm, sid, package, context = session
        with sm:
            actor = sm.read_session(sid, package, context=context).pc_actor_id
            _, receipt = cast(
                session, "template-fog", "fog-cloud", 1, {"kind": "point", "cell_id": "2,0"}
            )
            assert receipt.status == "committed", receipt
            source = dict(
                sm._conn.execute(
                    "SELECT * FROM spatial_light_effects WHERE session_id=? AND effect_id=?",
                    (sid, "spatial:template-fog"),
                ).fetchone()
            )
            assert (
                submit_world(
                    sm, sid, package, context, DropWorldConcentration(), "clear-template"
                ).status
                == "committed"
            )
            _seed_distant_fog_sources(sm, sid, actor, source, growth, cell=center, high=high)
            loader = sm._rules.loader
        with StateMachine(tmp_path / database, dnd_loader=loader) as reopened:
            session = reopened, sid, package, context
            magic = reopened._world.magic
            with (
                patch.object(magic, "_source", wraps=magic._source) as projected,
                patch.object(magic.owner, "spec", wraps=magic.owner.spec) as parsed,
            ):
                with measure(reopened) as (counts, _):
                    first = reopened._world.grid.visibility_topology(
                        sid, A, package, "0,0", ("2,0",)
                    )
                assert first.can_see_target("2,0", None)
                assert projected.call_count == 0
                assert parsed.call_count == 0
            before = reopened.get_spatial_view(sid, actor, package, context=context)
            with patch.object(magic, "_source", wraps=magic._source) as projected:
                cached = reopened._world.grid.visibility_topology(sid, A, package, "0,0", ("2,0",))
                assert cached.can_see_target("2,0", None)
                assert projected.call_count == 0
            observed.append((counts["sql_selects"], counts["sql_rows"]))
            radius_plan = reopened._conn.execute(
                "EXPLAIN QUERY PLAN SELECT radius_ft FROM spatial_light_effects "
                "WHERE session_id=? AND fixed_scene_id=? AND fixed_cell IS NOT NULL "
                "AND json_extract(environment_json, '$.obscurement') != 'none' "
                "ORDER BY radius_ft DESC LIMIT 1",
                (sid, A),
            ).fetchall()
            with measure(reopened) as (_, statements):
                magic.owner.fixed_at(sid, A, ("2,0",))
            (fog_lookup,) = (stmt for stmt in statements if "fixed_cell IN (" in stmt)
            cell_plan = reopened._conn.execute(
                f"EXPLAIN QUERY PLAN {fog_lookup}", (sid, A, "2,0")
            ).fetchall()
            assert any("spatial_light_obscuring_radius" in row[3] for row in radius_plan)
            assert any("spatial_light_obscuring_cell" in row[3] for row in cell_plan)
            command, cast_receipt = cast(
                session, "near-fog", "fog-cloud", 1, {"kind": "point", "cell_id": "2,0"}
            )
            assert cast_receipt.status == "committed", cast_receipt
            with patch.object(magic, "_source", wraps=magic._source) as projected:
                obscured = reopened._world.grid.visibility_topology(
                    sid, A, package, "0,0", ("2,0",)
                )
                assert not obscured.can_see_target("2,0", None)
                assert projected.call_count == 1
            assert reopened.submit_command(command, package, context=context) == cast_receipt
            assert (
                submit_world(
                    reopened, sid, package, context, DropWorldConcentration(), "end-near"
                ).status
                == "committed"
            )
            with patch.object(magic, "_source", wraps=magic._source) as projected:
                restored = reopened._world.grid.visibility_topology(
                    sid, A, package, "0,0", ("2,0",)
                )
                assert restored.can_see_target("2,0", None)
                assert projected.call_count == 0
            after = reopened.get_spatial_view(sid, actor, package, context=context)
            assert (after.objects, after.exits, after.doors) == (
                before.objects,
                before.exits,
                before.doors,
            )
    # A present source adds one indexed scalar-radius lookup and its one row;
    # 25 additional distant sources add no more reads or projections.
    assert observed[1] == (observed[0][0] + 1, observed[0][1] + 1)
    assert observed[3] == observed[2]


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


def test_public_creature_dispel_ends_bound_overlay_spell(timed_spell_world):
    """The public result verifier replays an admitted nonbundled victim identity."""

    def overlay(**kwargs):
        return timed_spell_world(**kwargs, spell_id=PERCEPTION_SPELL)

    session = _magic_world(overlay)
    sm, sid, package, context = session
    with sm:
        command = cast_command(session, spell_id=PERCEPTION_SPELL, command_id="bound-overlay")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        assert any(effect.target_id == command.actor_id for effect in before.effect_states)
        _, dispelled = cast(
            session,
            "dispel:bound-overlay",
            "dispel-magic",
            3,
            {"kind": "creature", "target_id": command.actor_id},
        )
        assert dispelled.status == "committed", dispelled
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert not any(effect.target_id == command.actor_id for effect in after.effect_states)


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


@pytest.mark.parametrize("cast_levels", [(3,), (3, 2, 2, 2)], ids=["one-aid", "four-aid"])
def test_public_creature_dispel_adopts_both_overlapping_hp_applications(
    timed_spell_world, cast_levels
):
    """One creature Dispel reads and adopts its affected HP family once."""
    session = timed_spell_world(
        spell_id="aid",
        caster_class="cleric",
        caster_subclass="life",
        additional_spells=("dispel-magic",),
        configure=lambda data: data["rule_bindings"].pop("combat_foundation"),
    )
    sm, sid, package, context = session
    with sm:
        state = sm.read_session(sid, package, context=context)
        caster = state.pc_actor_id

        def facts(snapshot=None):
            if snapshot is None:
                snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
            actor = next(actor for actor in snapshot.character_states if actor.entity_id == caster)
            slot = next(
                pool.current for pool in snapshot.resource_state.pools if pool.pool_id == "spell:3"
            )
            return (actor.hp_current, actor.hp_max), slot, snapshot.effect_states

        def command(spell_id, slot, command_id, target):
            state = sm.read_session(sid, package, context=context)
            return world_command_from_data(
                json.loads(
                    json.dumps(
                        {
                            "schema_version": "sm-command/1",
                            "session_id": sid,
                            "command_id": command_id,
                            "actor_id": caster,
                            "command_type": "world.cast_spell",
                            "expected_world_version": state.world_version,
                            "payload": {
                                "kind": "world.cast_spell",
                                "spell_id": spell_id,
                                "spell_source_id": "class:cleric",
                                "slot_level": slot,
                                **target,
                            },
                        }
                    )
                )
            )

        base_hp, _, _ = facts()
        for index, slot in enumerate(cast_levels):
            receipt = sm.submit_command(
                command("aid", slot, f"aid:{index}", {"target_id": caster}),
                package,
                context=context,
            )
            assert receipt.status == "committed", receipt
        old_snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        boosted_hp, old_slot, effects = facts(old_snapshot)
        assert boosted_hp == (base_hp[0] + 10, base_hp[1] + 10)
        assert len({effect.origin for effect in effects}) == len(cast_levels)
        rng = sm.read_rng_context(sid, package, context=context)
        dispel = command(
            "dispel-magic",
            3,
            "aid:dispel",
            {
                "target": {"kind": "creature", "target_id": caster},
            },
        )
        effects_owner = sm._characters.effects
        read_group = effects_owner.for_target_effect
        returned_rows = []

        def count_group_rows(*args):
            """Count the actual records read through the Effects owner."""
            rows = read_group(*args)
            returned_rows.append(len(rows))
            return rows

        with (
            patch.object(
                effects_owner, "for_target_effect", side_effect=count_group_rows
            ) as group_reads,
            patch.object(
                sm._characters, "effect_hp_facts", wraps=sm._characters.effect_hp_facts
            ) as hp_reads,
            patch.object(
                sm._characters, "set_hp_pair", wraps=sm._characters.set_hp_pair
            ) as hp_writes,
        ):
            receipt = sm.submit_command(dispel, package, context=context)
        assert receipt.status == "committed", receipt
        group_reads.assert_called_once_with(sid, caster, "effect:aid")
        assert returned_rows == [len(cast_levels)]
        hp_reads.assert_called_once_with(sid, caster)
        hp_writes.assert_called_once()
        assert hp_writes.call_args.args[:2] == (sid, caster)
        assert facts() == (base_hp, old_slot - 1, ())
        assert facts(old_snapshot) == (boosted_hp, old_slot, effects)
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.submit_command(dispel, package, context=context) == receipt
        assert facts() == (base_hp, old_slot - 1, ())


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
