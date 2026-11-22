"""Public passive Detect Magic through ritual, hidden World facts and private views."""

import json
from dataclasses import asdict
from unittest.mock import patch

import pytest
from item_support import DAGGER, NPC, A
from spatial_support import command as world_command
from test_exploration_spell_cast_activity import _command, _wire
from test_scoped_runtime_validation import measure
from test_session_time_coordination import submit as submit_time
from test_session_time_coordination import sync
from test_timed_spell_cast import cast_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world
from test_world_magic_light import CASE, _magic_world, prepare_cast

from trpg_state_machine import (
    AdvanceSessionTime,
    DropWorldConcentration,
    FinishSpellCast,
    ObjectState,
    PickupItem,
    PutItem,
    SetObjectState,
    StartSpellCast,
    StateMachine,
    StepActor,
)
from trpg_state_machine.perception import GameViewContext

MAGIC = ("magic_present",)


def _prepare(timed_spell_world, barrier, *, growth=0, screen=False):
    def configure(data):
        case = next(o for o in data["world_objects"] if o["object_id"] == CASE)
        case["material"] = "wood"  # Narrative material cannot establish thickness.
        if barrier is not None:
            case["detection_barrier"] = dict(
                zip(("material", "thickness_mils"), barrier, strict=True)
            )
        if screen:
            data["world_objects"].append(
                {
                    **case,
                    "object_id": "object.screen",
                    "definition_ref": "definition.detection_screen",
                    "capabilities": [],
                    "detection_barrier": {"material": "metal", "thickness_mils": 1_000},
                }
            )
            data["scenes"][0]["objects"].append(
                {
                    "object_id": "object.screen",
                    "state": None,
                    "movable": False,
                    "blocks_passage": False,
                    "initial_cell": "1,1",
                }
            )

    return _magic_world(
        timed_spell_world,
        opaque_case=True,
        additional_spells=("detect-magic",),
        growth=growth,
        configure_extra=configure,
    )


def _commit(session, command):
    sm, _, package, context = session
    receipt = sm.submit_command(_wire(command), package, context=context)
    assert receipt.status == "committed", receipt


def _world(session, operation, name):
    _commit(session, world_command(*session, operation, name))


def _hide_enchanted_item(session):
    _world(session, PickupItem(DAGGER, A, "0,1"), "take-dagger")
    sm, _, package, context = session
    command = prepare_cast(session, "light", "light", 0, {"kind": "item", "item_id": DAGGER})
    receipt = sm.submit_command(command, package, context=context)
    assert receipt.status == "committed", receipt
    _world(session, PutItem(A, DAGGER, CASE, "0,0"), "stow")
    _world(session, SetObjectState(A, CASE, ObjectState.CLOSED), "shut")


def _detect(session):
    _commit(
        session,
        _command(
            session,
            "start-detection",
            "activity.start_spell_cast",
            StartSpellCast("detect-ritual", "detect-magic", "class:wizard", as_ritual=True),
        ),
    )
    assert _view(session).detection_clues == ()
    assert submit_time(session, AdvanceSessionTime(600), "ritual-time").status == "committed"
    wait = sync(session).time_wait
    assert wait is not None
    _commit(
        session,
        _command(
            session,
            "finish-detection",
            "activity.finish_spell_cast",
            FinishSpellCast("detect-ritual"),
            wait=wait,
        ),
    )
    assert submit_time(session, AdvanceSessionTime(600), "continue").status == "committed"


def _view(session, role="player"):
    sm, sid, package, context = session
    scope = GameViewContext(context.principal_id, sid, context.player_id, role)
    return sm.get_scene_view(sid, package, context=scope)


@pytest.mark.parametrize(
    ("barrier", "expected"),
    [
        (("wood", 11_999), MAGIC),
        (("wood", 12_000), ()),
        (("stone", 12_000), ()),
        (("dirt", 12_000), ()),
        (("metal", 999), MAGIC),
        (("metal", 1_000), ()),
        (("lead", 1), ()),
        (None, ()),
    ],
)
def test_public_detect_magic_container_thresholds_and_opening(timed_spell_world, barrier, expected):
    session = _prepare(timed_spell_world, barrier)
    with session[0]:
        _hide_enchanted_item(session)
        _detect(session)
        view = _view(session)
        assert view.detection_clues == expected
        assert DAGGER not in json.dumps(asdict(view))
        _world(session, SetObjectState(A, CASE, ObjectState.OPEN), "uncover")
        assert _view(session).detection_clues == MAGIC


@pytest.mark.parametrize("expiry", [False, True])
def test_public_detect_magic_is_private_after_reopen_until_its_end(
    timed_spell_world, tmp_path, expiry
):
    session = _prepare(timed_spell_world, ("wood", 5_000))
    sm, sid, package, context = session
    with sm:
        _hide_enchanted_item(session)
        _detect(session)
        assert _view(session).detection_clues == MAGIC
        assert _view(session, "narrator").detection_clues == ()
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        assert sm.get_spatial_view(sid, pc, package, context=context).detection_clues == MAGIC
        npc = sm.get_spatial_view(sid, NPC, package, context=context)
        assert npc.actor_position.scene_id == A and npc.detection_clues == ()
        loader = sm._rules.loader

    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as reopened:
        session = (reopened, sid, package, context)
        assert _view(session).detection_clues == MAGIC
        assert reopened.get_spatial_view(sid, NPC, package, context=context).detection_clues == ()
        if expiry:
            assert submit_time(session, AdvanceSessionTime(1199), "before").status == "committed"
            assert _view(session).detection_clues == MAGIC
            assert submit_time(session, AdvanceSessionTime(1200), "expires").status == "committed"
        else:
            _world(session, DropWorldConcentration(), "drop-focus")
        assert _view(session).detection_clues == ()


def test_public_detect_magic_rechecks_range_when_actor_moves(timed_spell_world):
    session = _prepare(timed_spell_world, ("wood", 5_000))
    with session[0]:
        _hide_enchanted_item(session)
        _detect(session)
        _world(session, StepActor(A, "0,0", "0,1"), "down")
        for index in range(6):
            _world(session, StepActor(A, f"{index},1", f"{index + 1},1"), f"step-{index}")
        assert _view(session).detection_clues == MAGIC
        _world(session, StepActor(A, "6,1", "7,1"), "out-of-range")
        assert _view(session).detection_clues == ()


def test_public_detect_magic_checks_a_crossed_world_footprint(timed_spell_world):
    session = _prepare(timed_spell_world, ("wood", 5_000), screen=True)
    with session[0]:
        _hide_enchanted_item(session)
        _detect(session)
        assert _view(session).detection_clues == MAGIC
        _world(session, StepActor(A, "0,0", "0,1"), "down")
        _world(session, StepActor(A, "0,1", "1,1"), "near-screen")
        _world(session, StepActor(A, "1,1", "2,1"), "behind-screen")
        assert _view(session).detection_clues == ()


def test_indeterminate_path_does_not_mask_a_separate_source(timed_spell_world):
    session = _prepare(timed_spell_world, None)
    sm, _, package, context = session
    with sm:
        _hide_enchanted_item(session)
        _detect(session)
        assert _view(session).detection_clues == ()
        receipt = sm.submit_command(cast_command(session), package, context=context)
        assert receipt.status == "committed", receipt
        assert _view(session).detection_clues == MAGIC


def test_public_detect_magic_detects_fixed_darkness_origin(timed_spell_world):
    """Detect the actual Darkness rule profile with separate concentration owners."""
    session = _prepare(timed_spell_world, None)
    sm, sid, package, context = session
    with sm:
        command = prepare_cast(
            session, "fixed-darkness", "darkness", 2, {"kind": "point", "cell_id": "2,0"}
        )
        magic = sm._world.magic
        with patch.object(magic, "apply", wraps=magic.apply) as adopt:
            receipt = sm.submit_command(command, package, context=context)
            assert receipt.status == "committed", receipt
            _, _, _, darkness = adopt.call_args.args
        assert darkness.environment.obscurement == "none" and darkness.environment.light == "dark"
        _world(session, DropWorldConcentration(), "drop-first-darkness")
        _detect(session)
        assert _view(session).detection_clues == ()
        # Fixture setup only. Either real spell takes concentration, and the public
        # exploration cast command currently admits only the player character.
        with sm._transaction(write=True):
            row = sm._conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
            magic.apply(
                row, package, "fixture-darkness", darkness.model_copy(update={"source_id": NPC})
            )
        assert _view(session).detection_clues == MAGIC
        npc = sm.get_spatial_view(sid, NPC, package, context=context)
        assert npc.detection_clues == ()


def test_detection_candidate_work_is_bounded_by_range_in_public_view(timed_spell_world):
    counts = []
    for growth in (0, 40):
        session = _prepare(timed_spell_world, ("wood", 5_000), growth=growth)
        sm = session[0]
        with sm:
            _hide_enchanted_item(session)
            _detect(session)
            detection = sm._world.detection
            original = detection.clues
            samples = []

            def count(*args, original=original, samples=samples, sm=sm):
                with measure(sm) as (counts, statements):
                    clue = original(*args)
                samples.append((counts["sql_selects"], counts["sql_rows"], statements))
                return clue

            with patch.object(detection, "clues", side_effect=count):
                assert _view(session).detection_clues == MAGIC
            [(statements, rows, queries)] = samples
            counts.append((statements, rows))
            for table, index in (
                ("object_spatial_cells", "spatial_cell_lookup"),
                ("actors", "actors_cell_lookup"),
                ("spatial_light_effects", "spatial_magic_fixed_cell"),
            ):
                (query,) = [sql for sql in queries if f"FROM {table} " in sql and "cell IN" in sql]
                plan = sm._conn.execute(
                    f"EXPLAIN QUERY PLAN {query}", (session[1], A, '["0,0"]')
                ).fetchall()
                assert any(index in row[3] and "cell=?" in row[3] for row in plan), plan
    assert min(counts[0]) > 0 and counts[0] == counts[1]
