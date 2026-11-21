from dataclasses import asdict, replace

import pytest
from lock_support import CRED, A, lock_loader
from test_object_spatial_service import changed_grid
from test_world_containers import BOX
from test_world_lights import evidence, light_config, lighting_data
from world_support import command, setup, submit

from trpg_state_machine import AimLight, PickupItem, PutItem, StateMachine, StateMachineError
from trpg_state_machine.commands import world_command_from_data
from trpg_state_machine.package import _validate

# D&D Beyond 2024 Free Rules equipment profiles, declared independently of the item identity.
# https://www.dndbeyond.com/sources/dnd/br-2024/equipment#AdventuringGear
GRID_RADIUS = "grid_radius_static_los"
GRID_CONE = "grid_cone_static_los"
GEAR_PROFILES = [
    ("torch", GRID_RADIUS, 3600, [("normal", 20, 40)]),
    ("lamp", GRID_RADIUS, 21600, [("normal", 15, 45)]),
    ("hooded lantern", GRID_RADIUS, 21600, [("normal", 30, 60), ("hooded", None, 5)]),
    ("bullseye lantern", GRID_CONE, 21600, [("normal", 60, 120)]),
]


def configure_light(data, **fields):
    definition = next(x for x in data["world_objects"] if x["object_id"] == CRED)
    light = next(c for c in definition["capabilities"] if c["kind"] == "light")
    light.update(fields)


@pytest.mark.parametrize(("_gear", "shape", "fuel_seconds", "modes"), GEAR_PROFILES)
def test_2024_gear_profiles_are_declared_by_capability(_gear, shape, fuel_seconds, modes):
    data = lighting_data()
    configure_light(
        data,
        emission_shape=shape,
        fuel_policy="world_time_metered",
        fuel_seconds=fuel_seconds,
        modes=[
            {"mode_id": mode, "bright_radius_ft": bright, "dim_outer_radius_ft": dim}
            for mode, bright, dim in modes
        ],
    )
    package = _validate(data)
    definition = next(d for d in package.world_objects if d.object_id == CRED)
    emitter = definition.capability("light")
    assert emitter.emission_shape == shape
    assert emitter.fuel_seconds == fuel_seconds
    assert [(m.mode_id, m.bright_radius_ft, m.dim_outer_radius_ft) for m in emitter.modes] == modes


def test_carried_cone_aim_admission_and_persistent_direction(tmp_path, context):
    data = lighting_data()
    configure_light(data, emission_shape=GRID_CONE)
    changed_grid(data, default_lighting="bright")
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        aim = AimLight(A, CRED, "0,0", "north", "east")
        original = command(sm, sid, package, context, aim, "turn")
        encoded = asdict(original)
        encoded.pop("check_world_effects")
        envelope = world_command_from_data(encoded)
        assert envelope.payload == aim
        assert (
            sm.query_world_action(sid, actor, aim, package, context=context).status == "available"
        )
        assert (
            submit(sm, sid, package, context, replace(aim, expected_cell="1,0"), "away").error_code
            == "position_conflict"
        )
        assert (
            submit(
                sm, sid, package, context, replace(aim, expected_direction="west"), "stale"
            ).error_code
            == "orientation_conflict"
        )
        with pytest.raises(StateMachineError, match="Invalid light direction"):
            sm.query_world_action(
                sid, actor, replace(aim, direction="northeast"), package, context=context
            )
        receipt = sm.submit_command(envelope, package, context=context)
        assert receipt.status == "committed", receipt
        obj = sm.get_object(sid, actor, CRED, package, context=context)
        assert obj["orientation"] == "east"
        assert obj["location"]["carrier_id"] == actor
        assert len(receipt.event_seqs) == 2
        assert all(evidence(sm, sid, seq)["scenes"][A] for seq in receipt.event_seqs)
        assert (
            sm.submit_command(envelope, package, context=context).event_seqs == receipt.event_seqs
        )

        assert submit(sm, sid, package, context, PickupItem(BOX, A, "1,0"), "case").status == (
            "committed"
        )
        assert submit(sm, sid, package, context, PutItem(A, CRED, BOX, "0,0"), "inside").status == (
            "committed"
        )
        assert (
            submit(
                sm, sid, package, context, AimLight(A, CRED, "0,0", "east", "south"), "nested"
            ).error_code
            == "invalid_target"
        )


def test_ground_cone_declares_initial_direction_and_turns_without_spatial_profile(
    tmp_path, context
):
    data = lighting_data()
    data["manifest"]["schema_version"] = "state-machine-foundation/8"
    changed_grid(data, default_lighting="bright")
    ground = "object.directional_light"
    data["scenes"][0]["objects"].append(
        {
            "object_id": ground,
            "state": None,
            "movable": False,
            "blocks_passage": False,
            "initial_cell": "1,0",
            "initial_orientation": "west",
        }
    )
    data["world_objects"].append(
        {
            "object_id": ground,
            "definition_ref": "definition.directional_light",
            "display_name": "Test light",
            "description_ref": None,
            "material": None,
            "size_category": "small",
            "weight_lb": 2,
            "capabilities": [{**light_config(), "emission_shape": GRID_CONE}],
        }
    )
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        assert sm.get_object(sid, actor, ground, package, context=context)["orientation"] == "west"
        assert (
            submit(
                sm, sid, package, context, AimLight(A, CRED, "0,0", "north", "east"), "radial"
            ).error_code
            == "unsupported_object_capability"
        )
        receipt = submit(
            sm, sid, package, context, AimLight(A, ground, "0,0", "west", "south"), "aim"
        )
        assert receipt.status == "committed", receipt
        assert sm.get_object(sid, actor, ground, package, context=context)["orientation"] == "south"


def test_carried_aim_rolls_back_failed_evidence_and_recovers_committed_direction(
    tmp_path, context, monkeypatch
):
    data = lighting_data()
    configure_light(data, emission_shape=GRID_CONE)
    sm, sid, package, db = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        aim = AimLight(A, CRED, "0,0", "north", "east")
        capture = sm._objects.handler._capture_event

        def reject_after(*args, **kwargs):
            result = capture(*args, **kwargs)
            if kwargs["phase"] == "world.after_light_aim":
                raise StateMachineError("test_aim_evidence", "Could not adopt final evidence")
            return result

        with monkeypatch.context() as scoped:
            scoped.setattr(sm._objects.handler, "_capture_event", reject_after)
            rejected = submit(sm, sid, package, context, aim, "failed")
        assert rejected.error_code == "test_aim_evidence"
        assert not rejected.event_seqs
        assert sm.get_object(sid, actor, CRED, package, context=context)["orientation"] == "north"
        assert submit(sm, sid, package, context, aim, "retry").status == "committed"

    with StateMachine(db, dnd_loader=lock_loader()) as resumed:
        assert resumed.get_object(sid, actor, CRED, package, context=context)["orientation"] == (
            "east"
        )
