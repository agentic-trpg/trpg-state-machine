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
