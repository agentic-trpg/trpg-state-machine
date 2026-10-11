import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest
import yaml
from perception_support import synthetic_notice
from test_commands import make_command
from test_events import create_request, event_data

from trpg_state_machine import (
    EnterScene,
    MoveActor,
    StateMachine,
    StateMachineError,
    TrustedContext,
    load_adventure_package,
)
from trpg_state_machine.perception import (
    CurrentPerceptualView,
    JudgmentResult,
    NPCContext,
    SignalJudgment,
)


@pytest.fixture
def perception_package(tmp_path, package_data):
    for actor in package_data["actors"]:
        actor["senses"] = {"visual": actor["actor_id"] != "npc.attendant", "auditory": True}
    definition = event_data()
    definition["signals"].insert(
        0,
        {
            "signal_id": "signal.lamp",
            "channel": "visual",
            "content": "A lamp flashes.",
            "scope": {"kind": "scene", "scene_id": "scene.platform", "visibility": "clear"},
        },
    )
    package_data["events"] = [definition]
    package_data["scenes"][0]["event_definition_ids"] = [definition["event_definition_id"]]
    path = tmp_path / "perception.yaml"
    path.write_text(yaml.safe_dump(package_data), "utf-8")
    result = load_adventure_package(path)
    assert result.status == "valid", result.errors
    return result.package


def prepare(sm, package, context, request_for, colocate=True):
    created = sm.create_session(create_request(request_for, package), package, context=context)
    sm._apply(
        created.session_id,
        package,
        EnterScene("scene.workshop"),
        expected_world_version=0,
        context=context,
    )
    sm._apply(
        created.session_id,
        package,
        EnterScene("scene.platform"),
        expected_world_version=1,
        context=context,
    )
    if colocate:
        sm._apply(
            created.session_id,
            package,
            MoveActor("npc.attendant", "scene.platform"),
            expected_world_version=2,
            context=context,
        )
    command = make_command(sm, created, package, context)
    receipt = sm.submit_command(command, package, context=context)
    for observer in ("npc.caretaker", "npc.attendant"):
        sm.grant_npc_access(
            created.session_id, observer, "agent:" + observer, package, context=context
        )
    return created, receipt.event_seqs[-1]


def npc_context(created, observer="npc.caretaker"):
    return NPCContext("agent:" + observer, created.session_id, observer)


def project_prefix(sm, sid, package, context, judgment=synthetic_notice):
    events = sm.read_events(sid, package, context=context)
    return [
        sm.project_event(sid, event.event_seq, package, context=context, judgment=judgment)
        for event in events
    ]


def test_two_observers_multiple_signals_and_restart(
    tmp_path, perception_package, context, request_for
):
    package = perception_package
    database = tmp_path / "state.db"
    with StateMachine(database) as sm:
        created, seq = prepare(sm, package, context, request_for)
        assert set(project_prefix(sm, created.session_id, package, context)) == {"resolved"}
        first = sm.read_observations(created.session_id, package, context=npc_context(created))
        second = sm.read_observations(
            created.session_id, package, context=npc_context(created, "npc.attendant")
        )
        lamp = [
            o for o in first if any(s.content == "A lamp flashes." for s in o.perceived_signals)
        ]
        chime = [
            o for o in second if any(s.content == "A chime rings." for s in o.perceived_signals)
        ]
        assert len(lamp) == len(chime) == 1
        assert len(lamp[0].perceived_signals) == 2 and len(chime[0].perceived_signals) == 1
        assert (
            sm.project_event(
                created.session_id, seq, package, context=context, judgment=synthetic_notice
            )
            == "already_complete"
        )
        assert sm.perception_cursor(created.session_id, package, context=context) == seq
    with StateMachine(database) as sm:
        assert (
            sm.read_observations(created.session_id, package, context=npc_context(created)) == first
        )
        assert (
            sm.read_observations(
                created.session_id, package, context=npc_context(created, "npc.attendant")
            )
            == second
        )
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_observations WHERE event_seq=?", (seq,)
            ).fetchone()[0]
            == 2
        )
        assert conn.execute("SELECT COUNT(*) FROM npc_inbox").fetchone()[0] == len(first) + len(
            second
        )


def test_event_time_location_survives_npc_move(tmp_path, perception_package, context, request_for):
    package = perception_package
    with StateMachine(tmp_path / "state.db") as sm:
        created, seq = prepare(sm, package, context, request_for, colocate=False)
        version = sm.read_session(created.session_id, package, context=context).world_version
        sm._apply(
            created.session_id,
            package,
            MoveActor("npc.caretaker", "scene.workshop"),
            expected_world_version=version,
            context=context,
        )
        project_prefix(sm, created.session_id, package, context)
        first = sm.read_observations(created.session_id, package, context=npc_context(created))
        second = sm.read_observations(
            created.session_id, package, context=npc_context(created, "npc.attendant")
        )
        assert any(s.content == "A chime rings." for o in first for s in o.perceived_signals)
        assert not any(s.content == "A chime rings." for o in second for s in o.perceived_signals)
        assert (
            sm._conn.execute(
                "SELECT complete FROM perception_results WHERE event_seq=? "
                "AND observer_id='npc.attendant'",
                (seq,),
            ).fetchone()[0]
            == 1
        )
        view = sm.get_scene_view(created.session_id, package, context=npc_context(created))
        assert isinstance(view, CurrentPerceptualView) and view.scene_id == "scene.workshop"
        assert "npc.attendant" in view.visible_actor_ids
        assert not hasattr(view, "source_event_ref")


def test_observation_privacy_and_auth_boundaries(
    tmp_path, perception_package, context, request_for
):
    package = perception_package
    with StateMachine(tmp_path / "state.db") as sm:
        created, _ = prepare(sm, package, context, request_for)
        project_prefix(sm, created.session_id, package, context)
        npc = npc_context(created)
        observations = sm.read_observations(created.session_id, package, context=npc)
        serialized = json.dumps([asdict(o) for o in observations])
        pc_id = sm.read_session(created.session_id, package, context=context).pc_actor_id
        for forbidden in (
            "event_type",
            "source_command",
            "source_event",
            "target_actor",
            pc_id,
            "object.platform_gate",
            "flag.lamp_checked",
            "package.event_fired",
        ):
            assert forbidden not in serialized
        for method in (sm.read_session, sm.save_session, sm.resume_session, sm.read_events):
            with pytest.raises(StateMachineError):
                method(created.session_id, package, context=npc)
        for forged in (
            replace(npc, principal_id="intruder"),
            replace(npc, observer_id="npc.attendant"),
            replace(npc, session_id="wrong"),
            context,
        ):
            with pytest.raises(StateMachineError):
                sm.read_observations(created.session_id, package, context=forged)
        assert (
            sm.get_scene_view(created.session_id, package, context=context).scene_id
            == "scene.platform"
        )
        sm.grant_npc_access(
            created.session_id, npc.observer_id, "replacement", package, context=context
        )
        with pytest.raises(StateMachineError):
            sm.get_scene_view(created.session_id, package, context=npc)


def uncertain_package(package, tmp_path, visibility="undetermined"):
    data = yaml.safe_load((tmp_path / "perception.yaml").read_text("utf-8"))
    for signal in data["events"][0]["signals"]:
        signal["scope"]["visibility"] = visibility
    path = tmp_path / "uncertain.yaml"
    path.write_text(yaml.safe_dump(data), "utf-8")
    result = load_adventure_package(path)
    assert result.status == "valid", result.errors
    return result.package


@pytest.mark.parametrize(
    "answer",
    [
        None,
        JudgmentResult(()),
        JudgmentResult((SignalJudgment("signal.chime", True),)),
        JudgmentResult((SignalJudgment("signal.lamp", True), SignalJudgment("signal.lamp", False))),
        JudgmentResult((SignalJudgment("signal.lamp", True), SignalJudgment("unknown", True))),
        JudgmentResult((SignalJudgment("signal.lamp", 1), SignalJudgment("signal.chime", True))),
        {"signal.lamp": True, "signal.chime": False},
    ],
)
def test_invalid_judgment_stays_unresolved(
    tmp_path, perception_package, context, request_for, answer
):
    package = uncertain_package(perception_package, tmp_path)
    with StateMachine(tmp_path / "state.db") as sm:
        created, seq = prepare(sm, package, context, request_for, colocate=False)

        def judge(request):
            assert request.observer_id == "npc.caretaker"
            assert len(request.signals) == 2
            assert not sm._conn.in_transaction
            assert not hasattr(request, "event_type") and not hasattr(request, "payload")
            return answer

        assert (
            sm.project_event(created.session_id, seq, package, context=context, judgment=judge)
            == "unresolved"
        )
        assert sm.perception_cursor(created.session_id, package, context=context) == 0
        assert (
            sm._conn.execute(
                "SELECT COUNT(*) FROM event_observations WHERE event_seq=?", (seq,)
            ).fetchone()[0]
            == 0
        )


def test_judgment_timeout_restart_retry_and_cursor_gap(
    tmp_path, perception_package, context, request_for
):
    package = uncertain_package(perception_package, tmp_path)
    database = tmp_path / "state.db"

    def timeout(request):
        raise TimeoutError("JEV unavailable")

    def decide(request):
        return JudgmentResult(
            tuple(SignalJudgment(s.signal_id, s.channel == "auditory") for s in request.signals)
        )

    with StateMachine(database) as sm:
        created, seq = prepare(sm, package, context, request_for)
        assert (
            sm.project_event(created.session_id, seq, package, context=context, judgment=timeout)
            == "unresolved"
        )
        assert sm.read_observations(created.session_id, package, context=npc_context(created)) == ()
    with StateMachine(database) as sm:
        assert (
            sm.project_event(created.session_id, seq, package, context=context, judgment=decide)
            == "resolved"
        )
        # Later projection is durable but not visible across earlier work gaps.
        assert sm.perception_cursor(created.session_id, package, context=context) == 0
        assert sm.read_observations(created.session_id, package, context=npc_context(created)) == ()
        project_prefix(sm, created.session_id, package, context, judgment=decide)
        assert sm.perception_cursor(created.session_id, package, context=context) == seq
        observations = sm.read_observations(
            created.session_id, package, context=npc_context(created)
        )
        assert any(s.content == "A chime rings." for o in observations for s in o.perceived_signals)
        assert not any(
            s.content == "A lamp flashes." for o in observations for s in o.perceived_signals
        )


def test_all_false_completes_without_observation(
    tmp_path, perception_package, context, request_for
):
    package = uncertain_package(perception_package, tmp_path)
    with StateMachine(tmp_path / "state.db") as sm:
        created, seq = prepare(sm, package, context, request_for)

        def none_perceived(request):
            return JudgmentResult(
                tuple(SignalJudgment(s.signal_id, False) for s in request.signals)
            )

        assert (
            sm.project_event(
                created.session_id, seq, package, context=context, judgment=none_perceived
            )
            == "resolved"
        )
        assert (
            sm._conn.execute(
                "SELECT COUNT(*) FROM event_observations WHERE event_seq=?", (seq,)
            ).fetchone()[0]
            == 0
        )
        assert (
            sm._conn.execute(
                "SELECT SUM(complete) FROM perception_results WHERE event_seq=?", (seq,)
            ).fetchone()[0]
            == 2
        )


def test_projection_inbox_failure_atomic_and_retry(
    tmp_path, perception_package, context, request_for
):
    with StateMachine(tmp_path / "state.db") as sm:
        created, seq = prepare(sm, perception_package, context, request_for)
        sm._conn.execute(
            "CREATE TRIGGER fail_inbox BEFORE INSERT ON npc_inbox "
            "BEGIN SELECT RAISE(ABORT, 'fault'); END"
        )
        with pytest.raises(StateMachineError):
            sm.project_event(
                created.session_id,
                seq,
                perception_package,
                context=context,
                judgment=synthetic_notice,
            )
        assert (
            sm._conn.execute(
                "SELECT COUNT(*) FROM perception_results WHERE event_seq=?", (seq,)
            ).fetchone()[0]
            == 0
        )
        assert (
            sm._conn.execute(
                "SELECT COUNT(*) FROM event_observations WHERE event_seq=?", (seq,)
            ).fetchone()[0]
            == 0
        )
        sm._conn.execute("DROP TRIGGER fail_inbox")
        assert (
            sm.project_event(
                created.session_id,
                seq,
                perception_package,
                context=context,
                judgment=synthetic_notice,
            )
            == "resolved"
        )


def test_unknown_senses_and_legacy_evidence_not_false(tmp_path, package, context, request_for):
    with StateMachine(tmp_path / "state.db") as sm:
        created = sm.create_session(request_for(), package, context=context)
        assert (
            sm.project_event(
                created.session_id, 1, package, context=context, judgment=synthetic_notice
            )
            == "resolved"
        )
        assert (
            sm.project_event(
                created.session_id, 2, package, context=context, judgment=synthetic_notice
            )
            == "unresolved"
        )
        sm.grant_npc_access(
            created.session_id, "npc.caretaker", "agent:npc.caretaker", package, context=context
        )
        assert (
            sm.get_scene_view(created.session_id, package, context=npc_context(created)).status
            == "unresolved"
        )
        sm._conn.execute("DELETE FROM event_evidence WHERE event_seq=2")
        assert (
            sm.project_event(
                created.session_id, 2, package, context=context, judgment=synthetic_notice
            )
            == "unresolved"
        )
        assert sm.perception_cursor(created.session_id, package, context=context) == 1


def test_concurrent_projection_deduplicates(tmp_path, perception_package, context, request_for):
    database = tmp_path / "state.db"
    with StateMachine(database) as sm:
        created, seq = prepare(sm, perception_package, context, request_for)

    def project(_):
        with StateMachine(database) as sm:
            return sm.project_event(
                created.session_id,
                seq,
                perception_package,
                context=context,
                judgment=synthetic_notice,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert set(executor.map(project, (1, 2))) <= {"resolved", "already_complete"}
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_observations WHERE event_seq=?", (seq,)
            ).fetchone()[0]
            == 2
        )


def test_grant_validation_and_session_isolation(tmp_path, perception_package, context, request_for):
    package = perception_package
    with StateMachine(tmp_path / "state.db") as sm:
        first, _ = prepare(sm, package, context, request_for)
        other = sm.create_session(
            create_request(request_for, package, "other.session"), package, context=context
        )
        for observer in (
            "missing",
            sm.read_session(first.session_id, package, context=context).pc_actor_id,
        ):
            with pytest.raises(StateMachineError):
                sm.grant_npc_access(first.session_id, observer, "agent", package, context=context)
        with pytest.raises(StateMachineError):
            sm.grant_npc_access(first.session_id, "npc.caretaker", "", package, context=context)
        with pytest.raises(StateMachineError):
            sm.grant_npc_access(
                first.session_id,
                "npc.caretaker",
                "agent",
                package,
                context=TrustedContext("intruder", context.player_id),
            )
        with pytest.raises(StateMachineError):
            sm.read_observations(other.session_id, package, context=npc_context(first))
        assert (
            sm.get_scene_view(
                first.session_id, package, context=npc_context(first, "npc.attendant")
            ).visible_actor_ids
            == ()
        )


def test_known_occlusion_all_false_without_judgment(
    tmp_path, perception_package, context, request_for
):
    package = uncertain_package(perception_package, tmp_path, visibility="blocked")
    with StateMachine(tmp_path / "state.db") as sm:
        created, seq = prepare(sm, package, context, request_for)

        def forbidden(request):
            pytest.fail("Known occlusion is not a JEV question")

        assert (
            sm.project_event(created.session_id, seq, package, context=context, judgment=forbidden)
            == "resolved"
        )
        assert (
            sm._conn.execute(
                "SELECT COUNT(*) FROM event_observations WHERE event_seq=?", (seq,)
            ).fetchone()[0]
            == 0
        )


def test_hidden_flag_activity_does_not_reveal_actor(
    tmp_path, perception_package, context, request_for
):
    data = yaml.safe_load((tmp_path / "perception.yaml").read_text("utf-8"))
    data["events"] = []
    data["scenes"][0]["event_definition_ids"] = []
    path = tmp_path / "hidden.yaml"
    path.write_text(yaml.safe_dump(data), "utf-8")
    package = load_adventure_package(path).package
    with StateMachine(tmp_path / "state.db") as sm:
        created = sm.create_session(create_request(request_for, package), package, context=context)
        sm.grant_npc_access(
            created.session_id, "npc.caretaker", "agent:npc.caretaker", package, context=context
        )
        project_prefix(sm, created.session_id, package, context)
        before = sm.read_observations(created.session_id, package, context=npc_context(created))
        receipt = sm.submit_command(
            make_command(sm, created, package, context), package, context=context
        )
        assert (
            sm.project_event(
                created.session_id,
                receipt.event_seqs[0],
                package,
                context=context,
                judgment=synthetic_notice,
            )
            == "resolved"
        )
        assert (
            sm.read_observations(created.session_id, package, context=npc_context(created))
            == before
        )
        view = sm.get_scene_view(created.session_id, package, context=npc_context(created))
        assert (
            sm.read_session(created.session_id, package, context=context).pc_actor_id
            in view.visible_actor_ids
        )
        assert not hasattr(view, "last_actor") and not hasattr(view, "event_type")


def test_fresh_process_reads_real_durable_inbox(tmp_path, perception_package, context, request_for):
    database = tmp_path / "state.db"
    with StateMachine(database) as sm:
        created, _ = prepare(sm, perception_package, context, request_for)
        project_prefix(sm, created.session_id, perception_package, context)
        expected = sm.read_observations(
            created.session_id, perception_package, context=npc_context(created)
        )
    code = """
import json, sys
from dataclasses import asdict
from trpg_state_machine import StateMachine, load_adventure_package
from trpg_state_machine.perception import NPCContext
package = load_adventure_package(sys.argv[2]).package
with StateMachine(sys.argv[1]) as sm:
    observations = sm.read_observations(sys.argv[3], package,
        context=NPCContext('agent:npc.caretaker', sys.argv[3], 'npc.caretaker'))
    print(json.dumps([asdict(o) for o in observations]))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(database),
            str(tmp_path / "perception.yaml"),
            created.session_id,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == json.loads(json.dumps([asdict(o) for o in expected]))


def test_invalid_senses_refused(tmp_path, perception_package):
    data = yaml.safe_load((tmp_path / "perception.yaml").read_text("utf-8"))
    data["actors"][0]["senses"]["visual"] = 1
    path = tmp_path / "bad_senses.yaml"
    path.write_text(yaml.safe_dump(data), "utf-8")
    assert load_adventure_package(path).status == "invalid"


@pytest.mark.parametrize("seq", [True, 0])
def test_event_sequence_types(tmp_path, package, context, request_for, seq):
    with StateMachine(tmp_path / "state.db") as sm:
        created = sm.create_session(request_for(), package, context=context)
        with pytest.raises(StateMachineError) as error:
            sm.project_event(
                created.session_id, seq, package, context=context, judgment=synthetic_notice
            )
        assert error.value.code == "invalid_event"
        with pytest.raises(StateMachineError) as error:
            sm.deliver_event(
                created.session_id, seq, package, context=context, sink=lambda event: None
            )
        assert error.value.code == "invalid_event"
