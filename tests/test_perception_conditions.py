"""Real pinned Engine admission and committed Event-time SQLite regressions."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from copy import deepcopy
from dataclasses import replace

import pytest
import yaml
from dnd5e_engine.activities.passive_stats import CombatantSenses
from dnd5e_engine.rules.conditions import conditions_auto_fail_check
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd_support import NPC, create
from modern_support import modern_data, modern_loader
from test_events import event_data
from test_migrations import stored_rows
from test_npc_state import SOURCE

from trpg_state_machine import (
    ObjectState,
    SetObjectState,
    SetSceneFlag,
    StateMachine,
    StateMachineError,
    TypedCommand,
    load_adventure_package,
)
from trpg_state_machine.package import Signal, SignalOrigin, SignalScope, SignalStimulus, _validate
from trpg_state_machine.perception import JudgmentResult, NPCContext, SignalJudgment
from trpg_state_machine.perception_conditions import (
    PerceptionOutcome,
    discover_signals,
    judge_signal,
    signal_environment,
)


@pytest.fixture
def sensory_inputs(package_data):
    data = modern_data(package_data, modern_loader())
    for name in ("combat_foundation", "noncombat_foundation"):
        data["rule_bindings"][name]["snapshot"]["scene_state"]["grid"]["width"] = 5
    _validate(data)  # Complete real Engine DTOs, not a fake RuleEvaluationResult.
    snapshot = data["rule_bindings"]["combat_foundation"]["snapshot"]
    actor = snapshot["character_states"][1]
    observer = {
        "actor_id": NPC,
        "current_scene_id": "scene.platform",
        "senses": {"visual": True, "auditory": True},
        "mechanical": {
            "conditions": [],
            "senses": actor["senses"],
            "traits": [],
            "cell": "1,0",
            "is_alive": True,
            "dead_recorded": False,
        },
    }
    environment = {
        "grid": snapshot["scene_state"]["grid"],
        "origin_valid": True,
        "origin_cell": "3,0",
        "source_conditions": [],
        "unsupported_effects": False,
    }
    signal = Signal(
        "signal.test",
        "visual",
        "A plain activity.",
        SignalScope("scene.platform", "clear"),
        SignalOrigin("scene.platform", cell="3,0"),
        SignalStimulus("ordinary_activity"),
    )
    return signal, observer, environment


@pytest.mark.parametrize(
    "case,outcome,reason",
    [
        ("bright", "unresolved", "mechanically_visible_notice_unknown"),
        ("dim", "unresolved", "mechanically_visible_notice_unknown"),
        ("dark", "negative", "engine_visual_path_unavailable"),
        ("darkvision", "unresolved", "mechanically_visible_notice_unknown"),
        ("short_darkvision", "negative", "engine_visual_path_unavailable"),
        ("truesight", "unresolved", "mechanically_visible_notice_unknown"),
        ("fog", "negative", "engine_visual_path_unavailable"),
        ("blindsight", "unresolved", "mechanically_visible_notice_unknown"),
        ("wall", "negative", "engine_visual_path_unavailable"),
        ("total_cover", "negative", "engine_visual_path_unavailable"),
        ("blinded", "negative", "engine_sight_auto_fail"),
        ("blinded_truesight", "negative", "engine_sight_auto_fail"),
        ("blinded_blindsight", "unresolved", "blindsight_condition_interaction"),
        ("emission_dark", "unresolved", "light_emission_rules_unavailable"),
        ("emission_wall", "negative", "engine_line_of_sight_blocked"),
        ("deafened", "negative", "engine_hearing_auto_fail"),
        ("sound", "unresolved", "auditory_notice_rules_unavailable"),
        ("sound_wall", "unresolved", "auditory_notice_rules_unavailable"),
        ("sound_blocked", "negative", "declared_channel_blocked"),
        ("no_sense", "negative", "declared_sense_unavailable"),
        ("missing_origin", "unresolved", "missing_origin_or_stimulus"),
        ("missing_stimulus", "unresolved", "missing_origin_or_stimulus"),
        ("missing_state", "unresolved", "missing_mechanical_state"),
        ("missing_senses", "unresolved", "missing_sensory_capability"),
        ("missing_position", "unresolved", "missing_spatial_evidence"),
        ("missing_grid", "unresolved", "invalid_mechanical_evidence"),
        ("invalid_position", "unresolved", "invalid_spatial_evidence"),
        ("unknown_condition", "unresolved", "unknown_condition"),
        ("source_invisible", "unresolved", "unsupported_condition_interaction"),
        ("effects", "unresolved", "unsupported_environment_effects"),
        ("traits", "unresolved", "unsupported_sensory_traits"),
        ("other_channel", "unresolved", "unsupported_channel"),
        ("sense_conflict", "unresolved", "declared_vision_special_sense_conflict"),
        ("not_alive", "unresolved", "inactive_observer_requires_temporal_evidence"),
        ("dead_record", "unresolved", "inactive_observer_requires_temporal_evidence"),
    ],
)
def test_actual_rule_branches(sensory_inputs, case, outcome, reason):
    signal, observer, env = deepcopy(sensory_inputs)
    mechanical = observer["mechanical"]
    grid = env["grid"]
    if case in ("dark", "darkvision", "short_darkvision", "truesight", "emission_dark"):
        grid["default_lighting"] = "dark"
    if case == "dim":
        grid["default_lighting"] = "dim"
    if case in ("darkvision", "short_darkvision", "truesight"):
        mechanical["senses"]["truesight" if case == "truesight" else "darkvision"] = (
            5 if case == "short_darkvision" else 60
        )
    if case in ("fog", "blindsight"):
        grid["obscurement_cells"] = {"2,0": "heavy"}
    if case == "blindsight":
        mechanical["senses"]["blindsight"] = 60
    if case in ("wall", "sound_wall", "emission_wall"):
        grid["wall_segments"] = [{"x1": 2, "y1": 0, "x2": 2, "y2": 1}]
    if case == "total_cover":
        grid["cover_cells"] = {"3,0": "total"}
    if case.startswith("blinded"):
        mechanical["conditions"] = ["blinded"]
        if case != "blinded":
            mechanical["senses"][case.split("_")[1]] = 60
    if case.startswith("emission"):
        signal = replace(signal, stimulus=SignalStimulus("light_emission"))
    if case in ("sound", "sound_wall", "sound_blocked", "deafened"):
        signal = replace(signal, channel="auditory", stimulus=SignalStimulus("sound"))
        if case == "deafened":
            mechanical["conditions"] = ["deafened"]
        if case == "sound_blocked":
            signal = replace(signal, scope=replace(signal.scope, visibility="blocked"))
    if case == "no_sense":
        observer["senses"]["visual"] = False
    if case == "missing_origin":
        signal = replace(signal, origin=None)
    if case == "missing_stimulus":
        signal = replace(signal, stimulus=None)
    if case == "missing_state":
        observer["mechanical"] = None
    if case == "missing_senses":
        observer["senses"] = None
    if case == "missing_position":
        mechanical["cell"] = None
    if case == "missing_grid":
        env["grid"] = None
    if case == "invalid_position":
        mechanical["cell"] = "999,0"
    if case == "unknown_condition":
        mechanical["conditions"] = ["future_condition"]
    if case == "source_invisible":
        env["source_conditions"] = ["invisible"]
    if case == "effects":
        env["unsupported_effects"] = True
    if case == "traits":
        mechanical["traits"] = [{"unknown_sensory_rule": True}]
    if case == "other_channel":
        signal = replace(signal, channel="telepathy")
    if case == "sense_conflict":
        observer["senses"]["visual"] = False
        mechanical["senses"]["blindsight"] = 60
    if case == "not_alive":
        mechanical["is_alive"] = False
    if case == "dead_record":
        mechanical["dead_recorded"] = True
    decision = judge_signal(signal, observer, env)
    assert (decision.outcome.value, decision.reason) == (outcome, reason)
    if reason == "engine_visual_path_unavailable":
        assert not GridTopology(GridScene(**grid)).can_see(
            mechanical["cell"], env["origin_cell"], CombatantSenses(**mechanical["senses"])
        )
    if reason in ("engine_sight_auto_fail", "engine_hearing_auto_fail"):
        assert conditions_auto_fail_check(
            mechanical["conditions"], "sight" if signal.channel == "visual" else "hearing"
        )
    if reason == "mechanically_visible_notice_unknown":
        assert GridTopology(GridScene(**grid)).can_see(
            mechanical["cell"], env["origin_cell"], CombatantSenses(**mechanical["senses"])
        )
        assert decision.outcome != PerceptionOutcome.POSITIVE


@pytest.mark.parametrize("fault", ["extra", "mismatch", "entity", "scene", "cell", "detection"])
def test_signal_schema_strict(tmp_path, package_data, fault):
    definition = event_data()
    raw = definition["signals"][0]
    raw.update(origin={"scene_id": "scene.platform"}, stimulus={"kind": "sound"})
    if fault == "extra":
        raw["stimulus"]["intensity"] = 100
    elif fault == "mismatch":
        raw["stimulus"]["kind"] = "light_emission"
    elif fault == "entity":
        raw["origin"]["entity_id"] = "npc.foreign"
    elif fault == "scene":
        raw["origin"]["scene_id"] = "scene.workshop"
    elif fault == "cell":
        raw["origin"]["cell"] = True
    else:
        raw["detection"] = True
    package_data["events"] = [definition]
    package_data["scenes"][0]["event_definition_ids"] = ["event.open_gate"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(package_data), "utf-8")
    assert load_adventure_package(path).status == "invalid"


def object_command(sm, sid, package, context):
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        "open.gate",
        state.pc_actor_id,
        "set_object_state",
        SetObjectState("scene.platform", "object.platform_gate", ObjectState.OPEN),
        state.world_version,
    )


def test_per_19_22_01_dark_scene_no_false_positive_sqlite_restart(tmp_path, context):
    data = yaml.safe_load(SOURCE.read_text("utf-8"))
    assets = modern_loader()
    data = modern_data(data, assets)
    del data["rule_bindings"]["combat_foundation"]
    data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"][
        "default_lighting"
    ] = "dark"
    package = _validate(data)
    db = tmp_path / "dark.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        sm.grant_npc_access(sid, NPC, "observer", package, context=context)
        npc = NPCContext("observer", sid, NPC)
        assert sm.get_scene_view(sid, package, context=npc).status == "unresolved"
        command = object_command(sm, sid, package, context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        saved = sm.save_session(sid, package, context=context)
    with StateMachine(db, dnd_loader=assets) as sm:
        assert sm.resume_session(sid, package, context=context) == saved
        for event in sm.read_events(sid, package, context=context):
            expected = "unresolved" if event.signals else "resolved"
            assert (
                sm.project_event(
                    sid,
                    event.event_seq,
                    package,
                    context=context,
                    judgment=lambda _: pytest.fail(
                        "Missing dark-scene mechanical evidence is not a noticing question"
                    ),
                )
                == expected
            )
            assert sm.project_event(sid, event.event_seq, package, context=context) in (
                expected,
                "already_complete",
            )
            sm.deliver_event(sid, event.event_seq, package, context=context, sink=lambda _: None)
        assert sm.read_observations(sid, package, context=npc) == ()
        assert sm.perception_cursor(sid, package, context=context) == 1
        assert sm.delivery_cursor(sid, package, context=context) == saved.event_seq
        before = stored_rows(db)
        assert sm.submit_command(command, package, context=context) == receipt
        assert stored_rows(db) == before
    with sqlite3.connect(db) as conn:
        for table in (
            "event_observations",
            "npc_inbox",
            "npc_observation_work",
            "observation_signal_sources",
        ):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        evidence = json.loads(
            conn.execute("SELECT evidence_json FROM event_evidence WHERE event_seq=3").fetchone()[0]
        )
        assert evidence["schema"] == "event-evidence/3"
        assert evidence["scenes"]["scene.platform"]["grid"]["default_lighting"] == "dark"
        pending = conn.execute(
            "SELECT event_seq FROM event_outbox WHERE consumer='perception' "
            "AND done=0 ORDER BY event_seq"
        ).fetchall()
        assert pending == [(2,), (3,)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_scope_discovery_has_no_sensory_judgment(sensory_inputs):
    signal, observer, _ = sensory_inputs
    observer["senses"] = {"visual": False, "auditory": False}
    assert discover_signals((signal,), observer) == (signal,)
    assert not discover_signals((signal,), {**observer, "current_scene_id": "scene.workshop"})


def sensory_event_package(base, *, dark=False, deafened=False, blinded=False):
    assets = modern_loader()
    data = modern_data(base, assets)
    for name in ("combat_foundation", "noncombat_foundation"):
        snap = data["rule_bindings"][name]["snapshot"]
        snap["scene_state"]["grid"]["default_lighting"] = "dark" if dark else "bright"
        if deafened:
            snap["character_states"][1]["conditions"] = [
                {
                    "condition": "deafened",
                    "source_entity_id": "implied:deafened",
                    "scope": "session",
                    "duration_rounds": None,
                    "save_dc": None,
                    "applied_round": 0,
                    "exhaustion_level": 1,
                    "source_effect_id": None,
                }
            ]
    definition = event_data()
    sound = definition["signals"][0]
    sound.update(origin={"scene_id": "scene.platform", "cell": "2,0"}, stimulus={"kind": "sound"})
    visual = deepcopy(sound)
    visual.update(
        signal_id="signal.activity",
        channel="visual",
        content="A plain activity.",
        stimulus={"kind": "ordinary_activity"},
    )
    definition["signals"].append(visual)
    data["events"] = [definition]
    data["scenes"][0]["event_definition_ids"] = ["event.open_gate"]
    if dark or deafened or blinded:
        # The attack evaluator correctly refuses such initial combat snapshots.
        # This perception test uses the existing noncombat authority instead.
        del data["rule_bindings"]["combat_foundation"]
        if blinded:
            records = data["rule_bindings"]["noncombat_foundation"]["snapshot"]["character_states"][
                1
            ]["conditions"]
            record = deepcopy(records[0])
            record["condition"] = "blinded"
            records.append(record)
    return _validate(data), assets


def fire(sm, sid, package, context):
    snapshot = sm.read_session(sid, package, context=context)
    command = TypedCommand(
        sid,
        "fire.signal",
        snapshot.pc_actor_id,
        "set_scene_flag",
        SetSceneFlag("scene.platform", "flag.lamp_checked", True),
        snapshot.world_version,
    )
    receipt = sm.submit_command(command, package, context=context)
    assert receipt.status == "committed"
    return receipt.event_seqs[-1]


def test_mixed_real_admission_judgment_aggregation_reasons_atomic_retry(
    tmp_path, package_data, context
):
    package, assets = sensory_event_package(package_data, deafened=True)
    db = tmp_path / "mixed.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        seq = fire(sm, sid, package, context)
        sm.grant_npc_access(sid, NPC, "observer", package, context=context)

        def decide(request):
            assert not sm._conn.in_transaction
            assert [s.signal_id for s in request.signals] == ["signal.activity"]
            assert request.evidence["observer"]["mechanical"]["conditions"] == ["deafened"]
            # Deafened only blocks hearing: it need not prevent visual admission.
            return JudgmentResult((SignalJudgment("signal.activity", True),))

        assert sm.project_event(sid, seq, package, context=context) == "unresolved"
        assert sm._conn.execute("SELECT count(*) FROM event_observations").fetchone()[0] == 0
        assert sm.project_event(sid, seq, package, context=context, judgment=decide) == "resolved"
        row = sm._conn.execute(
            "SELECT decisions_json FROM perception_results WHERE event_seq=?", (seq,)
        ).fetchone()
        audit = json.loads(row[0])
        assert audit["answers"] == {"signal.chime": False, "signal.activity": True}
        assert audit["reasons"]["signal.chime"] == {
            "state": "negative",
            "reason": "engine_hearing_auto_fail",
        }
        assert audit["reasons"]["signal.activity"] == {
            "state": "positive",
            "reason": "validated_judgment",
        }
        assert sm.perception_cursor(sid, package, context=context) == 0
    before = stored_rows(db)
    with StateMachine(db, dnd_loader=assets) as sm:

        def forbidden(_):
            pytest.fail("Selected judgment must never be recomputed")

        assert (
            sm.project_event(sid, seq, package, context=context, judgment=forbidden)
            == "already_complete"
        )
    assert stored_rows(db) == before
    with sqlite3.connect(db) as conn:
        obs = conn.execute("SELECT observation_id,content_json FROM event_observations").fetchall()
        assert len(obs) == 1
        assert json.loads(obs[0][1]) == [{"channel": "visual", "content": "A plain activity."}]
        assert conn.execute("SELECT observation_id FROM npc_inbox").fetchall() == [(obs[0][0],)]
        assert conn.execute("SELECT observation_id FROM npc_observation_work").fetchall() == [
            (obs[0][0],)
        ]
        assert conn.execute("SELECT signal_id FROM observation_signal_sources").fetchall() == [
            ("signal.activity",)
        ]


def test_event_time_conditions_and_darkness_survive_current_change(tmp_path, package_data, context):
    package, assets = sensory_event_package(package_data, dark=True, deafened=True, blinded=True)
    db = tmp_path / "history.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        seq = fire(sm, sid, package, context)
        saved = sm._conn.execute(
            "SELECT evidence_json FROM event_evidence WHERE event_seq=?", (seq,)
        ).fetchone()[0]
        evidence = json.loads(saved)
        observer = next(a for a in evidence["actors"] if a["actor_id"] == NPC)
        event = sm._read_event(sid, seq)
        for signal in event.signals:
            assert (
                judge_signal(signal, observer, signal_environment(signal, evidence)).outcome
                == PerceptionOutcome.NEGATIVE
            )
        # Fault-inject a different CURRENT environment/condition set. Projection
        # must not read either table, much less use them to backfill history.
        sm._conn.execute(
            "UPDATE mechanical_scenes SET "
            "state_json=json_set(state_json,'$.grid.default_lighting','bright')"
        )
        sm._conn.execute(
            "UPDATE mechanical_actors SET state_json=json_set(state_json,'$.conditions',json('[]'))"
        )
    with StateMachine(db, dnd_loader=assets) as sm:

        def authorizer(action, table, *args):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ
                and table in ("mechanical_actors", "mechanical_scenes")
                else sqlite3.SQLITE_OK
            )

        sm._conn.set_authorizer(authorizer)
        assert sm.project_event(sid, seq, package, context=context) == "resolved"
        sm._conn.set_authorizer(None)
        assert (
            sm._conn.execute(
                "SELECT evidence_json FROM event_evidence WHERE event_seq=?", (seq,)
            ).fetchone()[0]
            == saved
        )
        result = json.loads(
            sm._conn.execute(
                "SELECT decisions_json FROM perception_results WHERE event_seq=?", (seq,)
            ).fetchone()[0]
        )
        assert set(result["answers"].values()) == {False}
        assert result["reasons"]["signal.activity"]["reason"] == "engine_sight_auto_fail"
        assert result["reasons"]["signal.chime"]["reason"] == "engine_hearing_auto_fail"
        assert sm._conn.execute("SELECT count(*) FROM event_observations").fetchone()[0] == 0


def test_real_legacy_save_missing_evidence_never_backfills(tmp_path, context):
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "4745f17", "src"], check=True, capture_output=True
    ).stdout
    old = tmp_path / "old"
    with zipfile.ZipFile(io.BytesIO(archive)) as files:
        files.extractall(old)
    db = tmp_path / "old.db"
    code = """
import sys
from trpg_state_machine import (
 StateMachine, CreateSessionRequest, PlayerSelection, TrustedContext, load_adventure_package,
)
p=load_adventure_package(sys.argv[2]).package
c=TrustedContext('authenticated:alice','player.alice')
r=CreateSessionRequest('legacy',p.adventure_id,p.package_version,p.package_digest,(PlayerSelection(c.player_id,'pc_template.observer'),))
with StateMachine(sys.argv[1]) as sm:
 print(sm.create_session(r,p,context=c).session_id)
"""
    env = dict(os.environ, PYTHONPATH=str(old / "src"))
    process = subprocess.run(
        [sys.executable, "-c", code, str(db), str(SOURCE)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    sid = process.stdout.strip()
    package = load_adventure_package(SOURCE).package
    before = stored_rows(db)
    with StateMachine(db) as sm:
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 18
        assert sm.project_event(sid, 1, package, context=context) == "resolved"
        assert (
            sm.project_event(
                sid,
                2,
                package,
                context=context,
                judgment=lambda _: pytest.fail("Legacy evidence is incomplete"),
            )
            == "unresolved"
        )
        assert sm.perception_cursor(sid, package, context=context) == 1
        assert sm._conn.execute("SELECT count(*) FROM event_observations").fetchone()[0] == 0
    after = stored_rows(db)
    for table in before:
        if table not in ("event_outbox", "projection_cursors", "npc_completion_cursors"):
            assert after[table] == before[table]


def test_event_evidence_write_failure_rolls_back_world_receipt_outbox(
    tmp_path, package_data, context
):
    package, assets = sensory_event_package(package_data)
    db = tmp_path / "rollback.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        command = object_command(sm, sid, package, context)
        before = stored_rows(db)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail BEFORE INSERT ON event_evidence "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "storage_error"
        assert stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER fail")
        assert sm.submit_command(command, package, context=context).status == "committed"


@pytest.mark.parametrize(
    "fault", ["missing_actors", "schema", "binding", "extra", "missing_traits"]
)
def test_invalid_evidence_cannot_be_overridden_or_completed(tmp_path, package_data, context, fault):
    package, assets = sensory_event_package(package_data)
    db = tmp_path / "invalid.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        seq = sm.submit_command(
            object_command(sm, sid, package, context), package, context=context
        ).event_seqs[0]
        data = json.loads(
            sm._conn.execute(
                "SELECT evidence_json FROM event_evidence WHERE event_seq=?", (seq,)
            ).fetchone()[0]
        )
        if fault == "missing_actors":
            del data["actors"]
        elif fault == "schema":
            data["schema"] = "event-evidence/future"
        elif fault == "binding":
            data["ruleset_binding"]["data_revision"] = "untrusted"
        elif fault == "missing_traits":
            del next(a for a in data["actors"] if a["actor_id"] == NPC)["mechanical"]["traits"]
        else:
            data["arbitrary_fact"] = True
        sm._conn.execute(
            "UPDATE event_evidence SET evidence_json=? WHERE event_seq=?", (json.dumps(data), seq)
        )
        assert (
            sm.project_event(
                sid,
                seq,
                package,
                context=context,
                judgment=lambda _: pytest.fail("Invalid evidence is not JEV input"),
            )
            == "unresolved"
        )
        assert sm._conn.execute("SELECT count(*) FROM event_observations").fetchone()[0] == 0
        assert (
            sm._conn.execute(
                "SELECT done FROM event_outbox WHERE event_seq=? AND consumer='perception'", (seq,)
            ).fetchone()[0]
            == 0
        )


def test_unknown_stored_signal_field_is_structured_error_without_writes(
    tmp_path, package_data, context
):
    package, assets = sensory_event_package(package_data)
    db = tmp_path / "invalid.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        seq = sm.submit_command(
            object_command(sm, sid, package, context), package, context=context
        ).event_seqs[0]
        data = json.loads(
            sm._conn.execute(
                "SELECT signals_json FROM event_records WHERE event_seq=?", (seq,)
            ).fetchone()[0]
        )
        data[0]["detection"] = True
        sm._conn.execute(
            "UPDATE event_records SET signals_json=? WHERE event_seq=?", (json.dumps(data), seq)
        )
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as error:
            sm.project_event(sid, seq, package, context=context)
        assert error.value.code == "invalid_event"
        assert stored_rows(db) == before
