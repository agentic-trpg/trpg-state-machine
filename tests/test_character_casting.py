"""Independent acceptance for explicitly authored casting inputs."""

from copy import deepcopy

from dnd_support import create, initialize
from modern_support import modern_data, modern_loader
from test_scoped_runtime_validation import measure

from trpg_state_machine import StateMachine
from trpg_state_machine.package import _validate


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
        assert actor.spells_known == ["light", "hold-person", "mage-hand"]
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
                "UPDATE character_spell_materials SET inventory_instance_id=? "
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
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        owner = sm.read_session(sid, package, context=context).pc_actor_id
        with sm._transaction(write=True):
            for ordinal in range(97):
                source = f"class.review_{ordinal}"
                sm._conn.execute(
                    "INSERT INTO character_spell_sources (session_id, actor_id, source_id, "
                    "class_slug, ability, ordinal) VALUES (?,?,?,?,?,?)",
                    (sid, owner, source, source, "int", ordinal),
                )
                sm._conn.execute(
                    "INSERT INTO character_spell_materials "
                    "(session_id, actor_id, source_id, spell_slug, "
                    "inventory_instance_id, available, ordinal) VALUES (?,?,?,?,?,?,?)",
                    (sid, owner, source, "hold-person", f"test.material_{ordinal}", 0, ordinal),
                )
    with sqlite3.connect(db) as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT inventory_instance_id FROM character_spell_materials "
            "WHERE session_id=? AND actor_id=? AND inventory_instance_id=?",
            (sid, owner, "test.material_0"),
        ).fetchall()
        assert any("character_spell_materials_object" in row[-1] for row in plan), plan
    with StateMachine(db, dnd_loader=assets) as sm:
        original = Range.read
        counts = []

        def tracked(self, predicates, changes, stats):
            rows = original(self, predicates, changes, stats)
            if self.table == "character_spell_materials":
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
        assert not [query for query in sql if "character_spell_materials" in query.lower()]
