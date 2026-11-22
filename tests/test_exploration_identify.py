"""Public Identify casts bind the exact pearl and the authorized touched item."""

from dataclasses import replace

import pytest
from dnd5e_srd_data import BundledAssetLoader
from item_support import A
from spatial_support import submit as submit_world
from test_exploration_spell_cast_activity import _command, _progress
from test_scoped_runtime_validation import measure
from test_session_time_coordination import submit as submit_time
from test_session_time_coordination import sync
from test_timed_spell_cast import first_level_balance
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import (
    AdvanceSessionTime,
    DropItem,
    FinishSpellCast,
    PickupItem,
    StartSpellCast,
    StateMachine,
    StateMachineError,
    StepActor,
)

PEARL = "object.identify_pearl"
SECOND_PEARL = "object.second_pearl"
WAND = "object.touched_wand"
SECOND_WAND = "object.unrelated_wand"


def _world(factory, *, slots=4, pearl_price=100, target_cell="0,1", growth=0):
    """Build a seconds Session with the published spell and two distinct item pairs."""
    canonical = BundledAssetLoader()
    pearl = canonical.get_item("pearl").model_copy(update={"cost_gp": pearl_price})

    def configure(data):
        """Author instance positions and unequal current charges independently of asset."""
        for oid, slug, charges, cell in (
            (PEARL, "pearl", 0, "0,0"),
            (SECOND_PEARL, "pearl", 0, "0,0"),
            (WAND, "wand-of-magic-missiles", 2, target_cell),
            (SECOND_WAND, "wand-of-magic-missiles", 5, "0,2"),
        ):
            data["scenes"][0]["objects"].append(
                {
                    "object_id": oid,
                    "state": None,
                    "movable": False,
                    "blocks_passage": False,
                    "initial_cell": cell,
                }
            )
            data["item_transfers"].append(
                {
                    "instance_id": oid,
                    "item_slug": slug,
                    "initial_owner_id": None,
                    "quantity": 1,
                    "charges_remaining_per_unit": charges,
                    "policy": "charge_exhaust_retain" if charges else "ordinary_public",
                }
            )
        for i in range(growth):
            data["scenes"][1]["objects"].append(
                {
                    "object_id": f"object.unrelated_identify_{i}",
                    "state": None,
                    "movable": False,
                    "blocks_passage": False,
                    "initial_cell": "1,1",
                }
            )

    return factory(
        first_level_slots=slots,
        material=False,
        spell_id="identify",
        configure_spell=lambda _: canonical.get_spell("identify"),
        additional_items=(pearl,),
        configure=configure,
        database="timed-spell.sqlite" if not growth else f"identify-growth-{growth}.sqlite",
    )


def _pickup_pearl(session, iid=PEARL):
    """Put the exact selected material in the caster's actual inventory."""
    sm, sid, package, context = session
    receipt = submit_world(sm, sid, package, context, PickupItem(iid, A, "0,0"), "take." + iid)
    assert receipt.status == "committed", receipt


def _start_identify(session, *, ritual=False, target=WAND, material=PEARL, key="identify-start"):
    """Build a start that explicitly selects one item and one physical pearl."""
    return _command(
        session,
        key,
        "activity.start_spell_cast",
        StartSpellCast(
            "my-casting",
            "identify",
            "class:wizard",
            None if ritual else 1,
            ritual,
            target,
            material,
        ),
    )


def _submit(session, command):
    """Execute via the authorized submission interface."""
    sm, _, package, context = session
    return sm.submit_command(command, package, context=context)


@pytest.mark.parametrize(("ritual", "initial_slots", "deadline"), [(False, 4, 60), (True, 0, 660)])
def test_identify_private_typed_result_reopens_and_pays_once(
    timed_spell_world, tmp_path, ritual, initial_slots, deadline
):
    """Finish once after reopen, retaining the same pearl and no lasting magic effect."""
    session = _world(timed_spell_world, slots=initial_slots)
    sm, sid, package, context = session
    with sm:
        _pickup_pearl(session)
        initial_rng = sm.read_rng_context(sid, package, context=context)
        started = _start_identify(session, ritual=ritual)
        receipt = _submit(session, started)
        assert receipt.status == "committed", receipt
        progress = _progress(session, started)
        assert (progress.target_object_id, progress.material_instance_id) == (WAND, PEARL)
        assert progress.deadline_tick == deadline
        assert progress.identification is None
        assert first_level_balance(sm, started) == initial_slots
        assert (
            submit_time(session, AdvanceSessionTime(deadline + 30), "reach").status == "committed"
        )
        waiting = sync(session)
        assert waiting.committed_tick == deadline
        assert waiting.time_wait is not None
        loader = sm._rules.loader

    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as sm:
        session = sm, sid, package, context
        finish = _command(
            session,
            "identify-finish",
            "activity.finish_spell_cast",
            FinishSpellCast("my-casting"),
            wait=waiting.time_wait,
        )
        receipt = _submit(session, finish)
        assert receipt.status == "committed", receipt
        assert _submit(session, finish) == receipt
        result = _progress(session, started).identification
        assert result.target_id == WAND
        assert result.item_name == "Wand of Magic Missiles"
        assert "While holding it" in result.properties_and_use
        assert result.charges_remaining == 2
        assert not result.requires_attunement
        assert not result.ongoing_spells
        assert result.created_by_spell is None
        assert first_level_balance(sm, started) == initial_slots - int(not ritual)
        snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        assert any(entry.instance_id == PEARL for entry in snapshot.inventory_state.entries)
        assert not snapshot.effect_states
        assert sm.read_rng_context(sid, package, context=context) == initial_rng
        assert not sync(session).time_wait
        events = sm.read_events(sid, package, context=context)
        assert all("properties_and_use" not in str(event.payload) for event in events)
        (cast,) = (
            event.payload["engine_event"]
            for event in events
            if event.payload.get("engine_event", {}).get("type") == "spell_cast"
        )
        assert (cast["material_cost_gp"], cast["material_consumed"], cast["ritual"]) == (
            100,
            False,
            ritual,
        )
        with pytest.raises(StateMachineError, match="Context does not control"):
            sm.read_spell_cast_activity(
                sid,
                started.actor_id,
                "my-casting",
                package,
                context=replace(context, principal_id="other"),
            )


@pytest.mark.parametrize("reason", ["wrong_pearl", "cheap_pearl", "distant_object", "missing_id"])
def test_rejects_unproven_material_or_contact(timed_spell_world, reason):
    """A different owned pearl or focus cannot stand in for the selected material."""
    session = _world(
        timed_spell_world,
        pearl_price=99 if reason == "cheap_pearl" else 100,
        target_cell="0,2" if reason == "distant_object" else "0,1",
    )
    sm, _, _, _ = session
    with sm:
        _pickup_pearl(session, SECOND_PEARL if reason == "wrong_pearl" else PEARL)
        start = _start_identify(
            session, target="unknown-object" if reason == "missing_id" else WAND
        )
        assert _submit(session, start).status == "rejected"
        assert first_level_balance(sm, start) == 4
        with pytest.raises(StateMachineError, match="Expected this caster's activity"):
            _progress(session, start)


@pytest.mark.parametrize("loss", ["move", "drop"])
def test_identify_interrupts_when_contact_or_exact_pearl_is_lost(timed_spell_world, loss):
    """Committed movement or transfer cannot be reversed to recover lost casting progress."""
    session = _world(timed_spell_world, target_cell="0,0")
    sm, sid, package, context = session
    with sm:
        _pickup_pearl(session)
        _pickup_pearl(session, SECOND_PEARL)
        start = _start_identify(session)
        assert _submit(session, start).status == "committed"
        if loss == "move":
            close = submit_world(sm, sid, package, context, StepActor(A, "0,0", "0,1"), "approach")
            assert close.status == "committed", close
            assert _progress(session, start).status == "casting"
        operation = DropItem(PEARL, A, "0,0") if loss == "drop" else StepActor(A, "0,1", "0,2")
        receipt = submit_world(sm, sid, package, context, operation, "break-contact")
        assert receipt.status == "committed", receipt
        assert _progress(session, start).status == "cancelled"
        assert first_level_balance(sm, start) == 4
        assert submit_time(session, AdvanceSessionTime(61), "after-contact").status == "committed"
        assert sync(session).time_wait is None


def test_failed_identify_receipt_rolls_back_result_payment_and_wait(timed_spell_world, monkeypatch):
    """A late transaction failure publishes neither private knowledge nor slot payment."""
    session = _world(timed_spell_world)
    sm, _, _, _ = session
    with sm:
        _pickup_pearl(session)
        start = _start_identify(session)
        assert _submit(session, start).status == "committed"
        assert submit_time(session, AdvanceSessionTime(60), "reach").status == "committed"
        waiting = sync(session)
        finish = _command(
            session,
            "finish",
            "activity.finish_spell_cast",
            FinishSpellCast("my-casting"),
            wait=waiting.time_wait,
        )
        evaluate = sm._rules._evaluate_dnd

        async def forged_properties(request):
            """Keep the correct selected object while substituting bogus properties."""
            result = await evaluate(request)
            intent, cast, evidence = result.proposed_events
            identification = evidence.identification.model_copy(
                update={"properties_and_use": "A forged identification."}
            )
            forged = evidence.model_copy(update={"identification": identification})
            return result.model_copy(update={"proposed_events": (intent, cast, forged)})

        with monkeypatch.context() as scope:
            scope.setattr(sm._rules, "_evaluate_dnd", forged_properties)
            with pytest.raises(StateMachineError, match="Identify result differs"):
                _submit(session, finish)
        assert first_level_balance(sm, start) == 4
        assert _progress(session, start).identification is None

        def fail_receipt(*_args):
            """Abort after Activity completion and before commit."""
            raise StateMachineError("storage_error", "injected Identify receipt failure")

        with monkeypatch.context() as scope:
            scope.setattr(sm._session_runtime, "record_receipt", fail_receipt)
            with pytest.raises(StateMachineError, match="injected Identify receipt failure"):
                _submit(session, finish)
        assert _progress(session, start).identification is None
        assert sync(session).time_wait == waiting.time_wait
        assert first_level_balance(sm, start) == 4
        assert _submit(session, finish).status == "committed"
        assert first_level_balance(sm, start) == 3


def test_identify_reads_only_fixed_contact_and_selected_instances(timed_spell_world):
    """Remote object growth adds no SQL reads through public start and finish."""
    samples = []
    for size in (0, 40):
        session = _world(timed_spell_world, growth=size)
        sm, _, _, _ = session
        with sm:
            _pickup_pearl(session)
            start = _start_identify(session)
            with measure(sm) as (start_counts, _):
                assert _submit(session, start).status == "committed"
            assert submit_time(session, AdvanceSessionTime(60), "reach").status == "committed"
            finish = _command(
                session,
                "finish",
                "activity.finish_spell_cast",
                FinishSpellCast("my-casting"),
                wait=sync(session).time_wait,
            )
            with measure(sm) as (finish_counts, _):
                assert _submit(session, finish).status == "committed"
            samples.append(
                tuple(
                    (counts["sql_selects"], counts["sql_rows"])
                    for counts in (start_counts, finish_counts)
                )
            )
    assert samples[0] == samples[1]
