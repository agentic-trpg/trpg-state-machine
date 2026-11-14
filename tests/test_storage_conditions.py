"""Schema48 independent condition ownership and effect projection boundaries."""

import json
import sqlite3
from contextlib import closing
from copy import deepcopy

import pytest
from dnd_support import NPC, create
from modern_support import modern_data, modern_loader
from test_dnd_npc_availability import npc_command
from test_dnd_runtime import setup
from test_ongoing_effect_storage import _effect

from trpg_state_machine import StateMachine, StateMachineError
from trpg_state_machine.package import _validate


def _condition(name="restrained", *, source="implied:condition", round_=0):
    """Build one canonical independent record for a mechanical actor."""
    return {
        "condition": name,
        "source_entity_id": source,
        "scope": "session",
        "duration_rounds": None,
        "save_dc": None,
        "applied_round": round_,
        "exhaustion_level": 1,
        "source_effect_id": None,
    }


def _rows(sm, sid):
    """Inspect identities and order in the authoritative relation."""
    return sm._conn.execute(
        "SELECT * FROM character_conditions WHERE session_id=? AND actor_id=? ORDER BY ordinal",
        (sid, NPC),
    ).fetchall()


@pytest.mark.parametrize("effect_first", [False, True])
def test_duplicate_independent_records_keep_ids_and_effect_removal_is_separate(
    tmp_path, package_data, context, effect_first
):
    sm, sid, package, assets = setup(tmp_path, package_data, context)
    with sm:
        with sm._store.transaction(write=True):
            sm._characters.replace_conditions(
                sid, NPC, [_condition(), _condition(round_=1), _condition()]
            )
        original = sm.read_combat_snapshot(sid, package, context=context)
        actor = next(a for a in original.character_states if a.entity_id == NPC)
        assert len(actor.conditions) == 3
        rows = _rows(sm, sid)
        assert len({r["condition_id"] for r in rows}) == 3
        stored = sm._conn.execute(
            "SELECT state_json FROM mechanical_actors WHERE session_id=? AND actor_id=?",
            (sid, NPC),
        ).fetchone()[0]
        assert "conditions" not in json.loads(stored)
        with sm._store.transaction(write=True):
            sm._characters.effects.add_effect(
                sid, _effect(NPC, "same.name"), ordinal=0, conditions=["restrained"]
            )
        mixed = sm.read_combat_snapshot(sid, package, context=context)
        current = next(a for a in mixed.character_states if a.entity_id == NPC)
        assert [c.source_effect_id for c in current.conditions] == [None, None, None, "same.name"]

        def remove_independent():
            with sm._store.transaction(write=True):
                assert sm._characters.remove_condition(sid, NPC, rows[1]["condition_id"])

        def remove_effect():
            with sm._store.transaction(write=True):
                sm._characters.effects.remove_effect(sid, NPC, "same.name", "test")

        (remove_effect if effect_first else remove_independent)()
        intermediate = sm.read_combat_snapshot(sid, package, context=context)
        assert (
            len(next(a for a in intermediate.character_states if a.entity_id == NPC).conditions)
            == 3
        )
        (remove_independent if effect_first else remove_effect)()
        assert [r["condition_id"] for r in _rows(sm, sid)] == [
            rows[0]["condition_id"],
            rows[2]["condition_id"],
        ]
        assert [r["ordinal"] for r in _rows(sm, sid)] == [0, 2]
        after_effect = sm.read_combat_snapshot(sid, package, context=context)
        assert [
            c.source_effect_id
            for c in next(a for a in after_effect.character_states if a.entity_id == NPC).conditions
        ] == [None, None]
        assert next(a for a in mixed.character_states if a.entity_id == NPC) == current
    with StateMachine(tmp_path / "dnd.db", dnd_loader=assets) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after_effect


def test_replace_keeps_ids_on_reorder_and_rejects_linked_injection(tmp_path, package_data, context):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    first, second = _condition("restrained"), _condition("poisoned")
    with sm:
        with sm._store.transaction(write=True):
            sm._characters.replace_conditions(sid, NPC, [first, second])
        original = _rows(sm, sid)
        with sm._store.transaction(write=True):
            sm._characters.replace_conditions(sid, NPC, [second, first])
        assert [r["condition_id"] for r in _rows(sm, sid)] == [
            original[1]["condition_id"],
            original[0]["condition_id"],
        ]
        current = _rows(sm, sid)
        assert sm._characters.condition(sid, NPC, current[0]["condition_id"]) == second
        with sm._store.transaction(write=True):
            sm._characters.replace_conditions(sid, NPC, [first])
        survivor = _rows(sm, sid)
        assert [(r["condition_id"], r["ordinal"]) for r in survivor] == [
            (original[0]["condition_id"], 1)
        ]
        linked = first | {"source_effect_id": "foreign"}
        with (
            pytest.raises(StateMachineError, match="Linked condition"),
            sm._store.transaction(write=True),
        ):
            sm._characters.add_condition(sid, NPC, linked)
        assert len(_rows(sm, sid)) == 1
        assert len(sm.read_combat_snapshot(sid, package, context=context).character_states) > 0


def test_noncombat_create_rejects_orphan_linked_condition_atomically(
    tmp_path, package_data, context
):
    assets = modern_loader()
    data = modern_data(package_data, assets)
    del data["rule_bindings"]["combat_foundation"]
    data["rule_bindings"]["noncombat_foundation"]["snapshot"]["character_states"][1][
        "conditions"
    ] = [_condition() | {"source_effect_id": "orphan"}]
    package = _validate(data)
    with StateMachine(tmp_path / "orphan.db", dnd_loader=assets) as sm:
        with pytest.raises(StateMachineError) as error:
            create(sm, package, context)
        assert error.value.code == "unsupported_noncombat_foundation"
        assert sm._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert sm._conn.execute("SELECT COUNT(*) FROM character_conditions").fetchone()[0] == 0


def test_mixed_projection_adoption_protects_effect_lineage(tmp_path, package_data, context):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        with sm._store.transaction(write=True):
            sm._characters.add_condition(sid, NPC, _condition())
            sm._characters.effects.add_effect(
                sid, _effect(NPC, "effect.same"), ordinal=0, conditions=["restrained"]
            )
        mixed = [
            c.model_dump(mode="json")
            for actor in sm.read_combat_snapshot(sid, package, context=context).character_states
            if actor.entity_id == NPC
            for c in actor.conditions
        ]
        assert [c["source_effect_id"] for c in mixed] == [None, "effect.same"]
        for changed in ([mixed[0]], [mixed[0], mixed[1] | {"source_effect_id": "foreign"}]):
            with pytest.raises(StateMachineError) as error, sm._store.transaction(write=True):
                sm._rules._apply_dnd_changes(
                    {"session_id": sid}, [("actor", NPC, "conditions", changed)]
                )
            assert error.value.code == "invalid_rule_result"
            assert sm._characters.has_conditions(sid, NPC)
        with sm._store.transaction(write=True):
            sm._rules._apply_dnd_changes(
                {"session_id": sid}, [("actor", NPC, "conditions", [mixed[1]])]
            )
        assert not sm._characters.has_conditions(sid, NPC)
        assert sm._characters.effects.has_linked_conditions(sid, NPC)


def test_public_npc_damage_condition_receipt_fault_retry_and_reopen(
    tmp_path, package_data, context
):
    base = deepcopy(package_data)
    base["pc_templates"][0]["character"]["hp"]["current"] = 1

    def change(foundation):
        snapshot = foundation["snapshot"]
        snapshot["character_states"][0]["hp_current"] = 1
        snapshot["combat_state"].update(
            current_turn_index=1, turn_serial=2, monster_turn_start_done=[2, NPC]
        )

    sm, sid, package, assets = setup(tmp_path, base, context, change)
    db = tmp_path / "dnd.db"
    with sm:
        command = npc_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        pc = before.character_states[0].entity_id

        def durable():
            with closing(sqlite3.connect(db)) as conn:
                return (
                    conn.execute(
                        "SELECT current_hp FROM characters WHERE session_id=? AND actor_id=?",
                        (sid, pc),
                    ).fetchone(),
                    conn.execute(
                        "SELECT condition FROM character_conditions "
                        "WHERE session_id=? AND actor_id=?",
                        (sid, pc),
                    ).fetchall(),
                    conn.execute(
                        "SELECT COUNT(*) FROM command_receipts WHERE session_id=? AND command_id=?",
                        (sid, command.command_id),
                    ).fetchone(),
                )

        assert durable() == ((1,), [], (0,))
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_conditions_review BEFORE INSERT ON command_receipts "
            "BEGIN SELECT RAISE(ABORT,'review fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "storage_error"
        assert durable() == ((1,), [], (0,))
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        sm._conn.execute("DROP TRIGGER fail_conditions_review")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert durable() == ((0,), [("unconscious",)], (1,))
        assert [
            (c.condition, c.source_effect_id) for c in after.character_states[0].conditions
        ] == [("unconscious", None)]
        assert before.character_states[0].hp_current == 1
        assert not before.character_states[0].conditions
        assert sm.submit_command(command, package, context=context) == receipt
    with StateMachine(db, dnd_loader=assets) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after
        assert reopened.submit_command(command, package, context=context) == receipt
