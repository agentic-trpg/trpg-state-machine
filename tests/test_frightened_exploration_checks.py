"""Public checks freeze exact fear-source sight and adopt only the committed roll."""

from copy import deepcopy
from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_check_facts import CreatureSightFact, CreatureSightNeed
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffect
from lock_support import CRED, DOOR, NPC, A, B, lock_loader
from spatial_support import create
from test_faerie_fire import _grow_remote
from test_lockable_passages import acquire
from test_mechanism_service import extended_data, method
from test_object_protection import large_data
from test_openable_checks import check_data
from test_real_checks import check_command
from test_scoped_runtime_validation import measure
from test_world_containers import BAG, BOX, container_data
from test_world_mechanisms import LEVER
from world_support import command as world_command
from world_support import setup, submit

from trpg_state_machine import (
    CheckMechanism,
    ClearJam,
    ForceOpen,
    ObjectState,
    PickLock,
    PickupItem,
    PutItem,
    SetObjectState,
    StateMachine,
    StateMachineError,
)
from trpg_state_machine.package import _validate
from trpg_state_machine.spatial import SpatialPosition

SOURCE_CELL = "8,6"
HIDDEN_CELL = "8,7"


def _setup(
    tmp_path, context, *, dark=False, untracked=False, unproven_ray=False, growth=0, poisoned=False
):
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
        if poisoned:
            characters.add_condition(
                sid,
                actor_id,
                ActiveCondition(
                    condition="poisoned", source_entity_id="implied:test", scope="session"
                ),
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
        assert detail["disadvantage_sources"] == ([] if dark else ["attacker:Frightened"])
        assert NPC not in str(event.payload)
        assert not event.signals
        assert sm.read_rng_context(sid, package, context=gm).state == RNGState.capture(rng)
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        assert restored.submit_command(command, package, context=gm) == receipt
        assert restored.read_rng_context(sid, package, context=gm).state == RNGState.capture(rng)


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_public_poisoned_frightened_sources_and_cancellation(tmp_path, context, dark, cancel):
    gm = replace(context, can_adjudicate_checks=True)
    sm, sid, package, db = _setup(tmp_path, gm, dark=dark, poisoned=True)
    with sm:
        command = check_command(
            sm,
            sid,
            package,
            gm,
            "check.poison.fear",
            advantage=("flag",) if cancel else (),
        )
        before = sm.read_rng_context(sid, package, context=gm)
        receipt = sm.submit_command(command, package, context=gm)
        assert receipt.status == "committed", receipt
        event = sm.read_events(sid, package, context=gm)[-1].payload["engine_event"]
        assert event["advantage"] == ("normal" if cancel else "disadvantage")
        assert event["advantage_sources"] == (["flag"] if cancel else [])
        assert event["disadvantage_sources"] == (
            ["attacker:Poisoned"] if dark else ["attacker:Poisoned", "attacker:Frightened"]
        )
        assert NPC not in str(event)
        rolls = sm.get_command_rolls(sid, command.command_id, package, context=gm)["rolls"]
        assert rolls[0]["disadvantage_sources"] == event["disadvantage_sources"]
        expected = before.state.restore()
        draws = 1 if cancel else 2
        for _ in range(draws):
            expected.randint(1, 20)
        assert sm.read_rng_context(sid, package, context=gm).state == RNGState.capture(expected)
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        assert restored.submit_command(command, package, context=gm) == receipt


@pytest.mark.parametrize("untracked", [False, True])
@pytest.mark.parametrize("poisoned", [False, True])
def test_public_frightened_check_requires_known_source_and_adjudication(
    tmp_path, context, untracked, poisoned
):
    gm = replace(context, can_adjudicate_checks=True)
    # The tracked source's ray crosses fixtures whose current vision is unproven.
    sm, sid, package, _ = _setup(
        tmp_path, gm, untracked=untracked, unproven_ray=not untracked, poisoned=poisoned
    )
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


@pytest.mark.parametrize(
    ("sight", "poisoned"),
    [
        ("visible", False),
        ("hidden", False),
        ("unknown", False),
        ("changed", False),
        ("visible", True),
        ("hidden", True),
    ],
)
def test_frightened_pick_lock_prepares_sight_and_replays(
    tmp_path, context, sight, poisoned, monkeypatch
):
    """An action-triggered check uses the same declared sight as rules.check."""
    data, loader = check_data(dc=100)
    if sight != "unknown":
        data["passages"][0]["door_object_id"] = None
        data["scenes"][0]["objects"][0]["initial_cell"] = "0,1"
    data["actors"][0]["initial_cell"] = "2,2"
    data["rule_bindings"].pop("combat_foundation", None)
    grids = [
        data["scenes"][0]["grid"],
        data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"],
    ]
    for grid in grids:
        grid["default_lighting"] = "bright"
        if sight == "hidden":
            grid["lighting"]["2,2"] = "dark"
    package = _validate(data)
    db = tmp_path / "fear-lock.db"
    with StateMachine(db, dnd_loader=loader) as sm:
        sid = create(sm, package, context)
        acquire(sm, sid, package, context, iid=CRED, key="tool")
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        with sm._store.transaction(write=True):
            effect = effect_state(
                ActiveEffect(
                    id="effect:fear",
                    name="Fear",
                    origin=f"cast:fear:{NPC}",
                    target_id=actor,
                    statuses={"frightened"},
                )
            )
            sm._world.characters.effects.add_effect(
                sid, effect.model_dump(mode="json"), conditions=["frightened"]
            )
            if poisoned:
                sm._world.characters.add_condition(
                    sid,
                    actor,
                    ActiveCondition(
                        condition="poisoned", source_entity_id="implied:test", scope="session"
                    ),
                )
        op = PickLock(A, DOOR, "lock.outer", CRED, "0,0")
        availability = sm.query_world_action(sid, actor, op, package, context=context)
        assert availability.status == ("unavailable" if sight == "unknown" else "available"), (
            availability
        )
        check = world_command(sm, sid, package, context, op, "fear-pick")
        before_rng = sm.read_rng_context(sid, package, context=context)
        expected_rng = before_rng.state.restore()
        disadvantaged = sight == "visible" or poisoned
        if sight in ("visible", "hidden"):
            for _ in range(2 if disadvantaged else 1):
                expected_rng.randint(1, 20)
        if sight == "changed":
            import trpg_state_machine.rule_execution as execution

            evaluate = execution.evaluate_openable

            async def move_source_before_adoption(spec, *, loader):
                outcome = await evaluate(spec, loader=loader)
                with sm._store.transaction(write=True):
                    sm._world.characters.move(sid, NPC, SpatialPosition(B, "0,0"))
                return outcome

            monkeypatch.setattr(execution, "evaluate_openable", move_source_before_adoption)
        receipt = sm.submit_command(check, package, context=context)
        if sight == "changed":
            assert (receipt.status, receipt.error_code) == ("conflict", "version_conflict")
            assert not receipt.event_seqs
            assert sm.read_rng_context(sid, package, context=context) == before_rng
            return
        if sight == "unknown":
            assert (receipt.status, receipt.error_code) == ("rejected", "check.effects")
            assert not receipt.event_seqs
            assert sm.read_rng_context(sid, package, context=context) == before_rng
            return
        assert receipt.status == "committed", receipt
        event = next(
            e
            for e in sm.read_events(sid, package, context=context)
            if e.event_seq in receipt.event_seqs and e.event_type == "rules.check_rolled"
        )
        detail = event.payload["engine_event"]
        assert detail["advantage"] == ("disadvantage" if disadvantaged else "normal")
        reasons = ["attacker:Frightened"] if sight == "visible" else []
        if poisoned:
            reasons.append("attacker:Poisoned")
        assert sorted(detail["disadvantage_sources"]) == reasons
        assert sm.read_rng_context(sid, package, context=context).state == RNGState.capture(
            expected_rng
        )
        assert sm.submit_command(check, package, context=context) == receipt
    with StateMachine(db, dnd_loader=loader) as restored:
        assert restored.submit_command(check, package, context=context) == receipt


def test_frightened_mechanism_check_uses_same_fact_preparation(tmp_path, context):
    """A service action checks sight in availability and at actual evaluation."""
    disarm = method("disarm")
    disarm.update(ability="dex", skill="sleight_of_hand", dc=100)
    data = extended_data(methods=[disarm])
    data["passages"][0]["door_object_id"] = None
    data["scenes"][0]["objects"][0]["initial_cell"] = "0,2"
    data["actors"][0]["initial_cell"] = "2,2"
    for grid in (
        data["scenes"][0]["grid"],
        data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"],
    ):
        grid["default_lighting"] = "bright"
    package = _validate(data)
    with StateMachine(tmp_path / "fear-mechanism.db", dnd_loader=lock_loader()) as sm:
        sid = create(sm, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        with sm._store.transaction(write=True):
            effect = effect_state(
                ActiveEffect(
                    id="effect:fear",
                    name="Fear",
                    origin=f"cast:fear:{NPC}",
                    target_id=actor,
                    statuses={"frightened"},
                )
            )
            sm._world.characters.effects.add_effect(
                sid, effect.model_dump(mode="json"), conditions=["frightened"]
            )
        op = CheckMechanism(A, LEVER, "0,0", "disarm")
        availability = sm.query_world_action(sid, actor, op, package, context=context)
        assert availability.status == "available", availability
        receipt = sm.submit_command(
            world_command(sm, sid, package, context, op, "fear-mech"), package, context=context
        )
        assert receipt.status == "committed", receipt
        event = next(
            e
            for e in sm.read_events(sid, package, context=context)
            if e.event_seq in receipt.event_seqs and e.event_type == "rules.check_rolled"
        )
        assert event.payload["engine_event"]["advantage"] == "disadvantage"


def test_frightened_crowbar_advantage_cancels_on_actual_check(tmp_path, context):
    """RE combines a check's tool advantage and visible fear disadvantage."""
    data, loader = check_data(dc=100, jammed=True)
    data["passages"][0]["door_object_id"] = None
    data["scenes"][0]["objects"][0]["initial_cell"] = "0,1"
    data["actors"][0]["initial_cell"] = "2,2"
    data["rule_bindings"].pop("combat_foundation", None)
    next(item for item in data["item_transfers"] if item["instance_id"] == CRED)["item_slug"] = (
        "crowbar"
    )
    method = data["world_objects"][0]["capabilities"][0]["force_methods"][0]
    method.update(required_tool="crowbar", leverage_applicable=True)
    package = _validate(data)
    with StateMachine(tmp_path / "fear-crowbar.db", dnd_loader=loader) as sm:
        sid = create(sm, package, context)
        acquire(sm, sid, package, context, iid=CRED, key="crowbar")
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        with sm._store.transaction(write=True):
            effect = effect_state(
                ActiveEffect(
                    id="effect:fear",
                    name="Fear",
                    origin=f"cast:fear:{NPC}",
                    target_id=actor,
                    statuses={"frightened"},
                )
            )
            sm._world.characters.effects.add_effect(
                sid, effect.model_dump(mode="json"), conditions=["frightened"]
            )
        op = ClearJam(A, DOOR, "free-jam", CRED, "0,0")
        assert sm.query_world_action(sid, actor, op, package, context=context).status == "available"
        receipt = sm.submit_command(
            world_command(sm, sid, package, context, op, "fear-crowbar"),
            package,
            context=context,
        )
        assert receipt.status == "committed", receipt
        event = next(
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in receipt.event_seqs and event.event_type == "rules.check_rolled"
        )
        assert event.payload["engine_event"]["advantage"] == "normal"
        assert event.payload["engine_event"]["advantage_sources"]
        assert event.payload["engine_event"]["disadvantage_sources"]


def test_world_check_still_rejects_unreviewed_actor_condition(tmp_path, context):
    """The check-specific sight path does not admit an unrelated action condition."""
    data, loader = check_data()
    package = _validate(data)
    with StateMachine(tmp_path / "unreviewed-condition.db", dnd_loader=loader) as sm:
        sid = create(sm, package, context)
        acquire(sm, sid, package, context, iid=CRED, key="tool")
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        with sm._store.transaction(write=True):
            sm._world.characters.add_condition(
                sid,
                actor,
                ActiveCondition(
                    condition="incapacitated",
                    source_entity_id="npc:012345abcdef",
                    scope="session",
                ),
            )
        op = PickLock(A, DOOR, "lock.outer", CRED, "0,0")
        availability = sm.query_world_action(sid, actor, op, package, context=context)
        assert (availability.status, availability.reason) == (
            "unavailable",
            "unsupported_actor_state",
        )


def test_frightened_lock_check_reads_stay_local_under_remote_growth(tmp_path, context):
    """The named lock and fear source keep fixed reads as remote instances grow."""
    observed = []
    for growth in (0, 25):
        data, loader = check_data(dc=100)
        data["passages"][0]["door_object_id"] = None
        data["scenes"][0]["objects"][0]["initial_cell"] = "0,1"
        data["actors"][0]["initial_cell"] = "2,2"
        data["rule_bindings"].pop("combat_foundation", None)
        package = _validate(data)
        with StateMachine(tmp_path / f"fear-scale-{growth}.db", dnd_loader=loader) as sm:
            sid = create(sm, package, context)
            acquire(sm, sid, package, context, iid=CRED, key="tool")
            actor = sm.read_session(sid, package, context=context).pc_actor_id
            with sm._store.transaction(write=True):
                effect = effect_state(
                    ActiveEffect(
                        id="effect:fear",
                        name="Fear",
                        origin=f"cast:fear:{NPC}",
                        target_id=actor,
                        statuses={"frightened"},
                    )
                )
                sm._world.characters.effects.add_effect(
                    sid, effect.model_dump(mode="json"), conditions=["frightened"]
                )
            if growth:
                _grow_remote(sm, sid, growth)
            op = PickLock(A, DOOR, "lock.outer", CRED, "0,0")
            with measure(sm) as (availability_work, _):
                availability = sm.query_world_action(sid, actor, op, package, context=context)
            assert availability.status == "available", availability
            with measure(sm) as (submit_work, _):
                receipt = sm.submit_command(
                    world_command(sm, sid, package, context, op, "fear-scale"),
                    package,
                    context=context,
                )
            assert receipt.status == "committed", receipt
            observed.append(
                (
                    (availability_work["sql_selects"], availability_work["sql_rows"]),
                    (submit_work["sql_selects"], submit_work["sql_rows"]),
                )
            )
    assert observed[0] == observed[1]


@pytest.mark.parametrize("operation_type", [ClearJam, ForceOpen])
@pytest.mark.parametrize("dc,success", [(0, True), (100, False)])
def test_frightened_open_after_check_keeps_direct_toggle_guard(
    tmp_path, context, operation_type, dc, success
):
    """Only an accepted check may carry its validated scope into open_after."""
    data, loader = check_data(dc=dc, jammed=True)
    data["passages"][0]["door_object_id"] = None
    data["scenes"][0]["objects"][0]["initial_cell"] = "0,1"
    data["actors"][0]["initial_cell"] = "2,2"
    data["rule_bindings"].pop("combat_foundation", None)
    for lock in data["world_objects"][0]["capabilities"][1]["locks"]:
        lock["initial_locked"] = False
    method = data["world_objects"][0]["capabilities"][0]["force_methods"][0]
    method["open_after"] = True
    package = _validate(data)
    with StateMachine(
        tmp_path / f"fear-open-{operation_type.__name__}-{dc}.db", dnd_loader=loader
    ) as sm:
        sid = create(sm, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        with sm._store.transaction(write=True):
            effect = effect_state(
                ActiveEffect(
                    id="effect:fear",
                    name="Fear",
                    origin=f"cast:fear:{NPC}",
                    target_id=actor,
                    statuses={"frightened"},
                )
            )
            sm._world.characters.effects.add_effect(
                sid, effect.model_dump(mode="json"), conditions=["frightened"]
            )
        direct = SetObjectState(A, DOOR, ObjectState.OPEN)
        assert (
            submit(sm, sid, package, context, direct, "direct").error_code
            == "unsupported_actor_state"
        )
        op = operation_type(A, DOOR, "free-jam", None, "0,0")
        assert sm.query_world_action(sid, actor, op, package, context=context).status == "available"
        receipt = submit(sm, sid, package, context, op, "checked-open")
        assert receipt.status == "committed", receipt
        assert sm._world_facts.object(sid, DOOR)["state"] == ("open" if success else "closed")
        assert bool(sm._world_facts.openable_state(sid, DOOR)["jammed"]) is not success
        assert (
            submit(
                sm,
                sid,
                package,
                context,
                SetObjectState(A, DOOR, ObjectState.CLOSED),
                "direct-again",
            ).error_code
            == "unsupported_actor_state"
        )


@pytest.mark.parametrize("operation_type", [ClearJam, ForceOpen])
def test_frightened_noop_openable_query_does_not_require_sight(tmp_path, context, operation_type):
    """Availability matches no-op submit when fear-source sight is unresolved."""
    data, loader = check_data()
    package = _validate(data)
    with StateMachine(
        tmp_path / f"fear-noop-{operation_type.__name__}.db", dnd_loader=loader
    ) as sm:
        sid = create(sm, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        with sm._store.transaction(write=True):
            effect = effect_state(
                ActiveEffect(
                    id="effect:fear",
                    name="Fear",
                    origin=f"cast:fear:{NPC}",
                    target_id=actor,
                    statuses={"frightened"},
                )
            )
            sm._world.characters.effects.add_effect(
                sid, effect.model_dump(mode="json"), conditions=["frightened"]
            )
        op = operation_type(A, DOOR, "free-jam", None, "0,0")
        before_rng = sm.read_rng_context(sid, package, context=context)
        availability = sm.query_world_action(sid, actor, op, package, context=context)
        assert availability.status == "available", availability
        receipt = submit(sm, sid, package, context, op, "noop-fear")
        assert receipt.status == "committed" and not receipt.event_seqs, receipt
        assert sm.read_rng_context(sid, package, context=context) == before_rng


def test_frightened_check_reaches_contained_openable(tmp_path, context):
    """A real force check can reach a contained target and apply its opening result."""
    data = container_data()
    data["manifest"]["schema_version"] = "state-machine-foundation/7"
    data["passages"][0]["door_object_id"] = None
    data["lockable_doors"] = [lock for lock in data["lockable_doors"] if lock["object_id"] != DOOR]
    data["actors"][0]["initial_cell"] = "2,2"
    bag = next(obj for obj in data["scenes"][0]["objects"] if obj["object_id"] == BAG)
    bag["state"] = "closed"
    profile = next(obj for obj in data["world_objects"] if obj["object_id"] == BAG)
    profile["capabilities"][0].update(
        initial_jammed=True,
        force_methods=[
            {
                "method_id": "clear-and-open",
                "ability": "str",
                "skill": "athletics",
                "dc": 0,
                "required_tool": None,
                "leverage_applicable": False,
                "unlock_lock_id": None,
                "clear_bar": False,
                "clear_jam": True,
                "open_after": True,
            }
        ],
    )
    package = _validate(data)
    with StateMachine(tmp_path / "fear-contained.db", dnd_loader=lock_loader()) as sm:
        sid = create(sm, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        assert (
            submit(sm, sid, package, context, PickupItem(BAG, A, "1,0"), "bag").status
            == "committed"
        )
        assert (
            submit(sm, sid, package, context, PutItem(A, BAG, BOX, "0,0"), "nest").status
            == "committed"
        )
        with sm._store.transaction(write=True):
            effect = effect_state(
                ActiveEffect(
                    id="effect:fear",
                    name="Fear",
                    origin=f"cast:fear:{NPC}",
                    target_id=actor,
                    statuses={"frightened"},
                )
            )
            sm._world.characters.effects.add_effect(
                sid, effect.model_dump(mode="json"), conditions=["frightened"]
            )
        op = ForceOpen(A, BAG, "clear-and-open", None, "0,0")
        assert sm.query_world_action(sid, actor, op, package, context=context).status == "available"
        receipt = submit(sm, sid, package, context, op, "contained-open")
        assert receipt.status == "committed", receipt
        assert sm._world_facts.object(sid, BAG)["state"] == "open"
        assert sm._world_facts.placement(sid, BAG)["container_id"] == BOX
