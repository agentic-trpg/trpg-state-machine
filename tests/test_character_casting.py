"""Independent acceptance for explicitly authored casting inputs."""

import json
from collections import Counter
from copy import deepcopy

from dnd_support import create, initialize
from item_support import DAGGER
from modern_support import modern_data, modern_loader
from test_scoped_runtime_validation import grow_scenes, measure

from trpg_state_machine import StateMachine
from trpg_state_machine.errors import StateMachineError
from trpg_state_machine.package import _validate, canonical_json


def test_nonempty_casting_roundtrip_and_scoped_reuse(tmp_path, package_data, context):
    assets = modern_loader()
    data = modern_data(package_data, assets, npc_hp=50)
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        pc = snapshot["character_states"][0]
        pc["spells_known"] = ["light", "hold-person", "mage-hand"]
        snapshot["casting_state"] = {
            "actors": [
                {
                    "actor_id": pc["entity_id"],
                    "verbal_available": False,
                    "sources": [
                        {
                            "source_id": "class.wizard",
                            "class_slug": "wizard",
                            "ability": "int",
                            "known_spells": ["hold-person", "light", "mage-hand"],
                            "prepared_spells": ["light", "hold-person"],
                        },
                        {
                            "source_id": "class.cleric",
                            "class_slug": "cleric",
                            "ability": "wis",
                            "known_spells": ["light", "hold-person"],
                            "prepared_spells": ["hold-person"],
                        },
                    ],
                    "materials": [
                        {
                            "spell_slug": "hold-person",
                            "source_id": "class.cleric",
                            "inventory_instance_id": "explicit.casting.reference",
                            "available": False,
                        },
                        {
                            "spell_slug": "hold-person",
                            "source_id": "class.wizard",
                            "inventory_instance_id": "explicit.casting.reference",
                            "available": True,
                        },
                    ],
                }
            ]
        }
    package = _validate(data)
    db = tmp_path / "casting.sqlite"
    expected = deepcopy(data["rule_bindings"]["noncombat_foundation"]["snapshot"]["casting_state"])
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        original = sm.read_noncombat_snapshot(sid, package, context=context)
        actor = next(a for a in original.character_states if a.entity_type == "Character")
        expected["actors"][0]["actor_id"] = actor.entity_id
        assert original.casting_state.model_dump(mode="json") == expected
        assert actor.spells_known == ["hold-person", "light", "mage-hand"]
    with StateMachine(db, dnd_loader=assets) as sm:
        assert sm.resume_session(sid, package, context=context).session_id == sid
        reopened = sm.read_noncombat_snapshot(sid, package, context=context)
        assert reopened.casting_state == original.casting_state
        assert initialize(sm, sid, package, context).status == "committed"
        combat = sm.read_combat_snapshot(sid, package, context=context)
        assert combat.casting_state == original.casting_state
        assert (
            next(a for a in combat.character_states if a.entity_id == actor.entity_id).spells_known
            == actor.spells_known
        )
        with measure(sm) as (_, sql):
            assert (
                sm.read_combat_snapshot(sid, package, context=context).casting_state
                == original.casting_state
            )
        assert not [s for s in sql if "casting_" in s.lower()], sql


def test_legacy_orphan_material_is_rejected_before_initial_write(tmp_path, package_data, context):
    import pytest

    assets = modern_loader()
    data = modern_data(package_data, assets, npc_hp=50)
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        pc = snapshot["character_states"][0]
        pc["spells_known"] = []
        snapshot["casting_state"] = {
            "actors": [
                {
                    "actor_id": pc["entity_id"],
                    "verbal_available": True,
                    "sources": [
                        {
                            "source_id": "class.wizard",
                            "class_slug": "wizard",
                            "ability": "int",
                            "known_spells": [],
                            "prepared_spells": [],
                        }
                    ],
                    "materials": [
                        {
                            "source_id": "class.wizard",
                            "spell_slug": "hold-person",
                            "inventory_instance_id": "orphan.material",
                            "available": False,
                        }
                    ],
                }
            ]
        }
    package = _validate(data)
    with StateMachine(tmp_path / "orphan.sqlite", dnd_loader=assets) as sm:
        with pytest.raises(StateMachineError) as error:
            create(sm, package, context)
        assert error.value.code == "invalid_combat_foundation"
        assert sm._conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        assert sm._conn.execute("SELECT count(*) FROM scene_instances").fetchone()[0] == 0


def test_casting_candidates_rollback_and_fixed_snapshot_stays_isolated(
    tmp_path, package_data, context
):
    import pytest

    assets = modern_loader()
    data = modern_data(package_data, assets, npc_hp=50)
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        pc = snapshot["character_states"][0]
        pc["spells_known"] = ["hold-person"]
        snapshot["casting_state"] = {
            "actors": [
                {
                    "actor_id": pc["entity_id"],
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
                            "inventory_instance_id": "material.old",
                            "available": False,
                        }
                    ],
                }
            ]
        }
    package = _validate(data)
    db = tmp_path / "rollback.sqlite"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        original = sm.read_noncombat_snapshot(sid, package, context=context)
        actor = original.casting_state.actors[0].actor_id
        bound = sm._characters.casting_material_bound

        def replace_binding(item):
            sm._conn.execute(
                "UPDATE character_spells SET material_instance_id=? "
                "WHERE session_id=? AND actor_id=? AND source_id=? AND spell_slug=?",
                (item, sid, actor, "class.wizard", "hold-person"),
            )

        assert bound(sid, actor, {"material.old"})
        assert not bound(sid, actor, {"material.rolled"})
        assert not bound(sid, actor, {"material.new"})
        with sm._transaction(write=True):
            with pytest.raises(RuntimeError, match="roll back"), sm._store.savepoint():
                replace_binding("material.rolled")
                assert bound(sid, actor, {"material.rolled"})
                assert not bound(sid, actor, {"material.old"})
                raise RuntimeError("roll back")
            assert bound(sid, actor, {"material.old"})
            assert not bound(sid, actor, {"material.rolled"})
            replace_binding("material.new")
        assert original.casting_state.actors[0].materials[0].inventory_instance_id == "material.old"
        assert bound(sid, actor, {"material.new"})
        assert not bound(sid, actor, {"material.rolled", "material.old"})
        current = sm.read_noncombat_snapshot(sid, package, context=context)
        assert current.casting_state.actors[0].materials[0].inventory_instance_id == "material.new"
    with StateMachine(db, dnd_loader=assets) as sm:
        assert sm.resume_session(sid, package, context=context).session_id == sid
        assert (
            sm.read_noncombat_snapshot(sid, package, context=context).casting_state
            == current.casting_state
        )


def test_cold_casting_projection_reuses_one_owner_assembly(
    tmp_path, package_data, context, monkeypatch
):
    assets = modern_loader()
    data = modern_data(package_data, assets, npc_hp=50)
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        pc = snapshot["character_states"][0]
        pc["spells_known"] = ["hold-person"]
        snapshot["casting_state"] = {
            "actors": [
                {
                    "actor_id": pc["entity_id"],
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
                    "materials": [],
                }
            ]
        }
    package = _validate(data)
    db = tmp_path / "cold.sqlite"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
    with StateMachine(db, dnd_loader=assets) as sm:
        import pytest

        from trpg_state_machine.session_memory import Range

        original = Range.read
        matches = []

        def tracked(self, predicates, changes, stats):
            rows = original(self, predicates, changes, stats)
            if self.table == "character_spells":
                matches.extend(row["spell_slug"] for row in rows)
            return rows

        monkeypatch.setattr(Range, "read", tracked)
        snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        assert snapshot.casting_state.actors[0].sources[0].known_spells == ("hold-person",)
        assert matches == ["hold-person"], matches
        aid = snapshot.casting_state.actors[0].actor_id
        old_all = sm._characters.all_spells(sid, aid)
        old_prepared = sm._characters.prepared_spells(sid, aid)
        assert old_all[0] is old_prepared[0]
        assert sm._characters.all_spells(sid, aid)[0] is old_all[0]
        with sm._transaction(write=True):
            with pytest.raises(RuntimeError, match="roll back"), sm._store.savepoint():
                sm._conn.execute(
                    "UPDATE character_spells SET prepared_ordinal=NULL "
                    "WHERE session_id=? AND actor_id=? AND spell_slug=?",
                    (sid, aid, "hold-person"),
                )
                assert sm._characters.prepared_spells(sid, aid) == ()
                raise RuntimeError("roll back")
            assert sm._characters.all_spells(sid, aid)[0] is old_all[0]
            assert sm._characters.prepared_spells(sid, aid)[0] is old_all[0]


def test_indexed_material_lookup_after_cold_boot(tmp_path, package_data, context, monkeypatch):
    import sqlite3

    from trpg_state_machine.session_memory import Range

    assets = modern_loader()
    data = modern_data(package_data, assets, npc_hp=50)
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        pc, npc = snapshot["character_states"]
        snapshot["casting_state"] = {
            "actors": [
                {
                    "actor_id": pc["entity_id"],
                    "verbal_available": True,
                    "sources": [],
                    "materials": [],
                },
                {
                    "actor_id": npc["entity_id"],
                    "verbal_available": True,
                    "sources": [],
                    "materials": [],
                },
            ]
        }
    package = _validate(data)
    db = tmp_path / "index.sqlite"

    def cold_vm_probe(sid, owner):
        """Count SQLite work for SessionMemory's actual nullable-range predicate."""
        sql = (
            "SELECT material_instance_id FROM character_spells "
            "WHERE session_id IS ? AND actor_id IS ? AND material_instance_id IS ?"
        )
        params = (sid, owner, "test.unknown")
        with sqlite3.connect(db) as conn:
            plan = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
            steps = 0

            def tick():
                nonlocal steps
                steps += 1

            conn.set_progress_handler(tick, 1)
            try:
                assert conn.execute(sql, params).fetchone() is None
            finally:
                conn.set_progress_handler(None, 0)
        return plan, steps

    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        owner = sm.read_session(sid, package, context=context).pc_actor_id

        def add_spells(start, stop):
            with sm._transaction(write=True):
                for ordinal in range(start, stop):
                    source = f"class.review_{ordinal}"
                    sm._conn.execute(
                        "INSERT INTO character_spell_sources (session_id, actor_id, source_id, "
                        "class_slug, ability, ordinal) VALUES (?,?,?,?,?,?)",
                        (sid, owner, source, source, "int", ordinal),
                    )
                    sm._conn.execute(
                        "INSERT INTO character_spells "
                        "(session_id, actor_id, source_id, spell_slug, known_ordinal, "
                        "material_instance_id, material_available) VALUES (?,?,?,?,?,?,?)",
                        (sid, owner, source, "hold-person", 0, f"test.material_{ordinal}", 0),
                    )

        add_spells(0, 1)
        small_plan, small_steps = cold_vm_probe(sid, owner)
        add_spells(1, 97)
        large_plan, large_steps = cold_vm_probe(sid, owner)
        for plan in (small_plan, large_plan):
            assert any("character_spells_material_owner" in row[-1] for row in plan), plan
        assert large_steps <= small_steps + 5, (small_steps, large_steps)
    with sqlite3.connect(db) as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT material_instance_id FROM character_spells "
            "WHERE session_id=? AND actor_id=? AND material_instance_id=?",
            (sid, owner, "test.material_0"),
        ).fetchall()
        assert any("character_spells_material_owner" in row[-1] for row in plan), plan
    with StateMachine(db, dnd_loader=assets) as sm:
        original = Range.read
        counts = []

        def tracked(self, predicates, changes, stats):
            rows = original(self, predicates, changes, stats)
            if self.table == "character_spells":
                counts.append(len(rows))
            return rows

        monkeypatch.setattr(Range, "read", tracked)
        bound = sm._characters.casting_material_bound
        assert bound(sid, owner, {"test.material_0"})
        assert not bound(sid, owner, {"test.unknown"})
        assert counts == [1, 0]
        with measure(sm) as (_, sql):
            assert bound(sid, owner, {"test.material_0"})
            assert not bound(sid, owner, {"test.unknown"})
        assert counts == [1, 0, 1, 0]
        assert not [query for query in sql if "character_spells" in query.lower()]


def test_public_v20_casting_projection_scales_with_fixed_actor(tmp_path, context, monkeypatch):
    """A fixed combat roster reads one source/spell closure as other world facts grow."""
    from test_cure_wounds import cure_world

    from trpg_state_machine.session_memory import Range

    sm, sid, package, _, _ = cure_world(tmp_path, context)
    with sm:
        counts = Counter()
        original = Range.read

        def tracked(self, predicates, changes, stats):
            rows = original(self, predicates, changes, stats)
            if self.table in ("character_spell_sources", "character_spells"):
                counts[self.table] += len(rows)
            return rows

        monkeypatch.setattr(Range, "read", tracked)

        def cold_projection():
            sm._store.release_memory()
            counts.clear()
            before_builds = sm._store.memory.stats["instance_builds"]
            with measure(sm) as (cost, sql):
                snap = sm.read_combat_snapshot(sid, package, context=context)
            assert snap.snapshot_schema_version == "engine-snapshot/20"
            selection = snap.casting_state.actors[0].sources[0].spells[0]
            assert selection.spell_id == "cure-wounds"
            assert selection.prepared_order == 0
            assert selection.material is None
            return (
                counts.copy(),
                cost.copy(),
                sm._store.memory.stats["instance_builds"] - before_builds,
                sql,
            )

        small_rows, small_cost, small_builds, small_sql = cold_projection()
        assert small_rows["character_spells"] == 1
        assert small_rows["character_spell_sources"] <= 2

        f, conn = sm._world_facts, sm._conn
        remote = next(
            row[0]
            for row in conn.execute(
                "SELECT scene_id FROM scene_instances WHERE session_id=? AND scene_id<>?",
                (sid, "scene.platform"),
            )
        )
        template = f.instances.get(sid, "object", DAGGER)
        with sm._transaction(write=True):
            conn.execute(
                "UPDATE mechanical_scenes SET state_json=json_set(state_json,'$.grid.width',?) "
                "WHERE session_id=? AND scene_id=?",
                (50, sid, remote),
            )
            for index in range(40):
                oid = f"object.spell_unrelated_{index}"
                definition = json.loads(template["definition"])
                if definition is not None:
                    definition["fields"]["object_id"] = oid
                    definition["fields"]["capabilities"] = {"tuple": []}
                conn.execute(
                    "INSERT INTO objects(session_id,object_id,scene_id,cell,orientation,"
                    "portable_marker,item_slug,item_charges,item_accessible) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, oid, remote, "2,2", "north", "{}", "dagger", 0, 1),
                )
                f.instances.put(
                    sid,
                    "object",
                    oid,
                    {
                        **template,
                        "definition": canonical_json(definition),
                        "movable": False,
                        "blocks_passage": False,
                    },
                )
                actor = f"npc.spell_unrelated_{index}"
                conn.execute(
                    "INSERT INTO actors(session_id,actor_id,kind,current_scene_id,cell) "
                    "VALUES (?,?,'npc',?,?)",
                    (sid, actor, remote, f"{index + 3},1"),
                )
                f.instances.put(
                    sid,
                    "actor",
                    actor,
                    {
                        "actor_id": actor,
                        "senses": (("visual", True),),
                    },
                )
        grow_scenes(sm, sid, 20)
        large_rows, large_cost, large_builds, large_sql = cold_projection()
        assert large_rows == small_rows
        assert large_builds == small_builds
        assert large_cost["sql_rows"] <= small_cost["sql_rows"]
        assert sum("FROM character_spells" in query for query in large_sql) == sum(
            "FROM character_spells" in query for query in small_sql
        )

        pc = sm.read_session(sid, package, context=context).pc_actor_id
        sm.read_combat_snapshot(sid, package, context=context)
        with sm._transaction(write=True):
            conn.execute(
                "UPDATE characters SET current_hp=current_hp-1 WHERE session_id=? AND actor_id=?",
                (sid, pc),
            )
        counts.clear()
        warm = sm.read_combat_snapshot(sid, package, context=context)
        assert warm.casting_state.actors[0].sources[0].spells[0].spell_id == "cure-wounds"
        assert counts["character_spells"] == 0
        assert counts["character_spell_sources"] <= small_rows["character_spell_sources"]


def test_public_v20_snapshot_reuses_version_and_rebuilds_after_spell_commit(tmp_path, context):
    """A public cast invalidates the fixed snapshot while its prior value stays isolated."""
    from test_cure_wounds import cast_command, cure_world

    sm, sid, package, _, _ = cure_world(tmp_path, context)
    with sm:
        old = sm.read_combat_snapshot(sid, package, context=context)
        actor_id = sm.read_session(sid, package, context=context).pc_actor_id
        old_pc = next(actor for actor in old.character_states if actor.entity_id == actor_id)
        builds = sm._store.memory.stats["snapshot_builds"]
        assert sm.read_combat_snapshot(sid, package, context=context) == old
        assert sm._store.memory.stats["snapshot_builds"] == builds

        receipt = sm.submit_command(
            cast_command(sm, sid, package, context), package, context=context
        )
        assert receipt.status == "committed"
        postcommit_builds = sm._store.memory.stats["snapshot_builds"]
        current = sm.read_combat_snapshot(sid, package, context=context)
        assert sm._store.memory.stats["snapshot_builds"] == postcommit_builds + 1
        current_pc = next(
            actor for actor in current.character_states if actor.entity_id == actor_id
        )
        assert current.world_version > old.world_version
        assert current_pc.hp_current > old_pc.hp_current == 2
        assert current_pc.spell_slots[1] == 1
        assert old_pc.spell_slots[1] == 2
