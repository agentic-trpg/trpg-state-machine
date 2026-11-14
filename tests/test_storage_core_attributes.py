"""Schema50 character owner rows preserve fixed RE input and write rollback."""

import json

import pytest
from dnd_support import NPC, create, initialize, loader
from dnd_support import package_data as dnd_package_data
from test_dnd_runtime import attack, setup

from trpg_state_machine import StateMachine, StateMachineError
from trpg_state_machine.package import _validate


def test_attributes_and_proficiencies_reassemble_fixed_combat_snapshot(
    tmp_path, package_data, context
):
    def change(foundation):
        pc, npc = foundation["snapshot"]["character_states"]
        pc.update(
            strength=17,
            dexterity=14,
            attack_bonus=None,
            ac=16,
            character_level=3,
            proficiency_bonus_override=2,
            save_proficiencies=["str", "dex"],
            skill_proficiencies=["athletics", "stealth"],
            skill_expertise=["stealth"],
            tool_proficiencies=["thieves-tools"],
            weapon_proficiencies=[],
            senses={"darkvision": 60, "blindsight": None, "tremorsense": None, "truesight": None},
        )
        npc.update(weapon_proficiencies=None, ac=17)

    sm, sid, package, assets = setup(tmp_path, package_data, context, change)
    with sm:
        before = sm.read_combat_snapshot(sid, package, context=context)
        fixed_json = before.model_dump(mode="json")
        pc, npc = before.character_states
        assert (pc.strength, pc.ac, pc.character_level, pc.proficiency_bonus_override) == (
            17,
            16,
            3,
            2,
        )
        assert (pc.skill_proficiencies, pc.skill_expertise, pc.tool_proficiencies) == (
            ["athletics", "stealth"],
            ["stealth"],
            ("thieves-tools",),
        )
        assert pc.weapon_proficiencies == []
        assert npc.weapon_proficiencies is None
        assert pc.senses.darkvision == 60
        for actor in (pc, npc):
            stored = sm._conn.execute(
                "SELECT state_json FROM mechanical_actors WHERE session_id=? AND actor_id=?",
                (sid, actor.entity_id),
            ).fetchone()[0]
            assert (
                not (
                    set(sm._characters.ATTRIBUTE_FIELDS)
                    | set(sm._characters.PROFICIENCY_FIELDS_NAMES)
                    | {"skill_expertise", "senses"}
                )
                & json.loads(stored).keys()
            )
        assert "USING INDEX" in " ".join(
            str(x)
            for x in sm._conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM character_proficiencies "
                "WHERE session_id=? AND actor_id=? AND kind=? AND proficiency_id=?",
                (sid, pc.entity_id, "skill", "stealth"),
            ).fetchone()
        )
        receipt = sm.submit_command(attack(sm, sid, package, context), package, context=context)
        assert receipt.status == "committed"
        roll = next(
            event.payload["engine_event"]
            for event in sm.read_events(sid, package, context=context)
            if event.event_type == "combat.engine_event"
            and event.payload["engine_event"]["type"] == "attack_rolled"
        )
        assert (roll["modifier"], roll["natural"], roll["roll_total"], roll["is_hit"]) == (
            3,
            13,
            16,
            False,
        )
        assert before.model_dump(mode="json") == fixed_json
        after = sm.read_combat_snapshot(sid, package, context=context)
    with StateMachine(tmp_path / "dnd.db", dnd_loader=assets) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after


def test_attribute_insert_fault_rolls_back_initialization(tmp_path, package_data, context):
    assets = loader()
    package = _validate(dnd_package_data(package_data, assets))
    with StateMachine(tmp_path / "fault.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_attribute BEFORE INSERT ON character_attributes "
            "BEGIN SELECT RAISE(ABORT,'attribute fault'); END"
        )
        with pytest.raises(StateMachineError, match="SQLite transaction failed"):
            initialize(sm, sid, package, context)
        assert sm._conn.execute("SELECT COUNT(*) FROM character_attributes").fetchone()[0] == 0
        assert sm._conn.execute("SELECT COUNT(*) FROM mechanical_actors").fetchone()[0] == 0
        sm._conn.execute("DROP TRIGGER fail_attribute")
        assert initialize(sm, sid, package, context).status == "committed"
        assert sm.read_combat_snapshot(sid, package, context=context).character_states


def test_unrepresentable_attribute_rejects_without_partial_mechanics(
    tmp_path, package_data, context
):
    assets = loader()
    data = dnd_package_data(package_data, assets)
    data["rule_bindings"]["combat_foundation"]["snapshot"]["character_states"][0]["strength"] = (
        2**63
    )
    package = _validate(data)
    with StateMachine(tmp_path / "large.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        receipt = initialize(sm, sid, package, context)
        assert receipt.status == "rejected"
        assert sm._conn.execute("SELECT COUNT(*) FROM mechanical_actors").fetchone()[0] == 0
        assert sm._conn.execute("SELECT COUNT(*) FROM character_attributes").fetchone()[0] == 0


def test_fixed_character_read_ignores_unrelated_actor_and_proficiency_growth(
    tmp_path, package_data, context
):
    """Hold a public combat target fixed while unrelated rows increase."""
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        fixed = sm.read_combat_snapshot(sid, package, context=context)
        target = fixed.character_states[0].entity_id
        scene = sm._conn.execute(
            "SELECT current_scene_id FROM actors WHERE session_id=? AND actor_id=?",
            (sid, NPC),
        ).fetchone()[0]

        def counts():
            attrs = sm._conn.execute(
                "SELECT * FROM character_attributes WHERE session_id=? AND actor_id=?",
                (sid, target),
            ).fetchall()
            profs = sm._conn.execute(
                "SELECT * FROM character_proficiencies WHERE session_id=? AND actor_id=? "
                "ORDER BY kind,ordinal",
                (sid, target),
            ).fetchall()
            return len(attrs), len(profs)

        baseline = counts()
        with sm._store.transaction(write=True):
            for i in range(80):
                sm._conn.execute(
                    "INSERT INTO actors(session_id,actor_id,kind,current_scene_id,cell) "
                    "VALUES (?,?,'npc',?,NULL)",
                    (sid, f"npc.unrelated_{i}", scene),
                )
                sm._conn.execute(
                    "INSERT INTO character_proficiencies VALUES (?,?,?,?,?,?,?)",
                    (sid, NPC, "tool", i, f"unrelated.tool_{i}", 1, None),
                )
        assert counts() == baseline
        assert (
            sm.read_combat_snapshot(sid, package, context=context).character_states[0]
            == (fixed.character_states[0])
        )
