"""Real Schema38 producer, explicit upgrade, commit failure and snapshot isolation."""

import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from lock_support import CRED, lock_loader
from test_dnd_ledger_scope import stored_state
from test_world_containers import BOX, container_data
from world_support import setup

from trpg_state_machine import StateMachine, StateMachineError
from trpg_state_machine.instance_data import InstanceData
from trpg_state_machine.package import _validate


def test_real_schema38_save_preserves_every_fact_and_requires_explicit_upgrade(tmp_path, context):
    root = Path(__file__).resolve().parents[1]
    old = tmp_path / "producer"
    source = subprocess.check_output(["git", "archive", "--format=zip", "f826cb8", "src"], cwd=root)
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        archive.extractall(old)
    destination = tmp_path / "save"
    destination.mkdir()
    script = """
import json, sys
from pathlib import Path
import trpg_state_machine
assert Path(trpg_state_machine.__file__).resolve().is_relative_to(Path(sys.argv[2]).resolve())
from trpg_state_machine import TrustedContext
from world_support import setup
from test_world_containers import container_data, BOX
from lock_support import CRED
c = TrustedContext('authenticated:alice', 'player.alice')
s, sid, p, db = setup(Path(sys.argv[1]), c, container_data())
assert s._conn.execute('PRAGMA user_version').fetchone()[0] == 38
with s._transaction(write=True):
    obj = s._world_facts.object(sid, CRED)
    s._world_facts.transfer_item(sid, obj, container_id=BOX)
    s._characters.sync_world_inventory(sid)
s.close()
print(json.dumps({'session_id': sid, 'database': str(db)}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(destination), str(old / "src")],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(old / "src"), str(root / "tests")))},
    )
    identities = json.loads(result.stdout)
    db, sid = identities["database"], identities["session_id"]
    before = stored_state(db)
    package = _validate(container_data())
    with StateMachine(db, dnd_loader=lock_loader()) as sm:
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 39
        after_schema = stored_state(db)
        assert after_schema["instance_configs"] == ()
        assert {key: after_schema[key] for key in before} == before
        with pytest.raises(StateMachineError) as error:
            sm.resume_session(sid, package, context=context)
        assert error.value.code == "incomplete_instance_config"
        sm.upgrade_instance_configs(sid, package, context=context)
        assert sm._world_facts.object(sid, CRED)["container_id"] == BOX
        after = stored_state(db)
        assert {key: after[key] for key in before} == before
        saved = sm.save_session(sid, package, context=context)
        assert sm.upgrade_instance_configs(sid, package, context=context) == saved
        assert stored_state(db) == after


def test_upgrade_commit_failure_rolls_back_all_config_records(tmp_path, context, monkeypatch):
    sm, sid, package, db = setup(tmp_path, context, container_data())
    with sm:
        sm._conn.execute("DELETE FROM instance_configs")
        before = stored_state(db)
        put = InstanceData.put
        injected = False

        def deferred_fault(self, *args):
            nonlocal injected
            put(self, *args)
            if not injected:
                injected = True
                self.conn.execute("PRAGMA defer_foreign_keys=ON")
                self.conn.execute(
                    "INSERT INTO instance_configs VALUES ('session.missing','actor','npc.x','{}')"
                )

        with monkeypatch.context() as patch:
            patch.setattr(InstanceData, "put", deferred_fault)
            with pytest.raises(StateMachineError) as error:
                sm.upgrade_instance_configs(sid, package, context=context)
            assert error.value.code == "storage_error"
        assert stored_state(db) == before
        sm.upgrade_instance_configs(sid, package, context=context)
        sm.save_session(sid, package, context=context)


def test_containment_reads_candidate_and_discards_rolled_back_weights(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, container_data())
    with sm:
        svc = sm._objects.containers
        assert svc.total_weight(BOX, svc._rows(sid), package) == 2
        with pytest.raises(RuntimeError), sm._transaction(write=True):
            sm._world_facts.transfer_item(sid, sm._world_facts.object(sid, CRED), container_id=BOX)
            candidate = svc._rows(sid)
            assert svc.total_weight(BOX, candidate, package) == 3
            raise RuntimeError("rollback")
        assert svc.total_weight(BOX, svc._rows(sid), package) == 2
        assert sm._world_facts.object(sid, CRED)["container_id"] is None
