"""Observer preparation, object row locality and public view isolation."""

from collections import Counter
from copy import deepcopy
from dataclasses import replace

import pytest
from lock_support import CRED, NPC, A
from test_object_spatial_service import OBSTACLE, changed_grid, oriented_data
from test_scoped_runtime_validation import CountConnection, grow
from test_world_containers import BOX
from test_world_lights import lighting_data
from world_support import setup, submit

from trpg_state_machine import SetLightEnabled, StateMachine, StateMachineError
from trpg_state_machine.perception import NPCContext
from trpg_state_machine.spatial import ObserverVisibility


@pytest.mark.parametrize("unrelated", (0, 30))
def test_known_object_reads_one_primary_row(tmp_path, context, monkeypatch, unrelated):
    data = oriented_data(anchor="1,0", footprint=[[0, 0]], blocking=False, openable=True)
    writer, sid, package, db = setup(tmp_path, context, data)
    loader = writer._rules.loader
    with writer:
        if unrelated:
            grow(writer, sid, unrelated)

    with StateMachine(db, dnd_loader=loader) as sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        connection = sm._store._conn
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM objects WHERE session_id=? AND object_id=?",
            (sid, OBSTACLE),
        ).fetchone()[3]
        assert "SEARCH objects USING INDEX" in plan
        assert "session_id=? AND object_id=?" in plan

        class CountTargetRows(CountConnection):
            """Count actual SQLite target fetches without replacing the read path."""

            target_selects = 0

            def execute(self, sql, parameters=()):
                """Record targeted object statements before executing them."""
                if 'FROM "objects" WHERE' in sql and OBSTACLE in parameters:
                    self.target_selects += 1
                return super().execute(sql, parameters)

        counted = CountTargetRows(connection, Counter(), [])
        monkeypatch.setattr(sm._store, "_conn", counted)
        memory_select = sm._store.memory.select
        requests = []

        def observe_query(sql, parameters):
            """Track submitted selectors, including requests served by RAM."""
            if "FROM objects WHERE" in sql:
                requests.append((sql, parameters))
            return memory_select(sql, parameters)

        monkeypatch.setattr(sm._store.memory, "select", observe_query)
        view = sm.get_object(sid, actor, OBSTACLE, package, context=context)
        assert view["can_open"] and view["can_close"]
        assert counted.target_selects == 1
        assert not any("scene_id,state,cell FROM objects" in sql for sql, _ in requests)
        assert not any("AND scene_id=" in sql or "AND carrier_id=" in sql for sql, _ in requests)
        assert sm.get_object(sid, actor, OBSTACLE, package, context=context) == view
        assert counted.target_selects == 1
        monkeypatch.setattr(sm._store, "_conn", connection)


def test_list_and_spatial_view_reuse_sight_for_overlapping_targets(tmp_path, context, monkeypatch):
    data = oriented_data(anchor="2,0", footprint=[[0, 0], [0, 1]], blocking=False)
    changed_grid(data, lighting={"2,0": "dark"})
    duplicate = "object.observer_twin"
    obj = deepcopy(data["scenes"][0]["objects"][-1])
    data["scenes"][0]["objects"].append({**obj, "object_id": duplicate})
    profile = deepcopy(data["world_objects"][-1])
    data["world_objects"].append(
        {**profile, "object_id": duplicate, "definition_ref": "definition.observer_twin"}
    )
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        sight_prepare = sm._world._prepare_visibility
        can_see = ObserverVisibility.can_see
        sightings = []

        def observe_scope(*args, **kwargs):
            """Track each real observer preparation and its target cells."""
            sight = sight_prepare(*args, **kwargs)
            sightings.append((sight, tuple(kwargs["cells"])))
            return sight

        def observe_ray(sight, cell):
            """Count rays in the public disclosure pass."""
            if sightings and sight is sightings[0][0]:
                rays[cell] += 1
            return can_see(sight, cell)

        rays = Counter()
        monkeypatch.setattr(sm._world, "_prepare_visibility", observe_scope)
        monkeypatch.setattr(ObserverVisibility, "can_see", observe_ray)
        objects = sm.list_scene_objects(sid, actor, package, context=context)
        ids = {v["object_id"] for v in objects}
        assert {OBSTACLE, duplicate} <= ids
        assert {"2,0", "2,1"} <= set(sightings[0][1])
        assert rays == Counter({cell: 1 for cell in sightings[0][1]})

        sightings.clear()
        rays.clear()
        spatial = sm.get_spatial_view(sid, actor, package, context=context)
        assert not {OBSTACLE, duplicate} & {o[0] for o in spatial.objects}
        assert len(sightings) == 1
        assert rays == Counter({cell: 1 for cell in sightings[0][1]})
        for oid in (OBSTACLE, duplicate):
            assert sm.get_object(sid, actor, oid, package, context=context) in objects


def test_public_views_follow_candidate_light_and_fixed_old_read(tmp_path, context, monkeypatch):
    writer, sid, package, db = setup(tmp_path, context, lighting_data())
    with writer, StateMachine(db, dnd_loader=writer._rules.loader) as reader:
        actor = writer.read_session(sid, package, context=context).pc_actor_id
        writer.grant_npc_access(sid, NPC, "agent:observer", package, context=context)
        npc_context = NPCContext("agent:observer", sid, NPC)

        def viewed(sm):
            """Read spatial and object catalogs through their public entry points."""
            return (
                tuple(
                    v["object_id"]
                    for v in sm.list_scene_objects(sid, actor, package, context=context)
                ),
                sm.get_spatial_view(sid, actor, package, context=context),
            )

        def detail(sm):
            """Read the fixture's target through the public object query."""
            return sm.get_object(sid, actor, BOX, package, context=context)

        with pytest.raises(StateMachineError, match="authorized"):
            detail(writer)
        dark = viewed(writer)
        assert BOX not in dark[0]
        assert BOX not in {obj[0] for obj in dark[1].objects}
        assert CRED in dark[0]
        assert CRED not in {
            v["object_id"]
            for v in writer.list_scene_objects(sid, NPC, package, context=npc_context)
        }
        with pytest.raises(StateMachineError) as denied:
            writer.get_spatial_view(sid, actor, package, context=npc_context)
        assert denied.value.code == "unauthorized"
        with pytest.raises(StateMachineError) as denied:
            writer.list_scene_objects(
                sid, actor, package, context=replace(context, player_id="other")
            )
        assert denied.value.code == "unauthorized"

        with writer._transaction(write=True):
            with pytest.raises(RuntimeError, match="rollback"), writer._store.savepoint():
                writer._world_facts.set_light_state(sid, CRED, True, "normal")
                assert writer._objects._visible(sid, actor, BOX, package)["object_id"] == BOX
                raise RuntimeError("rollback")
            with pytest.raises(StateMachineError, match="authorized"):
                writer._objects._visible(sid, actor, BOX, package)
        assert viewed(writer) == dark
        with pytest.raises(StateMachineError):
            detail(writer)

        prepare = reader._world._prepare_visibility

        def commit_after_reader_starts(*args, **kwargs):
            """Publish on a second Store after the public spatial query starts its read."""
            monkeypatch.setattr(reader._world, "_prepare_visibility", prepare)
            receipt = submit(
                writer, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "light"
            )
            assert receipt.status == "committed"
            return prepare(*args, **kwargs)

        assert viewed(reader) == dark
        monkeypatch.setattr(reader._world, "_prepare_visibility", commit_after_reader_starts)
        fixed = reader.get_spatial_view(sid, actor, package, context=context)
        assert fixed == dark[1]
        bright = viewed(writer)
        assert BOX in bright[0]
        assert BOX in {obj[0] for obj in bright[1].objects}
        assert detail(writer)["object_id"] == BOX
        assert viewed(reader) == bright
        assert fixed.world_version < bright[1].world_version
