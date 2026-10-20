"""Public command replay and disclosure regressions from review of 631d957."""

import hashlib
import json
from dataclasses import asdict, replace

import pytest
from lock_support import A, lock_loader
from test_environment_service import (
    WEATHER,
    Conditions,
    Facts,
    Protection,
    Source,
    gm,
    project,
    seed,
    value,
)
from test_migrations import stored_rows
from test_object_protection import large_data
from world_support import command, setup, submit

from trpg_state_machine import (
    AddEnvironmentSource,
    BatchEnvironment,
    EnvironmentArea,
    HeightInterval,
    InitializeEnvironment,
    RemoveEnvironmentSource,
    ReplaceEnvironmentSource,
    SetEnvironmentBase,
    SetEnvironmentProtection,
    StateMachine,
    Temperature,
    Wind,
    world_command_from_data,
)
from trpg_state_machine.commands import command_fingerprint
from trpg_state_machine.environment_commands import (
    ENVIRONMENT_OPERATIONS,
    environment_operation_from_data,
    environment_operation_to_data,
)
from trpg_state_machine.errors import StateMachineError
from trpg_state_machine.package import canonical_json


def legacy_fingerprint(cmd, package, context):
    """Frozen pre-fix algorithm for DTOs without legacy Reposition optional fields."""
    data = asdict(cmd)
    data.pop("check_world_effects")
    data.pop("time_wait")  # This frozen algorithm predates the optional field.
    return hashlib.sha256(
        canonical_json(
            {
                "command": data,
                "principal": context.principal_id,
                "player": context.player_id,
                "package": package.package_digest,
                "binding": asdict(package.ruleset_binding),
            }
        ).encode()
    ).hexdigest()


@pytest.mark.parametrize("initial", ["success", "rejected"])
def test_add_replace_batch_cannot_borrow_stored_receipt(tmp_path, context, initial):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    ctx = gm(context)
    with sm:
        seed(sm, sid, p, ctx)
        source = Source("review.source", Conditions(Temperature(40)), observable=True)
        if initial == "success":
            op, other = AddEnvironmentSource(A, source), ReplaceEnvironmentSource(A, source)
        else:
            op, other = ReplaceEnvironmentSource(A, source), AddEnvironmentSource(A, source)
        cmd = command(sm, sid, p, ctx, BatchEnvironment((op,)), "review.same.key")
        receipt = sm.submit_command(cmd, p, context=ctx)
        assert receipt.status == ("committed" if initial == "success" else "rejected")
        before = stored_rows(db)
        assert sm.submit_command(cmd, p, context=ctx) == receipt
        for mutation in (
            other,
            replace(op, source=replace(source, conditions=Conditions(Temperature(41)))),
        ):
            with pytest.raises(StateMachineError) as conflict:
                sm.submit_command(
                    replace(cmd, payload=BatchEnvironment((mutation,))), p, context=ctx
                )
            assert conflict.value.code == "idempotency_conflict"
            assert stored_rows(db) == before
        assert sm.get_command_receipt(sid, cmd.command_id, p, context=ctx) == receipt
        assert stored_rows(db) == before  # facts/version/events/RNG/evidence/original receipt
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        restored.resume_session(sid, p, context=ctx)
        assert restored.submit_command(cmd, p, context=ctx) == receipt
        with pytest.raises(StateMachineError) as conflict:
            restored.submit_command(
                replace(cmd, payload=BatchEnvironment((other,))), p, context=ctx
            )
        assert conflict.value.code == "idempotency_conflict"
        assert stored_rows(db) == before


def test_batch_fingerprint_preserves_types_values_and_order(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    with sm:
        source = Source("review.source", Conditions(Temperature(40)))
        add = AddEnvironmentSource(A, source)
        base = SetEnvironmentBase(A, WEATHER)
        cmd = command(sm, sid, p, gm(context), BatchEnvironment((add, base)), "fingerprint")
        variants = (
            cmd,
            replace(cmd, payload=BatchEnvironment((ReplaceEnvironmentSource(A, source), base))),
            replace(
                cmd,
                payload=BatchEnvironment(
                    (
                        replace(
                            add, source=replace(source, conditions=Conditions(Temperature(41)))
                        ),
                        base,
                    )
                ),
            ),
            replace(cmd, payload=BatchEnvironment((base, add))),
        )
        assert len({command_fingerprint(c, p, gm(context)) for c in variants}) == 4
        receipt = sm.submit_command(cmd, p, context=gm(context))
        assert receipt.status == "committed"
        before = stored_rows(db)
        for changed in variants[1:]:
            with pytest.raises(StateMachineError) as conflict:
                sm.submit_command(changed, p, context=gm(context))
            assert conflict.value.code == "idempotency_conflict"
            assert stored_rows(db) == before


def test_environment_single_command_fingerprint_stays_compatible(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    ctx = gm(context)
    with sm:
        for i, op in enumerate(
            (
                SetEnvironmentBase(A, WEATHER),
                AddEnvironmentSource(A, Source("s", WEATHER)),
                RemoveEnvironmentSource(A, "s"),
            )
        ):
            cmd = command(sm, sid, p, ctx, op, f"legacy.single.{i}")
            assert command_fingerprint(cmd, p, ctx) == legacy_fingerprint(cmd, p, ctx)
            receipt = sm.submit_command(cmd, p, context=ctx)
            assert sm.submit_command(cmd, p, context=ctx) == receipt


def test_old_ambiguous_batch_receipt_is_not_a_replay_fallback(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    ctx = gm(context)
    with sm:
        cmd = command(
            sm, sid, p, ctx, BatchEnvironment((SetEnvironmentBase(A, WEATHER),)), "old.batch"
        )
        receipt = sm.submit_command(cmd, p, context=ctx)
        # Simulate a historical 631d957 receipt; payload types cannot be recovered from this hash.
        with sm._transaction(write=True):
            sm._conn.execute(
                "UPDATE command_receipts SET fingerprint=? WHERE session_id=? AND command_id=?",
                (legacy_fingerprint(cmd, p, ctx), sid, cmd.command_id),
            )
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as conflict:
            sm.submit_command(cmd, p, context=ctx)
        assert conflict.value.code == "idempotency_conflict"
        assert sm.get_command_receipt(sid, cmd.command_id, p, context=ctx) == receipt
        assert stored_rows(db) == before


def test_tagged_wire_batch_and_typed_batch_have_same_fingerprint(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    ctx = gm(context)
    with sm:
        source = Source("wire", Conditions(Temperature(4)), EnvironmentArea(("1,0", "0,0")))
        cmd = command(
            sm,
            sid,
            p,
            ctx,
            BatchEnvironment((AddEnvironmentSource(A, source), SetEnvironmentBase(A, WEATHER))),
            "wire",
        )
        raw = asdict(cmd)
        raw.pop("check_world_effects")
        raw["payload"] = {
            "changes": [
                {
                    "command_type": "world.add_environment_source",
                    "payload": asdict(cmd.payload.changes[0]),
                },
                {
                    "command_type": "world.set_environment_base",
                    "payload": asdict(cmd.payload.changes[1]),
                },
            ]
        }
        decoded = world_command_from_data(json.loads(json.dumps(raw)))
        assert decoded == cmd
        assert command_fingerprint(decoded, p, ctx) == command_fingerprint(cmd, p, ctx)
        encoded = environment_operation_to_data(cmd.payload)
        assert encoded == environment_operation_to_data(decoded.payload)
        assert canonical_json(encoded["payload"]) == canonical_json(raw["payload"])
        receipt = sm.submit_command(cmd, p, context=ctx)
        before = stored_rows(db)
        assert sm.submit_command(decoded, p, context=ctx) == receipt
        assert stored_rows(db) == before


def test_canonical_encoder_roundtrips_every_registered_environment_type():
    source = Source("roundtrip", WEATHER)
    mutations = (
        InitializeEnvironment(A, Facts(WEATHER)),
        SetEnvironmentBase(A, WEATHER),
        AddEnvironmentSource(A, source),
        ReplaceEnvironmentSource(A, source),
        RemoveEnvironmentSource(A, source.source_id),
        SetEnvironmentProtection(A, Protection((), ())),
    )
    operations = (*mutations, BatchEnvironment(mutations))
    assert {type(op) for op in operations} == set(ENVIRONMENT_OPERATIONS)
    for op in operations:
        encoded = environment_operation_to_data(op)
        decoded = environment_operation_from_data(encoded["command_type"], encoded["payload"])
        assert decoded == op
        assert environment_operation_to_data(decoded) == encoded


def test_public_point_queries_cannot_probe_hidden_vertical_or_horizontal_scope(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    with sm:
        hidden = Source(
            "secret", Conditions(Temperature(-20)), EnvironmentArea(("0,0",), HeightInterval(2, 4))
        )
        seed(sm, sid, p, context, Facts(WEATHER, (hidden,)))
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        before, writes = stored_rows(db), sm._conn.total_changes
        sm._conn.execute("PRAGMA query_only=ON")
        try:
            outputs = [
                sm.environment.query_cell(sid, pc, A, cell, p, context=context, height_ft=z)
                for cell in ("0,0", "1,0", "2,0")
                for z in (0, 2, 3, 4, 6)
            ]
            assert all(
                r.temperature.status == "unknown" and not r.temperature.contributions
                for r in outputs
            )
            assert all(value(r.wind) == Wind(20, "north") for r in outputs)
            assert value(project(sm, sid, p, "0,0", height=3).temperature) == Temperature(-20)
            assert value(project(sm, sid, p, "0,0", height=6).temperature) == Temperature(8)
            assert value(project(sm, sid, p, "1,0", height=3).temperature) == Temperature(8)
        finally:
            sm._conn.execute("PRAGMA query_only=OFF")
        assert stored_rows(db) == before and sm._conn.total_changes == writes


@pytest.mark.parametrize("owner", ["facts", "effects"])
def test_scene_field_disclosure_stable_across_hidden_changes_and_all_query_paths(
    tmp_path, context, owner
):
    sm, sid, p, db = setup(tmp_path, context, large_data())

    class EffectsFixture:
        current = ()

        def environment_sources(self, sid, scene_id, package):
            return self.current if scene_id == A else ()

    class HeightFixture:
        def environment_heights(self, sid, actor_id, package, *, position):
            return HeightInterval(0, 6)

    provider = EffectsFixture()
    with sm:
        hidden = Source("secret.first", WEATHER, EnvironmentArea(("0,0",), HeightInterval(2, 4)))
        seed(
            sm,
            sid,
            p,
            context,
            Facts(
                WEATHER, (hidden,) if owner == "facts" else (), protection=Protection(("0,0",), ())
            ),
        )
        sm.environment.effects_provider = provider
        sm.environment.unit_height_provider = HeightFixture()
        public = Source(
            "public",
            Conditions(Temperature(30)),
            EnvironmentArea(("1,0",), HeightInterval(1, 5)),
            observable=True,
        )
        assert (
            submit(sm, sid, p, gm(context), AddEnvironmentSource(A, public), "public").status
            == "committed"
        )
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        snapshots = []
        for index, current_hidden in enumerate(
            (
                hidden,
                replace(hidden, source_id="secret.identity.only"),
                replace(
                    hidden,
                    conditions=replace(
                        WEATHER, temperature=Temperature(-99), wind=Wind(50, "west")
                    ),
                ),
                replace(hidden, area=EnvironmentArea(("2,0",), HeightInterval(8, 9))),
                replace(
                    hidden,
                    source_id="secret.changed",
                    conditions=replace(
                        WEATHER, temperature=Temperature(-99), wind=Wind(50, "west")
                    ),
                    area=EnvironmentArea(("2,0",), HeightInterval(8, 9)),
                ),
                replace(
                    hidden,
                    source_id="secret.final",
                    area=EnvironmentArea(("1,0",), HeightInterval(-4, 10)),
                ),
            )
        ):
            if owner == "facts" and index:
                old = sm.environment.read_facts(sid, A, p, context=gm(context)).sources
                old_secret = next(s for s in old if not s.observable)
                submit(
                    sm,
                    sid,
                    p,
                    gm(context),
                    BatchEnvironment(
                        (
                            RemoveEnvironmentSource(A, old_secret.source_id),
                            AddEnvironmentSource(A, current_hidden),
                        )
                    ),
                    f"move.hidden.{index}",
                )
            elif owner == "effects":
                provider.current = (current_hidden,)
            internal = project(
                sm,
                sid,
                p,
                current_hidden.area.cells[0],
                height=current_hidden.area.heights.lower_ft,
            )
            assert value(internal.temperature) == current_hidden.conditions.temperature
            assert internal.temperature.contributions[0].source_ref == (
                ("fact:" if owner == "facts" else "effect:") + current_hidden.source_id
            )
            before, writes = stored_rows(db), sm._conn.total_changes
            sm._conn.execute("PRAGMA query_only=ON")
            try:
                cells = ("0,0", "1,0", "2,0")
                points = tuple(
                    sm.environment.query_cell(sid, pc, A, c, p, context=context, height_ft=z)
                    for c in cells
                    for z in (0, 2, 3, 4, 6)
                )
                areas = tuple(
                    sm.environment.query_area(
                        sid, pc, A, EnvironmentArea(cells), p, context=context, height_ft=z
                    )
                    for z in (0, 3, 6)
                )
                ranges = tuple(
                    sm.environment.query_height_range(
                        sid, pc, A, c, HeightInterval(0, 6), p, context=context
                    )
                    for c in cells
                )
                unit = sm.environment.query_unit(sid, pc, p, context=context)
                for result in points:
                    for field in (
                        "temperature",
                        "wind",
                        "precipitation",
                        "obscurant",
                        "ground_precipitation",
                        "wind_exposure",
                    ):
                        assert getattr(result, field).status == "unknown"
                        assert getattr(result, field).contributions == ()
                    assert (
                        result.protection.rain_status == result.protection.wind_status == "unknown"
                    )
                    assert result.protection.rain_sources == result.protection.wind_bands == ()
                assert all(
                    tuple(s.heights for s in row) == (HeightInterval(0, 6),) for row in ranges
                )
                snapshots.append((points, areas, ranges, unit))
            finally:
                sm._conn.execute("PRAGMA query_only=OFF")
            assert stored_rows(db) == before and sm._conn.total_changes == writes
        assert all(snapshot == snapshots[0] for snapshot in snapshots)
