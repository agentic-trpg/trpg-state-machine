"""Run archived runtime code in a fresh interpreter, never synthesize SQLite rows."""

import json
import sys
from dataclasses import replace
from pathlib import Path

import yaml

source, database, fixture, forward_authored = sys.argv[1:]
sys.path.insert(0, str(Path(source) / "src"))

from trpg_state_machine import (  # noqa: E402
    CreateSessionRequest,
    PlayerSelection,
    StateMachine,
    TrustedContext,
    load_adventure_package,
)
from trpg_state_machine._operations import EnterScene, SetSceneFlag  # noqa: E402

if forward_authored == "yes":
    # Compatibility fixture for the trusted typed-package service boundary ONLY.
    # The parent validates the complete authored YAML with the current loader.
    # Old loaders cannot parse senses: load their supported shape, then pin the
    # complete approved content digest. Old runtime never uses/reconstructs senses.
    # Separate tests below use the unmodified old loader and original fixture.
    from trpg_state_machine.package import _digest

    authored = yaml.safe_load(Path(fixture).read_text("utf-8"))
    supported = yaml.safe_load(Path(fixture).read_text("utf-8"))
    for actor in supported["actors"]:
        actor.pop("senses", None)
    compatible = Path(database).with_suffix(".yaml")
    compatible.write_text(yaml.safe_dump(supported), "utf-8")
    package = replace(load_adventure_package(compatible).package, package_digest=_digest(authored))
else:
    result = load_adventure_package(fixture)
    assert result.status == "valid", result.errors
    package = result.package

context = TrustedContext("authenticated:alice", "player.alice")
sessions = []
with StateMachine(database) as sm:
    for key in ("old.first", "old.second"):
        request = CreateSessionRequest(
            key,
            package.adventure_id,
            package.package_version,
            package.package_digest,
            (PlayerSelection(context.player_id, "pc_template.visitor"),),
        )
        created = sm.create_session(request, package, context=context)
        sessions.append(created.session_id)
        if key == "old.first":
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
                SetSceneFlag("scene.workshop", "flag.workshop_checked", True),
                expected_world_version=1,
                context=context,
            )
            sm._apply(
                created.session_id,
                package,
                EnterScene("scene.platform"),
                expected_world_version=2,
                context=context,
            )
            if package.ruleset_binding.ruleset_id == "synthetic.atomic":
                from trpg_state_machine import TypedCommand
                from trpg_state_machine.mechanics import RuleOperation

                pc = sm.read_session(created.session_id, package, context=context).pc_actor_id
                command = TypedCommand(
                    created.session_id,
                    "old.mechanical",
                    pc,
                    "rule_operation",
                    RuleOperation(
                        "synthetic_attempt",
                        pc,
                        "resource.tokens",
                        "scene.platform",
                        "flag.lamp_checked",
                        1,
                    ),
                    3,
                )
                assert sm.submit_command(command, package, context=context).status == "committed"
    print(
        json.dumps(
            {"sessions": sessions, "schema": sm._conn.execute("PRAGMA user_version").fetchone()[0]}
        )
    )
