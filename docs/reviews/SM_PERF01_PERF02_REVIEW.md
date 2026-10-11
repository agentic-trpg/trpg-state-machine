# PERF-01 / PERF-02 Review — NPC Perception Efficiency

Date: 2026-10-12 (Asia/Shanghai). Repository: `agentic-trpg/trpg-state-machine`.
Branch: `feat/sm-perf01-perf02-perception-efficiency`.
Baseline: merged Batch C `e23cde5` (Schema 19).

## Scope and implementation

Batch C recalculated and persisted every instantiated NPC's perception prefix
after each event append and judgment completion, then recalculated and persisted
every NPC's cognition prefix. The gap UNION scanned pending references through an
index containing completed history. The worker metadata API materialized all
historical queue rows in Python.

PERF-01 removes those Session-wide sweeps. `_npc_perception_cursor(sid,npc)` is one
SQL statement, inside the caller's existing transaction snapshot:

```text
effective = min(global event_seq,
                first pending candidate event_seq - 1, if present,
                first applicable barrier event_seq - 1, if present)
```

The two ordered seeks use `LIMIT 1`. Pending uses the new partial index; barriers
use their existing `(session_id,event_seq)` primary key. A barrier is waived only
by the same NPC's completed Queue row, whose existing SQLite trigger requires its
own completed perception result. This is the same gap predicate as Batch C's
UNION/min: the minimum of the union is the minimum of the two first gaps. Taking
the global end also bounds the result. Neither current actor position nor current
mechanical state participates in historical membership or proof.

The public perception cursor, Recent/ordinary OB reads, retention and Pending Work,
Memory Gate, mode routing and EVA source authorization all call this helper.
Each reads the watermark and the corresponding rows in the same transaction.
EVA computes the watermark once for its entire requested source scope. The older
`PerceptionRuntime.read_observations` implementation, normally shadowed by the
lifecycle MRO, is updated too. No live SQL consumer joins the removed cursor table.

Cognition reads derive `min(effective perception prefix, first pending cognition
Work before that prefix minus one)`. Gate/EVA completion still writes the existing
`npc_completion_cursors` checkpoint atomically, now only for its own NPC. That
table is preserved for historical compatibility and is never read as current
authority. Event append and perception completion update neither checkpoint table.
No Event/Evidence/Queue/proof/OB/Outbox/Receipt/RNG transaction boundary changes;
the two-phase JEV invocation remains outside the write transaction.

## Pending API and compatibility

`get_pending_npc_perception_queue(session_id,package,*,context,limit=20,
after_event_seq=None)` returns the existing immutable `NPCPerceptionEvent` tuple.
The SQL filters `session_id`, authorized `npc_id`, `complete=0` and exclusive
`event_seq > after_event_seq`, orders ascending, and limits rows in SQLite.
Limits are strict integers 1–1000; positions are `None` or integers 0–2^63-1.
Booleans, floats, negative/out-of-range values are rejected before SQL binding.
NPC grant, Session binding and package pins use the unchanged authorization path.
The API contains no Event payload, Evidence or cognition text.

Repository call-site review found the old Queue API used by tests for complete
history, migration, identity and retry auditing. There is no deployed Host polling
loop in this repository to cut over. Its signature and unbounded historical return
are preserved and explicitly documented as audit access. New worker consumers use
the dedicated Pending API; the benchmark compares those two intended paths.

`after_event_seq` is a scan position, not an acknowledgment. Each retry sweep must
start without it so earlier Unresolved work is revisited. Concurrent completion
may remove rows from later pages; appended rows may appear in a later page or the
next sweep. Immutable increasing sequences and exclusive keyset paging avoid
offset shifts. Concurrent duplicate workers still commit through the existing
idempotent projection. Unknown Discovery barriers remain shared fences without
invented candidate rows: an empty Pending page does not prove delivery is current.

## Schema 20 and historical protection

The versioned `_migration_20.sql` adds the partial index and drops only the derived
`npc_perception_cursors` table. Queue history and immutable per-NPC proofs remain.
This prevents accidental reads of a stale persisted perception prefix. Raw SQL
clients of that internal table must use the public cursor API; the public API is
unchanged. The legacy cognition checkpoint and all authoritative/audit rows are
preserved byte-for-byte in the Schema 19 upgrade fixture.

Migration uses the existing `BEGIN IMMEDIATE`, reads `user_version` after acquiring
the cross-process lock, applies contiguous steps without `executescript`, checks
foreign keys and commits once. Schema <19 backfill still consumes only frozen
Evidence and per-NPC proofs; it no longer refreshes a removed cursor table. Schema
19 requires no backfill. Reopening Schema 20 does not recreate or rebuild work.
There is no downgrade migration: an older runtime rejects Schema 20 normally.

New tests use the actual archived Batch C runtime to create a Schema 19 save with
pending work. They inject an SQL failure after DROP, terminate a real process
after CREATE INDEX or DROP, assert rollback to Schema 19 and unchanged rows, then
open with three real processes and reopen twice. SQLite integrity and FK checks,
pending references, own watermarks and all retained tables are checked. Existing
actual Schema 1–18 upgrade tests continue to exercise older saves, own-proof-only
barrier exemptions and later-created NPCs. Unknown membership is never converted
into non-candidacy and historical Evidence is never rebuilt from current state.

## Reproducible synthetic performance evidence

Run:

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:PYTHONUTF8 = '1'
uv run --locked python examples/benchmark_perception_efficiency.py --npcs 200 --history 5000
```

The script archives actual baseline source `e23cde5` and runs both runtimes in
separate subprocesses/databases with the same interpreter. SQLite 3.53.1 was used.
No-signal Event/Evidence/Outbox commits use the real `_append_events` path with 200
NPCs. A separate one-NPC fixture seeds synthetic completed proofs through SQLite
constraints, retaining 5,001 complete rows plus 40 pending rows. This is a local
synthetic microbenchmark; it excludes LLM/JEV latency, real Host scheduling and
production load. VM-step tracing adds timing overhead. Counts and plans, rather
than sampled timings, substantiate the optimization.

| Operation | Batch C | PERF-01/02 |
|---|---:|---:|
| NPCs visited for perception refresh on one unrelated Event commit | 200 | 0 |
| NPCs visited for cognition refresh on that commit | 200 | 0 |
| Commit SQL statements, including BEGIN/COMMIT | 815 | 12 |
| Commit SQLite VM instructions | 32,959 | 323 |
| Perception / cognition checkpoint writes on commit | 200 / 200 | 0 / 0 |
| One NPC negative judgment SQL statements | 825 | 22 |
| Judgment SQLite VM instructions | 35,049 | 2,418 |
| Perception / cognition checkpoint writes on judgment | 200 / 200 | 0 / 0 |
| Core cursor calculation SQL statements | 1 | 1 |
| Core cursor calculation VM instructions | 25,511 | 70 |
| Queue API SQL statements, including authorization/transaction | 6 | 6 |
| Queue API VM instructions | 50,507 | 398 |
| Queue rows materialized in Python | 5,041 | 20 |

The cursor comparison measures Batch C's actual gap UNION/min against the new
full effective-prefix query. Batch C's persisted *read* was cheaper than its
recomputation; this change deliberately moves bounded work to reads and removes
write amplification. The benchmark does not claim every public read is faster.

The actual Worker query plan is:

```text
SEARCH npc_perception_queue USING INDEX npc_perception_pending
    (session_id=? AND npc_id=? AND event_seq>?)
```

The old audit plan uses `sqlite_autoindex_npc_perception_queue_1` over all history.
Tests also EXPLAIN the exact runtime cursor SQL and assert partial-index usage and
absence of a temporary sort for both runtime queries. The JSON evidence is emitted
under `.test-tmp/perception-benchmark-*/results.json` on each run.

## Validation

No `AGENTS.md` exists in this checkout or its parent directories. Commands follow
the Fast/Focused/Full tiers documented in the Batch C Review. Locked dependency
sync passed without changing dependency files. uv uses the repository cache;
the existing historical Python 3.14 environment required execution outside the
filesystem sandbox for interpreter probing. Initial sandbox attempts were stopped;
only completed runs are reported below.

| Check | Result |
|---|---|
| Fast: Commands, Events, Perception, Game Time | 76 passed, 19.27 s |
| Focused: new efficiency tests plus Batch C's nine related modules | 245 passed, 118.77 s; before the final additional migration-exception case |
| Additional focused Schema 20 migration-exception case | 1 passed, 6.16 s |
| Full | 837 passed, 339.66 s; baseline 820 + 20 new cases - 3 obsolete cache-write parameters |
| Ruff | Passed |
| Format | Passed; 121 files already formatted |
| `git diff --check` | Passed |

Completed test commands (all prefixed with `uv run --locked python -m`):

```text
pytest -q tests/test_commands.py tests/test_events.py tests/test_perception.py tests/test_game_time.py --basetemp=.test-tmp/perf-fast-final --tb=short
pytest -q tests/test_perception_efficiency.py tests/test_npc_perception_queue.py tests/test_event_local_evidence.py tests/test_perception_conditions.py tests/test_observation_retention.py tests/test_observation_eva_lifecycle.py tests/test_memory_gate.py tests/test_npc_modes.py tests/test_npc_outputs.py tests/test_npc_evaluation_tasks.py --basetemp=.test-tmp/perf-focused-final --tb=short
pytest -q tests/test_perception_efficiency.py::test_schema20_sql_failure_rolls_back_then_reopens --basetemp=.test-tmp/perf-final-migration --tb=short
pytest -q --basetemp=.test-tmp/perf-full --tb=short
ruff check .
ruff format --check .
```

All completed pytest runs report only the two existing upstream Lark
`sre_parse`/`sre_constants` deprecation warnings; there are no failures or skips.

New regression coverage includes 2,000 unrelated Events; pending pagination,
default/max bounds, exclusive boundaries, repeated reads, retry of Unresolved
first rows, negative out-of-order completion, grant replacement and cross-Session
isolation; EXPLAIN plans; simultaneous writer/reader snapshots during judgment
and Event append; actual Schema 19 exception/crash/reopen/multiprocess migration.
Existing SQLite tests retain multi-Signal aggregation, positive/negative judgment,
Unknown Discovery and Engine barriers, later-instantiated NPCs, Gate/EVA source
ordering, TTL, cognition completion, command/result idempotency, RNG/Receipt and
projection rollback checks.

Three obsolete fault-injection parameter cases were retired: INSERT/UPDATE on the
deleted perception cache, and UPDATE on the cognition checkpoint during perception
projection (which no longer occurs). Queue/Barrier/Inbox/Work/Outbox rollback cases
remain; Gate/EVA checkpoint rollback cases remain. Three tests which manually
lowered the old cache now inject an unknown-membership Barrier and assert the
same blocked read/Gate/EVA behavior. No business rule assertion is relaxed.

## Changed files and remaining costs

- Runtime: `perception_queue.py`, `service.py`, `perception.py`,
  `observation_lifecycle.py`, `npc_evaluation.py`, `npc_modes.py`, `npc_outputs.py`,
  `npc_state.py`, `_migration_20.sql`.
- Tests: new `test_perception_efficiency.py`; old cache gap/fault-injection fixtures
  in `test_npc_evaluation_tasks.py`, `test_npc_modes.py`,
  `test_observation_retention.py`, `test_npc_perception_queue.py`; latest-schema
  expectations in those and `test_commands.py`, `test_dnd_hardening.py`,
  `test_event_local_evidence.py`, `test_event_traceability.py`, `test_game_time.py`,
  `test_migrations.py`, `test_npc_outputs.py`, `test_npc_state.py`,
  `test_perception_conditions.py`, `test_versioned_state.py`.
- Evidence/documentation: `examples/benchmark_perception_efficiency.py`, README,
  this independent Review document.

Remaining costs: event-time Discovery still enumerates all applicable Scene
candidates and persists one reference per candidate. The per-event projection
still loads that Event's candidate metadata, and the global audit cursor retains
its existing algorithm. A barrier seek may walk many earlier barriers already
proven complete for this NPC. Cognition prefix and OB/retention/Gate queries still
scan the relevant NPC's Work/history; historical audit is intentionally unbounded.
Schema <19 backfill and startup FK checks retain their historical costs. The new
partial index adds storage and maintenance on queue insertion/completion. Host
scheduling, real LLM latency and production contention are outside this batch.

## Actual Diff review

Reviewed the actual runtime, migration, test and documentation Diff against
`e23cde5`, including all former cursor SQL readers, the lifecycle MRO, migration
ordering, authorization and retry boundaries. No P0/P1/P2 regression was found
within this batch. The unchanged per-NPC-proof predicate and the same-transaction
watermark/OB queries preserve the Batch C protections. Removed fault injections
target writes that no longer exist; remaining authoritative rollback assertions
still execute. The complete Full suite and the synthetic query/VM evidence above
support this conclusion without claiming production performance or a complete
perception performance overhaul. Delivery is confined to the named feature branch;
no merge or modification of `main` is performed.
