"""World object capability orchestration within the existing command transaction."""

from dataclasses import asdict

from .errors import StateMachineError


class WorldObjectService:
    def __init__(self, handler):
        self.handler = handler
        self.facts = handler.facts

    def _visible(self, sid, actor_id, oid, package):
        h = self.handler
        position = h._actor_position(sid, actor_id)
        h._public_scope(sid, actor_id, package)
        obj = self.facts.object(sid, oid)
        if obj is None:
            raise StateMachineError("invalid_target", "No authorized local object")
        if obj["carrier_id"] == actor_id:
            return obj
        targets = [e.threshold_cell for _, e in h._door_ports(position.scene_id, oid, package)]
        if obj["scene_id"] == position.scene_id:
            targets.append(obj["cell"])
        if not any(
            cell is not None
            and h._base_topology(sid, position.scene_id).can_see(position.cell, cell)
            for cell in targets
        ):
            raise StateMachineError("invalid_target", "No authorized local object")
        return obj

    def view(self, sid, actor_id, oid, package):
        obj = self._visible(sid, actor_id, oid, package)
        definition = self.facts.object_definition(package, oid)
        config = (
            asdict(definition)
            if definition
            else {
                "object_id": oid,
                "definition_ref": oid,
                "display_name": oid,
                "description_ref": None,
                "material": None,
                "size_category": None,
                "weight_lb": None,
            }
        )
        config.pop("capabilities", None)
        declared = (
            tuple(c.kind for c in definition.capabilities)
            if definition
            else (("openable",) if obj["state"] is not None else ())
        )
        return {
            **config,
            "package_revision": package.package_digest,
            "capabilities": declared,
            "effective_capabilities": declared,
            "open_state": obj["state"],
            "location": self.facts.object_position(sid, oid),
        }

    def get_object(self, sid, actor_id, oid, package, *, context):
        h = self.handler
        with h._transaction():
            h._spatial_reader(sid, actor_id, package, context)
            return self.view(sid, actor_id, oid, package)

    def list_objects(self, sid, actor_id, package, *, context):
        h = self.handler
        with h._transaction():
            h._spatial_reader(sid, actor_id, package, context)
            result = []
            for obj in h._conn.execute(
                "SELECT object_id FROM objects WHERE session_id=? ORDER BY object_id", (sid,)
            ):
                try:
                    result.append(self.view(sid, actor_id, obj[0], package))
                except StateMachineError as exc:
                    if exc.code != "invalid_target":
                        raise
            return tuple(result)
