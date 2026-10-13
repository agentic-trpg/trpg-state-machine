from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd_support import NPC, WEAPON
from test_dnd_runtime import attack, authority, setup

from trpg_state_machine import SetSceneFlag, StateMachine, StateMachineError, TypedCommand


def npc_command(sm, sid, package, context, *, action="scimitar", key="attack.npc"):
    s = sm.read_session(sid, package, context=context)
    payload = (
        CombatIntentPayload(
            intent_type="attack",
            source_id=NPC,
            stat_block_action_id=action,
            target_id=s.pc_actor_id,
        )
        if action
        else CombatIntentPayload(
            intent_type="attack", source_id=NPC, weapon_id=WEAPON, target_id=s.pc_actor_id
        )
    )
    return TypedCommand(sid, key, NPC, "combat.intent", payload, s.world_version)


@pytest.mark.parametrize("action", [None, "scimitar", "light-crossbow"])
def test_pc_and_npc_share_real_attack_commit_and_replay(tmp_path, package_data, context, action):
    def equip_weapon(foundation):
        if action is None:
            npc = foundation["snapshot"]["character_states"][1]
            npc.update(
                carried_item_slugs=[WEAPON], weapon_in_hands=WEAPON, weapon_grip="one_handed"
            )
            foundation["snapshot"]["inventory_state"]["entries"].append(
                {
                    "instance_id": "inventory.npc.lantern",
                    "owner_id": NPC,
                    "item_slug": WEAPON,
                    "quantity": 1,
                    "charges_remaining_per_unit": 0,
                    "accessible": True,
                }
            )

    sm, sid, package, assets = setup(tmp_path, package_data, context, equip_weapon)
    with sm:
        assert (
            sm.submit_command(attack(sm, sid, package, context), package, context=context).status
            == "committed"
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        assert before.combat_state.initiative_ids[before.combat_state.current_turn_index] == NPC
        command = npc_command(sm, sid, package, context, action=action)
        query = sm.query_action_availability(sid, NPC, command.payload, package, context=context)
        assert query.status == "available"
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.character_states[0].hp_current <= before.character_states[0].hp_current
        assert sm.read_rng_context(sid, package, context=context).version == rng.version + 1
        rows = authority(sm, sid)
        assert sm.submit_command(command, package, context=context) == receipt
        assert authority(sm, sid) == rows
        assert any(
            e.payload.get("engine_event", {}).get("actor_id") == NPC
            for e in sm.read_events(sid, package, context=context)
        )
    with StateMachine(tmp_path / "dnd.db", dnd_loader=assets) as reopened:
        assert (
            reopened.get_command_receipt(sid, command.command_id, package, context=context)
            == receipt
        )


@pytest.mark.parametrize("principal,grants", [("other", (NPC,)), ("authenticated:alice", ())])
def test_wrong_principal_and_revoked_grant_cannot_submit_or_query(
    tmp_path, package_data, context, principal, grants
):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        sm.submit_command(attack(sm, sid, package, context), package, context=context)
        command = npc_command(sm, sid, package, context)
        wrong = replace(context, principal_id=principal, controlled_npc_ids=grants)
        before = authority(sm, sid)
        with pytest.raises(StateMachineError) as caught:
            sm.submit_command(command, package, context=wrong)
        assert caught.value.code == "unauthorized"
        with pytest.raises(StateMachineError):
            sm.query_action_availability(sid, NPC, command.payload, package, context=wrong)
        assert authority(sm, sid) == before
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        with pytest.raises(StateMachineError):
            sm.get_command_receipt(sid, command.command_id, package, context=wrong)


def test_npc_budget_exhaustion_is_unavailable_and_rejected(tmp_path, package_data, context):
    def change(data):
        c = data["snapshot"]["combat_state"]
        c.update(current_turn_index=1, turn_serial=2, monster_turn_start_done=[2, NPC])
        data["snapshot"]["character_states"][1].update(
            action_available=False, attacks_remaining=0, attack_action_engaged=True
        )

    sm, sid, package, _ = setup(tmp_path, package_data, context, change)
    with sm:
        command = npc_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        query = sm.query_action_availability(sid, NPC, command.payload, package, context=context)
        assert query.status == "unavailable"
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "rejected"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng


@pytest.mark.parametrize(
    "action", ["unknown-action", "multiattack", "recharge-unreviewed", "spellcasting"]
)
def test_unknown_stat_block_capability_refused_before_evaluation(
    tmp_path, package_data, context, action
):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        sm.submit_command(attack(sm, sid, package, context), package, context=context)
        command = npc_command(sm, sid, package, context, action=action)

        async def forbidden(_):
            raise AssertionError("unsupported action evaluated")

        sm._rules._evaluate_dnd = forbidden
        before = authority(sm, sid)
        query = sm.query_action_availability(sid, NPC, command.payload, package, context=context)
        assert query.status == "unknown"
        assert authority(sm, sid) == before
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "unsupported_stat_block_action")
        assert sm.submit_command(command, package, context=context) == receipt
        assert {k: v for k, v in authority(sm, sid).items() if k != "command_receipts"} == {
            k: v for k, v in before.items() if k != "command_receipts"
        }


@pytest.mark.parametrize(
    "kind,status",
    [
        ("ordinary", "available"),
        ("target", "unavailable"),
        ("choice", "unknown"),
        ("dash", "unknown"),
    ],
)
def test_availability_status_filtering_and_no_database_writes(
    tmp_path, package_data, context, kind, status
):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        command = attack(sm, sid, package, context)
        payload = command.payload
        if kind == "target":
            payload = payload.model_copy(update={"target_id": "secret.nonexistent"})
        if kind == "choice":
            payload = CombatIntentPayload(intent_type="attack", target_id=NPC)
        if kind == "dash":
            payload = CombatIntentPayload(intent_type="dash")
        before = authority(sm, sid)
        changes = sm._conn.total_changes
        result = sm.query_action_availability(
            sid, command.actor_id, payload, package, context=context
        )
        assert result.status == status
        assert result.snapshot_world_version == command.expected_world_version
        assert sm._conn.total_changes == changes
        assert authority(sm, sid) == before
        assert "secret" not in result.reason.reason
        assert NPC not in result.reason.reason
        assert (
            not {"command_id", "rng_context", "state_delta", "proposed_events", "receipt"}
            & result.model_dump().keys()
        )


def test_available_does_not_authorize_stale_execution_or_revoked_control(
    tmp_path, package_data, context
):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        command = attack(sm, sid, package, context)
        assert (
            sm.query_action_availability(
                sid, command.actor_id, command.payload, package, context=context
            ).status
            == "available"
        )
        other = TypedCommand(
            sid,
            "world.change",
            command.actor_id,
            "set_scene_flag",
            SetSceneFlag("scene.platform", "flag.lamp_checked", True),
            command.expected_world_version,
        )
        assert sm.submit_command(other, package, context=context).status == "committed"
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("conflict", "version_conflict")
        command = replace(
            command, command_id="pc.current", expected_world_version=receipt.world_version
        )
        assert sm.submit_command(command, package, context=context).status == "committed"
        npc = npc_command(sm, sid, package, context)
        assert (
            sm.query_action_availability(sid, NPC, npc.payload, package, context=context).status
            == "available"
        )
        revoked = replace(context, controlled_npc_ids=())
        with pytest.raises(StateMachineError):
            sm.submit_command(npc, package, context=revoked)
        assert sm.get_command_receipt(sid, npc.command_id, package, context=context) is None


def test_query_revalidates_source_and_binding(tmp_path, package_data, context):
    from dnd5e_srd_data import MemoryAssetLoader

    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        command = attack(sm, sid, package, context)
        before = authority(sm, sid)
        with pytest.raises(StateMachineError) as caught:
            sm.query_action_availability(
                sid,
                command.actor_id,
                command.payload.model_copy(update={"source_id": NPC}),
                package,
                context=context,
            )
        assert caught.value.code == "rule_schema_error"
        sm._rules.loader = MemoryAssetLoader()
        with pytest.raises(StateMachineError) as caught:
            sm.query_action_availability(
                sid, command.actor_id, command.payload, package, context=context
            )
        assert caught.value.code == "ruleset_binding_mismatch"
        assert authority(sm, sid) == before


def test_known_multiattack_is_not_dispatched_as_a_single_attack(
    tmp_path, package_data, context, monkeypatch
):
    import test_dnd_runtime
    from dnd5e_srd_data import MemoryAssetLoader
    from dnd_support import loader

    assets = loader()
    monster = assets.get_monster("bandit")
    # Original variant with a known reserved ID and otherwise typed ordinary activity.
    # Upstream must reject that ID instead of entering its prose-based legacy planner.
    monster = monster.model_copy(
        update={
            "actions": [
                *monster.actions,
                monster.actions[0].model_copy(update={"slug": "multiattack"}),
            ]
        }
    )
    modified = MemoryAssetLoader(items=[assets.get_weapon(WEAPON)], monsters=[monster])
    monkeypatch.setattr(test_dnd_runtime, "loader", lambda: modified)
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    with sm:
        sm.submit_command(attack(sm, sid, package, context), package, context=context)
        command = npc_command(sm, sid, package, context, action="multiattack")
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        assert (
            sm.query_action_availability(sid, NPC, command.payload, package, context=context).status
            == "unknown"
        )
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "action.capability")
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
