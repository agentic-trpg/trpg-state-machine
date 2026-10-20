"""Reproduce public-path SQL/model/preparation counts against a chosen source tree.

Run with the locked interpreter and PYTHONPATH=<revision>/src;<checkout>/tests.
The sole argument is a NEW fixture output directory; no production database is used.
The observer executes every real query/projection and only records returned work.
"""

import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

from lock_support import CRED, A
from test_lockable_passages import acquire
from test_mechanism_service import ordered_data
from test_object_spatial_service import OBSTACLE, oriented_data
from test_scoped_runtime_validation import grow, grow_scenes, measure
from test_world_containers import BOX, container_data
from test_world_lights import lighting_data
from test_world_mechanisms import LEVER
from world_support import command, setup

from trpg_state_machine import (
    ActivateMechanism,
    PutItem,
    RepositionObject,
    TrustedContext,
    instance_data,
)
from trpg_state_machine.world_lights import LightService

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=False)
ctx = TrustedContext("authenticated:alice", "player.alice", ("npc.caretaker", "npc.attendant"))
result = {}
(root / "placement").mkdir()
sm, sid, p, _ = setup(root / "placement", ctx, oriented_data())
with sm:
    actor = sm.read_session(sid, p, context=ctx).pc_actor_id
    op = RepositionObject(A, OBSTACLE, "1,1", "1,1", "north", "east")
    gm = replace(ctx, can_place_world_objects=True)
    original = instance_data.decode
    builds = Counter()

    def decode(v):
        if isinstance(v, dict) and v.get("type") == "WorldObjectDefinition":
            builds["definition_models"] += 1
        return original(v)

    instance_data.decode = decode
    for n in (0, 100):
        if n:
            grow(sm, sid, n)
            grow_scenes(sm, sid, 20)
        sm._world_facts.maps.clear()
        builds.clear()
        with measure(sm) as (counts, sql):
            assert sm.query_world_action(sid, actor, op, p, context=gm).status == "available"
        result["placement_" + str(n)] = {**counts, **builds}
    instance_data.decode = original
for case, data in [("container", container_data()), ("light_list", lighting_data())]:
    folder = root / case
    folder.mkdir(exist_ok=True)
    sm, sid, p, _ = setup(folder, ctx, data)
    with sm:
        if case == "container":
            acquire(sm, sid, p, ctx)
            cmd = command(sm, sid, p, ctx, PutItem(A, CRED, BOX, "0,0"), "put")
            with measure(sm) as (counts, sql):
                r = sm.submit_command(cmd, p, context=ctx)
            assert r.status == "committed", r
            result[case] = {
                **counts,
                "inventory_updates": sum(
                    s.startswith("UPDATE mechanical_components SET inventory_json") for s in sql
                ),
                "owner_updates": sum(
                    s.startswith("UPDATE characters SET inventory_json") for s in sql
                ),
            }
        else:
            actor = sm.read_session(sid, p, context=ctx).pc_actor_id
            builds = Counter()
            original = LightService.project
            if hasattr(original, "__wrapped__"):
                from trpg_state_machine.scoped_reads import scoped_read

                raw = original.__wrapped__

                def count(self, *args, _counts=builds, _call=raw, **kw):
                    _counts["preparations"] += 1
                    return _call(self, *args, **kw)

                LightService.project = scoped_read(count)
            else:

                def count(self, *args, _counts=builds, _call=original, **kw):
                    _counts["preparations"] += 1
                    return _call(self, *args, **kw)

                LightService.project = count
            with measure(sm) as (counts, sql):
                sm.list_scene_objects(sid, actor, p, context=ctx)
            result[case] = {**counts, **builds}
            LightService.project = original
folder = root / "mechanism"
folder.mkdir(exist_ok=True)
sm, sid, p, _ = setup(folder, ctx, ordered_data())
with sm:
    calls = Counter()
    original = sm._objects.mechanisms._build_index

    def build(*a, **kw):
        calls["builds"] += 1
        return original(*a, **kw)

    sm._objects.mechanisms._build_index = build
    cmd = command(sm, sid, p, ctx, ActivateMechanism(A, LEVER, "0,0"), "activate")
    with measure(sm) as (counts, sql):
        r = sm.submit_command(cmd, p, context=ctx)
    assert r.status == "committed", r
    result["mechanism"] = {**counts, **calls}
print(json.dumps(result, indent=2))
