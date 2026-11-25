"""Open dynamic encounters through the public, durable command boundary."""

from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd_support import NPC, create
from modern_support import modern_data, modern_loader
from test_dnd_npc_availability import npc_command
from test_scoped_runtime_validation import measure
from test_session_time_coordination import Clock, Scheduler, start, sync

from trpg_state_machine import (
    CombatOpponent,
    DistanceWithdrawal,
    StartCombat,
    StateMachine,
    StateMachineError,
    StepActor,
    TimeBinding,
    TypedCommand,
)
from trpg_state_machine.package import _validate

SCENE = "scene.platform"


def opening_data(base, assets, *, extra=0, obstacle=False):
    data = modern_data(base, assets, npc_hp=12, spatial=True)
    data["time_binding"] = asdict(TimeBinding())
    data["rule_bindings"].pop("combat_foundation")
    foundation = data["rule_bindings"]["noncombat_foundation"]
    snapshot = foundation["snapshot"]
    data["scenes"][0]["grid"].update(width=14, height=10)
    if obstacle:
        data["scenes"][0]["grid"]["blocked_cells"] = ["4,4"]
    snapshot["scene_state"]["grid"] = deepcopy(data["scenes"][0]["grid"])
    snapshot["character_states"][0]["hp_current"] = 6
    data["pc_templates"][0]["character"]["hp"]["current"] = 6
    data["actors"][1].update(initial_scene_id=SCENE, initial_cell="3,3")
    data["scenes"][0]["actor_ids"].append("npc.attendant")
    data["scenes"][1]["actor_ids"].remove("npc.attendant")
    bystander = deepcopy(snapshot["character_states"][1])
    bystander.update(entity_id="npc.attendant", name="Platform witness")
    snapshot["character_states"].append(bystander)
    foundation["xp"]["npc.attendant"] = 0
    for index in range(extra):
        actor = f"npc.remote_{index}"
        data["actors"].append(
            {
                "actor_id": actor,
                "name": actor,
                "kind": "npc",
                "controller": {"kind": "scripted_event"},
                "initial_scene_id": "scene.workshop",
                "initial_cell": f"{index},2",
            }
        )
        data["scenes"][1]["actor_ids"].append(actor)
        data["scenes"][1]["objects"].append(
            {
                "object_id": f"object.remote_{index}",
                "state": None,
                "movable": False,
                "blocks_passage": False,
                "initial_cell": f"{index},1",
            }
        )
    data["scenes"][1]["grid"]["width"] = max(3, extra)
    data["rule_bindings"]["noncombat_foundation"]["additional_scenes"][0]["grid"] = deepcopy(
        data["scenes"][1]["grid"]
    )
    return data


def start_command(sm, sid, package, context, command_id="open", **options):
    session = sm.read_session(sid, package, context=context)
    pc = session.pc_actor_id
    params = {
        "participant_ids": (pc, NPC),
        "initiative_order": (NPC, pc),
        "opening_cell": "0,0",
        "opponent_profiles": (CombatOpponent(NPC, "bandit"),),
        **options,
    }
    return TypedCommand(
        sid, command_id, pc, "combat.start", StartCombat(**params), session.world_version
    )


def test_open_from_exploration_with_clipped_region_and_real_actors(tmp_path, package_data, context):
    assets = modern_loader()
    package = _validate(opening_data(package_data, assets))
    host = replace(context, can_start_combat=True, can_advance_time=True)
    path = tmp_path / "dynamic.db"
    with StateMachine(path, dnd_loader=assets) as sm:
        sid = create(sm, package, host)
        pc = sm.read_session(sid, package, context=host).pc_actor_id
        move = TypedCommand(sid, "move", pc, "world.step_actor", StepActor(SCENE, "0,0", "0,1"), 0)
        motion = sm.submit_command(move, package, context=host)
        assert motion.status == "committed", motion
        before = sm.read_noncombat_snapshot(sid, package, context=host)
        rng = sm.read_rng_context(sid, package, context=host)
        assert sm.read_combat_region(sid, package, context=host) is None
        clock = Clock()
        runtime = (sm, sid, package, host, clock, Scheduler(clock))
        start(runtime)
        clock.advance(10)
        command = start_command(sm, sid, package, host)
        receipt = sm.submit_command(command, package, context=host)
        assert receipt.status == "committed", receipt
        assert sync(runtime).state == "combat" and sync(runtime).committed_tick == 10
        clock.advance(30)
        assert sync(runtime).estimated_target_tick == 10
        area = sm.read_combat_region(sid, package, context=host)
        assert sm.read_combat_withdrawal(sid, package, context=host) == area
        assert area.scene_id == SCENE
        assert area.cells == frozenset(f"{x},{y}" for x in range(7) for y in range(7))
        current = sm.read_combat_snapshot(sid, package, context=host)
        assert current.combat_state.combat_id == area.combat_id
        assert current.combat_state.initiative_ids == (NPC, pc)
        assert current.combat_state.actor_zone == {pc: "0,1", NPC: "1,0"}
        assert current.inventory_state == before.inventory_state
        assert {a.entity_id: a.hp_current for a in current.character_states} == {
            a.entity_id: a.hp_current for a in before.character_states if a.entity_id in (pc, NPC)
        }
        intent = npc_command(sm, sid, package, host)
        npc_action = sm.query_action_availability(sid, NPC, intent.payload, package, context=host)
        assert npc_action.status == "available", npc_action
        assert sm.read_rng_context(sid, package, context=host) == rng
        assert sm.submit_command(command, package, context=host) == receipt
        assert sm.read_combat_snapshot(sid, package, context=host) == current
        assert (
            sm.submit_command(
                replace(command, command_id="different"), package, context=host
            ).status
            == "conflict"
        )
        second = start_command(sm, sid, package, host, "still.active")
        assert (
            sm.submit_command(second, package, context=host).error_code == "combat_already_active"
        )
        pass_command = TypedCommand(
            sid,
            "npc-pass",
            NPC,
            "combat.intent",
            CombatIntentPayload(intent_type="pass", source_id=NPC),
            current.world_version,
        )
        passed = sm.submit_command(pass_command, package, context=host)
        assert passed.status == "committed", passed
        combat = sm.read_combat_snapshot(sid, package, context=host).combat_state
        assert combat.initiative_ids[combat.current_turn_index] == pc

    with StateMachine(path, dnd_loader=assets) as reopened:
        assert reopened.read_combat_region(sid, package, context=host) == area
        assert reopened.submit_command(command, package, context=host) == receipt


def test_exact_region_refuses_bad_requests_and_rolls_back_late_fault(
    tmp_path, package_data, context
):
    assets = modern_loader()
    package = _validate(opening_data(package_data, assets, obstacle=True))
    host = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "exact.db", dnd_loader=assets) as sm:
        sid = create(sm, package, host)
        good = start_command(sm, sid, package, host, opening_cell=None, area_cells=("0,0", "1,0"))
        before = sm.read_session(sid, package, context=host)
        with pytest.raises(StateMachineError, match="Trusted encounter-start grant"):
            sm.submit_command(good, package, context=context)
        with pytest.raises(StateMachineError, match="unique roster"):
            bad_order = replace(good.payload, initiative_order=(NPC, NPC))
            sm.submit_command(replace(good, payload=bad_order), package, context=host)
        for cells in (("0,0",), ("0,0", "1,0", "4,4"), ("0,0", "1,0", "99,99")):
            rejected = replace(
                good, command_id=str(cells), payload=replace(good.payload, area_cells=cells)
            )
            assert (
                sm.submit_command(rejected, package, context=host).error_code
                == "invalid_combat_region"
            )
        bad_profile = replace(good.payload, opponent_profiles=(CombatOpponent(NPC, "absent"),))
        assert (
            sm.submit_command(
                replace(good, command_id="bad.profile", payload=bad_profile), package, context=host
            ).error_code
            == "unsupported_combat_foundation"
        )
        assert sm.read_combat_region(sid, package, context=host) is None

        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_open_receipt BEFORE INSERT ON command_receipts "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError) as fault:
            sm.submit_command(good, package, context=host)
        assert fault.value.code == "storage_error"
        assert sm.read_session(sid, package, context=host) == before
        assert sm.read_combat_region(sid, package, context=host) is None
        assert sm.get_command_receipt(sid, good.command_id, package, context=host) is None
        sm._conn.execute("DROP TRIGGER fail_open_receipt")
        assert sm.submit_command(good, package, context=host).status == "committed"
        assert sm.read_combat_region(sid, package, context=host).cells == frozenset(
            good.payload.area_cells
        )


def closed_door_opening_data(package_data, assets, npc_cell, door_count):
    data = opening_data(package_data, assets)
    data["actors"][0]["initial_cell"] = npc_cell
    data["passages"][0]["door_object_id"] = "object.platform_gate"
    opening_scene = data["scenes"][0]
    for index in range(door_count):
        room, door, passage = (
            f"scene.remote_room_{index}",
            f"object.remote_door_{index}",
            f"passage.remote_port_{index}",
        )
        x = index + 2
        opening_scene["exits"].append({"to_scene_id": room, "passage_id": passage})
        opening_scene["objects"].append(
            {
                "object_id": door,
                "state": "closed",
                "movable": False,
                "blocks_passage": False,
                "initial_cell": f"{x},6",
            }
        )
        data["world"]["locations"][0]["scene_ids"].append(room)
        data["scenes"].append(
            {
                "scene_id": room,
                "location_id": opening_scene["location_id"],
                "description": "Unused locked side room.",
                "exits": [{"to_scene_id": SCENE, "passage_id": passage}],
                "initial_state": {"local_flags": {}},
                "actor_ids": [],
                "objects": [],
                "event_definition_ids": [],
                "access_policy": "public_interactions",
                "grid": deepcopy(data["scenes"][1]["grid"]),
            }
        )
        data["passages"].append(
            {
                "passage_id": passage,
                "door_object_id": door,
                "endpoints": [
                    {
                        "scene_id": SCENE,
                        "standing_cell": f"{x},5",
                        "threshold_cell": f"{x},6",
                    },
                    {"scene_id": room, "standing_cell": "0,0", "threshold_cell": "0,1"},
                ],
            }
        )
    return data


@pytest.mark.parametrize(("npc_cell", "attack_status"), [("1,0", "available"), ("0,2", "unknown")])
def test_closed_passage_projection_is_local_to_combat_attack_ray(
    tmp_path, package_data, context, monkeypatch, npc_cell, attack_status
):
    assets = modern_loader()
    host = replace(context, can_start_combat=True)
    reads_by_size = []
    for door_count in (0, 12):
        package = _validate(closed_door_opening_data(package_data, assets, npc_cell, door_count))
        with StateMachine(tmp_path / f"door-{door_count}.db", dnd_loader=assets) as sm:
            sid = create(sm, package, host)
            command = start_command(sm, sid, package, host)
            receipt = sm.submit_command(command, package, context=host)
            assert receipt.status == "committed", receipt
            combat = sm.read_combat_snapshot(sid, package, context=host)
            assert set(combat.scene_state.grid.blocked_cells) == {
                "0,1",
                *(f"{i + 2},6" for i in range(door_count)),
            }
            intent = npc_command(sm, sid, package, host, action="light-crossbow")
            door_reads = []
            read_blocker = sm._world._door_blocks_passage

            def count_door_read(
                session, door_id, current_package, *, reads=door_reads, read=read_blocker
            ):
                reads.append(door_id)
                return read(session, door_id, current_package)

            with monkeypatch.context() as patch:
                patch.setattr(sm._world, "_door_blocks_passage", count_door_read)
                no_control = replace(host, controlled_npc_ids=())
                with pytest.raises(StateMachineError, match="NPC control grant"):
                    sm.query_action_availability(
                        sid, NPC, intent.payload, package, context=no_control
                    )
                with pytest.raises(StateMachineError, match="NPC control grant"):
                    sm.submit_command(intent, package, context=no_control)
                assert door_reads == []
                result = sm.query_action_availability(
                    sid, NPC, intent.payload, package, context=host
                )
                assert result.status == attack_status, result
                query_reads = tuple(door_reads)
                door_reads.clear()
                attack = sm.submit_command(intent, package, context=host)
                commit_reads = tuple(door_reads)
            reads_by_size.append((query_reads, commit_reads))
        if attack_status == "unknown":
            assert result.reason.code == "snapshot.capability"
            assert attack.status == "rejected" and attack.error_code == "snapshot.capability"
        else:
            assert attack.status == "committed", attack
    expected = ("object.platform_gate",) if attack_status == "unknown" else ()
    assert reads_by_size == [(expected, expected), (expected, expected)]
    print("attack door reads (query/submit), with zero and 12 off-ray doors:", reads_by_size)


def test_pursuit_opening_persists_distance_and_current_facts(tmp_path, package_data, context):
    assets = modern_loader()
    data = opening_data(package_data, assets)
    data["actors"][0]["initial_cell"] = "12,0"
    package = _validate(data)
    host = replace(context, can_start_combat=True, can_advance_time=True)
    database = tmp_path / "pursuit.db"
    with StateMachine(database, dnd_loader=assets) as sm:
        sid = create(sm, package, host)
        before = sm.read_noncombat_snapshot(sid, package, context=host)
        original_rng = sm.read_rng_context(sid, package, context=host)
        clock = Clock()
        runtime = (sm, sid, package, host, clock, Scheduler(clock))
        start(runtime)
        clock.advance(8)
        command = start_command(
            sm, sid, package, host, opening_cell=None, withdrawal_distance_ft=75
        )
        assert sm.read_combat_withdrawal(sid, package, context=host) is None
        receipt = sm.submit_command(command, package, context=host)
        assert receipt.status == "committed", receipt
        combat = sm.read_combat_snapshot(sid, package, context=host)
        policy = DistanceWithdrawal(combat.combat_state.combat_id, SCENE, 75)
        assert sm.read_combat_withdrawal(sid, package, context=host) == policy
        assert sm.read_combat_region(sid, package, context=host) is None
        denied = replace(host, principal_id="other-principal")
        with pytest.raises(StateMachineError):
            sm.read_combat_withdrawal(sid, package, context=denied)
        assert combat.combat_state.actor_zone == {command.actor_id: "0,0", NPC: "12,0"}
        assert combat.inventory_state == before.inventory_state
        assert {a.entity_id: a.hp_current for a in combat.character_states} == {
            a.entity_id: a.hp_current
            for a in before.character_states
            if a.entity_id in combat.combat_state.initiative_ids
        }
        assert sm.read_rng_context(sid, package, context=host) == original_rng
        clock.advance(50)
        assert sync(runtime).state == "combat"
        assert sync(runtime).committed_tick == sync(runtime).estimated_target_tick == 8
        changed = replace(command, payload=replace(command.payload, withdrawal_distance_ft=90))
        with pytest.raises(StateMachineError, match="different content"):
            sm.submit_command(changed, package, context=host)
    with StateMachine(database, dnd_loader=assets) as reopened:
        assert reopened.read_combat_withdrawal(sid, package, context=host) == policy
        assert reopened.read_combat_region(sid, package, context=host) is None
        assert reopened.submit_command(command, package, context=host) == receipt


def test_start_requires_one_valid_withdrawal_configuration(tmp_path, package_data, context):
    assets = modern_loader()
    package = _validate(opening_data(package_data, assets))
    host = replace(context, can_start_combat=True)
    with StateMachine(tmp_path / "bad-policy.db", dnd_loader=assets) as sm:
        sid = create(sm, package, host)
        original = sm.read_session(sid, package, context=host)
        invalid_options = (
            {"opening_cell": None},
            {"opening_cell": None, "withdrawal_distance_ft": 0},
            {"opening_cell": None, "withdrawal_distance_ft": -5},
            {"opening_cell": None, "withdrawal_distance_ft": True},
            {"opening_cell": None, "withdrawal_distance_ft": 30.5},
            {"opening_cell": None, "withdrawal_distance_ft": 2**63},
            {"withdrawal_distance_ft": 30},
            {"opening_cell": None, "area_cells": ("0,0", "1,0"), "withdrawal_distance_ft": 30},
        )
        for index, options in enumerate(invalid_options):
            command = start_command(sm, sid, package, host, f"invalid-{index}", **options)
            with pytest.raises(StateMachineError, match="positive integer distance") as error:
                sm.submit_command(command, package, context=host)
            assert error.value.code == "invalid_operation"
            assert sm.get_command_receipt(sid, command.command_id, package, context=host) is None
        assert sm.read_session(sid, package, context=host) == original
        assert sm.read_combat_withdrawal(sid, package, context=host) is None


@pytest.mark.parametrize("distance", [None, 60])
def test_public_opening_reads_ignore_unrelated_scene_growth(
    tmp_path, package_data, context, distance
):
    assets = modern_loader()
    host = replace(context, can_start_combat=True)
    counts = []
    for size in (0, 25):
        package = _validate(opening_data(package_data, assets, extra=size))
        with StateMachine(tmp_path / f"scope-{size}.db", dnd_loader=assets) as sm:
            sid = create(sm, package, host)
            with sm._transaction(write=True):
                sm.scene_lifecycle.ensure_scene(sid, "scene.workshop", package)
            options = {"opening_cell": None, "withdrawal_distance_ft": distance} if distance else {}
            command = start_command(sm, sid, package, host, **options)
            with measure(sm) as (work, _):
                receipt = sm.submit_command(command, package, context=host)
            assert receipt.status == "committed", receipt
            counts.append((work["sql_selects"], work["sql_rows"]))
    assert counts[0] == counts[1]
    print("combat.start SELECT statements and fetched rows before and after growth:", counts)
