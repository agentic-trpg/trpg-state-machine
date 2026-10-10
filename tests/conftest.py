import io
import os
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

from trpg_state_machine import (
    CreateSessionRequest,
    PlayerSelection,
    TrustedContext,
    load_adventure_package,
)

FIXTURE = Path(__file__).parent / "fixtures" / "lantern_station.yaml"


@pytest.fixture
def package():
    result = load_adventure_package(FIXTURE)
    assert result.status == "valid", result.errors
    assert result.package is not None
    return result.package


@pytest.fixture
def package_data():
    return yaml.safe_load(FIXTURE.read_text("utf-8"))


@pytest.fixture
def context():
    return TrustedContext("authenticated:alice", "player.alice", ("npc.caretaker", "npc.attendant"))


@pytest.fixture
def request_for(package, context):
    def make(command_id="cmd.create.001", pc="pc_template.observer"):
        return CreateSessionRequest(
            command_id,
            package.adventure_id,
            package.package_version,
            package.package_digest,
            (PlayerSelection(context.player_id, pc),),
        )

    return make


@pytest.fixture(scope="session")
def legacy_python():
    """A fixed /4 environment for actual historical code, never a patched evaluator."""
    root = Path(__file__).resolve().parents[1]
    destination = root / ".test-tmp" / "legacy-sm8"
    if not (destination / "uv.lock").exists():
        archived = subprocess.run(
            [
                "git",
                "archive",
                "--format=zip",
                "a00854374a38b01a13cc0fbfa089ef6e0b709cef",
                "pyproject.toml",
                "uv.lock",
                "src",
            ],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        with zipfile.ZipFile(io.BytesIO(archived)) as contents:
            contents.extractall(destination)
    result = subprocess.run(
        ["uv", "sync", "--locked", "--group", "dev", "--project", str(destination)],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return str(destination / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
