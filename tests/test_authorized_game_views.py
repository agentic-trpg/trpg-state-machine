"""Role views and observed-only provenance, without new knowledge storage."""

import json
import subprocess
import sys
from dataclasses import asdict, replace

import pytest
from dnd_support import create
from lock_support import CRED, NPC, TWIN, A, B
from test_migrations import stored_rows
from test_npc_evaluation_tasks import evaluation_package as evaluation_package
from test_npc_evaluation_tasks import prepare
from test_npc_state import enter
from test_object_spatial_service import OBSTACLE, oriented_data
from test_perception import perception_package as perception_package
from test_perception import prepare as perception_prepare
from test_perception import project_prefix
from test_world_lights import lighting_data
from world_support import setup, submit

from trpg_state_machine import SetLightEnabled, StateMachine, StateMachineError
from trpg_state_machine.perception import GameViewContext, NPCContext


def viewer(context, sid, role):
    return GameViewContext(context.principal_id, sid, context.player_id, role)


def test_fine_scene_views_filter_current_members_and_reuse_observer_disclosure(
    tmp_path, context, monkeypatch
):
    data = lighting_data()
    wall = [{"x1": 1, "y1": 1, "x2": 4, "y2": 1}]
    for grid in (
        data["scenes"][0]["grid"],
        data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"],
    ):
        grid["wall_segments"] = wall
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        sm.grant_npc_access(sid, NPC, "agent:caretaker", package, context=context)
        npc = NPCContext("agent:caretaker", sid, NPC)
        player, narrator, gm = (viewer(context, sid, role) for role in ("player", "narrator", "gm"))

        def read(ctx):
            return sm.get_scene_view(sid, package, context=ctx)

        prepared = []
        prepare = sm._world._prepare_visibility

        def counted(session, actor, position, bound, *, cells=None):
            prepared.append((position.scene_id, position.cell, cells))
            return prepare(session, actor, position, bound, cells=cells)

        monkeypatch.setattr(sm._world, "_prepare_visibility", counted)
        before_pc, before_npc = read(player), read(npc)
        target = (TWIN, None)
        assert before_pc.status == before_npc.status == "resolved"
        assert target not in before_pc.visible_objects and target not in before_npc.visible_objects
        assert target in read(gm).visible_objects
        assert [(scene, cell) for scene, cell, _ in prepared] == [(A, "0,0"), (A, "2,2")]
        assert all("2,0" in cells and "4,4" not in cells for _, _, cells in prepared)
        assert read(player) == before_pc and read(npc) == before_npc
        assert read(narrator).visible_objects == before_pc.visible_objects
        assert len(prepared) == 2

        receipt = submit(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "on")
        assert receipt.status == "committed", receipt
        prepared.clear()
        lit_pc, lit_npc = read(player), read(npc)
        assert lit_pc.view_world_version > before_pc.view_world_version
        assert target in lit_pc.visible_objects and target not in lit_npc.visible_objects
        assert NPC not in lit_pc.visible_actor_ids
        assert target in read(narrator).visible_objects and read(narrator).own_character is None
        assert target not in before_pc.visible_objects
        assert len(prepared) == 2

        with sm._transaction(write=True):
            sm._characters.award_xp(sid, lit_pc.own_character.actor_id, 1)
        now = read(player)
        assert now.visible_objects == lit_pc.visible_objects
        assert now.own_character.xp == lit_pc.own_character.xp + 1
        assert len(prepared) == 2

        with sm._transaction(write=True):
            flags = sm._world_facts.scene_flags(sid, B)
            flag = next(iter(flags))
            sm._world_facts.set_scene_flags(sid, B, {**flags, flag: not flags[flag]})
        assert sm._world_facts.scene_flags(sid, B)[flag] != flags[flag]
        assert read(player).visible_objects == lit_pc.visible_objects
        assert read(npc) == lit_npc
        assert len(prepared) == 2

        with (
            sm._transaction(write=True),
            pytest.raises(RuntimeError, match="rollback"),
            sm._store.savepoint(),
        ):
            sm._world_facts.set_light_state(sid, CRED, False, "normal")
            candidate = sm._observer_scene_disclosure(sid, A, now.own_character.actor_id, package)
            assert target not in candidate.objects
            raise RuntimeError("rollback")
        assert len(prepared) == 3
        assert read(player) == now and target not in candidate.objects
        assert len(prepared) == 3

        sm.grant_npc_access(sid, NPC, "replacement", package, context=context)
        with pytest.raises(StateMachineError) as denied:
            read(npc)
        assert denied.value.code == "unauthorized" and len(prepared) == 3


def test_fine_scene_view_hides_invisible_target_without_hiding_its_own_view(tmp_path, context):
    data = lighting_data()
    data["actors"][0]["initial_cell"] = "2,0"
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        sm.grant_npc_access(sid, NPC, "agent:caretaker", package, context=context)
        player = viewer(context, sid, "player")
        npc = NPCContext("agent:caretaker", sid, NPC)
        receipt = submit(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "on")
        assert receipt.status == "committed", receipt
        before = sm.get_scene_view(sid, package, context=player)
        assert before.status == "resolved" and NPC in before.visible_actor_ids

        with sm._transaction(write=True):
            sm._characters.add_condition(
                sid,
                NPC,
                {
                    "condition": "invisible",
                    "source_entity_id": "implied:condition",
                    "scope": "session",
                },
            )
        view = sm.get_scene_view(sid, package, context=player)
        assert view.status == "resolved" and NPC not in view.visible_actor_ids
        assert view.visible_objects == before.visible_objects
        observer = sm.get_scene_view(sid, package, context=npc)
        assert observer.status == "resolved" and NPC in observer.visible_actor_ids
        assert observer.visible_objects
        gm = sm.get_scene_view(sid, package, context=viewer(context, sid, "gm"))
        assert gm.status == "resolved" and NPC in gm.visible_actor_ids


def test_fine_scene_view_stays_unresolved_with_unsupported_dynamic_occlusion(tmp_path, context):
    data = oriented_data(blocking=False)
    data["world_objects"][-1]["capabilities"][0]["closed_properties"] = {"vision_occlusion": True}
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        assert OBSTACLE in dict(
            sm.get_scene_view(sid, package, context=viewer(context, sid, "gm")).visible_objects
        )
        view = sm.get_scene_view(sid, package, context=viewer(context, sid, "player"))
        assert view.status == "unresolved" and view.visible_actor_ids == view.visible_objects == ()


@pytest.mark.parametrize("role", ["gm", "player", "narrator"])
def test_scoped_game_views_are_read_only(tmp_path, evaluation_package, context, role):
    db = tmp_path / "state.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        before = stored_rows(db)
        view = sm.get_scene_view(sid, evaluation_package, context=viewer(context, sid, role))
        assert view.schema_version == "game-view/1" and view.status == "resolved"
        assert (
            view.view_world_version
            == sm.read_session(sid, evaluation_package, context=context).world_version
        )
        text = json.dumps(asdict(view))
        if role == "gm":
            assert "buried blue key" in text and view.gm_scene_flags
        else:
            assert not view.gm_facts and not view.gm_scene_flags
            assert "buried blue key" not in text and "belief" not in text and "memory" not in text
        if role == "player":
            assert (
                view.own_character
                == sm.read_session(sid, evaluation_package, context=context).character
            )
        else:
            assert view.own_character is None
        assert sm.get_npc_view(sid, "npc.caretaker", evaluation_package, context=npc).belief_history
        assert stored_rows(db) == before
        with pytest.raises(StateMachineError):
            sm.read_session(sid, evaluation_package, context=viewer(context, sid, role))
        with pytest.raises(StateMachineError):
            sm.read_events(sid, evaluation_package, context=viewer(context, sid, role))


@pytest.mark.parametrize("changed", ["principal", "player", "session", "role", "payload"])
def test_view_credentials_do_not_grant_extra_access(tmp_path, evaluation_package, context, changed):
    with StateMachine(tmp_path / "state.db") as sm:
        sid, _, _ = prepare(sm, evaluation_package, context)
        view_context = viewer(context, sid, "player")
        if changed == "principal":
            view_context = replace(view_context, principal_id="attacker")
        elif changed == "player":
            view_context = replace(view_context, player_id="other")
        elif changed == "session":
            view_context = replace(view_context, session_id="other")
        elif changed == "role":
            view_context = replace(view_context, role="npc")
        else:
            view_context = asdict(view_context)
        with pytest.raises(StateMachineError) as caught:
            sm.get_scene_view(sid, evaluation_package, context=view_context)
        assert caught.value.code == "unauthorized"


def test_two_npc_private_history_and_grant_revocation(tmp_path, evaluation_package, context):
    with StateMachine(tmp_path / "state.db") as sm:
        sid, caretaker, _ = prepare(sm, evaluation_package, context)
        attendant = NPCContext("agent:npc.attendant", sid, "npc.attendant")
        # The helper uses the persisted principal it actually granted.
        principal = sm._conn.execute(
            "SELECT principal_id FROM npc_access WHERE observer_id=?", ("npc.attendant",)
        ).fetchone()[0]
        attendant = replace(attendant, principal_id=principal)
        a = sm.get_npc_view(sid, "npc.caretaker", evaluation_package, context=caretaker)
        b = sm.get_npc_view(sid, "npc.attendant", evaluation_package, context=attendant)
        assert a.known_facts != b.known_facts
        assert "buried blue key" not in json.dumps(asdict(a)) + json.dumps(asdict(b))
        with pytest.raises(StateMachineError):
            sm.get_npc_view(sid, "npc.attendant", evaluation_package, context=caretaker)
        sm.grant_npc_access(
            sid, "npc.caretaker", "replacement", evaluation_package, context=context
        )
        for method in (sm.get_scene_view, sm.read_observations):
            with pytest.raises(StateMachineError):
                method(sid, evaluation_package, context=caretaker)


def test_partial_signals_source_audit_does_not_leak_event(
    tmp_path, perception_package, context, request_for
):
    with StateMachine(tmp_path / "state.db") as sm:
        created, seq = perception_prepare(sm, perception_package, context, request_for)
        project_prefix(sm, created.session_id, perception_package, context)
        npc = NPCContext("agent:npc.attendant", created.session_id, "npc.attendant")
        observations = sm.read_observations(created.session_id, perception_package, context=npc)
        obs = next(
            o
            for o in observations
            if any(s.content == "A chime rings." for s in o.perceived_signals)
        )
        assert len(obs.perceived_signals) == 1
        source = sm._conn.execute(
            "SELECT source_event_id,signal_id FROM observation_signal_sources "
            "WHERE observation_id=?",
            (obs.observation_id,),
        ).fetchall()
        event = sm._read_event(created.session_id, seq)
        expected = [(event.event_id, s.signal_id) for s in event.signals if s.channel == "auditory"]
        assert [tuple(r) for r in source] == expected
        assert "source_event" not in json.dumps(asdict(obs)) and "signal.lamp" not in json.dumps(
            asdict(obs)
        )
        before = stored_rows(tmp_path / "state.db")
        assert (
            sm.project_event(created.session_id, seq, perception_package, context=context)
            == "already_complete"
        )
        assert stored_rows(tmp_path / "state.db") == before


def test_historical_observation_is_not_current_view(tmp_path, evaluation_package, context):
    with StateMachine(tmp_path / "state.db") as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        history = sm.read_observations(sid, evaluation_package, context=npc)
        old = sm.get_scene_view(sid, evaluation_package, context=npc)
        enter(sm, sid, evaluation_package, context, "scene.workshop")
        from trpg_state_machine import MoveActor

        sm._apply(
            sid,
            evaluation_package,
            MoveActor("npc.caretaker", "scene.workshop"),
            expected_world_version=sm.read_session(
                sid, evaluation_package, context=context
            ).world_version,
            context=context,
        )
        current = sm.get_scene_view(sid, evaluation_package, context=npc)
        assert (
            old.scene_id != current.scene_id and old.view_world_version < current.view_world_version
        )
        assert sm.read_observations(sid, evaluation_package, context=npc) == history
        assert "source_event" not in json.dumps(asdict(current))


def test_view_cannot_cross_session_or_pinned_package(tmp_path, evaluation_package, context):
    with StateMachine(tmp_path / "state.db") as sm:
        sid, _, _ = prepare(sm, evaluation_package, context)
        other = create(sm, evaluation_package, context, "create.other")
        with pytest.raises(StateMachineError):
            sm.get_scene_view(other, evaluation_package, context=viewer(context, sid, "gm"))
        with pytest.raises(StateMachineError) as caught:
            sm.get_scene_view(
                sid,
                replace(evaluation_package, package_digest="wrong"),
                context=viewer(context, sid, "gm"),
            )
        assert caught.value.code == "package_digest_mismatch"


def test_views_restore_in_fresh_process(tmp_path, evaluation_package, context):
    with StateMachine(tmp_path / "state.db") as sm:
        sid, _, _ = prepare(sm, evaluation_package, context)
        expected = asdict(
            sm.get_scene_view(sid, evaluation_package, context=viewer(context, sid, "narrator"))
        )
    script = """
import json,sys
from dataclasses import asdict
from trpg_state_machine import StateMachine,load_adventure_package
from trpg_state_machine.perception import GameViewContext
p=load_adventure_package(sys.argv[2]).package
with StateMachine(sys.argv[1]) as sm:
 c=GameViewContext('authenticated:alice',sys.argv[3],'player.alice','narrator')
 print(json.dumps(asdict(sm.get_scene_view(sys.argv[3],p,context=c))))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "state.db"),
            str(tmp_path / "evaluation.yaml"),
            sid,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == json.loads(json.dumps(expected))


@pytest.mark.parametrize("kind", ["dark", "condition", "obscurement"])
def test_mechanical_unknown_visibility_fails_closed(tmp_path, package_data, context, kind):
    from test_npc_combat_handoff import prepare as combat_prepare

    sm, sid, package, _, npc, _, _, _ = combat_prepare(tmp_path, context)
    with sm:
        # Inject an unsupported current environment after a valid Engine initialization.
        # This tests read admission, not mechanical evaluation of an invented result.
        scene = json.loads(
            sm._conn.execute(
                "SELECT state_json FROM mechanical_scenes WHERE session_id=?", (sid,)
            ).fetchone()[0]
        )
        if kind == "dark":
            scene["grid"]["default_lighting"] = "dark"
        elif kind == "condition":
            actor_id = sm._conn.execute(
                "SELECT actor_id FROM mechanical_actors WHERE session_id=? LIMIT 1", (sid,)
            ).fetchone()[0]
            with sm._store.transaction(write=True):
                sm._characters.add_condition(
                    sid,
                    actor_id,
                    {
                        "condition": "invisible",
                        "source_entity_id": "implied:condition",
                        "scope": "session",
                    },
                )
        else:
            scene["grid"]["obscurement_cells"] = {"0,0": "heavy"}
        sm._conn.execute(
            "UPDATE mechanical_scenes SET state_json=? WHERE session_id=?",
            (json.dumps(scene), sid),
        )
        for ctx in (npc, viewer(context, sid, "player"), viewer(context, sid, "narrator")):
            v = sm.get_scene_view(sid, package, context=ctx)
            assert v.status == "unresolved" and not v.visible_actor_ids and not v.visible_objects
        assert (
            sm.get_scene_view(sid, package, context=viewer(context, sid, "gm")).status == "resolved"
        )
