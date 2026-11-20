import json

import pytest
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from lock_support import CRED, A
from test_world_containers import BOX, container_data
from world_support import setup, submit

from trpg_state_machine import (
    ObjectState,
    PickupItem,
    PutItem,
    SetLightEnabled,
    SetObjectState,
    StepActor,
    SwitchLightMode,
)
from trpg_state_machine.world_lights import LightService
from trpg_state_machine.world_package import LightMode


def light_config():
    return {
        "kind": "light",
        "initial_enabled": False,
        "initial_mode": "normal",
        "emission_shape": "grid_radius_static_los",
        "source_kind": "scenario",
        "sunlight": False,
        "fuel_policy": "scenario_unmetered",
        "modes": [
            {"mode_id": "normal", "bright_radius_ft": 5, "dim_outer_radius_ft": 10},
            {"mode_id": "hooded", "bright_radius_ft": None, "dim_outer_radius_ft": 5},
        ],
    }


def lighting_data():
    d = container_data()
    d["lockable_doors"] = []
    d["scenes"][0]["objects"][0]["state"] = "open"
    next(x for x in d["world_objects"] if x["object_id"] == CRED)["capabilities"].append(
        light_config()
    )
    pc = d["pc_templates"][0]["pc_template_id"]
    next(x for x in d["item_transfers"] if x["instance_id"] == CRED)["initial_owner_id"] = pc
    for foundation in ("combat_foundation", "noncombat_foundation"):
        snapshot = d["rule_bindings"][foundation]["snapshot"]
        next(a for a in snapshot["character_states"] if a["entity_id"] == pc)[
            "carried_item_slugs"
        ].append("amulet")
        snapshot["inventory_state"]["entries"].append(
            {
                "instance_id": CRED,
                "owner_id": pc,
                "item_slug": "amulet",
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
        )
        snapshot["scene_state"]["grid"]["default_lighting"] = "dark"
    d["scenes"][0]["grid"]["default_lighting"] = "dark"
    del d["rule_bindings"]["combat_foundation"]
    d["pc_templates"][0]["character"]["inventory"].append("amulet")
    return d


def evidence(sm, sid, seq):
    return json.loads(
        sm._conn.execute(
            "SELECT evidence_json FROM event_evidence WHERE session_id=? AND event_seq=?",
            (sid, seq),
        ).fetchone()[0]
    )


def test_light_modes_carry_container_shading_snapshot_and_stage_evidence(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, lighting_data())
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        assert sm.get_illumination(sid, actor, "1,0", p, context=context)["illumination"] == "dark"
        receipt = submit(sm, sid, p, context, SetLightEnabled(A, CRED, "0,0", True), "on")
        assert receipt.status == "committed", receipt
        before, after = (evidence(sm, sid, seq) for seq in receipt.event_seqs)
        assert before["scenes"][A]["grid"]["default_lighting"] == "dark"
        assert after["scenes"][A]["grid"]["lighting"]["1,0"] == "bright"
        assert sm.get_illumination(sid, actor, "2,0", p, context=context)["illumination"] == "dim"
        snapshot = sm.read_noncombat_snapshot(sid, p, context=context)
        assert snapshot.scene_state.grid.lighting["1,0"] == "bright"
        assert (
            submit(sm, sid, p, context, SwitchLightMode(A, CRED, "0,0", "hooded"), "hood").status
            == "committed"
        )
        assert sm.get_illumination(sid, actor, "1,0", p, context=context)["illumination"] == "dim"
        assert (
            submit(sm, sid, p, context, SwitchLightMode(A, CRED, "0,0", "normal"), "normal").status
            == "committed"
        )
        assert submit(sm, sid, p, context, PickupItem(BOX, A, "1,0"), "case").status == "committed"
        assert (
            submit(sm, sid, p, context, PutItem(A, CRED, BOX, "0,0"), "inside").status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, SetObjectState(A, BOX, ObjectState.CLOSED), "close").status
            == "committed"
        )
        assert sm.get_illumination(sid, actor, "1,0", p, context=context)["illumination"] == "dark"
        assert (
            submit(sm, sid, p, context, SetObjectState(A, BOX, ObjectState.OPEN), "open").status
            == "committed"
        )
        assert submit(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "move").status == "committed"
        assert (
            sm.get_illumination(sid, actor, "2,0", p, context=context)["illumination"] == "bright"
        )
        assert evidence(sm, sid, receipt.event_seqs[-1]) == after


def test_unknown_container_transmission_is_not_a_deterministic_brightness(tmp_path, context):
    d = lighting_data()
    box = next(x for x in d["world_objects"] if x["object_id"] == BOX)
    box["capabilities"][1]["light_transmission_closed"] = "unknown"
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        assert (
            submit(sm, sid, p, context, SetLightEnabled(A, CRED, "0,0", True), "on").status
            == "committed"
        )
        put = submit(sm, sid, p, context, PutItem(A, CRED, BOX, "0,0"), "put")
        assert put.status == "committed"
        before = evidence(sm, sid, put.event_seqs[-1])["scenes"][A]
        receipt = submit(sm, sid, p, context, SetObjectState(A, BOX, ObjectState.CLOSED), "close")
        assert receipt.status == "committed"
        after = evidence(sm, sid, receipt.event_seqs[-1])["scenes"][A]
        assert "1,0" not in before["light_uncertain_cells"]
        assert "1,0" in after["light_uncertain_cells"]
        assert "1,0" not in after["lighting_sources"]
        view = sm.get_illumination(sid, actor, "1,0", p, context=context)
        assert view["status"] == "unknown" and view["illumination"] is None
        assert CRED not in view["contributing_sources"]


@pytest.mark.parametrize("size", (12, 92))
def test_carried_light_event_work_and_evidence_depend_on_radius(
    tmp_path, context, monkeypatch, size
):
    data = lighting_data()
    snapshot_grid = data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"]
    for grid in (data["scenes"][0]["grid"], snapshot_grid):
        grid.update(width=size, height=size, lighting={"0,1": "bright", "8,8": "dim"})
    sm, sid, package, _ = setup(tmp_path, context, data)
    evaluated = []
    illuminate_cell = LightService._illuminate_cell

    def count_cell(topology, uncertain_geometry, candidates, sources, cell):
        evaluated.append(cell)
        return illuminate_cell(topology, uncertain_geometry, candidates, sources, cell)

    initial_cells = [f"{x},{y}" for y in range(3) for x in range(3)]
    moved_cells = [f"{x},{y}" for y in range(3) for x in range(4)]
    with sm, monkeypatch.context() as patch:
        patch.setattr(LightService, "_illuminate_cell", staticmethod(count_cell))
        light_on = submit(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "light")
        assert light_on.status == "committed", light_on
        assert evaluated == initial_cells
        before, after = (evidence(sm, sid, seq)["scenes"][A] for seq in light_on.event_seqs)
        assert before["grid"]["lighting"] == {"0,1": "bright", "8,8": "dim"}
        assert set(after["grid"]["lighting"]) == {*initial_cells, "8,8"}
        assert after["grid"]["lighting"]["1,0"] == "bright"
        assert after["grid"]["lighting"]["0,1"] == "bright"
        assert after["grid"]["lighting"]["2,0"] == "dim"
        assert after["lighting_sources"]["0,1"] == [CRED]

        evaluated.clear()
        step = submit(sm, sid, package, context, StepActor(A, "0,0", "1,0"), "step")
        assert step.status == "committed", step
        assert evaluated == moved_cells
        moved = evidence(sm, sid, step.event_seqs[-1])["scenes"][A]
        assert set(moved["grid"]["lighting"]) == {*moved_cells, "8,8"}
        assert moved["grid"]["lighting"]["2,0"] == "bright"
        assert "3,0" in moved["lighting_sources"]
        assert moved["light_uncertain_cells"] == []
        assert evidence(sm, sid, light_on.event_seqs[-1])["scenes"][A] == after

        evaluated.clear()
        snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        assert evaluated == moved_cells
        assert snapshot.scene_state.grid.lighting == moved["grid"]["lighting"]
        assert moved["grid"]["lighting"]["8,8"] == "dim"
        with sm._transaction():
            lights = sm._objects.lights
            base = sm._world.grid.reader.current_grid(sid, A, package)
            full_grid, unknown, sources = lights.project(
                sid,
                A,
                package,
                base,
                cells=(f"{x},{y}" for y in range(size) for x in range(size)),
            )
        assert not unknown
        assert sources == moved["lighting_sources"]
        sparse = GridTopology(GridScene(**moved["grid"]))
        assert {cell: sparse.light_on_cell(cell) for cell in full_grid["lighting"]} == full_grid[
            "lighting"
        ]


@pytest.mark.parametrize("origin", (None, "3,3"))
def test_event_preserves_uncertainty_when_source_position_is_unavailable(
    tmp_path, context, monkeypatch, origin
):
    sm, sid, package, _ = setup(tmp_path, context, lighting_data())
    sources = LightService.sources

    def missing_source_position(self, sid, scene, package):
        return [{**source, "cell": origin} for source in sources(self, sid, scene, package)]

    with sm:
        monkeypatch.setattr(LightService, "sources", missing_source_position)
        receipt = submit(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "light")
        assert receipt.status == "committed", receipt
        scene = evidence(sm, sid, receipt.event_seqs[-1])["scenes"][A]
        assert scene["light_uncertain_cells"] == [f"{x},{y}" for y in range(3) for x in range(3)]
        assert scene["lighting_sources"] == {}


def test_light_envelopes_merge_overlap_and_skip_gaps():
    grid = {"width": 8, "height": 3, "cell_size_ft": 3}
    topology = GridTopology(GridScene(**grid))
    mode = LightMode("test", None, 4)
    sources = [{"cell": cell, "mode": mode} for cell in ("0,0", "2,0", "7,1")]
    assert list(LightService._affected_light_cells(topology, grid, sources)) == [
        *(f"{x},{y}" for y in range(2) for x in (0, 1, 2, 3, 6, 7)),
        "6,2",
        "7,2",
    ]


def test_light_equal_to_bright_default_preserves_missing_position_judgment(tmp_path, context):
    from trpg_state_machine.perception_conditions import judge_signal, signal_environment

    data = lighting_data()
    snapshot_grid = data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"]
    for grid in (data["scenes"][0]["grid"], snapshot_grid):
        grid["default_lighting"] = "bright"
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        receipt = submit(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "light")
        assert receipt.status == "committed", receipt
        signal = sm.read_events(sid, package, context=context)[-1].signals[0]
        before, after = (evidence(sm, sid, seq) for seq in receipt.event_seqs)
        for stage, judgment_allowed in ((before, True), (after, False)):
            condition = signal_environment(signal, stage)
            condition["origin_cell"] = None
            decision = judge_signal(signal, stage["actors"][0], condition)
            assert decision.reason == "missing_spatial_evidence"
            assert decision.judgment_allowed is judgment_allowed
        assert set(after["scenes"][A]["grid"]["lighting"].values()) == {"bright"}


def test_current_perception_uses_frozen_local_grid_and_recovery(tmp_path, context):
    import os
    import subprocess
    import sys

    from trpg_state_machine.perception_conditions import judge_signal, signal_environment

    sm, sid, p, db = setup(tmp_path, context, lighting_data())
    with sm:
        receipt = submit(sm, sid, p, context, SetLightEnabled(A, CRED, "0,0", True), "on")
        before = evidence(sm, sid, receipt.event_seqs[0])
        after = evidence(sm, sid, receipt.event_seqs[1])
        event = sm.read_events(sid, p, context=context)[-1]
        signal = event.signals[0]
        observer = after["actors"][0]
        dark = judge_signal(signal, before["actors"][0], signal_environment(signal, before))
        lit = judge_signal(signal, observer, signal_environment(signal, after))
        assert dark.outcome == "negative"
        assert lit.outcome == "unresolved" and lit.judgment_allowed
        assert (
            submit(sm, sid, p, context, SwitchLightMode(A, CRED, "0,0", "hooded"), "hood").status
            == "committed"
        )
        assert evidence(sm, sid, receipt.event_seqs[1]) == after
    code = (
        "from test_world_lights import lighting_data; from lock_support import lock_loader; "
        "from trpg_state_machine import StateMachine,TrustedContext; "
        "from trpg_state_machine.package import _validate; import sys,json; "
        "p=_validate(lighting_data()); c=TrustedContext('authenticated:alice','player.alice'); "
        "s=StateMachine(sys.argv[1],dnd_loader=lock_loader()); "
        "a=s.read_session(sys.argv[2],p,context=c).pc_actor_id; "
        "print(json.dumps(s.get_object(sys.argv[2],a,sys.argv[3],p,context=c)['light'])); s.close()"
    )
    process = subprocess.run(
        [sys.executable, "-c", code, str(db), sid, CRED],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(("src", "tests"))},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(process.stdout) == {"enabled": True, "mode": "hooded"}


def test_fixed_emitter_and_failed_commit_leave_no_candidate_light(tmp_path, context):
    from copy import deepcopy

    import pytest

    from trpg_state_machine import StateMachineError

    d = lighting_data()
    oid = "object.fixed_glow"
    d["scenes"][0]["objects"].append(
        {
            "object_id": oid,
            "state": None,
            "initial_cell": "0,0",
            "movable": False,
            "blocks_passage": False,
        }
    )
    profile = deepcopy(d["world_objects"][1])
    profile.update(
        object_id=oid, definition_ref="definition.fixed_glow", capabilities=[light_config()]
    )
    d["world_objects"].append(profile)
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_light BEFORE INSERT ON event_evidence "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        from world_support import command

        cmd = command(sm, sid, p, context, SetLightEnabled(A, oid, "0,0", True), "fixed")
        with pytest.raises(StateMachineError):
            sm.submit_command(cmd, p, context=context)
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        assert sm.get_illumination(sid, actor, "1,0", p, context=context)["illumination"] == "dark"
        sm._conn.execute("DROP TRIGGER fail_light")
        receipt = sm.submit_command(cmd, p, context=context)
        assert receipt.status == "committed"
        assert sm.submit_command(cmd, p, context=context) == receipt
        assert (
            sm.get_illumination(sid, actor, "1,0", p, context=context)["illumination"] == "bright"
        )


def test_dynamic_movement_geometry_is_unknown_for_light_and_not_passed_to_rules(tmp_path, context):
    import pytest
    from test_world_spatial import spatial_world_data

    from trpg_state_machine import StateMachineError

    d, geometry = lighting_data(), spatial_world_data()
    d["scenes"][0]["objects"].append(geometry["scenes"][0]["objects"][-1])
    d["world_objects"].append(geometry["world_objects"][-1])
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        assert (
            submit(sm, sid, p, context, SetLightEnabled(A, CRED, "0,0", True), "on").status
            == "committed"
        )
        result = sm.get_illumination(sid, actor, "2,0", p, context=context)
        assert result["status"] == "unknown" and result["illumination"] is None
        with pytest.raises(StateMachineError, match="uncertainty"):
            sm.read_noncombat_snapshot(sid, p, context=context)


def test_destroyed_emitter_ceases_contributing_without_identity_loss(tmp_path, context):
    from test_world_durability import adopt

    from trpg_state_machine.object_durability import _ObjectHPSettlement

    d = lighting_data()
    next(x for x in d["world_objects"] if x["object_id"] == CRED)["capabilities"].append(
        {
            "kind": "durability",
            "armor_class": 10,
            "max_hp": 5,
            "initial_hp": 5,
            "damage_profile_ref": "scenario.explicit_settled_damage",
            "damage_threshold": None,
            "destruction_policy_ref": "disable_preserve_obstruction",
        }
    )
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        assert (
            submit(sm, sid, p, context, SetLightEnabled(A, CRED, "0,0", True), "on").status
            == "committed"
        )
        version = sm.read_session(sid, p, context=context).world_version
        adopt(sm, sid, p, context, _ObjectHPSettlement(sid, CRED, "destroy.emitter", version, 5, 0))
        assert sm.get_illumination(sid, actor, "1,0", p, context=context)["illumination"] == "dark"
        assert sm.get_object(sid, actor, CRED, p, context=context)["destroyed"]


def test_real_perception_projection_accepts_light_evidence_and_filters_source_debug(
    tmp_path, context
):
    from lock_support import NPC

    from trpg_state_machine.perception import JudgmentResult, NPCContext, SignalJudgment

    sm, sid, p, _ = setup(tmp_path, context, lighting_data())
    with sm:
        sm.grant_npc_access(sid, NPC, "agent:light", p, context=context)
        receipt = submit(sm, sid, p, context, SetLightEnabled(A, CRED, "0,0", True), "on")
        requests = []

        def notice(request):
            assert not sm._conn.in_transaction
            for environment in request.evidence["environments"].values():
                assert "lighting_sources" not in environment
            requests.append(request)
            return JudgmentResult(tuple(SignalJudgment(s.signal_id, True) for s in request.signals))

        for event in sm.read_events(sid, p, context=context):
            assert (
                sm.project_event(sid, event.event_seq, p, context=context, judgment=notice)
                == "resolved"
            )
        assert requests
        assert evidence(sm, sid, receipt.event_seqs[-1])["schema"] == "event-evidence/6"
        observations = sm.read_observations(sid, p, context=NPCContext("agent:light", sid, NPC))
        assert observations
