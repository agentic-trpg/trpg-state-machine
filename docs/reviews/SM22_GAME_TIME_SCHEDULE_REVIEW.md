# SM-22: authoritative game clock and NPC schedule

**Batch-only completion review, 2026-10-11.** The earlier pre-commit audit stopped
on existing P1 `PER-19-22-01`; its evidence and the original stop decision remain
below. The user subsequently authorized independent SM-22 Commit + Push if SM-22
itself introduces no new P0/P1, while preserving that P1 as a **Global Merge
Blocker**. This review found no new SM-22 P0/P1. The original Perception defect is
neither repaired nor bypassed; it requires a later independent development batch.
This is a targeted SM-22 review, not the formal SM-19–22 Global Review. No Merge
or PR is authorized or performed.

## Delivered ownership and admission

`WorldState.game_tick` / `world_states.game_tick` is the sole Session clock. Local
`game-clock/1` uses abstract integer ticks 0..2147483647; ticks have no implicit
relationship to seconds, combat rounds, dice or real time. New Sessions and
Schema 1–14 saves start at tick 0. This local encoding does not freeze the Open
cross-module time protocol. No historical time is invented.

`TypedCommand(command_type="world.advance_time", payload=AdvanceGameTime(...))`
requires the adapter's trusted `can_advance_time` grant and selected PC envelope.
The same world-version fence, durable command fingerprint, original receipt,
savepoint and SQLite transaction apply. A same-tick command does not advance time;
if schedule state is already reconciled it is a no-op, otherwise the normal
schedule reconciliation can still commit a lifecycle change. A wrong expected
time is a durable `game_time_conflict`. Backwards/bool/overflow
input is invalid. Grant revocation is checked even on command replay. Active
combat time advancement is unsupported: SM does not infer rounds or effect expiry.

## Runtime lifecycle, not cognition or movement

`game_time.sync_npc_schedules` reads the existing immutable Runtime schedule,
current tick and actual Actor location. When represented mechanical state exists,
only its committed `is_alive` / absence of conditions admit activity. SM never
derives death from HP or decides condition mechanics. For synthetic NPCs with no
mechanical state, the activity is only a domain label, not a mechanical capability.

Activities start within their declared window at the actual matching scene;
continuation changes no NPC version. An active activity completes at its end,
or is interrupted by location/life/condition mismatch. A never-started expired
window is recorded as `missed`, not fabricated completion. Terminal runs never
restart on re-entry. Initial Package activity remains at tick 0; schedule execution
begins at the first authorized positive time sample. Overlapping eligible entries
are explicitly unsupported; no arbitrary priority is inferred.

The original world command reconciles schedules after its world operations and
bounded existing cascade. Entering a new Scene lazily creates the same NPC identity
and reconciles at actual tick, without reinitializing any old Meta. Runtime changes
increment NPCStateVersion once per affected NPC and cancel running Tasks that used
the previous version. Their pending Observation work and Mode remain unchanged.
No autonomous Background Task, LLM, Gate bypass, cognitive update or movement is
created by time. A Host can request a fresh Task through the existing Mode/Gate.

## Persistence and compatibility

Schema 15 adds one World column, monotonic clock trigger, `npc_schedule_runs`, a
single-active-run index and retained/terminal-transition triggers. Runs use the
existing `(session_id,npc_id)`, stable schedule ID and deferred FK to the original
CommandReceipt; events retain the original start/end command provenance. No static
schedule or Observation is copied. Authorized `NPCView.schedule_runs` exposes only
that NPC's lifecycle and ticks, without internal command provenance. Profile,
private records and Package digest are unchanged.

Clock, runtime, run, NPC version, stale-task cancellation, events/evidence/outbox,
WorldVersion and receipt are in one existing short write transaction. More than
32 resulting events rejects atomically through the existing cascade limit. No
periodic worker, second scheduler, ACK, proposal ID or mode epoch is added.

## Evidence and remaining boundaries

`tests/test_game_time.py` adds 29 cases: lifecycle/continuation/completion,
duplicates/conflicting fingerprints, grant/input/time conflicts, missed windows,
movement/interruption/revisit, dual Sessions, seven real SQL failpoints, concurrent
ticks and Task/Mode races, stale result refusal/fresh Task, real Engine death and
combat admission, lazy instantiation, bounded event rollback, fresh-process replay,
actual archived Schema-14 runtime data, concurrent migration and interrupted
migration retry. Independent SQLite comparisons cover all tables and foreign keys.
The synthetic vertical test uses actual Event → Observation → Gate Background Task
→ five-kind Meta + Move handoff → TypedCommand → Clock/Schedule. It does not claim
to run a real NPC model or copyrighted adventure.

Full locked pytest: **659 passed**, 2 existing Lark warnings, 203.22s
(630 previous + 29 added). This run had already started when the blocking audit
finding was reproduced. Locked sync, Ruff check/format (98 files at final check) and diff check
pass. The reproduced perception case is not covered by those 659 tests; it remains
a separate failing behavioral acceptance check. Existing test changes update
only the expected latest Schema from 14 to 15; historical source-version assertions
and data preservation checks remain intact. Complex world-condition AST, recurring/overlap policy,
autonomous Schedule-to-EVA Gate policy, combat time and mechanical effect duration
remain unsupported or Open; this Batch implements deterministic activity lifecycle.
Observation TTL/retention and maximum-wait policies are not implemented by this
clock foundation; their specific fields and timing policies remain separate work.

## Completion review scope and file audit

All 18 previously uncommitted paths belong to SM-22:

| Paths | SM-22 purpose |
| --- | --- |
| `src/trpg_state_machine/game_time.py`, `_migration_15.sql` | Clock command implementation and persisted schedule lifecycle |
| `src/trpg_state_machine/__init__.py`, `models.py` | Typed operation export, trusted time grant and World clock projection |
| `src/trpg_state_machine/commands.py` | Closed payload admission, authorization, original idempotency/transaction and bounded reconciliation |
| `src/trpg_state_machine/npc_state.py` | Own-NPC lifecycle view through existing authorization |
| `src/trpg_state_machine/service.py` | Schema 15 migration registration, initial clock and snapshot/operation integration |
| `tests/test_game_time.py`, `_clock_resume_worker.py` | 29 added real SQLite cases and actual process recovery |
| `tests/test_commands.py`, `test_dnd_hardening.py`, `test_event_traceability.py`, `test_migrations.py`, `test_npc_outputs.py`, `test_npc_state.py`, `test_versioned_state.py` | Only latest-Schema assertions 14 → 15; no weakened historical data or recovery assertions |
| `README.md`, this review | Implemented scope, limitations and retained Global Merge Blocker |

The audit reread the actual clock/runtime source, migration SQL, original command
savepoint/receipt path, authorized NPC projection, SQL-fault tests, concurrency
tests and process-recovery worker. Migration still acquires `BEGIN IMMEDIATE`
before reading `user_version`, executes all steps atomically and checks foreign
keys. The new lifecycle FK defers until the original receipt is present at commit.
Time is a single monotonic World column; schedule changes neither move Actors nor
update private cognition/RNG. Stale Task cancellation shares the runtime/version
transaction, preserving Mode and Observation work. Terminal runs cannot restart.

The completion pass changes documentation only. `perception.py` has no diff;
`service.py:_append_events` remains unchanged from the already-pushed HEAD. No
Perception fix, substitute evidence, new ACK, or Global Review is included.
Targeted result: no new confirmed SM-22 P0/P1; independent batch delivery is
permitted by the user's follow-up. The whole integration remains blocked for Merge.

Completion validation rerun on 2026-10-11, using the saved SM-22 code:

```text
uv sync --locked --group dev                         PASS
uv run --locked python -m pytest -q                  659 passed, 2 warnings in 230.08s
uv run --locked python -m ruff check .               All checks passed!
uv run --locked python -m ruff format --check .      98 files already formatted
git diff --check                                   PASS
```

The 29 added cases bring the previous 630 tests to 659. The two warnings are the
existing Lark `sre_parse` / `sre_constants` deprecations. This green rerun validates
SM-22 and the existing automated regression suite; it does not close or suppress
`PER-19-22-01`, which remains a Global Merge Blocker. Only this validation record
and documentation wording were updated during completion; no runtime/test code
or Perception behavior was changed.

## Blocking cross-batch finding PER-19-22-01 (P1)

Current View fail-closed admission does not protect Event → Observation projection.
`service.py:_append_events` (lines 534–548 in this candidate) saves only Actor
identity, location and static senses in `event-evidence/2`. It omits the committed
mechanical environment and relevant observer state. `perception.py:project_event`
(lines 232–243) treats an explicit static visual sense plus `visibility='clear'`
as perceived, without that missing evidence. `_scene_perception_clear` protects
only current reads. This conflicts with Module Contracts §7.2 event-time evidence
and the SM-20 requirement to fail closed for unsupported mechanical perception.
The projection shortcut already exists in the starting baseline; SM-20 did not
close this full-path gap. A green existing suite does not disprove the finding.

Actual reproduction, using original `npc_station.yaml`, the pinned real Engine
asset loader and `modern_data` (no fake RuleEvaluationResult or direct DB editing):

1. Remove the optional combat foundation; set the noncombat foundation's initial
   Scene grid `default_lighting='dark'`. `_validate` accepts this Package.
2. Create a Session, grant its caretaker an NPCContext, read the current Scene View:
   `status='unresolved'`, no visible actors or objects.
3. Submit an authorized `set_object_state` opening `object.platform_gate`:
   original CommandReceipt `committed`.
4. Project all actual committed events and read the caretaker's Inbox. It contains
   visual `Someone enters the scene.` and visual `An object changes position.`.
5. The actual object's Event Evidence contains just `actors` / `schema`, with
   caretaker static senses `{auditory:true, visual:true}` and no dark Scene evidence.

Reproduction SQLite file (ignored, not staged):
`.test-tmp/perception-dark-80611f6638b047f78580756b42b7401b.db`.
The entry Scene really was dark at creation and at object commit; this is not
an inference from a later observer position or a corrupted test-only snapshot.

Minimum next fix: capture sufficient channel/observer/environment admission at
Event commit, and project from that immutable evidence; unresolved or unsupported
mechanical perception must not become true/false by static Scene co-location.
Legacy evidence needs an explicit compatible unknown boundary, not current-state
backfilling or invented history. Add dark/condition/partial-Signal/restart tests
in that later independent repair. The original audit stopped before SM-22 delivery;
the user's follow-up permits SM-22-only delivery without closing this finding.
No production Perception fix was made, no Global Review was claimed complete,
and no Merge/PR was made.

Recommendation: **SM-22 Commit + Push permitted; Global Merge Blocked**. Confirmed
existing open P1: 1; no new SM-22 P0/P1 identified in this targeted audit. Complete
the independent Perception repair before approving the integration as a whole.
