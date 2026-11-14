"""Schema48 independent condition ownership and effect projection boundaries."""

import json

import pytest
from dnd_support import NPC
from test_dnd_runtime import setup
from test_ongoing_effect_storage import _effect

from trpg_state_machine import StateMachine, StateMachineError


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
