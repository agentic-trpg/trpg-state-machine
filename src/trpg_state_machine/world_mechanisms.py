"""Bounded mechanism lifecycle and ordered capability orchestration.

Definitions are immutable Package data; WorldFacts owns runtime state. Automatic
hooks consume admitted mechanical events in the caller's savepoint, never NPC OB.
"""

import hashlib
import json
from dataclasses import asdict, replace

from ._execution import _WorldEffectKind, _WorldEffectOrigin
from ._operations import ObjectState, SetObjectState
from .errors import StateMachineError
from .mechanism_effects import MechanismEffectRunner
from .runtime_instances import MechanismLinks, TriggerIndex, instance_read, world_read
from .world_commands import CheckMechanism, SetLightEnabled


class MechanismService:
    """Coordinate indexed mechanism triggers, lifecycle, service checks and bounded chains."""

    def __init__(self, handler):
        """Bind mechanism handling and the rules effect port."""
        self.h = handler
        self._invocation = None
        self.effects = MechanismEffectRunner(handler)

    def config(self, sid, oid):
        """Read the object's instance mechanism capability."""
        d = self.h.facts.object_definition(sid, oid)
        return d.capability("mechanism") if d else None

    @instance_read("object", "mechanism", "configuration")
    def trigger_config(self, sid, oid):
        """Static instance trigger/link fields do not depend on activation status."""
        from .instance_data import decode

        encoded = self.h.facts.instances.get(sid, "object", oid)["definition"]
        data = self.h.store.memory.json(encoded)
        if data:
            for capability in data["fields"]["capabilities"]["tuple"]:
                if capability["type"] == "Mechanism":
                    fields = capability["fields"]
                    return MechanismLinks(
                        decode(fields.get("triggers", {"tuple": []})),
                        decode(fields.get("components", {"tuple": []})),
                    )
        return None

    @instance_read("scene", "mechanisms", "index", maintained="mechanism_index")
    def _scene_index(self, sid, scene):
        """Build a read-only event/cell/object index from scene capability members."""
        index = {}
        for oid in self.h.facts.scene_capability_ids(sid, scene, "object_mechanisms"):
            config = self.trigger_config(sid, oid)
            if config is None:
                raise StateMachineError("incomplete_instance_config", "Missing mechanism config")
            for number, trigger in enumerate(config.triggers):
                if trigger.scene_id == scene:
                    targets = (
                        tuple(dict.fromkeys(trigger.cells))
                        if trigger.kind == "enter_region"
                        else (trigger.target_object_id,)
                    )
                    kind = (
                        trigger.event_type if trigger.kind == "object_operation" else trigger.kind
                    )
                    for target in targets:
                        index.setdefault((kind, scene, target), []).append((oid, number, trigger))
        return TriggerIndex({key: tuple(sorted(entries)) for key, entries in index.items()})

    def qualify_link(self, sid, origin, target, kind, value, package):
        """Validate internal linkage origin and target parameters."""
        if (
            type(origin) is not _WorldEffectOrigin
            or origin.kind is not _WorldEffectKind.MECHANISM
            or self._invocation != (sid, origin, target, kind, value)
        ):
            raise StateMachineError("invalid_execution_origin", "No admitted mechanism consequence")

    def _preflight(self, sid, action, package, candidates):
        """Preflight one linked target's capability, position and candidate opening state."""
        h = self.h
        obj = h.facts.object(sid, action.target_object_id)
        d = h.facts.object_definition(sid, action.target_object_id)
        if obj is None or d is None or h.facts.object_hp(sid, action.target_object_id) == 0:
            raise StateMachineError("mechanism_target_unavailable", "Mechanism target unavailable")
        if action.kind == "set_open_state":
            if d.capability("openable") is None:
                raise StateMachineError("unsupported_object_capability", "Target is not openable")
            location = h.facts.placement(sid, action.target_object_id)
            if obj["carrier_id"] is not None or obj["container_id"] is not None:
                raise StateMachineError(
                    "unsupported_mechanism_target", "Linked opening needs ground pose"
                )
            h.objects.openable.validate_transition(
                sid,
                SetObjectState(
                    location["scene_id"], action.target_object_id, ObjectState(action.value)
                ),
                package,
                candidates=candidates,
            )
            before = candidates.get(action.target_object_id) or h.objects.spatial.state(
                sid, action.target_object_id, package
            )
            candidates[action.target_object_id] = replace(before, open_state=action.value)
        elif action.kind == "set_light_enabled":
            if (
                d.capability("light") is None
                or h.facts.light_state(sid, action.target_object_id) is None
            ):
                raise StateMachineError("unsupported_object_capability", "Target is not an emitter")
        else:
            raise StateMachineError("unsupported_operation", "Unknown linked result")

    def preflight(self, sid, actions, package):
        """Preflight the action batch, rejecting an unavailable rules port before fact
        writes.
        """
        candidates, lights, changed = {}, {}, False
        for action in actions:
            if action.kind == "rule_effect":
                # Production refusal occurs before any lifecycle/world write.
                if not self.effects.available():
                    raise StateMachineError(
                        "unsupported_mechanism_effect",
                        "Pinned RE has no mechanism effect capability",
                    )
                changed = True
                continue
            before = (
                (
                    candidates.get(action.target_object_id)
                    or self.h.objects.spatial.state(sid, action.target_object_id, package)
                ).open_state
                if action.kind == "set_open_state"
                else lights.get(
                    action.target_object_id,
                    (self.h.facts.light_state(sid, action.target_object_id) or {}).get("enabled"),
                )
            )
            self._preflight(sid, action, package, candidates)
            changed |= before != action.value
            if action.kind == "set_light_enabled":
                lights[action.target_object_id] = action.value
        return changed

    def _state(self, sid, oid, config, package):
        """Read mechanism lifecycle and verify component integrity."""
        state = self.h.facts.mechanism_state(sid, oid)
        if state is None:
            raise StateMachineError("invalid_target", "Mechanism not initialized")
        if any(self.h.facts.object(sid, component) is None for component in config.components):
            raise StateMachineError("mechanism_target_unavailable", "Component not initialized")
        if self.h.facts.object_hp(sid, oid) == 0 or any(
            self.h.facts.object_hp(sid, component) == 0 for component in config.components
        ):
            raise StateMachineError("mechanism_broken", "Destroyed mechanism component")
        return state

    def _pose(self, sid, oid, config):
        """Validate mechanism placement and container access."""
        obj = self.h.facts.object(sid, oid)
        if config.placement_policy == "ground_prepared" and (
            obj["scene_id"] is None
            or obj["cell"] is None
            or obj["carrier_id"] is not None
            or obj["container_id"] is not None
        ):
            raise StateMachineError("mechanism_unprepared", "Mechanism requires a ground pose")
        if not self.h.facts.item_accessible(sid, oid, include_self=False):
            raise StateMachineError("container_inaccessible", "Mechanism is enclosed")

    def _eligible(self, sid, oid, config, actor, package):
        """Check arming, placement, frequency and cooldown activation prerequisites."""
        state = self._state(sid, oid, config, package)
        if state.status != "armed":
            code = {
                "spent": "mechanism_spent",
                "blocked": "mechanism_blocked",
                "disarmed": "mechanism_disarmed",
                "unprepared": "mechanism_unprepared",
                "broken": "mechanism_broken",
            }[state.status]
            raise StateMachineError(code, "Mechanism cannot be activated")
        if state.remaining_uses == 0:
            raise StateMachineError("mechanism_exhausted", "No remaining uses")
        self._pose(sid, oid, config)
        if config.frequency == "once_per_actor":
            if actor is None:
                raise StateMachineError("unsupported_mechanism_source", "Actor identity required")
            if self.h._conn.execute(
                "SELECT 1 FROM mechanism_activations "
                "WHERE session_id=? AND object_id=? AND actor_id=?",
                (sid, oid, actor),
            ).fetchone():
                raise StateMachineError(
                    "mechanism_frequency_limited", "Actor already activated instance"
                )
        if config.reset_policy == "after_seconds":
            self.h.time.seconds_deadline(sid, config.reset_delay_seconds)
        return state

    def _manual(self, row, actor, op, package):
        """Validate actor, placement and target for a manual mechanism operation."""
        h, sid = self.h, row["session_id"]
        h._world_operation_capability(row, op)
        h._world_actor_ready(sid, actor)
        position = h._public_scope(sid, actor, package)
        if (position.scene_id, position.cell) != (op.scene_id, op.expected_cell):
            raise StateMachineError("position_conflict", "Actor position changed")
        h.objects.containers.reachable(sid, actor, op.object_id, package)
        c = self.config(row["session_id"], op.object_id)
        if c is None:
            raise StateMachineError("unsupported_object_capability", "Object is not a mechanism")
        return c

    def validate(self, row, actor, op, package):
        """Validate manual activation eligibility and preflight all actions."""
        c = self._manual(row, actor, op, package)
        self._eligible(row["session_id"], op.object_id, c, actor, package)
        operation = c.operation(op.operation_id)
        self.preflight(row["session_id"], operation.actions, package)
        return c

    @staticmethod
    def activation_id(oid, source_command_id, source_event_seq, operation_id):
        """Derive a stable activation key from source command, event and operation
        identity.
        """
        raw = json.dumps(
            [oid, source_command_id, source_event_seq, operation_id], separators=(",", ":")
        )
        return "activation." + hashlib.sha256(raw.encode()).hexdigest()

    def _activate(self, row, oid, actor, operation_id, package, command_id, source_seq):
        """Apply one admitted, idempotently identified mechanism activation.

        Check activation replay and eligibility, preflight configured actions, then
        update lifecycle and execute supported consequences through the existing world
        interfaces. Return captured ordered events. All writes remain in the caller's
        shared transaction; the activation marker is not an independent commit boundary.
        """
        h, sid = self.h, row["session_id"]
        if type(command_id) is not str or not command_id:
            raise StateMachineError("invalid_execution_origin", "Admitted source command required")
        c = self.config(row["session_id"], oid)
        activation = self.activation_id(oid, command_id, source_seq, operation_id)
        if h._conn.execute(
            "SELECT 1 FROM mechanism_activations WHERE session_id=? AND activation_id=?",
            (sid, activation),
        ).fetchone():
            return []
        state = self._eligible(sid, oid, c, actor, package)
        operation = c.operation(operation_id)
        changed = self.preflight(sid, operation.actions, package)
        if not changed and not operation.consume_on_noop:
            return []
        location = h.facts.placement(sid, oid)
        payload = {
            "scene_id": location["scene_id"],
            "object_id": oid,
            "actor_id": actor,
            "activation_id": activation,
            "operation_id": operation_id,
            "source_event_seq": source_seq,
            "source_command_id": command_id,
        }
        events = [
            h._capture_event(
                sid,
                package,
                "mechanism.activated",
                payload,
                phase="world.before_mechanism_results",
                source_actor_id=actor,
            )
        ]
        for index, action in enumerate(operation.actions):
            origin = _WorldEffectOrigin(
                _WorldEffectKind.MECHANISM, oid, activation, activation + "." + str(index)
            )
            if action.kind == "rule_effect":
                audit = self.effects.apply(
                    row,
                    package,
                    oid,
                    actor,
                    activation,
                    activation + "." + str(index),
                    source_seq,
                    command_id,
                    action,
                )
                payload.setdefault("rule_results", []).append(audit)
                continue
            self._invocation = (sid, origin, action.target_object_id, action.kind, action.value)
            try:
                target = h.facts.placement(sid, action.target_object_id)
                if action.kind == "set_light_enabled":
                    events.extend(
                        h.objects.lights.operate(
                            row,
                            None,
                            SetLightEnabled(
                                target["scene_id"],
                                action.target_object_id,
                                target["cell"],
                                action.value,
                            ),
                            package,
                            origin=origin,
                        )
                    )
                else:
                    events.extend(
                        h.objects.openable.operate_open(
                            row,
                            None,
                            SetObjectState(
                                target["scene_id"],
                                action.target_object_id,
                                ObjectState(action.value),
                            ),
                            package,
                            origin=origin,
                        )
                    )
            finally:
                self._invocation = None
        remaining = state.remaining_uses - 1 if state.remaining_uses is not None else None
        spent = remaining == 0 or c.reset_policy != "none" or c.schema_version == "mechanism/1"
        deadline = (
            h.time.seconds_deadline(sid, c.reset_delay_seconds)
            if remaining != 0 and c.reset_policy == "after_seconds"
            else None
        )
        h.facts.set_mechanism_state(
            sid,
            oid,
            replace(
                state,
                status="spent" if spent else "armed",
                remaining_uses=remaining,
                activation_count=state.activation_count + 1,
                reset_at_tick=deadline,
            ),
        )
        h._conn.execute(
            "INSERT INTO mechanism_activations VALUES (?,?,?,?,?,?,?)",
            (sid, activation, oid, command_id, source_seq, actor, operation_id),
        )
        if c.schema_version == "mechanism/1":
            h._conn.execute(
                "INSERT INTO trigger_history VALUES (?,?,?,?,'linked')",
                (sid, "mechanism." + oid, command_id, row["event_seq"] + 1),
            )
        events.append(
            h._capture_event(
                sid,
                package,
                "mechanism.spent" if spent else "mechanism.ready",
                payload,
                phase="world.after_mechanism_results",
                source_actor_id=actor,
            )
        )
        return events

    def operate(self, row, actor, op, package, source_command_id):
        """Validate and execute manual mechanism activation."""
        self.validate(row, actor, op, package)
        return self._activate(
            row,
            op.object_id,
            actor,
            op.operation_id,
            package,
            source_command_id,
            row["event_seq"] + 1,
        )

    def method_plan(self, row, actor, op, package):
        """Select a disarm/rearm service method and validate its prerequisites."""
        c = self._manual(row, actor, op, package)
        sid = row["session_id"]
        state = self._state(sid, op.object_id, c, package)
        method = next((m for m in c.methods if m.method_id == op.method_id), None)
        if method is None:
            raise StateMachineError("unsupported_mechanism_method", "Undeclared lifecycle method")
        if (type(op) is CheckMechanism) != (method.ability is not None):
            raise StateMachineError(
                "unsupported_mechanism_method", "Method requires its configured execution path"
            )
        allowed = {
            "disarm": ("armed", "spent", "unprepared", "blocked"),
            "block": ("armed", "spent", "unprepared"),
            "unblock": ("blocked",),
            "prepare": ("unprepared",),
            "reset": ("spent",),
        }
        if state.status not in allowed[method.operation]:
            raise StateMachineError(
                "mechanism_state_conflict", "Method not applicable to current state"
            )
        if method.operation in ("prepare", "reset"):
            if state.remaining_uses == 0:
                raise StateMachineError("mechanism_exhausted", "No remaining uses")
            if method.operation == "reset" and c.reset_policy != "manual":
                raise StateMachineError(
                    "unsupported_mechanism_method", "Manual reset not configured"
                )
            self._pose(sid, op.object_id, c)
        if (method.required_tool is None) != (op.tool_instance_id is None):
            raise StateMachineError("tool_unavailable", "Explicit tool must match method")
        if method.required_tool is not None:
            obj = self.h.facts.object(sid, op.tool_instance_id)
            location = self.h.facts.placement(sid, op.tool_instance_id) if obj else None
            data = json.loads(obj["item_json"]) if obj and obj["item_json"] else {}
            if (
                not location
                or location["carrier_id"] != actor
                or not self.h.facts.item_accessible(sid, op.tool_instance_id)
                or data.get("item_slug") != method.required_tool
                or data.get("quantity") != 1
            ):
                raise StateMachineError("tool_unavailable", "No matching accessible owned tool")
        if method.trigger_on_failure is not None:
            self._eligible(sid, op.object_id, c, actor, package)
            self.preflight(sid, c.operation(method.trigger_on_failure).actions, package)
        return method, state

    def service(self, row, actor, op, package, *, succeeded=True, command_id=None):
        """Adopt a service check result, updating lifecycle or triggering failure actions."""
        method, state = self.method_plan(row, actor, op, package)
        if not succeeded:
            if method.trigger_on_failure:
                return self._activate(
                    row,
                    op.object_id,
                    actor,
                    method.trigger_on_failure,
                    package,
                    command_id,
                    row["event_seq"] + 1,
                )
            return []
        if method.operation == "block":
            after = replace(state, status="blocked", resume_status=state.status)
        elif method.operation == "unblock":
            after = replace(state, status=state.resume_status, resume_status=None)
        else:
            after = replace(
                state,
                status="disarmed" if method.operation == "disarm" else "armed",
                reset_at_tick=None,
                resume_status=None,
            )
        self.h.facts.set_mechanism_state(row["session_id"], op.object_id, after)
        return [
            self.h._capture_event(
                row["session_id"],
                package,
                "mechanism.serviced",
                {
                    **asdict(op),
                    "actor_id": actor,
                    "operation": method.operation,
                    "status": after.status,
                },
                phase="world.after_mechanism_service",
                source_actor_id=actor,
            )
        ]

    def prepare_check(self, row, actor, op, package):
        """Prepare fixed rules check input for a mechanism service method."""
        from dnd5e_engine.evaluation_contracts import CheckPayload
        from pydantic import ValidationError

        method, state = self.method_plan(row, actor, op, package)
        if method.required_tool is not None:
            # Generic checks cannot adjudicate tool proficiency; never infer it from possession.
            raise StateMachineError("unsupported_mechanism_rule", "Tool check needs an RE contract")
        try:
            payload = CheckPayload(
                actor_id=actor,
                ability=method.ability,
                skill=method.skill,
                tool=None,
                dc=method.dc,
                target_id=None,
                context="generic",
                required_sense="none",
                social_interaction=False,
                advantage=(),
                disadvantage=(),
                redeem_granted_die=None,
            )
        except ValidationError as exc:
            raise StateMachineError(
                "unsupported_mechanism_rule", "Check outside RE schema"
            ) from exc
        return payload, method, state

    def check_availability(self, row, actor, op, package):
        """Query actual rules eligibility for mechanism service availability."""
        plan = self.prepare_check(row, actor, op, package)
        from dnd5e_engine.evaluation_checks import check_support_failure

        from .commands import TypedCommand

        rules = self.h.objects.openable.rules
        snapshot = rules._noncombat_snapshot(row, package)
        rules._verify_dnd_binding(package, row)
        if failure := rules._dnd_capability(plan[0], actor, snapshot):
            raise StateMachineError(failure, "Check outside supported rule facts")
        request = rules.request(
            TypedCommand(
                row["session_id"], "query", actor, "rules.check", plan[0], row["world_version"]
            ),
            package,
            snapshot,
            rules._rng_context(row),
        )
        if failure := check_support_failure(request, rules.loader):
            raise StateMachineError(failure["error"].code, "Check outside supported rule facts")
        return plan

    def component_destroyed(self, row, package, component, *, prepared=None):
        """Update associated mechanisms through component destruction reverse relations and
        emit events.
        """
        index = self._batch_index(row["session_id"], prepared)
        sid, events = row["session_id"], []
        for oid, _, _ in index.get(("destroyed", None, component), ()):
            state = self.h.facts.mechanism_state(sid, oid)
            if state is not None and state.status != "broken":
                self.h.facts.set_mechanism_state(
                    sid,
                    oid,
                    replace(state, status="broken", reset_at_tick=None, resume_status=None),
                )
                location = self.h.facts.placement(sid, oid)
                events.append(
                    self.h._capture_event(
                        sid,
                        package,
                        "mechanism.broken",
                        {
                            "scene_id": location["scene_id"],
                            "object_id": oid,
                            "component_id": component,
                        },
                        phase="world.after_mechanism_damage",
                    )
                )
        return events

    @world_read("mechanisms", "index")
    def _build_index(self, sid):
        """Cross-scene component reverse links; load only mechanism identities."""
        index = {}
        for (oid,) in self.h._conn.execute(
            "SELECT object_id FROM object_mechanisms WHERE session_id=? ORDER BY object_id", (sid,)
        ):
            c = self.trigger_config(sid, oid)
            if c is None:
                raise StateMachineError("incomplete_instance_config", "Missing mechanism config")
            for number, t in enumerate(c.triggers):
                key = (t.kind, t.scene_id, t.target_object_id)
                index.setdefault(key, []).append((oid, number, t))
            for component in (oid, *c.components):
                index.setdefault(("destroyed", None, component), []).append((oid, 0, None))
        return {
            key: tuple(sorted(value, key=lambda v: (v[0], v[1]))) for key, value in index.items()
        }

    def _batch_index(self, sid, prepared):
        """Share the prepared Session mechanism index within one event batch."""
        if prepared is None:
            return self._build_index(sid)
        if sid not in prepared:
            prepared[sid] = self._build_index(sid)
        return prepared[sid]

    def automatic(self, row, package, event, command_id, source_seq, *, prepared=None):
        """Resolve automatic mechanism consequences for one source event.

        Select candidates through the existing scene mechanism index, then recheck
        placement, lifecycle, frequency, cooldown and other eligibility. The local
        fired set permits at most one matching operation per mechanism for this event;
        a different source event may trigger it again if still eligible.

        This path uses _scene_index directly; the accepted prepared parameter does
        not provide a batch index or cross-event activation deduplication here. Index
        reuse and activation deduplication have separate scopes. Preserve command and
        source-event identities when activating, and return ordered consequences for
        the caller's bounded event expansion within its shared transaction.
        """
        sid, payload, kind = row["session_id"], event.payload, event.event_type
        if kind not in {
            "actor.stepped",
            "actor.moved",
            "scene.entered",
            "rules.check_rolled",
            "object.state_changed",
            "object.light_changed",
            "door.lock_changed",
            "object.bar_changed",
            "object.jam_cleared",
            "item.picked_up",
            "item.dropped",
            "item.container_transferred",
        }:
            return []
        if self.h._active_combat(sid):
            return []  # Combat mechanical hooks/Episode routing require a separate contract.
        if kind == "object.light_changed" and event.mechanical_phase != "world.after_light_update":
            return []
        derived = []
        if kind == "item.picked_up" or (
            kind == "item.container_transferred" and payload.get("phase") == "after"
        ):
            oid = payload["instance_id"]
            c = self.config(row["session_id"], oid)
            state = self.h.facts.mechanism_state(sid, oid)
            if (
                c
                and c.placement_policy == "ground_prepared"
                and (
                    state.status == "armed"
                    or state.status == "blocked"
                    and state.resume_status == "armed"
                )
            ):
                after = (
                    replace(state, status="unprepared")
                    if state.status == "armed"
                    else replace(state, resume_status="unprepared")
                )
                self.h.facts.set_mechanism_state(sid, oid, after)
                derived.append(
                    self.h._capture_event(
                        sid,
                        package,
                        "mechanism.unprepared",
                        {
                            "scene_id": payload["scene_id"],
                            "object_id": oid,
                            "source_event_seq": source_seq,
                        },
                        phase="world.after_mechanism_stow",
                    )
                )
        # Durability applies component destruction synchronously before returning
        # its event batch; dispatching that event must not repeat the same work.
        if (
            kind in ("actor.stepped", "actor.moved", "scene.entered")
            and event.mechanical_motion is None
        ):
            return derived
        if kind == "item.container_transferred" and payload.get("phase") != "after":
            return []
        if kind == "rules.check_rolled":
            adjudication = payload.get("adjudication", {})
            if not (
                adjudication.get("openable_operation") or adjudication.get("mechanism_operation")
            ):
                return []
        scene = payload.get("scene_id", payload.get("to_scene_id"))
        if event.mechanical_motion is not None:
            scene = event.mechanical_motion.to_scene_id
        if scene is None:
            return derived
        index = self._scene_index(sid, scene)
        actor = payload.get("actor_id", payload.get("execution", {}).get("actor_id"))
        if actor is None and payload.get("execution", {}).get("activation_id"):
            adopted = self.h._conn.execute(
                "SELECT actor_id FROM mechanism_activations WHERE session_id=? AND activation_id=?",
                (sid, payload["execution"]["activation_id"]),
            ).fetchone()
            actor = adopted[0] if adopted else None
        if kind in ("actor.stepped", "actor.moved", "scene.entered"):
            motion = event.mechanical_motion
            if motion is None:
                return derived
            scene, actor = motion.to_scene_id, motion.actor_id
            candidates = index.get(("enter_region", scene, motion.to_cell), ())
            target_kind = "enter_region"
        elif kind == "rules.check_rolled":
            adjudication = payload.get("adjudication", {})
            admitted = adjudication.get("openable_operation") or adjudication.get(
                "mechanism_operation"
            )
            # Generic checks do not have a mechanically bound object target.
            if admitted is None:
                return []
            actor = adjudication["payload"]["actor_id"]
            candidates = index.get(("check_result", scene, admitted["payload"]["object_id"]), ())
            target_kind = "check_result"
        else:
            if kind == "item.container_transferred" and payload.get("phase") != "after":
                return []
            target = payload.get("object_id", payload.get("instance_id"))
            candidates = index.get((kind, scene, target), ())
            target_kind = "object_operation"
        events, fired = derived, set()
        for oid, _, trigger in candidates:
            if oid in fired:
                continue  # One matching operation per instance and mechanical source event.
            if target_kind == "enter_region":
                if motion.to_cell not in trigger.cells or (
                    motion.from_scene_id == scene and motion.from_cell in trigger.cells
                ):
                    continue
            elif target_kind == "check_result":
                if payload["engine_event"]["succeeded"] != trigger.succeeded:
                    continue
            elif (
                kind != trigger.event_type
                or trigger.value is not None
                and payload.get(
                    {
                        "object.state_changed": "state",
                        "object.light_changed": "enabled",
                        "door.lock_changed": "locked",
                        "object.bar_changed": "barred",
                    }[kind]
                )
                != trigger.value
            ):
                continue
            c = self.config(row["session_id"], oid)
            state = self.h.facts.mechanism_state(sid, oid)
            if state is None or state.status != "armed":
                continue
            location = self.h.facts.placement(sid, oid)
            if location["scene_id"] != scene:
                continue
            try:
                self._eligible(sid, oid, c, actor, package)
            except StateMachineError as exc:
                if exc.code in (
                    "mechanism_frequency_limited",
                    "mechanism_unprepared",
                    "mechanism_broken",
                    "container_inaccessible",
                ):
                    continue
                raise
            events.extend(
                self._activate(
                    row, oid, actor, trigger.operation_id, package, command_id, source_seq
                )
            )
            fired.add(oid)
        return events

    def next_boundary(self, session_id, package):
        """Read the earliest mechanism reset boundary."""
        return self.h._conn.execute(
            "SELECT min(reset_at_tick) FROM object_mechanisms WHERE session_id=?",
            (session_id,),
        ).fetchone()[0]

    def settle_one(self, row, package, command_id):
        """Settle currently due mechanisms and return the domain time result."""
        from .time_coordination import DueResult

        return DueResult(tuple(self._reset_due(row, package)))

    def _reset_due(self, row, package):
        """Reset due mechanism state against the sole world clock and record events."""
        sid, events = row["session_id"], []
        tick = self.h.facts.time_state(sid)[1]
        due = self.h._conn.execute(
            "SELECT object_id FROM object_mechanisms WHERE session_id=? AND reset_at_tick<=? "
            "ORDER BY reset_at_tick,object_id LIMIT 1",
            (sid, tick),
        ).fetchall()
        for (oid,) in due:
            state = self.h.facts.mechanism_state(sid, oid)
            c = self.config(row["session_id"], oid)
            self._state(sid, oid, c, package)
            obj = self.h.facts.object(sid, oid)
            ready = (
                "unprepared"
                if c.placement_policy == "ground_prepared"
                and (obj["carrier_id"] is not None or obj["container_id"] is not None)
                else "armed"
            )
            after = replace(
                state,
                reset_at_tick=None,
                status="blocked" if state.status == "blocked" else ready,
                resume_status=ready if state.status == "blocked" else None,
            )
            self.h.facts.set_mechanism_state(sid, oid, after)
            location = self.h.facts.placement(sid, oid)
            events.append(
                self.h._capture_event(
                    sid,
                    package,
                    "mechanism.reset",
                    {
                        "scene_id": location["scene_id"],
                        "object_id": oid,
                        "boundary_tick": state.reset_at_tick,
                    },
                    phase="world.after_mechanism_reset",
                )
            )
        return events
