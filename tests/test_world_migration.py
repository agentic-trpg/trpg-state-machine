"""Real committed Schema25 save upgraded in a new process/runtime."""

import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from lock_support import CRED, DOOR, lock_data, lock_loader
from test_migrations import stored_rows

from trpg_state_machine import StateMachine, TrustedContext
from trpg_state_machine.package import _validate


def test_old_nonempty_save_preserves_facts_keys_receipts_and_adds_no_capabilities(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "old"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "1fa7cea", "src"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as contents:
        contents.extractall(source)
    db = tmp_path / "legacy.db"
    code = (
        "from lock_support import lock_data,lock_loader; "
        "from spatial_support import create; from trpg_state_machine.package import _validate; "
        "from trpg_state_machine import StateMachine,TrustedContext; import sys,json; "
        "p=_validate(lock_data()); c=TrustedContext('authenticated:alice','player.alice'); "
        "s=StateMachine(sys.argv[1],dnd_loader=lock_loader()); sid=create(s,p,c); "
        "v=s._conn.execute('PRAGMA user_version').fetchone()[0]; "
        "print(json.dumps({'sid':sid,'schema':v})); "
        "s.close()"
    )
    old = subprocess.run(
        [sys.executable, "-c", code, str(db)],
        cwd=tmp_path,
        check=True,
        env={**os.environ, "PYTHONPATH": str(source / "src") + os.pathsep + str(root / "tests")},
        capture_output=True,
        text=True,
    )
    record = json.loads(old.stdout)
    assert record["schema"] == 25
    before = stored_rows(db)
    ctx = TrustedContext("authenticated:alice", "player.alice")
    with StateMachine(db, dnd_loader=lock_loader()) as sm:
        sid = record["sid"]
        p = _validate(lock_data())
        sm.resume_session(sid, p, context=ctx)
        after = stored_rows(db)
        for table in before.keys() - {"door_locks", "door_credentials", "objects"}:
            assert after[table] == before[table], table
        from test_migrations import assert_original_rows_equal

        assert_original_rows_equal(before, after)
        assert sm._world_facts.door_lock(sid, DOOR) is True
        assert sm._world_facts.accepts_credential(sid, DOOR, CRED, "lock." + DOOR)
        assert sm._conn.execute("SELECT count(*) FROM object_durability").fetchone()[0] == 0
        assert sm._conn.execute("SELECT count(*) FROM object_openables").fetchone()[0] == 0
        assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []
