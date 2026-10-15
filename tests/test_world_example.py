import json
import os
import subprocess
import sys
from pathlib import Path


def test_public_world_station_survives_process_boundary_and_continues(tmp_path):
    root = Path(__file__).resolve().parents[1]
    process = subprocess.run(
        [sys.executable, str(root / "examples/world_station.py")],
        cwd=root,
        env={**os.environ, "TEMP": str(tmp_path), "TMP": str(tmp_path), "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    report = json.loads(process.stdout)
    assert report["new_process_restore"] and report["receipt_replay"]
    assert report["shading"] == {"lit": "bright", "closed": "dim"}
    assert report["final_scene"] == "scene.workshop" and report["final_cell"] == "1,0"
    assert report["local_illumination"] == "bright"
    assert len(report["stable_object_ids"]) == len(set(report["stable_object_ids"]))
    assert len(report["gm_management_paths"]) == 1
