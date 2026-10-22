"""Managed cache lookup costs through production readers, separately from output."""

import builtins
import json
from collections import Counter
from contextlib import contextmanager

import pytest
from dnd5e_engine.evaluation_state import InventoryState, MechanicalGrid
from lock_support import A
from test_runtime_field_maintenance import many_contents
from test_scoped_runtime_validation import grow, measure
from test_world_lights import lighting_data
from world_support import setup

from trpg_state_machine import domain_facts, event_capture, grid_maps, spatial
from trpg_state_machine import grid as grid_module


@contextmanager
def costs(sm, monkeypatch):
    counts = Counter()
    before = dict(sm._store.memory.stats)
    with monkeypatch.context() as patch, measure(sm) as (sql, _):
        for module in (grid_maps, domain_facts, spatial, event_capture):
            original = module.canonical_json if hasattr(module, "canonical_json") else None
            if original is None:
                continue

            def encoded(value, _original=original, _module=module.__name__):
                counts["encoded_calls:" + _module.rsplit(".", 1)[-1]] += 1
                if isinstance(value, dict):
                    counts["encoded_grid_records"] += len(value.get("blocked_cells", ()))
                    counts["encoded_inventory_records"] += len(value.get("entries", ()))
                    if "scenes" in value and _module.endswith("event_capture"):
                        counts["evidence_encodings"] += 1
                        counts["evidence_block_records"] += sum(
                            len((scene.get("grid") or {}).get("blocked_cells", ()))
                            for scene in value["scenes"].values()
                        )
                return _original(value)

            patch.setattr(module, "canonical_json", encoded)
        for obj, name, label in (
            (grid_maps.PreparedGrid, "copy_data", "map_copies"),
            (grid_maps.PreparedScene, "copy_data", "scene_copies"),
            (grid_maps.ReadOnlyTopology, "__init__", "topology_builds"),
            (sm._store.memory, "version", "dependency_checks"),
            (sm._store.memory, "select", "ram_queries"),
        ):
            original = getattr(obj, name)

            def counted(*args, _original=original, _label=label, **kwargs):
                counts[_label] += 1
                return _original(*args, **kwargs)

            patch.setattr(obj, name, counted)
        for schema in (InventoryState, MechanicalGrid):
            validate = schema.model_validate

            def validated(cls, value, _validate=validate, **kwargs):
                counts["python_models:" + cls.__name__] += 1
                return _validate(value, **kwargs)

            patch.setattr(schema, "model_validate", classmethod(validated))

        def sorted_values(values, *args, **kwargs):
            if isinstance(values, (list, set, frozenset, tuple)):
                counts["sorted_records"] += len(values)
            return builtins.sorted(values, *args, **kwargs)

        patch.setattr(grid_module, "sorted", sorted_values, raising=False)
        yield counts
        counts.update(sql)
        for key, value in before.items():
            if not key.endswith("_ns"):
                counts[key] = sm._store.memory.stats[key] - value


def expanded(sm, sid, n, package):
    grow(sm, sid, n)
    with sm._transaction(write=True):
        sm._conn.execute(
            "UPDATE mechanical_scenes SET state_json=json_set(state_json,"
            "'$.grid.width',?,'$.grid.blocked_cells',json(?)) "
            "WHERE session_id=? AND scene_id=?",
            (n + 10, json.dumps([f"{i + 5},2" for i in range(n)]), sid, A),
        )
        stored = sm._conn.execute(
            "SELECT inventory_json FROM mechanical_components WHERE session_id=?", (sid,)
        ).fetchone()[0]
        inventory = json.loads(stored)
        inventory["entries"].extend(
            {
                "instance_id": f"unrelated.stock_{i}",
                "owner_id": "npc.caretaker",
                "item_slug": "amulet",
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
            for i in range(n)
        )
        sm._conn.execute(
            "UPDATE mechanical_components SET inventory_json=? WHERE session_id=?",
            (json.dumps(inventory), sid),
        )
        core = sm._conn.execute(
            "SELECT inventory_json FROM characters WHERE session_id=? AND actor_id='npc.caretaker'",
            (sid,),
        ).fetchone()[0]
        slugs = list(dict.fromkeys([*json.loads(core), "amulet"]))
        sm._conn.execute(
            "UPDATE characters SET inventory_json=? "
            "WHERE session_id=? AND actor_id='npc.caretaker'",
            (json.dumps(slugs), sid),
        )
        sm.scene_lifecycle._validate_scene(sid, A, package)


@pytest.mark.parametrize("n", (10, 100))
@pytest.mark.parametrize("reader", ("local", "light", "inventory", "perception"))
def test_warm_managed_readers(tmp_path, context, monkeypatch, n, reader):
    sm, sid, package, _ = setup(tmp_path, context, lighting_data())
    with sm:
        expanded(sm, sid, n, package)
        actor = sm.read_session(sid, package, context=context).pc_actor_id

        def read(cell="1,0"):
            if reader == "local":
                return sm._world.grid.local_topology(
                    sid, A, package, ("0,0", cell), exclude_actor=actor
                ).edge_distance("0,0", cell)
            if reader == "light":
                return sm.get_illumination(sid, actor, cell, package, context=context)
            if reader == "inventory":
                return sm.read_carried_items(sid, actor, package, context=context)
            with sm._transaction():
                return sm._world.capture._perception_geometry(sid, A)

        read()
        with costs(sm, monkeypatch) as counts:
            read()
            if reader in ("local", "light"):
                read("0,1")
            read()
        print("MANAGED", reader, n, dict(counts))
        assert counts["encoded_grid_records"] == 0
        assert counts["encoded_inventory_records"] == 0
        assert counts["topology_builds"] == 0
        assert counts["model_parses"] == 0
        assert counts["python_models:InventoryState"] == 0
        assert counts["python_models:MechanicalGrid"] == 0
        if reader in ("local", "light"):
            assert counts["map_copies"] == 0
            assert counts["scene_copies"] == 0
            assert counts["sorted_records"] <= 60


@pytest.mark.parametrize("n", (10, 100))
def test_inventory_output_and_selection_identity(tmp_path, context, monkeypatch, n):
    data, identities = many_contents(n)
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        facts, characters = sm._world_facts, sm._world.characters
        with sm._transaction(write=True):
            prepared = characters.prepare_inventory_change(sid, identities)
            for oid in identities:
                facts.transfer_item(sid, facts.object(sid, oid), carrier_id=actor)
            characters.sync_inventory_change(sid, prepared)
        original = sm.read_carried_items(sid, actor, package, context=context)
        with costs(sm, monkeypatch) as counts:
            current = sm.read_carried_items(sid, actor, package, context=context)
        print("INVENTORY", n, dict(counts))
        assert len(current.entries) == len(original.entries) >= n
        assert counts["encoded_inventory_records"] == 0
        assert counts["python_models:InventoryState"] == 0
        assert counts["dependency_checks"] <= 20
        with pytest.raises(ValueError):
            current.entries[0].quantity += 2
        assert sm.read_carried_items(sid, actor, package, context=context) == original
        with sm._transaction():
            first = characters._inventory_selection(sid, set(), {identities[0]})
            second = characters._inventory_selection(sid, set(), {identities[-1]})
            assert {e["instance_id"] for e in first["entries"]} == {identities[0]}
            assert {e["instance_id"] for e in second["entries"]} == {identities[-1]}
            assert characters._inventory_selection(sid, {"npc.caretaker"}, set()) != first


def test_models_follow_candidate_stages_rollback_reload_and_complete_schema(tmp_path, context):
    from test_grid_maps import update_grid

    from trpg_state_machine import StateMachine

    sm, sid, package, db = setup(tmp_path, context, lighting_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        maps = sm._world_facts.maps
        old_grid = maps.mechanical_grid(sid, A)
        old_inventory = sm.read_carried_items(sid, actor, package, context=context)
        legacy = next(e for e in old_inventory.entries if not e.instance_id.startswith("object."))

        def quantity(value):
            stored = sm._conn.execute(
                "SELECT inventory_json FROM mechanical_components WHERE session_id=?", (sid,)
            ).fetchone()[0]
            data = json.loads(stored)
            next(e for e in data["entries"] if e["instance_id"] == legacy.instance_id)[
                "quantity"
            ] = value
            sm._conn.execute(
                "UPDATE mechanical_components SET inventory_json=? WHERE session_id=?",
                (json.dumps(data), sid),
            )

        def current_quantity():
            inventory = (
                sm._world.characters.inventory_model(sid, actor)
                if sm._store._scope is not None
                else sm.read_carried_items(sid, actor, package, context=context)
            )
            return next(
                e.quantity for e in inventory.entries if e.instance_id == legacy.instance_id
            )

        with pytest.raises(RuntimeError), sm._transaction(write=True):
            update_grid(sm, sid, width=7)
            quantity(2)
            first = maps.mechanical_grid(sid, A)
            assert first.width == 7 and current_quantity() == 2
            with pytest.raises(ValueError), sm._store.savepoint():
                update_grid(sm, sid, width=8)
                quantity(3)
                assert maps.mechanical_grid(sid, A).width == 8 and current_quantity() == 3
                raise ValueError("savepoint")
            assert maps.mechanical_grid(sid, A).width == 7 and current_quantity() == 2
            update_grid(sm, sid, width=9)
            quantity(4)
            assert maps.mechanical_grid(sid, A).width == 9 and current_quantity() == 4
            raise RuntimeError("outer")
        assert maps.mechanical_grid(sid, A) == old_grid
        assert sm.read_carried_items(sid, actor, package, context=context) == old_inventory
        with sm._transaction(write=True):
            update_grid(sm, sid, width=6)
            quantity(2)
            sm.scene_lifecycle._validate_scene(sid, A, package)
        old_grid.blocked_cells.append("0,0")
        assert "0,0" not in maps.mechanical_grid(sid, A).blocked_cells
        assert first.width == 7 and legacy.quantity == 1
        with pytest.raises(ValueError), sm._transaction(write=True):
            sm._conn.execute(
                "UPDATE mechanical_scenes SET state_json=json_remove(state_json,'$.grid.sunlight') "
                "WHERE session_id=? AND scene_id=?",
                (sid, A),
            )
            assert maps.scene(sid, A).grid.topology is not None
            assert sm._world.capture._perception_geometry(sid, A) == (None, None)
            raise ValueError("missing sensory field")
        sm.unload_session(sid, package, context=context)
        assert maps.mechanical_grid(sid, A).width == 6 and current_quantity() == 2
    with StateMachine(db, dnd_loader=sm._rules.loader) as restored:
        restored.resume_session(sid, package, context=context)
        assert restored._world_facts.maps.mechanical_grid(sid, A).width == 6
        assert (
            next(
                e.quantity
                for e in restored.read_carried_items(sid, actor, package, context=context).entries
                if e.instance_id == legacy.instance_id
            )
            == 2
        )


@pytest.mark.parametrize("n", (10, 100))
def test_real_movement_capture_keeps_evidence_encoding_only(tmp_path, context, monkeypatch, n):
    from lock_support import CRED
    from world_support import command, submit

    from trpg_state_machine import SetLightEnabled, StepActor

    sm, sid, package, _ = setup(tmp_path, context, lighting_data())
    with sm:
        expanded(sm, sid, n, package)
        assert (
            submit(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "on").status
            == "committed"
        )
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        for cell in ("1,0", "0,1"):
            sm._world.grid.local_topology(sid, A, package, ("0,0", cell), exclude_actor=actor)
        with sm._transaction():
            sm._world.capture._perception_geometry(sid, A)
        sm._objects.lights.current_project(sid, A, package)
        op = command(sm, sid, package, context, StepActor(A, "0,0", "0,1"), "step")
        with costs(sm, monkeypatch) as counts:
            receipt = sm.submit_command(op, package, context=context)
        print("MOVEMENT", n, dict(counts))
        assert receipt.status == "committed", receipt
        assert counts["encoded_grid_records"] == 0
        assert counts["topology_builds"] == 0
        assert counts["evidence_encodings"] > 0
        assert counts["evidence_block_records"] >= n
        assert sm.submit_command(op, package, context=context) == receipt
        assert sm.read_actor_position(sid, actor, package, context=context).cell == "0,1"


def test_explicit_old_map_and_current_overlay_remain_distinct(tmp_path, context):
    from test_grid_maps import update_grid

    sm, sid, package, _ = setup(tmp_path, context, lighting_data())
    with sm:
        reader = sm._world.grid.reader
        old = reader.current_grid(sid, A, package)
        explicit = old.copy_data()
        with sm._transaction(write=True):
            update_grid(sm, sid, blocked_cells=["1,0"])
            current = reader.current_grid(sid, A, package)
            assert not current.topology.is_valid_cell("1,0")
            assert reader.topology_for_grid(sid, A, explicit).is_valid_cell("1,0")
            assert reader.topology_for_grid(sid, A, old) is old.topology
            overlay = old.topology.with_blocked({"0,1"})
            assert not overlay.is_valid_cell("0,1") and old.topology.is_valid_cell("0,1")
            with pytest.raises((AttributeError, TypeError)):
                overlay._blocked.extra.add("0,0")
        assert old.topology.is_valid_cell("1,0")


@pytest.mark.parametrize("failure", ("before", "unknown", "publish"))
def test_map_model_commit_faults_and_cross_connection_recovery(
    tmp_path, context, monkeypatch, failure
):
    import sqlite3

    from test_grid_maps import update_grid

    from trpg_state_machine import StateMachine, StateMachineError

    first, sid, package, db = setup(tmp_path, context, lighting_data())
    with first, StateMachine(db, dnd_loader=first._rules.loader) as second:
        old = first._world_facts.maps.mechanical_grid(sid, A)
        commit = first._store._commit

        def fault():
            if failure == "unknown":
                commit()
                raise OSError("acknowledgement")
            raise sqlite3.OperationalError("before commit")

        def publish(scope):
            raise MemoryError("committed map publication")

        with monkeypatch.context() as patch:
            if failure == "publish":
                patch.setattr(first._store.memory, "publish", publish)
            else:
                patch.setattr(first._store, "_commit", fault)
            with pytest.raises(StateMachineError) as error, first._transaction(write=True):
                update_grid(first, sid, width=5)
                assert first._world_facts.maps.mechanical_grid(sid, A).width == 5
                first.scene_lifecycle._validate_scene(sid, A, package)
        if failure == "before":
            assert error.value.code == "storage_error"
            assert first._world_facts.maps.mechanical_grid(sid, A).width == old.width
        else:
            assert error.value.code == (
                "commit_outcome_unknown"
                if failure == "unknown"
                else "committed_publication_pending"
            )
            with pytest.raises(StateMachineError, match="reconciliation"):
                first._world_facts.maps.mechanical_grid(sid, A)
            second.resume_session(sid, package, context=context)
            assert first._world_facts.maps.mechanical_grid(sid, A).width == 5
        assert old.width == 3


@pytest.mark.parametrize("change", ("accessible", "location", "durability"))
def test_inventory_cached_model_cannot_hide_unprojected_source_changes(tmp_path, context, change):
    from lock_support import CRED

    from trpg_state_machine import StateMachineError

    sm, sid, package, _ = setup(tmp_path, context, lighting_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        old = sm.read_carried_items(sid, actor, package, context=context)
        with pytest.raises(RuntimeError), sm._transaction(write=True):
            if change == "accessible":
                sm._conn.execute(
                    "UPDATE objects SET item_json=json_set(item_json,'$.accessible',json('false')) "
                    "WHERE session_id=? AND object_id=?",
                    (sid, CRED),
                )
            elif change == "location":
                sm._conn.execute(
                    "UPDATE objects SET carrier_id=NULL,scene_id=?,cell='0,0' "
                    "WHERE session_id=? AND object_id=?",
                    (A, sid, CRED),
                )
            else:
                sm._conn.execute("INSERT INTO object_durability VALUES (?,?,0)", (sid, CRED))
            with pytest.raises(StateMachineError) as error:
                sm._world.characters.inventory_model(sid, actor)
            assert error.value.code == "invalid_mechanical_state"
            raise RuntimeError("discard invalid candidate")
        assert sm.read_carried_items(sid, actor, package, context=context) == old


@pytest.mark.parametrize("n", (10, 100))
def test_foreign_inventory_patch_preserves_target_model(tmp_path, context, monkeypatch, n):
    from test_world_containers import BAG

    sm, sid, package, _ = setup(tmp_path, context, lighting_data())
    with sm:
        expanded(sm, sid, n, package)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        old = sm.read_carried_items(sid, actor, package, context=context)
        characters, facts = sm._world.characters, sm._world_facts
        with sm._transaction(write=True):
            prepared = characters.prepare_inventory_change(sid, (BAG,))
            facts.transfer_item(sid, facts.object(sid, BAG), carrier_id="npc.caretaker")
            characters.sync_inventory_change(sid, prepared)
        with costs(sm, monkeypatch) as counts:
            current = sm.read_carried_items(sid, actor, package, context=context)
        print("FOREIGN", n, dict(counts))
        assert current == old
        assert counts["encoded_inventory_records"] == 0
        assert counts["python_models:InventoryState"] == 0
        assert counts["parsed_builds"] == counts["instance_builds"] == 0
