"""Count real SM loading/evaluation work, without replacing RE implementations."""

import json
import os
import random
import subprocess
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from dnd5e_engine import evaluation_ruleset as re_rules
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.frozen import FrozenAssetLoader
from lock_support import CRED, DOOR, A
from pydantic import BaseModel
from spatial_support import create
from test_dnd_ledger_scope import stored_state
from test_lockable_passages import acquire
from test_openable_checks import check_data
from test_real_checks import check_command
from world_support import command

from trpg_state_machine import PickLock, StateMachine, StateMachineError
from trpg_state_machine.package import _validate


def test_real_predecessor_save_is_auditable_but_never_silently_repinned(
    tmp_path, context, pre_frozen_python
):
    root = Path(__file__).resolve().parents[1]
    historical = pre_frozen_python.parents[2]
    producer = r"""
import json, sys
from pathlib import Path
import trpg_state_machine
assert Path(trpg_state_machine.__file__).resolve().is_relative_to(Path(sys.argv[3]).resolve())
sys.path.insert(0, sys.argv[1])
from test_openable_checks import setup_check, check_data
from world_support import command
from lock_support import A, DOOR, CRED
from trpg_state_machine import PickLock, TrustedContext
directory = Path(sys.argv[2])
context = TrustedContext("authenticated:alice", "player.alice", ("npc.caretaker", "npc.attendant"))
sm, sid, package, _ = setup_check(directory, context, dc=100)
with sm:
    assert package.ruleset_binding.evaluator_version == "dnd5e-evaluation/36"
    op = PickLock(A, DOOR, "lock.outer", CRED, "0,0")
    cmd = command(sm, sid, package, context, op, "old.pick")
    assert sm.submit_command(cmd, package, context=context).status == "committed"
    sm.save_session(sid, package, context=context)
data, _ = check_data(dc=100)
payload = json.dumps({"data": data, "sid": sid})
(directory / "predecessor.json").write_text(payload, encoding="utf-8")
"""
    environment = dict(os.environ, PYTHONPATH=str(historical / "src"), PYTHONUTF8="1")
    made = subprocess.run(
        [
            str(pre_frozen_python),
            "-c",
            producer,
            str(root / "tests"),
            str(tmp_path),
            str(historical),
        ],
        cwd=historical,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert made.returncode == 0, made.stdout + made.stderr
    payload = json.loads((tmp_path / "predecessor.json").read_text("utf-8"))
    package, sid = _validate(payload["data"]), payload["sid"]
    _, source = check_data(dc=100)
    database = tmp_path / "check.db"
    with StateMachine(database, dnd_loader=source) as sm:
        before = stored_state(database)
        assert (
            sm.read_session(sid, package, context=context).ruleset_binding.evaluator_version
            == "dnd5e-evaluation/36"
        )
        # Existing explicit instance upgrade remains available, never an RE repin.
        sm.upgrade_instance_configs(sid, package, context=context)
        assert stored_state(database) == before
        for action in (
            lambda: sm.resume_session(sid, package, context=context),
            lambda: sm.submit_command(
                command(
                    sm,
                    sid,
                    package,
                    context,
                    PickLock(A, DOOR, "lock.outer", CRED, "0,0"),
                    "new.pick",
                ),
                package,
                context=context,
            ),
        ):
            with pytest.raises(StateMachineError) as error:
                action()
            assert error.value.code == "ruleset_binding_mismatch"
            assert stored_state(database) == before


@pytest.fixture
def work(monkeypatch):
    """Observe actual reads, canonical validation, enumeration and RE digest work."""
    counts = Counter()
    read = Path.read_text
    validate = BaseModel.model_validate.__func__
    compute = re_rules._compute_binding

    def read_text(path, *args, **kwargs):
        if path.suffix == ".json" and "canonical" in path.parts:
            counts["files"] += 1
        return read(path, *args, **kwargs)

    def model_validate(cls, *args, **kwargs):
        if cls.__module__.startswith("dnd5e_srd_data.schema."):
            counts["models"] += 1
        return validate(cls, *args, **kwargs)

    def digest(loader):
        counts["digests"] += 1
        return compute(loader)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(BaseModel, "model_validate", classmethod(model_validate))
    monkeypatch.setattr(re_rules, "_compute_binding", digest)
    for cls, key in (
        (BundledAssetLoader, "source_enumerations"),
        (MemoryAssetLoader, "source_enumerations"),
        (FrozenAssetLoader, "frozen_enumerations"),
    ):
        original = cls.list_slugs

        def enumerate_assets(self, category, original=original, key=key):
            counts[key] += 1
            return original(self, category)

        monkeypatch.setattr(cls, "list_slugs", enumerate_assets)
    return counts


def test_default_sm_loader_materializes_once_and_reuses_re_snapshot(tmp_path, monkeypatch, work):
    # Cold process equivalent; restore the pre-test singleton automatically.
    monkeypatch.setattr(re_rules, "_DEFAULT_RULESET", None)
    with StateMachine(tmp_path / "first.db") as sm:
        assets = sm._rules.loader
        assert type(assets) is re_rules.FrozenRuleset
        assert sm._world.loader is assets is re_rules.get_default_ruleset()
        cold = work.copy()
        assert cold["files"] == cold["models"] > 0
        assert cold["digests"] == 1
        assert cold["source_enumerations"] == cold["frozen_enumerations"] == 11
        binding = re_rules.ruleset_binding(assets)
        first = assets.get_item("dagger")
        for _ in range(10):
            item = sm._world.loader.get_item("dagger")
            assert item == first
            re_rules.verify_ruleset(binding, sm._rules.loader)
        # Official RE getters return defensive copies, never mutable shared data.
        first.name = "consumer mutation"
        assert assets.get_item("dagger").name != first.name
        with StateMachine(tmp_path / "second.db") as other:
            assert other._rules.loader is assets
        assert work == cold
        print("default_cold=" + json.dumps(dict(cold), sort_keys=True))
        print("default_steady=" + json.dumps(dict(work - cold), sort_keys=True))


def test_live_compatibility_baseline_vs_sm_frozen_verification(tmp_path, work):
    """Same real bundled corpus and RE API: three mutable verifications vs SM."""
    raw = BundledAssetLoader()
    binding = re_rules.ruleset_binding(raw)
    work.clear()
    for _ in range(3):
        re_rules.verify_ruleset(binding, raw)
    live = work.copy()
    assert live["digests"] == 3 and live["source_enumerations"] == 33
    assert live["files"] == live["models"] > 0
    with StateMachine(tmp_path / "frozen.db", dnd_loader=raw) as sm:
        work.clear()
        for _ in range(3):
            re_rules.verify_ruleset(binding, sm._rules.loader)
        assert not work
    print("live_three_verifications=" + json.dumps(dict(live), sort_keys=True))
    print("frozen_three_verifications={}")


@pytest.mark.parametrize("default", [False, True])
def test_real_sm_openable_checks_availability_and_restart_do_no_corpus_work(
    tmp_path, context, monkeypatch, work, default
):
    data, source = check_data(dc=100)
    if default:
        monkeypatch.setattr(re_rules, "_DEFAULT_RULESET", None)
        source = None
        assets = re_rules.get_default_ruleset()
    else:
        assets = re_rules.freeze_ruleset(source)
    data["manifest"]["ruleset_binding"] = re_rules.ruleset_binding(assets).model_dump(mode="json")
    package = _validate(data)
    gm = replace(context, can_adjudicate_checks=True)
    database = tmp_path / "checks.db"
    results = []
    import trpg_state_machine.rule_execution as execution
    import trpg_state_machine.rules_adapter as adapter

    generic, openable = adapter.evaluate, execution.evaluate_openable

    async def evaluate(request, **kwargs):
        assert kwargs["loader"] is assets
        result = await generic(request, **kwargs)
        results.append(result)
        return result

    async def evaluate_openable(spec, **kwargs):
        assert kwargs["loader"] is assets
        result = await openable(spec, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(adapter, "evaluate", evaluate)
    monkeypatch.setattr(execution, "evaluate_openable", evaluate_openable)
    with StateMachine(database, dnd_loader=None if default else assets) as sm:
        assert sm._world.loader is sm._rules.loader is assets
        cold = work.copy()
        sid = create(sm, package, gm)
        acquire(sm, sid, package, gm, iid=CRED, key="tools")
        actor = sm.read_session(sid, package, context=gm).pc_actor_id
        for index in range(3):
            op = PickLock(A, DOOR, "lock.outer", CRED, "0,0")
            assert sm.query_world_action(sid, actor, op, package, context=gm).status == "available"
            assert (
                sm.submit_command(
                    command(sm, sid, package, gm, op, f"pick.{index}"), package, context=gm
                ).status
                == "committed"
            )
            assert (
                sm.submit_command(
                    check_command(sm, sid, package, gm, f"generic.{index}"), package, context=gm
                ).status
                == "committed"
            )
        saved = sm.save_session(sid, package, context=gm)
        assert asdict(saved.ruleset_binding) == assets.binding.model_dump()
        assert len(results) == 6
        for result in results:
            assert result.schema_version == "engine-evaluation/24"
            assert result.stage_facts.schema_version == "engine-stage-facts/2"
            assert result.stage_facts.coverage == "uncovered"
            assert result.stage_facts.reason == "operation_not_in_stage_profile"
            assert not result.stage_facts.stages and not result.stage_facts.event_references
        assert work == cold
    # A restarted custom runtime rebuilds once; default shares RE's process singleton.
    with StateMachine(database, dnd_loader=source) as restored:
        rebuilt = work.copy()
        assert restored.resume_session(sid, package, context=gm) == saved
        for _ in range(3):
            row = restored._conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (sid,)
            ).fetchone()
            restored._rules._verify_dnd_binding(package, row)
        assert work == rebuilt
        assert rebuilt["digests"] - cold["digests"] == (0 if default else 1)
    print(f"evaluations_default_{default}_steady=" + json.dumps(dict(work - rebuilt)))


def test_source_edit_rebuild_isolated_and_failed_binding_never_changes_session(tmp_path, context):
    data, source = check_data()
    package = _validate(data)
    db = tmp_path / "versions.db"
    with StateMachine(db, dnd_loader=source) as old:
        sid = create(old, package, context)
        saved = old.save_session(sid, package, context=context)
        before = stored_state(db)
        source.get_item("dagger").name += " revised"
        assert old.resume_session(sid, package, context=context) == saved
        with StateMachine(db, dnd_loader=source) as revised:
            assert revised._rules.loader is not old._rules.loader
            assert revised._rules.loader.binding != old._rules.loader.binding
            for action in (
                lambda: revised.resume_session(sid, package, context=context),
                lambda: revised.submit_command(
                    check_command(revised, sid, package, context),
                    package,
                    context=replace(context, can_adjudicate_checks=True),
                ),
            ):
                with pytest.raises(StateMachineError) as error:
                    action()
                assert error.value.code == "ruleset_binding_mismatch"
                assert stored_state(db) == before


def test_conflicting_custom_spell_uuid_fails_before_store_and_can_retry(tmp_path):
    bundled = BundledAssetLoader()
    spell = bundled.get_spell("fireball")
    twin = spell.model_copy(update={"slug": "conflicting-fireball"}, deep=True)
    source = MemoryAssetLoader(spells=[spell, twin])
    db = tmp_path / "conflict.db"
    with pytest.raises(re_rules.RulesetBindingError, match="duplicate effective spell UUID"):
        StateMachine(db, dnd_loader=source)
    assert not db.exists()
    source._spells.pop(twin.slug)
    with StateMachine(db, dnd_loader=source) as sm:
        assert sm._rules.loader.get_spell_by_uuid(spell.foundry_uuid) == spell


def test_changed_bundled_source_files_require_new_freeze(tmp_path, work):
    canonical = tmp_path / "canonical"
    (canonical / "items").mkdir(parents=True)
    path = canonical / "items" / "dagger.json"
    item = BundledAssetLoader().get_item("dagger")
    path.write_text(item.model_dump_json(), encoding="utf-8")
    source = BundledAssetLoader(root=canonical)
    work.clear()
    with StateMachine(tmp_path / "old.db", dnd_loader=source) as old:
        cold = work.copy()
        assert cold["files"] == cold["models"] == cold["digests"] == 1
        item.name += " edited file"
        path.write_text(item.model_dump_json(), encoding="utf-8")
        assert old._rules.loader.get_item("dagger").name != item.name
        assert work == cold
        with StateMachine(tmp_path / "new.db", dnd_loader=source) as new:
            assert new._rules.loader.get_item("dagger").name == item.name
            assert new._rules.loader.binding != old._rules.loader.binding
            assert work["files"] == work["models"] == work["digests"] == 2
        print("custom_file_two_builds=" + json.dumps(dict(work), sort_keys=True))


def test_combat_initialization_availability_and_evaluation_share_frozen_corpus(
    tmp_path, package_data, context, monkeypatch, work
):
    from dnd_support import initialize
    from modern_support import modern_data, modern_loader
    from test_history_free_rules_adapter import attack

    source = modern_loader()
    package = _validate(modern_data(package_data, source, npc_hp=50))
    with StateMachine(tmp_path / "combat.db", dnd_loader=source) as sm:
        frozen = sm._rules.loader
        import trpg_state_machine.rules_adapter as adapter

        query = adapter.query_action_availability
        calls = []

        def availability(request, **kwargs):
            assert kwargs["loader"] is frozen
            calls.append(request)
            return query(request, **kwargs)

        monkeypatch.setattr(adapter, "query_action_availability", availability)
        cold = work.copy()
        sid = create(sm, package, context)
        assert initialize(sm, sid, package, context).status == "committed"
        cmd = attack(sm, sid, package, context)
        for _ in range(3):
            assert (
                sm.query_action_availability(
                    sid, cmd.actor_id, cmd.payload, package, context=context
                ).status
                == "available"
            )
        assert sm.submit_command(cmd, package, context=context).status == "committed"
        assert len(calls) == 3
        assert work == cold


@pytest.mark.parametrize(
    "skill,expertise,tool,jack,reliable,modifier,mode,clamped",
    [
        (True, False, True, False, False, 6, "advantage", False),
        (True, True, True, False, False, 8, "advantage", False),
        (False, False, True, True, False, 6, "normal", False),
        (False, False, True, False, True, 6, "normal", True),
        (False, False, False, False, True, 4, "normal", False),
    ],
)
def test_frozen_openable_preserves_shared_tool_modifiers_and_reliable_talent(
    tmp_path, context, skill, expertise, tool, jack, reliable, modifier, mode, clamped
):
    from dnd5e_engine.evaluation_rng import RNGState

    data, source = check_data(dc=100)
    for foundation in data["rule_bindings"].values():
        actor = foundation["snapshot"]["character_states"][0]
        actor.update(
            dexterity=18,
            proficiency_bonus_override=2,
            skill_proficiencies=["sleight_of_hand"] if skill else [],
            skill_expertise=["sleight_of_hand"] if expertise else [],
            tool_proficiencies=["thieves-tools"] if tool else [],
            jack_of_all_trades=jack,
            reliable_talent=reliable,
        )
        foundation["rng_state"] = RNGState.capture(random.Random(1)).model_dump(mode="json")
    package = _validate(data)
    with StateMachine(tmp_path / "tools.db", dnd_loader=source) as sm:
        sid = create(sm, package, context)
        acquire(sm, sid, package, context, iid=CRED, key="tools")
        op = PickLock(A, DOOR, "lock.outer", CRED, "0,0")
        receipt = sm.submit_command(
            command(sm, sid, package, context, op, "pick"), package, context=context
        )
        assert receipt.status == "committed"
        rolled = sm.read_events(sid, package, context=context)[-1].payload["engine_event"]
        assert rolled["modifier"] == modifier
        assert rolled["advantage"] == mode
        assert rolled["reliable_talent_applied"] is clamped
        assert rolled["succeeded"] is False
        assert rolled["natural"] == (10 if clamped else rolled["rolled_natural"])


@pytest.mark.parametrize("invalid", ["missing", "invented_complete", "records_when_uncovered"])
def test_invalid_stage_coverage_cannot_commit(tmp_path, context, monkeypatch, invalid):
    from test_openable_checks import setup_check

    sm, sid, package, _ = setup_check(tmp_path, context, dc=100)
    with sm:
        import trpg_state_machine.rule_execution as execution

        real = execution.evaluate_openable

        async def corrupt(spec, **kwargs):
            result = await real(spec, **kwargs)
            facts = result.stage_facts
            facts = {
                "missing": None,
                "invented_complete": facts.model_copy(
                    update={"coverage": "complete", "reason": None}
                ),
                "records_when_uncovered": facts.model_copy(update={"final_stage": 0}),
            }[invalid]
            return result.model_copy(update={"stage_facts": facts})

        monkeypatch.setattr(execution, "evaluate_openable", corrupt)
        before = stored_state(tmp_path / "check.db")
        op = PickLock(A, DOOR, "lock.outer", CRED, "0,0")
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(
                command(sm, sid, package, context, op, "bad.stage"), package, context=context
            )
        assert error.value.code == "invalid_rule_result"
        assert stored_state(tmp_path / "check.db") == before
