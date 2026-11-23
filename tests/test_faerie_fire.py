"""Public Faerie Fire cast and source-linked visual effect smoke tests."""

import json
from dataclasses import replace

from item_support import NPC, ROPE, A, B
from spatial_support import submit as submit_world
from test_scoped_runtime_validation import measure
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import (
    AdvanceSessionTime,
    DropWorldConcentration,
    PickupItem,
    StateMachine,
    StepActor,
    world_command_from_data,
)
from trpg_state_machine.visual_target_state import target_is_invisible


def _grow_remote(sm, sid, amount):
    """Add unrelated persisted actors and objects beyond the selected scene."""
    with sm._transaction(write=True):
        for index in range(amount):
            oid = f"object.remote_{index}"
            sm._conn.execute(
                "INSERT INTO objects(session_id,object_id,scene_id,cell,orientation) "
                "VALUES (?,?,?,?,?)",
                (sid, oid, B, "2,2", "north"),
            )
            sm._conn.execute(
                "INSERT INTO actors(session_id,actor_id,kind,current_scene_id,cell) "
                "VALUES (?,?,'npc',?,?)",
                (sid, f"npc.remote_{index}", B, "2,1"),
            )


def _session(
    timed_spell_world, *, cover=False, dark=False, save_trait=False, database="faerie-fire.sqlite"
):
    """Place a druid, creature and ordinary object inside one bounded cube."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        data["actors"][0]["initial_cell"] = "2,0"
        if save_trait:
            data["rule_bindings"]["noncombat_foundation"]["snapshot"]["character_states"][1][
                "trait_mechanics"
            ] = ["pack_tactics"]
        if dark:
            for grid in (
                data["scenes"][0]["grid"],
                data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"],
            ):
                grid["width"] = 14
                grid["default_lighting"] = "dark"
        if cover:
            data["scenes"][0]["grid"]["cover_cells"]["1,0"] = "total"
            data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"][
                "cover_cells"
            ]["1,0"] = "total"

    return timed_spell_world(
        database=database,
        caster_class="druid",
        caster_subclass="land",
        additional_spells=("faerie-fire",),
        configure=configure,
    )


def _cast_command(session, command_id, *, cell):
    """Decode the same JSON command accepted by the public entry point."""
    sm, sid, package, context = session
    state = sm.read_session(sid, package, context=context)
    wire = {
        "schema_version": "sm-command/1",
        "session_id": sid,
        "command_id": command_id,
        "actor_id": state.pc_actor_id,
        "command_type": "world.cast_spell",
        "expected_world_version": state.world_version,
        "payload": {
            "kind": "world.cast_spell",
            "spell_id": "faerie-fire",
            "spell_source_id": "class:druid",
            "slot_level": 1,
            "target": {"kind": "point", "cell_id": cell},
        },
    }
    return world_command_from_data(json.loads(json.dumps(wire)))


def _cast(session, command_id="faerie", *, cell="2,0"):
    sm, _, package, context = session
    command = _cast_command(session, command_id, cell=cell)
    return command, sm.submit_command(command, package, context=context)


def test_faerie_fire_cast_persists_target_source_and_replays(timed_spell_world):
    """One paid public cast stores only failed saves and automatic object targets."""
    session = _session(timed_spell_world)
    sm, sid, package, context = session
    with sm:
        command, receipt = _cast(session)
        assert receipt.status == "committed", receipt
        with sm._store.transaction():
            sources = sm._store.connection.execute(
                "SELECT * FROM visual_effect_sources WHERE session_id=?", (sid,)
            ).fetchall()
            targets = sm._store.connection.execute(
                "SELECT * FROM target_visual_effects WHERE session_id=?", (sid,)
            ).fetchall()
            assert len(sources) == 1
            assert sources[0]["source_id"] == "visual:faerie"
            assert any(row["target_kind"] == "object" for row in targets)
            assert not target_is_invisible(sm._world, sid, command.actor_id)
            anchors = sm._world.magic.visual_illumination_anchors(sid, A)
            assert any(anchor[3] == "object" for anchor in anchors)
        assert sm.submit_command(command, package, context=context) == receipt
        ended = submit_world(sm, sid, package, context, DropWorldConcentration(), "end-faerie")
        assert ended.status == "committed", ended
        with sm._store.transaction():
            assert not sm._store.connection.execute(
                "SELECT 1 FROM visual_effect_sources WHERE session_id=?", (sid,)
            ).fetchone()


def test_faerie_fire_expires_as_one_source(timed_spell_world):
    """The Session time boundary removes all selected targets and concentration."""
    session = _session(timed_spell_world)
    sm, sid, package, context = session
    with sm:
        _, receipt = _cast(session)
        assert receipt.status == "committed", receipt
        ended = submit_world(sm, sid, package, context, AdvanceSessionTime(60), "minute")
        assert ended.status == "committed", ended
        with sm._store.transaction():
            assert not sm._store.connection.execute(
                "SELECT 1 FROM target_visual_effects WHERE session_id=?", (sid,)
            ).fetchone()


def test_faerie_fire_cube_excludes_creature_behind_total_cover(timed_spell_world):
    """A source-side cube cannot outline an occupant beyond Total Cover."""
    session = _session(timed_spell_world, cover=True)
    sm, sid, package, _ = session
    with sm:
        _, receipt = _cast(session, "cover", cell="0,0")
        assert receipt.status == "committed", receipt
        with sm._store.transaction():
            assert not sm._store.connection.execute(
                "SELECT 1 FROM target_visual_effects WHERE session_id=? "
                "AND target_kind='creature' AND target_id=?",
                (sid, NPC),
            ).fetchone()


def test_faerie_fire_object_light_follows_carrier(timed_spell_world):
    """A source-linked object emitter follows World placement after a public step."""
    session = _session(timed_spell_world)
    sm, sid, package, context = session
    with sm:
        _, receipt = _cast(session, "follow", cell="0,0")
        assert receipt.status == "committed", receipt
        with sm._store.transaction():
            old = sm._world.magic.visual_illumination_anchors(sid, A)
        picked = submit_world(sm, sid, package, context, PickupItem(ROPE, A, "1,0"), "pick")
        assert picked.status == "committed", picked
        moved = submit_world(sm, sid, package, context, StepActor(A, "0,0", "0,1"), "walk")
        assert moved.status == "committed", moved
        with sm._store.transaction():
            current = sm._world.magic.visual_illumination_anchors(sid, A)
        before = {identity: cell for _, identity, cell, kind, _, _ in old if kind == "object"}
        after = {identity: cell for _, identity, cell, kind, _, _ in current if kind == "object"}
        assert before[ROPE] == "1,0"
        assert after[ROPE] == "0,1"


def test_visual_light_anchors_do_not_scan_unrelated_world_growth(timed_spell_world):
    """Cast and selected source reads stay fixed as other-scene instances grow."""
    observed = []
    casts = []
    for growth in (0, 25):
        session = _session(timed_spell_world, database=f"faerie-scale-{growth}.sqlite")
        sm, sid, package, context = session
        with sm:
            if growth:
                _grow_remote(sm, sid, growth)
            command = _cast_command(session, "scale", cell="2,0")
            with measure(sm) as (counts, _):
                receipt = sm.submit_command(command, package, context=context)
            assert receipt.status == "committed", receipt
            casts.append(
                tuple(counts[key] for key in ("sql_selects", "sql_rows", "returned_bytes"))
            )
            with sm._store.transaction():
                with measure(sm) as (counts, _):
                    anchors = sm._world.magic.visual_illumination_anchors(sid, A)
                assert anchors
                observed.append(
                    tuple(counts[key] for key in ("sql_selects", "sql_rows", "returned_bytes"))
                )
    assert observed[0] == observed[1]
    assert casts[0] == casts[1]


def test_environmental_cast_uses_prepared_actor_order(timed_spell_world, monkeypatch):
    """The public cast builds and rechecks the snapshot in RE's declared order."""
    session = _session(timed_spell_world)
    sm, _, _, _ = session
    prepare = sm._rules.prepare_environmental_spell
    snapshot = sm._rules._noncombat_snapshot
    prepared = []
    assembled = []

    def reverse_prepared_order(command, positions):
        result = prepare(command, positions)
        assert result.actor_ids == (command.actor_id, NPC)
        result = replace(result, actor_ids=result.actor_ids[::-1])
        prepared.append(result.actor_ids)
        return result

    def record_snapshot(*args, **kwargs):
        result = snapshot(*args, **kwargs)
        assembled.append(tuple(actor.entity_id for actor in result.character_states))
        return result

    with sm:
        monkeypatch.setattr(sm._rules, "prepare_environmental_spell", reverse_prepared_order)
        monkeypatch.setattr(sm._rules, "_noncombat_snapshot", record_snapshot)
        command, receipt = _cast(session)
        assert receipt.status == "committed", receipt
    assert prepared == [(NPC, command.actor_id)]
    assert len(assembled) == 2
    assert assembled == prepared * 2


def test_unadmitted_save_trait_rejects_before_payment_or_rng(timed_spell_world):
    """RUO-54-F1 preserves slot, target state and RNG for unsupported save facts."""
    session = _session(timed_spell_world, save_trait=True)
    sm, sid, package, context = session
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        _, receipt = _cast(session, "trait-limit", cell="2,0")
        assert receipt.status != "committed"
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert after.resource_state == before.resource_state
        assert sm.read_rng_context(sid, package, context=context) == rng
        with sm._store.transaction():
            assert not sm._store.connection.execute(
                "SELECT 1 FROM visual_effect_sources WHERE session_id=?", (sid,)
            ).fetchone()


def test_faerie_fire_emits_ten_foot_dim_light_and_reopens(timed_spell_world):
    """Actual light queries see a local dim source and its atomic removal."""
    session = _session(timed_spell_world, dark=True)
    sm, sid, package, context = session
    with sm:
        before = sm._world.objects.lights.current_cells(sid, A, package, ("0,0", "13,1"))
        assert before["0,0"].level == "dark"
        _, receipt = _cast(session, "dim", cell="0,0")
        assert receipt.status == "committed", receipt
        during = sm._world.objects.lights.current_cells(sid, A, package, ("0,0", "13,1"))
        assert during["0,0"].level == "dim"
        assert during["13,1"].level == "dark"
        ended = submit_world(sm, sid, package, context, DropWorldConcentration(), "end-dim")
        assert ended.status == "committed", ended
        after = sm._world.objects.lights.current_cells(sid, A, package, ("0,0", "13,1"))
        assert after["0,0"].level == "dark"


def test_faerie_fire_reopens_from_current_schema_and_expires(timed_spell_world, tmp_path):
    """Persisted target anchors reconstruct after restart and expire on one clock."""
    session = _session(timed_spell_world, dark=True, database="faerie-cold.sqlite")
    sm, sid, package, context = session
    with sm:
        _, receipt = _cast(session, "cold", cell="0,0")
        assert receipt.status == "committed", receipt
        assert (
            sm._world.objects.lights.current_cells(sid, A, package, ("0,0",))["0,0"].level == "dim"
        )
        loader = sm._rules.loader
    with StateMachine(tmp_path / "faerie-cold.sqlite", dnd_loader=loader) as reopened:
        light = reopened._world.objects.lights.current_cells(sid, A, package, ("0,0",))
        assert light["0,0"].level == "dim"
        ended = submit_world(reopened, sid, package, context, AdvanceSessionTime(60), "cold-minute")
        assert ended.status == "committed", ended
        assert (
            reopened._world.objects.lights.current_cells(sid, A, package, ("0,0",))["0,0"].level
            == "dark"
        )
