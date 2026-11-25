"""Run one bounded enemy turn from an adopted NPC combat goal.

The goal is a narrow machine value, not interpreted prose. The Rule Engine
remains responsible for action legality, costs, dice, and turn handoff.
"""

import json
from dataclasses import dataclass
from hashlib import sha256

from dnd5e_engine.evaluation_contracts import CombatIntentPayload

from .commands import TypedCommand
from .errors import StateMachineError
from .models import TrustedContext


@dataclass(frozen=True, slots=True)
class EnemyTurnResult:
    """Report bounded policy progress and the public command receipts it obtained."""

    status: str
    receipts: tuple
    turn_serial: int | None
    npc_state_version: int | None


class NPCCombatPolicyRuntime:
    """Project one adopted goal and submit at most move, attack, and Pass."""

    def _enemy_policy(self, session_id, npc_id, package, context):
        """Read only the controlled NPC's current policy goal and source version."""
        with self._transaction():
            self._session(session_id, package, context)
            if type(context) is not TrustedContext or npc_id not in context.controlled_npc_ids:
                raise StateMachineError("unauthorized", "Enemy policy requires NPC control")
            grant = self._conn.execute(
                "SELECT principal_id FROM npc_access WHERE session_id=? AND observer_id=?",
                (session_id, npc_id),
            ).fetchone()
            if grant is None:
                raise StateMachineError("unauthorized", "Enemy policy grant is absent")
            state = self._conn.execute(
                "SELECT npc_state_version FROM npc_states WHERE session_id=? AND npc_id=?",
                (session_id, npc_id),
            ).fetchone()
            goal = self._conn.execute(
                "SELECT payload_json FROM npc_private_records WHERE session_id=? AND npc_id=? "
                "AND record_kind='goal' AND record_id='goal.combat.policy' AND is_current=1",
                (session_id, npc_id),
            ).fetchone()
            if state is None or goal is None:
                return None
            payload = json.loads(goal[0])
            if payload.get("status") != "active" or payload.get("text") not in ("engage", "hold"):
                return None
            return payload["text"], state[0]

    def run_enemy_turn(
        self,
        session_id,
        npc_id,
        package,
        *,
        context,
        attack_id,
        expected_combat_id,
        expected_turn_serial,
    ):
        """Attempt one controlled enemy Turn from its committed goal.

        The caller fixes the combat and Turn identity before invoking or retrying.
        Durable stage receipts take priority over current state, so a lost reply
        after Pass still returns the original result. A refused active action
        skips further tactics and tries Pass only on the same live Turn.
        """
        if type(attack_id) is not str or not attack_id:
            raise StateMachineError("invalid_request", "An attack source is required")
        if (
            type(expected_combat_id) is not str
            or not expected_combat_id
            or type(expected_turn_serial) is not int
            or expected_turn_serial < 1
        ):
            raise StateMachineError("invalid_request", "Fixed combat and Turn identity required")
        policy = self._enemy_policy(session_id, npc_id, package, context)
        stage_prefix = f"npc-policy:{expected_combat_id}:{expected_turn_serial}:"
        with self._transaction():
            keys = tuple(
                row[0]
                for row in self._conn.execute(
                    "SELECT command_id FROM command_receipts WHERE session_id=? "
                    "AND command_id>=? AND command_id<? ORDER BY command_id",
                    (session_id, stage_prefix, stage_prefix + "\U0010ffff"),
                )
            )
        prefixes = {key.rsplit(":", 1)[0] for key in keys}
        if len(prefixes) > 1:
            raise StateMachineError("policy_changed", "Multiple policies used one enemy Turn")
        if prefixes:
            prefix = prefixes.pop()
            try:
                meta_version = int(prefix.rsplit(":", 3)[-3])
                mode, attack_hash = prefix.rsplit(":", 2)[-2:]
            except ValueError as exc:
                raise StateMachineError("invalid_command", "Invalid policy receipt key") from exc
            if (
                mode not in ("engage", "hold")
                or attack_hash != sha256(attack_id.encode("utf-8")).hexdigest()[:16]
            ):
                raise StateMachineError("invalid_command", "Invalid policy receipt mode")
        elif policy is not None:
            mode, meta_version = policy
            attack_hash = sha256(attack_id.encode("utf-8")).hexdigest()[:16]
            prefix = f"{stage_prefix}{meta_version}:{mode}:{attack_hash}"
        else:
            return EnemyTurnResult("no_policy", (), expected_turn_serial, None)
        prior = {
            step: self.get_command_receipt(session_id, f"{prefix}:{step}", package, context=context)
            for step in ("move", "attack", "pass")
        }
        receipts = [prior[step] for step in ("move", "attack", "pass") if prior[step] is not None]
        if any(receipt.actor_id != npc_id for receipt in receipts):
            raise StateMachineError("unauthorized", "Policy receipt belongs to another NPC")
        if prior["pass"] is not None:
            return EnemyTurnResult(
                "completed"
                if prior["pass"].status == "committed"
                else "waiting"
                if prior["pass"].status == "pending_choice"
                else "blocked",
                tuple(receipts),
                expected_turn_serial,
                meta_version,
            )
        snapshot = self.read_combat_snapshot(session_id, package, context=context)
        combat = snapshot.combat_state
        serial = expected_turn_serial
        if (
            combat.ended
            or combat.combat_id != expected_combat_id
            or combat.turn_serial != serial
            or len(combat.party_ids) != 1
            or combat.encounter_ids != {npc_id}
            or combat.initiative_ids[combat.current_turn_index] != npc_id
        ):
            return EnemyTurnResult("not_enemy_turn", tuple(receipts), serial, meta_version)
        pc_id = next(iter(combat.party_ids))
        if len(prefix) > 230:
            raise StateMachineError("invalid_request", "Combat identity exceeds policy key bound")

        for step in ("move", "attack", "pass"):
            if prior[step] is not None:
                if prior[step].status != "committed":
                    if prior[step].status == "pending_choice":
                        return EnemyTurnResult("waiting", tuple(receipts), serial, meta_version)
                    if step == "pass":
                        return EnemyTurnResult("blocked", tuple(receipts), serial, meta_version)
                    mode = "hold"
                continue
            snapshot = self.read_combat_snapshot(session_id, package, context=context)
            combat = snapshot.combat_state
            if (
                combat.ended
                or combat.combat_id != expected_combat_id
                or combat.turn_serial != serial
                or combat.initiative_ids[combat.current_turn_index] != npc_id
            ):
                return EnemyTurnResult("turn_changed", tuple(receipts), serial, meta_version)
            key = f"{prefix}:{step}"
            if self._enemy_policy(session_id, npc_id, package, context) != (
                prefix.rsplit(":", 2)[-2],
                meta_version,
            ):
                return EnemyTurnResult("policy_changed", tuple(receipts), serial, meta_version)
            if step != "pass" and mode != "engage":
                continue
            if step != "pass":
                with self._transaction():
                    visible = self._sessions._npc_combat_target_visible(
                        session_id, npc_id, pc_id, snapshot
                    )
                if not visible:
                    continue
                here = combat.actor_zone[npc_id]
                target = combat.actor_zone[pc_id]
                x, y = (int(value) for value in here.split(","))
                tx, ty = (int(value) for value in target.split(","))
                distance = max(abs(tx - x), abs(ty - y))
            if step == "move":
                if mode != "engage" or distance <= 1:
                    continue
                # The evaluator validates this candidate against terrain, budget,
                # occupancy, encounter region, and reactions.
                x += (tx > x) - (tx < x)
                y += (ty > y) - (ty < y)
                payload = CombatIntentPayload(
                    intent_type="move", source_id=npc_id, target_zone_id=f"{x},{y}"
                )
            elif step == "attack":
                if mode != "engage" or distance > 1:
                    continue
                source = (
                    {"stat_block_action_id": attack_id}
                    if npc_id in combat.monster_slug_by_entity
                    else {"weapon_id": attack_id}
                )
                payload = CombatIntentPayload(
                    intent_type="attack", source_id=npc_id, target_id=pc_id, **source
                )
            else:
                payload = CombatIntentPayload(intent_type="pass", source_id=npc_id)
            receipt = self.submit_command(
                TypedCommand(
                    session_id, key, npc_id, "combat.intent", payload, snapshot.world_version
                ),
                package,
                context=context,
            )
            receipts.append(receipt)
            if receipt.status != "committed":
                if receipt.status == "pending_choice":
                    return EnemyTurnResult("waiting", tuple(receipts), serial, meta_version)
                if step == "pass":
                    return EnemyTurnResult("blocked", tuple(receipts), serial, meta_version)
                # A rejected candidate did not advance the Turn. Only the Pass
                # branch may follow, and only after a fresh snapshot check.
                mode = "hold"
            if step == "pass":
                return EnemyTurnResult("completed", tuple(receipts), serial, meta_version)
        return EnemyTurnResult("blocked", tuple(receipts), serial, meta_version)
