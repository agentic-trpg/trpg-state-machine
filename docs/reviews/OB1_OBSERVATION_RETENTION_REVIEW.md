# OB-1: Observation retention foundation

Architecture baseline: Meta `307cde1d36170ff021564ff5155ba6f349f35e80`,
MODULE_CONTRACTS §7.4 and §9.6–9.9. Runtime baseline: SM-22 `491e98c`.
This is the first of three sequential implementation batches, not their Global Review.

## Implemented

Schema 16 extends the existing `npc_observation_work` with nullable
`accepted_game_tick` and `expires_game_tick`. A same-transaction insert trigger
captures the authoritative World clock when Work is first durably adopted, with
100 abstract ticks of validity. `ob-retention/1` is a bounded local candidate:
it does not assert historical event time, wall-clock seconds or combat rounds,
nor freeze the architecture's OPEN TTL start/length/encoding policy. A repeated
projection cannot reset the first adoption or deadline. Existing saves retain
NULL timing (`legacy_unknown`), never invented occurrence or expiry timestamps.

`get_recent_npc_observations` returns all authorized, projection-watermark-visible
Pending and Completed observations with validity metadata. Existing
`read_observations` preserves its return type and now returns only Recent OB.
`get_pending_npc_observation_work` independently returns Pending scope metadata;
`get_npc_observation_retention` exposes own-NPC lifecycle metadata, without
archived content. Reads authenticate the existing NPC grant and Package pin;
they never acknowledge, create, complete or duplicate Work.

Recent is derived from `world_states.game_tick < expires_game_tick` in the same
read snapshot. At the exact deadline, on a large jump or after restart, expired
OB is excluded without waiting for a sweep. Processing state remains independent.
Expired or unknown evidence referenced by any private record/history is marked
archived by the existing multi-source relation. No DELETE/GC interface is added;
all evidence, Inbox identity, Work, Signal sources, historical Meta, Task, Receipt,
dedupe keys and Cursors are retained. Conservative retention is intentional;
Discardable/physical cleanup is not implemented.

The migration retains the existing serialized migration/foreign-key transaction.
Perception source and Event Evidence construction have no changes. The lifecycle
trigger runs only after an Observation already exists; it never judges perception
or supplies substitute evidence. Tests use ordinary lawful synthetic observations.

## Validation and review

Nine new SQLite cases cover Pending/Completed Recent, exact TTL boundary, jump,
no-op EVA receipt replay, expired Pending/in-flight protection, projection gaps,
five-kind Meta with multi-source archival and Belief history, two NPCs/Sessions,
authorization, re-projection, SQL rollback and retry, fresh-process recovery, and
concurrent/repeated migration of a real save created by archived Schema-15 code.
The old save contains actual World/Scene/Actor/RNG, Command and Evaluation receipts,
Meta and Observation provenance. Independent database connections verify rows.

Projection already commits an attempt counter before its projection transaction;
the injected lifecycle failure preserves all authority except that expected audit
increment, leaves Work unfinished and permits retry. This existing behavior is
explicitly tested rather than changing Perception.

Full locked regression on 2026-10-11: **668 passed**, 2 existing Lark deprecation
warnings, 215.85s (659 baseline + 9 added). Commands executed successfully:

```text
uv sync --locked --group dev
uv run --locked python -m pytest -q
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .  (102 files)
git diff --check
```

Existing tests
change latest-schema expectations from 15 to 16. Two compatibility assertions also
verify preserved original Work columns plus NULL lifetime, and watermark-visible
legacy evidence excluded from Recent but retained as Pending. Historical schema
fixtures and source-version assertions remain intact. No new confirmed P0/P1 was identified
in the targeted source/SQL/test review. Gate eligibility and Evaluation input/source
integration are OB-2/OB-3 work, not claimed as delivered by OB-1.

## Remaining blocker

**PER-19-22-01 remains an unresolved P1 and Global Merge Blocker.** See
[SM-22 evidence](SM22_GAME_TIME_SCHEDULE_REVIEW.md). This batch neither repairs,
bypasses, hides nor downgrades it. Independent verified batch Commit + Push is
authorized; Merge and PR are not.
