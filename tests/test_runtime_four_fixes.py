"""Production regression costs for movement, NPC targets and version metadata."""

import builtins
import json
from collections import Counter
from contextlib import contextmanager

import pytest
from lock_support import A
from test_npc_combat_handoff import prepare
from test_runtime_field_maintenance import many_contents
from test_scoped_runtime_validation import grow, measure
from test_world_containers import BOX, container_data
from world_support import command, setup

from trpg_state_machine import StepActor, session_memory, storage
from trpg_state_machine.domain_facts import CharacterFacts


@contextmanager
def counts_for(sm, monkeypatch):
    counts = Counter()
    before = dict(sm._store.memory.stats)
    memory = sm._store.memory
    copied = []

    class DictMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, builtins.dict)

    class CountDict(builtins.dict, metaclass=DictMeta):
        def __init__(self, *args, **kwargs):
            scope = sm._store._scope
            if args and (
                args[0] is memory.versions
                or scope is not None
                and args[0] is scope.memory_versions
                or any(args[0] is item for item in copied)
            ):
                counts["version_records_copied"] += len(args[0])
                copied.append(self)
            super().__init__(*args, **kwargs)

    with monkeypatch.context() as patch, measure(sm) as (sql, _):
        patch.setattr(storage, "dict", CountDict, raising=False)
        patch.setattr(session_memory, "dict", CountDict, raising=False)
        validate = CharacterFacts._inventory_model

        def validated(value):
            counts["inventory_validations"] += 1
            counts["validated_entries"] += len(value["entries"])
            return validate(value)

        patch.setattr(CharacterFacts, "_inventory_model", staticmethod(validated))
        for name, label in (("select", "ram_queries"), ("version", "dependency_checks")):
            original = getattr(memory, name)

            def counted(*args, _original=original, _label=label, **kwargs):
                counts[_label] += 1
                return _original(*args, **kwargs)

            patch.setattr(memory, name, counted)
        try:
            from trpg_state_machine import version_map
        except ImportError:
            pass
        else:
            iterate = version_map.VersionMap.__iter__
            build = version_map._node

            def visited(self):
                for key in iterate(self):
                    counts["version_records_iterated"] += 1
                    yield key

            def nodes(*args):
                counts["version_nodes_built"] += 1
                return build(*args)

            patch.setattr(version_map.VersionMap, "__iter__", visited)
            patch.setattr(version_map, "_node", nodes)
        yield counts
        counts.update(sql)
        for key, value in before.items():
            if not key.endswith("_ns"):
                counts[key] = memory.stats[key] - value


@pytest.mark.parametrize("n", (10, 100))
def test_movement_keeps_valid_inventory(tmp_path, context, monkeypatch, n):
    data, identities = many_contents(n)
    data["scenes"][0]["objects"][0]["state"] = "open"
    data["lockable_doors"] = []
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        facts, characters = sm._world_facts, sm._world.characters
        with sm._transaction(write=True):
            prepared = characters.prepare_inventory_change(sid, identities)
            for oid in identities:
                facts.transfer_item(sid, facts.object(sid, oid), carrier_id=actor)
            characters.sync_inventory_change(sid, prepared)
        old = sm.read_carried_items(sid, actor, package, context=context)
        op = command(sm, sid, package, context, StepActor(A, "0,0", "0,1"), "step")
        receipt = sm.submit_command(op, package, context=context)
        assert receipt.status == "committed", receipt
        with counts_for(sm, monkeypatch) as counts:
            current = sm.read_carried_items(sid, actor, package, context=context)
            counts["output_entries"] = len(current.entries)
        print("AFTER_STEP", n, dict(counts))
        assert current.entries == old.entries
        assert counts["inventory_validations"] == counts["validated_entries"] == 0
        assert counts["instance_builds"] == 0
        assert counts["ram_rows"] <= 8


@pytest.mark.parametrize("n", (0, 100))
def test_local_read_and_write_do_not_copy_versions(tmp_path, context, monkeypatch, n):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        grow(sm, sid, n)
        for i in range(n):
            sm._world_facts.object(sid, f"object.unrelated_{i}")
            sm._world_facts.object_definition(sid, f"object.unrelated_{i}")
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        sm.get_object(sid, actor, BOX, package, context=context)
        with counts_for(sm, monkeypatch) as counts:
            sm.get_object(sid, actor, BOX, package, context=context)
        print("VERSION_READ", n, dict(counts))
        with counts_for(sm, monkeypatch) as write, sm._transaction(write=True):
            with pytest.raises(ValueError), sm._store.savepoint():
                sm._world_facts.switch(sid, BOX, "closed")
                raise ValueError("rollback")
            sm._world_facts.switch(sid, BOX, "closed")
        print("VERSION_WRITE", n, dict(write))
        assert counts["version_records_copied"] == counts["version_records_iterated"] == 0
        assert write["version_records_copied"] == write["version_records_iterated"] == 0


@pytest.mark.parametrize("n", (0, 100))
def test_npc_target_checks_visit_only_requested_identities(tmp_path, context, monkeypatch, n):
    sm, sid, package, _, _, task, _, _ = prepare(tmp_path, context)
    with sm:
        with sm._transaction(write=True):
            sm._initialize_scene(sid, "scene.workshop", package)
            for i in range(n):
                sm._conn.execute(
                    "INSERT INTO actors(session_id,actor_id,kind,current_scene_id) "
                    "VALUES (?,?,'npc','scene.workshop')",
                    (sid, f"npc.remote_{i}"),
                )
        counts, active = Counter(), []
        guard = sm._sessions._npc_combat_target_guard
        select = sm._store.memory.select

        def guarded(*args):
            active.append(True)
            counts["target_checks"] += 1
            try:
                return guard(*args)
            finally:
                active.pop()

        def selected(sql, params):
            costs["ram_queries"] += 1
            result = select(sql, params)
            if active and "actor_id,current_scene_id FROM actors" in sql:
                rows = result.fetchall()
                counts["target_rows"] += len(rows)
                return session_memory.Rows(rows, description=result.description)
            return result

        with monkeypatch.context() as patch, counts_for(sm, monkeypatch) as costs:
            patch.setattr(sm._sessions, "_npc_combat_target_guard", guarded)
            patch.setattr(sm._store.memory, "select", selected)
            delivered = sm.dispatch_npc_output(task.evaluation_id, 0, package, context=context)
        print("NPC_TARGET", n, dict(counts), dict(costs))
        assert delivered.command_receipt.status == "committed"
        assert counts["target_checks"] == 2
        assert counts["target_rows"] == 4


@pytest.mark.parametrize("field", ("hp", "actions"))
def test_actor_metadata_preserves_valid_inventory(tmp_path, context, monkeypatch, field):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        old = sm.read_carried_items(sid, actor, package, context=context)
        with sm._transaction(write=True):
            if field == "hp":
                sm._conn.execute(
                    "UPDATE characters SET current_hp=current_hp-1 "
                    "WHERE session_id=? AND actor_id=?",
                    (sid, actor),
                )
            else:
                sm._conn.execute(
                    "UPDATE mechanical_actors SET state_json="
                    "json_set(state_json,'$.action_available',json('false')) "
                    "WHERE session_id=? AND actor_id=?",
                    (sid, actor),
                )
        with counts_for(sm, monkeypatch) as counts:
            assert sm.read_carried_items(sid, actor, package, context=context) == old
        assert counts["inventory_validations"] == counts["validated_entries"] == 0


def test_changed_inventory_is_validated_once_and_pruning_keeps_composite_stamp(
    tmp_path, context, monkeypatch
):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        old = sm.read_carried_items(sid, actor, package, context=context)
        identity = old.entries[0].instance_id
        with sm._transaction(write=True):
            value = sm._conn.execute(
                "SELECT inventory_json FROM mechanical_components WHERE session_id=?", (sid,)
            ).fetchone()[0]
            value = json.loads(value)
            entry = next(e for e in value["entries"] if e["instance_id"] == identity)
            entry["quantity"], entry["charges_remaining_per_unit"] = 2, 3
            # Unknown aggregate writer: retain conservative component invalidation.
            sm._conn.execute(
                "UPDATE mechanical_components SET inventory_json=? WHERE session_id=?",
                (json.dumps(value), sid),
            )
        with counts_for(sm, monkeypatch) as counts:
            current = sm.read_carried_items(sid, actor, package, context=context)
        assert counts["inventory_validations"] == counts["validated_entries"] == 1
        assert current.entries[0].quantity == 2
        assert current.entries[0].charges_remaining_per_unit == 3
        assert old.entries[0].quantity == 1
        memory = sm._store.memory
        dep = (f"inventory:{actor!r}", sid)
        stamp, old_versions = memory.version(dep), memory.versions.fork()
        memory.capacity = 128
        for i in range(4300):
            memory.versions[(f"obsolete:{i}", sid)] = i
        memory.trim_versions()
        assert memory.version(dep) == stamp
        assert len(memory.versions) < 2048
        assert old_versions[("inventory_components", sid)] == stamp[2]
        with counts_for(sm, monkeypatch) as counts:
            assert sm.read_carried_items(sid, actor, package, context=context) == current
        assert counts["inventory_validations"] == counts["version_records_iterated"] == 0


def test_components_and_inventory_reuse_same_complete_typed_validation(
    tmp_path, context, monkeypatch
):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        sm._store.memory.clear()
        with counts_for(sm, monkeypatch) as counts:
            components = sm._world.characters.rule_components(sid, {actor})
            inventory = sm.read_carried_items(sid, actor, package, context=context)
        assert [entry.instance_id for entry in inventory.entries] == [
            entry["instance_id"] for entry in components["inventory_state"]["entries"]
        ]
        assert counts["inventory_validations"] == counts["validated_entries"] == 1
