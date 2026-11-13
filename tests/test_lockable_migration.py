"""Actual Schema24 history and transfer receipt preservation; atomic Schema25 upgrade."""

import json
import subprocess
import sys
from pathlib import Path


def test_public_example_uses_common_pump_and_new_process_restore(tmp_path):
    root = Path(__file__).parents[1]
    # The example creates its own fresh SQLite and executes an actual restore subprocess.
    result = subprocess.run(
        [sys.executable, str(root / "examples/lockable_passages.py")],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["restored_in_new_process"]["locked"] is True
    assert report["restored_in_new_process"]["door"] == "closed"
    assert "object.amulet_a" in report["restored_in_new_process"]["owned_instances"]
