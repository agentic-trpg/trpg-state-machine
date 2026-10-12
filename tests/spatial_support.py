"""Original single-cell fixtures and public command helpers; no success-path SQL."""

from copy import deepcopy

from dnd5e_engine.specs import GridScene

from trpg_state_machine import CreateSessionRequest, PlayerSelection, TypedCommand
from trpg_state_machine.commands import OPERATIONS
from trpg_state_machine.package import _validate

A, B, NPC = "scene.platform", "scene.workshop", "npc.attendant"
PASSAGE = "passage.station"


def spatial_data(base):
    data = deepcopy(base)
    data["manifest"].update(schema_version="state-machine-foundation/2", package_version="gc1.1")
    grid = GridScene(width=5, height=5).model_dump(mode="json")
    # MechanicalGrid intentionally has a smaller closed schema than GridScene.
    from dnd5e_engine.evaluation_state import MechanicalGrid

    grid = {k: grid[k] for k in MechanicalGrid.model_fields}
    for scene in data["scenes"]:
        scene.update(grid=deepcopy(grid), access_policy="public_interactions")
        for ex in scene["exits"]:
            ex["passage_id"] = PASSAGE
        for obj in scene["objects"]:
            obj.update(movable=False, blocks_passage=False, initial_cell="1,1", state="open")
    data["scenes"][0]["objects"].extend(
        [
            {
                "object_id": "object.crate",
                "state": None,
                "movable": True,
                "blocks_passage": True,
                "initial_cell": "3,2",
            },
            {
                "object_id": "object.stool",
                "state": None,
                "movable": True,
                "blocks_passage": True,
                "initial_cell": "3,0",
            },
        ]
    )
    data["scenes"][1]["objects"][0]["initial_cell"] = "4,4"
    for actor in data["actors"]:
        actor.update(
            initial_cell="1,3" if actor["initial_scene_id"] == A else "1,2",
            senses={"visual": True, "auditory": True},
        )
    for pc in data["pc_templates"]:
        pc["initial_cell"] = "2,1"
    data["passages"] = [
        {
            "passage_id": PASSAGE,
            "door_object_id": "object.platform_gate",
            "endpoints": [
                {"scene_id": A, "standing_cell": "2,1", "threshold_cell": "3,1"},
                {"scene_id": B, "standing_cell": "1,1", "threshold_cell": "0,1"},
            ],
        }
    ]
    return data


def create(sm, package, context, key="create"):
    return sm.create_session(
        CreateSessionRequest(
            key,
            package.adventure_id,
            package.package_version,
            package.package_digest,
            (PlayerSelection(context.player_id, package.pc_templates[0].pc_template_id),),
        ),
        package,
        context=context,
    ).session_id


def command(sm, sid, package, context, operation, key, actor_id=None):
    s = sm.read_session(sid, package, context=context)
    kind = next(k for k, v in OPERATIONS.items() if type(operation) is v)
    return TypedCommand(sid, key, actor_id or s.pc_actor_id, kind, operation, s.world_version)


def submit(sm, sid, package, context, operation, key, actor_id=None):
    return sm.submit_command(
        command(sm, sid, package, context, operation, key, actor_id), package, context=context
    )


def spatial_package(base):
    return _validate(spatial_data(base))
