"""Keyed effect ownership through public snapshots and shared Store scopes."""

import pytest
from dnd_support import create, initialize, loader, make_package

from trpg_state_machine import StateMachine


def _effect(target, effect_id, *, status="restrained"):
    return {
        "id": effect_id,
        "name": "storage fixture",
        "origin": "test",
        "target_id": target,
        "disabled": False,
        "transfer": False,
        "duration": {
            key: None for key in ("rounds", "turns", "seconds", "start_round", "start_turn")
        },
        "changes": [],
        "action_policy": None,
        "statuses": [status],
        "flags": {},
        "lifecycle": None,
        "end_effects": [],
    }


def _clock(target, effect_id):
    return {
        "identity": [target, effect_id, "test"],
        "application": {
            "spec": {
                "repeat_save": None,
                "maximum_rounds": 3,
                "expiry_boundary": None,
                "expire_on_positive_damage": False,
                "one_use_modifiers": [],
                "stacking": None,
                "stacking_group": None,
                "next_attack_scope": None,
                "next_attack_bonus_group": None,
                "on_end": [],
            },
            "source_id": target,
            "source_kind": "spell",
            "source_slug": "fixture",
            "activity_id": "fixture.cast",
            "save_ability": None,
            "save_dc": None,
            "is_magical": True,
        },
        "applied_turn_serial": 2,
        "applied_round": 1,
        "expires_round": 4,
        "expiry_actor_id": None,
        "last_repeat_turn_serial": None,
        "remaining_one_use_modifiers": [],
    }


def test_multi_target_effect_ids_survive_member_removal_reopen_and_group_removal(
    tmp_path, package_data, context
):
    assets = loader()
    package = make_package(package_data, assets)
    db = tmp_path / "effects.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        initialize(sm, sid, package, context)
        original = sm.read_combat_snapshot(sid, package, context=context)
        caster, target = (actor.entity_id for actor in original.character_states[:2])
        owner = sm._characters.effects
        with sm._store.transaction(write=True):
            sm._characters.update_rule_metadata(
                sid,
                target,
                "conditions",
                [
                    {
                        "condition": "restrained",
                        "source_entity_id": "implied:condition",
                        "scope": "session",
                        "duration_rounds": None,
                        "save_dc": None,
                        "applied_round": 0,
                        "exhaustion_level": 1,
                        "source_effect_id": None,
                    }
                ],
            )
            owner.add_concentration(sid, "group.one", caster, "effect.two", 7)
            owner.add_effect(
                sid,
                _effect(caster, "effect.one"),
                ordinal=0,
                concentration_id="group.one",
                chain_ordinal=0,
                conditions=["restrained"],
                lifecycle=_clock(caster, "effect.one"),
            )
            owner.add_effect(
                sid,
                _effect(target, "effect.two"),
                ordinal=1,
                concentration_id="group.one",
                chain_ordinal=1,
                conditions=["restrained"],
            )
            owner.add_effect(
                sid,
                {**_effect(target, "effect.two"), "origin": "other"},
                ordinal=2,
                conditions=["restrained"],
            )
            owner.add_effect(
                sid,
                _effect(target, "unrelated", status="poisoned"),
                ordinal=3,
                conditions=["poisoned"],
            )
        full = sm.read_combat_snapshot(sid, package, context=context)
        assert [effect.id for effect in full.effect_states] == [
            "effect.one",
            "effect.two",
            "effect.two",
            "unrelated",
        ]
        assert full.combat_state.concentration_chain[caster] == [
            (caster, "effect.one", "test"),
            (target, "effect.two", "test"),
        ]
        assert full.combat_state.effect_lifecycles[0].state.expires_round == 4
        assert (
            next(a for a in full.character_states if a.entity_id == caster).concentration_effect_id
            == "effect.two"
        )
        target_conditions = next(
            a for a in full.character_states if a.entity_id == target
        ).conditions
        assert {(c.condition, c.source_effect_id) for c in target_conditions} == {
            ("restrained", None),
            ("restrained", "effect.two"),
            ("poisoned", "unrelated"),
        }
        with sm._store.transaction(write=True):
            owner.remove_effect(sid, target, "effect.two", "test")
        remaining = sm.read_combat_snapshot(sid, package, context=context)
        assert [(effect.id, effect.origin) for effect in remaining.effect_states] == [
            ("effect.one", "test"),
            ("effect.two", "other"),
            ("unrelated", "test"),
        ]
        assert remaining.combat_state.concentration_chain[caster] == [
            (caster, "effect.one", "test")
        ]
        assert remaining.combat_state.effect_lifecycles == full.combat_state.effect_lifecycles
        assert (
            next(
                a for a in remaining.character_states if a.entity_id == caster
            ).concentration_effect_id
            == "effect.two"
        )
        assert [link.identity[1] for link in remaining.combat_state.conditions_by_effect] == [
            "effect.one",
            "effect.two",
            "unrelated",
        ]
        assert {
            (c.condition, c.source_effect_id)
            for c in next(a for a in remaining.character_states if a.entity_id == target).conditions
        } == {("restrained", None), ("restrained", "effect.two"), ("poisoned", "unrelated")}
    with StateMachine(db, dnd_loader=assets) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == remaining
        with reopened._store.transaction(write=True):
            reopened._characters.effects.remove_concentration(sid, "group.one")
        final = reopened.read_combat_snapshot(sid, package, context=context)
        assert [(effect.id, effect.origin) for effect in final.effect_states] == [
            ("effect.two", "other"),
            ("unrelated", "test"),
        ]
        assert final.combat_state.concentration_chain == {}
        assert final.combat_state.effect_lifecycles == ()
        assert (
            next(a for a in final.character_states if a.entity_id == caster).concentration_effect_id
            is None
        )


def test_effect_savepoint_rollback_and_fixed_target_read(tmp_path, package_data, context):
    assets = loader()
    package = make_package(package_data, assets)
    with StateMachine(tmp_path / "rollback.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        initialize(sm, sid, package, context)
        actors = [
            actor.entity_id
            for actor in sm.read_combat_snapshot(sid, package, context=context).character_states
        ]
        owner = sm._characters.effects
        with sm._store.transaction(write=True):
            owner.add_effect(sid, _effect(actors[0], "keep"), ordinal=0)
            with pytest.raises(RuntimeError), sm._store.savepoint():
                owner.add_effect(sid, _effect(actors[1], "rolled-back"), ordinal=1)
                raise RuntimeError("rollback")
            assert [effect["id"] for effect in owner.for_targets(sid, {actors[0]})] == ["keep"]
            assert owner.for_targets(sid, {actors[1]}) == []
        assert [effect["id"] for effect in owner.for_targets(sid, {actors[0]})] == ["keep"]
        assert owner.for_targets(sid, {actors[1]}) == []
