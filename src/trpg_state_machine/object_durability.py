"""Private settlement application only; no player damage or repair command.

This is invoked inside an admitted caller's shared commit scope. It cannot admit
rule results itself and must not be exposed through an RPC/command adapter.
"""

from dataclasses import asdict, dataclass, replace

from ._execution import _execution_data, _WorldEffectOrigin
from .errors import StateMachineError
from .package import canonical_json


@dataclass(frozen=True, slots=True)
class _ObjectHPSettlement:
    session_id: str
    object_id: str
    resolution_id: str
    expected_world_version: int
    expected_hp: int
    current_hp: int


class ObjectDurabilityService:
    def __init__(self, handler):
        self.handler = handler

    def _apply_admitted(self, row, package, result, *, origin):
        h = self.handler
        h.store.require_write()
        if type(result) is not _ObjectHPSettlement or type(origin) is not _WorldEffectOrigin:
            raise StateMachineError("invalid_rule_result", "Expected an internally admitted result")
        if (
            any(
                type(value) is not str or not 0 < len(value) <= 256
                for value in (result.session_id, result.object_id, result.resolution_id)
            )
            or type(result.expected_world_version) is not int
            or result.expected_world_version < 0
        ):
            raise StateMachineError("invalid_rule_result", "Invalid settlement identities/version")
        execution = _execution_data(None, origin)
        if result.session_id != row["session_id"] or not result.resolution_id:
            raise StateMachineError("invalid_rule_result", "Result session/source mismatch")
        encoded = canonical_json({"result": asdict(result), "origin": execution})
        old = h._conn.execute(
            "SELECT fingerprint FROM world_result_adoptions WHERE session_id=? AND resolution_id=?",
            (result.session_id, result.resolution_id),
        ).fetchone()
        if old:
            if old[0] != encoded:
                raise StateMachineError("idempotency_conflict", "Settlement identity reused")
            return []
        actual = h._conn.execute(
            "SELECT world_version FROM sessions WHERE session_id=?", (result.session_id,)
        ).fetchone()[0]
        if actual != result.expected_world_version:
            raise StateMachineError("version_conflict", "Settlement snapshot is stale")
        d = h.facts.object_definition(package, result.object_id)
        capability = d.capability("durability") if d else None
        hp = h.facts.object_hp(result.session_id, result.object_id)
        if capability is None or hp is None:
            raise StateMachineError("unsupported_object_capability", "No durability capability")
        if hp != result.expected_hp:
            raise StateMachineError("version_conflict", "Settlement HP is stale")
        if any(type(v) is not int for v in (result.expected_hp, result.current_hp)) or not (
            0 <= result.current_hp <= hp <= capability.max_hp
        ):
            raise StateMachineError("invalid_rule_result", "Invalid damage settlement bounds")
        if h.objects.spatial.config(package, result.object_id):
            h.objects.spatial.check_candidate(
                result.session_id,
                result.object_id,
                package,
                replace(
                    h.objects.spatial.state(result.session_id, result.object_id, package),
                    destroyed=result.current_hp == 0,
                ),
                error_code="invalid_spatial_state",
            )
        h.facts.apply_object_hp(result.session_id, result.object_id, hp, result.current_hp)
        h._conn.execute(
            "INSERT INTO world_result_adoptions VALUES (?,?,?)",
            (result.session_id, result.resolution_id, encoded),
        )
        h.objects.spatial.audit(result.session_id, package)
        if package.portable_items:
            h.characters.sync_world_inventory(result.session_id)
        position = h.facts.object_position(result.session_id, result.object_id)
        return [
            h._capture_event(
                result.session_id,
                package,
                "object.durability_changed",
                {**asdict(result), "scene_id": position["scene_id"], "execution": execution},
                phase="world.after_durability_update",
            )
        ]
