"""Public checks freeze exact fear-source sight and adopt only the committed roll."""

from copy import deepcopy
from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_check_facts import CreatureSightFact, CreatureSightNeed
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffect
from lock_support import NPC, A, lock_loader
from test_faerie_fire import _grow_remote
from test_object_protection import large_data
from test_real_checks import check_command
from test_scoped_runtime_validation import measure
from world_support import setup

from trpg_state_machine import StateMachine, StateMachineError
from trpg_state_machine.spatial import SpatialPosition

SOURCE_CELL = "8,6"
HIDDEN_CELL = "8,7"


def _setup(tmp_path, context, *, dark=False, untracked=False, unproven_ray=False, growth=0):
    """Seed one effect-linked fear source, or a direct condition with a missing source."""
    data = large_data()
    data["pc_templates"][0]["initial_cell"] = "0,0" if unproven_ray else "8,8"
    source_cell = "0,4" if unproven_ray else SOURCE_CELL
    data["actors"][0]["initial_cell"] = source_cell
    for grid in (
        data["scenes"][0]["grid"],
        data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"],
    ):
        grid["default_lighting"] = "bright"
        grid["lighting"][HIDDEN_CELL] = "dark"
        if dark:
            grid["lighting"][source_cell] = "dark"
    foundation = data["rule_bindings"]["noncombat_foundation"]
    actors = foundation["snapshot"]["character_states"]
    bystander = deepcopy(data["actors"][0])
    bystander.pop("npc_state", None)
    for index in range(growth):
        actor_id = f"npc.unrelated_{index}"
        cell = f"{index % 7},{5 + index // 7}"
        data["actors"].append({**deepcopy(bystander), "actor_id": actor_id, "initial_cell": cell})
        data["scenes"][0]["actor_ids"].append(actor_id)
        data["navigation"]["actor_grants"].append(
            {"actor_id": actor_id, "region_ids": ["region.public"]}
        )
        actors.append({**deepcopy(actors[1]), "entity_id": actor_id})
        foundation["xp"][actor_id] = 0
    sm, sid, package, db = setup(tmp_path, context, data)
    actor_id = sm.read_session(sid, package, context=context).pc_actor_id
    with sm._store.transaction(write=True):
        characters = sm._world.characters
        if untracked:
            characters.add_condition(
                sid,
                actor_id,
                ActiveCondition(
                    condition="frightened", source_entity_id="npc:012345abcdef", scope="session"
                ),
            )
        else:
            effect = effect_state(
                ActiveEffect(
                    id="effect:fear",
                    name="Fear",
                    origin=f"cast:fear:{NPC}",
                    target_id=actor_id,
                    statuses={"frightened"},
                )
            )
            characters.effects.add_effect(
                sid, effect.model_dump(mode="json"), conditions=["frightened"]
            )
    return sm, sid, package, db


@pytest.mark.parametrize("dark", [False, True])
def test_public_frightened_check_distinguishes_visibility_and_replays(tmp_path, context, dark):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package, db = _setup(tmp_path, gm, dark=dark)
    with sm:
        command = check_command(sm, sid, package, gm, "check.fear")
        before_rng = sm.read_rng_context(sid, package, context=gm)
        rng = before_rng.state.restore()
        rolls = [rng.randint(1, 20) for _ in range(1 if dark else 2)]
        evaluate = sm._rules._evaluate_dnd

        async def inspect(request):
            assert request.creature_sight_facts == (
                CreatureSightFact(
                    need=CreatureSightNeed(observer_id=command.actor_id, source_id=NPC),
                    visible=not dark,
                ),
            )
            assert not sm._conn.in_transaction
            return await evaluate(request)

        sm._rules._evaluate_dnd = inspect
        receipt = sm.submit_command(command, package, context=gm)
        assert receipt.status == "committed", receipt
        event = sm.read_events(sid, package, context=gm)[-1]
        detail = event.payload["engine_event"]
        assert detail["rolled_natural"] == min(rolls)
        assert detail["advantage"] == ("normal" if dark else "disadvantage")
        assert detail["disadvantage_sources"] == ([] if dark else ["condition:attacker"])
        assert NPC not in str(event.payload)
        assert not event.signals
        assert sm.read_rng_context(sid, package, context=gm).state == RNGState.capture(rng)
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        assert restored.submit_command(command, package, context=gm) == receipt
        assert restored.read_rng_context(sid, package, context=gm).state == RNGState.capture(rng)


@pytest.mark.parametrize("untracked", [False, True])
def test_public_frightened_check_requires_known_source_and_adjudication(
    tmp_path, context, untracked
):
    gm = replace(context, can_adjudicate_checks=True)
    # The tracked source's ray crosses fixtures whose current vision is unproven.
    sm, sid, package, _ = _setup(tmp_path, gm, untracked=untracked, unproven_ray=not untracked)
    with sm:
        command = check_command(sm, sid, package, gm, "check.fear.unknown")
        before = sm.read_rng_context(sid, package, context=gm)
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(command, package, context=context)
        assert denied.value.code == "unauthorized"
        receipt = sm.submit_command(command, package, context=gm)
        assert receipt.status == "rejected", receipt
        assert receipt.error_code == "check.effects"
        assert not receipt.event_seqs
        assert sm.read_rng_context(sid, package, context=gm) == before


def test_public_frightened_check_rechecks_sight_when_position_changes(tmp_path, context):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _ = _setup(tmp_path, gm)
    with sm:
        command = check_command(sm, sid, package, gm, "check.fear.conflict")
        before = sm.read_rng_context(sid, package, context=gm)
        evaluate = sm._rules._evaluate_dnd

        async def move_before_adoption(request):
            assert request.creature_sight_facts[0].visible
            outcome = await evaluate(request)
            # Fault injection deliberately preserves the Session version to exercise sight recheck.
            with sm._store.transaction(write=True):
                sm._world.characters.move(sid, NPC, SpatialPosition(A, HIDDEN_CELL))
                row = sm._world.sessions._session(sid, package, gm)
                assert (
                    sm._rules._noncombat_snapshot(row, package, actor_ids=(command.actor_id,))
                    == request.state_snapshot
                )
            return outcome

        sm._rules._evaluate_dnd = move_before_adoption
        receipt = sm.submit_command(command, package, context=gm)
        assert receipt.status == "conflict", receipt
        assert receipt.error_code == "version_conflict"
        assert not receipt.event_seqs
        assert sm.read_rng_context(sid, package, context=gm) == before


def test_public_fear_sight_reads_stay_local_under_scene_and_remote_growth(tmp_path, context):
    gm = replace(context, can_adjudicate_checks=True)
    counts = []
    for size in (0, 25):
        directory = tmp_path / str(size)
        directory.mkdir()
        sm, sid, package, _ = _setup(directory, gm, growth=size)
        with sm:
            _grow_remote(sm, sid, size)
            command = check_command(sm, sid, package, gm, "check.fear.growth")
            evaluate = sm._rules._evaluate_dnd

            async def verify_scope(request, *, actor_id=command.actor_id, evaluate=evaluate):
                assert [actor.entity_id for actor in request.state_snapshot.character_states] == [
                    actor_id
                ]
                assert request.creature_sight_facts[0].need.source_id == NPC
                return await evaluate(request)

            sm._rules._evaluate_dnd = verify_scope
            with measure(sm) as (work, statements):
                receipt = sm.submit_command(command, package, context=gm)
            assert receipt.status == "committed", receipt
            assert any("scene_integrity" in sql for sql in statements)
            counts.append((work["sql_selects"], work["sql_rows"]))
    assert counts[0] == counts[1]


@pytest.mark.parametrize("target_id", [NPC, "npc.remote_0", "npc.absent"])
def test_public_check_projects_only_named_target_in_current_scene(tmp_path, context, target_id):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _ = _setup(tmp_path, gm)
    with sm:
        _grow_remote(sm, sid, 1)
        command = check_command(sm, sid, package, gm, "check.target", target_id=target_id)
        before = sm.read_rng_context(sid, package, context=gm)
        receipt = sm.submit_command(command, package, context=gm)
        if target_id == NPC:
            assert receipt.status == "committed", receipt
        else:
            assert (receipt.status, receipt.error_code) == ("rejected", "target_invalid")
            assert not receipt.event_seqs
            assert sm.read_rng_context(sid, package, context=gm) == before
