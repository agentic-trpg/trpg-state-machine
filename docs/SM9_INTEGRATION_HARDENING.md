# SM-9 — integration hardening and explicit recovery boundary

## Implementation

- Schema 8 gives each new Trigger History record a deferred foreign key to its
  exact committed `event_records(session_id,event_seq)`, not merely the source
  command. Queue order determines the future event sequence in the same atomic
  commit. A command with flag→object→flag effects has distinct verified event
  sources. Once-per-session deduplication and cascade ordering remain unchanged.
- Older trigger records retain their identity/command/deduplication but receive
  `source_event_seq=NULL, source_event_status=legacy_unavailable`. Migration does
  not guess a source from multiple events or invent historical Signals/Evidence.
  Tests upgrade real archived Schema 3/6/7 databases, repeat startup and inject a
  failure in Schema 8 DDL to prove rollback/recovery and row preservation.
- Only a committed Engine `IntentSubmitted(attack)` gets the standard visual
  Signal “Someone makes an attack.” Raw Engine events remain trusted internal
  facts. NPC Inbox contains only authorized PerceivedSignals, not HP, rolls,
  equipment, RNG, rejected intents or uncommitted proposals. Existing Outbox,
  event-time evidence, perception ordering and inbox mechanisms are reused.
- Scene traversal, Actor relocation and object changes during active Combat are
  explicitly refused because their mechanical effects are not represented by this
  candidate's attack Delta vocabulary. The same guard covers Package consequences.
  Nonmechanical scene flags remain supported. There is no parallel world/combat
  position update path or silently stale mechanical snapshot.
- Real Delta validation checks closed operation types, pre-values, actor/combat
  ownership, bounds, duplicate fields, monotonic damage/death ledgers and proposed
  event Actor references. Configuration, missing required fields, unexpected
  choices and malformed Delta remain retryable independent errors.
- The old synthetic slice now refuses unsupported difficulty choice before
  invoking its Evaluator. Unexpected post-admission choices are independent
  protocol errors. Its existing choice regression now checks zero calls, rather
  than endorsing conversion of an evaluated choice to a normal rejection.

## F6 acceptance scope

The final review contains the detailed contract matrix. The real path uses actual
upstream versioned request/result/snapshot/delta/RNG/availability models. The local
synthetic fixture protocol is not extended into a second D&D ABI. Full related
mechanical state is mandatory, RNG is separate, and the current whole-session OCC
read fence is conservative and complete. `NonCombatSnapshot` discriminator/schema
is checked against actual upstream types, but real noncombat evaluation is not
implemented and is explicitly out of this basic-attack slice.

Formal PendingOperation/Continuation remains out of scope. Missing explicit choice
is refused before evaluation; a needs_choice result after admission is an error
with no receipt, RNG, Delta or events. No reaction engine was added to close F6.

## Vertical evidence and blocker

`test_real_vertical_pipeline_committed_observations_fresh_process_audit` creates a
Session, initializes the Package's restricted encounter, executes real PC and NPC
attacks, commits actual state/events/outbox, projects and delivers authorized
observations, then closes the runtime and starts a new Python process. That process
checks the exact authoritative Snapshot/RNG, reads persisted Inbox and replays the
original receipt without evaluation. SQL versions and foreign keys are also checked.
Legacy registry, initialization and NPC AI entry points are forbidden in this test.

This proves durable audit/reconciliation, **not successful safe combat continuation**.
The candidate lacks a stateless combat.end API and does not set `ended` after a
lethal attack. Architecture SM-02 prohibits successful ordinary save/resume for an
active Combat. The new process therefore explicitly refuses save, resume and a new
attack while retaining every committed fact; original receipt replay is permitted.
Normal completed-combat safe save/resume is **Blocked**, reported during SM-7.
Implementation of that affected acceptance is paused pending upstream contract/
human clarification; no local end condition, checkpoint rewind or fake resume was
introduced. This is a cross-repository capability gap, not a failed SQL restore.

Additional tests cover real Schema 7 attacked-combat migration, zero-HP PC
conditions/damage ledger, independent RNG conflicts, Required Result fields,
unsupported world changes, unknown/duplicate/bad-prevalue Delta and configuration
change during evaluation. Cascade overflow rolls back candidate HP, world flags,
RNG/events/trigger history and stores one durable rejection; injected storage faults
store no rejection and permit the exact same request to retry.

## Verification

SM-9 adds **27** executed cases (19 hardening + 8 traceability). The full suite
completed **273 passed, 2 dependency deprecation warnings in 77.49s**, preserving
the existing 246 cases. Locked dependency sync, Ruff check, Ruff format check
(45 files) and `git diff --check` passed. No database/cache/temporary fixture
runtime was added to tracked files.

```powershell
uv sync --locked --group dev
uv run --locked python -m pytest -q --tb=short --basetemp=.test-tmp/sm9-verified
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
```

The full SM-9 vertical acceptance remains **Partial**: durable cross-process
audit/replay is verified, while normal completed-combat safe continuation is
**Blocked** on the upstream completion contract. Passing tests do not close that
acceptance gap. Global Review is a separate read-only phase and documentation commit.
