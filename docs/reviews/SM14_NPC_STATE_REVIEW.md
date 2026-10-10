# SM-14 Batch Review — NPC Persistent State & Authorized View Foundation

Date: 2026-10-10. Branch: `feat/sm-b14-npc-persistent-state`.
Baseline: `4e5cc5ac2765be89f0b4164bc6923083539374c4`.
Meta main verified by fetch: `fc41d3c584816198ba4f23bb4d326655ea3c774d`.

## Scope and architecture review

Reviewed Module Contracts §§7/9/10 as the highest cross-module authority;
State Machine Architecture §§3.4/3.8–3.9/6; Agent Architecture §§4–6/8;
Adventure Package Schema §§5/9 and MVP Scope. No removed ADR overrides these
contracts. No confirmed architecture change or conflicting requirement was needed.
Concrete NPC field encoding remains local `npc-state/1`, not a frozen public ABI.

The final source/SQL/diff review follows Package approval → Session creation →
lazy Actor/Scene initialization → private-row persistence → authorized read →
fresh-process resume, as well as concurrent migration and commit-failure paths.
`actors` remains the only identity/location model. `npc_access`, Perception,
`event_observations` and `npc_inbox` are reused without modification. NPC Meta
does not enter Engine Snapshot/Delta, combat permissions, Event payloads or RNG.
No independent service, controller execution, model Context or shadow state exists.

## Implementation and evidence matrix

| Requirement | Actual implementation | Actual SQLite/test evidence |
|---|---|---|
| Unique Session/NPC state | `npc_state.py`, `_migration_10.sql`; FK to existing Actor, NPC-kind trigger | `test_complete_initial_private_view_and_belief_history`; `test_sqlite_ownership_and_immutable_history_constraints` |
| Complete typed private domain view | Frozen Profile, Memory, Belief, Relationship, Goal, Plan, Schedule/Activity and NPCView; full ordered histories, no semantic selection | Complete-view test checks each domain field, versions, sources, confidence and selected PC target |
| World Fact / Known Fact / Belief separation | Static `world_facts`, scoped `npc_known_facts`, separate private Belief records | Wrong wooden-lamp Belief coexists with unchanged metal-lamp SQL World Fact; DM-only and other-NPC grants reject |
| Versioned Belief history and provenance | Contiguous content versions, latest current marker, active/retracted state; Package or same-NPC Memory sources | Two initial history versions remain present after read/revisit/restart; invalid gaps/current markers/duplicate IDs/sources reject |
| Private identity and authorization | `get_npc_view` reuses `_npc_authorized`, durable Principal/Session/Observer binding and Package pins in one read transaction | Six unauthorized cases, revoked grant, cross-NPC/cross-Session access and digest mismatch; all stored rows unchanged |
| View is not mechanical control or ACK | No write/cursor/Inbox calls in view; command control remains existing `TrustedContext.controlled_npc_ids` | `test_view_does_not_ack_observations_or_grant_command_control` verifies real Inbox, projection state and all SQL rows before/after |
| Lazy initialization and idempotency | Successful existing Actor INSERT alone seeds private state; existing Session creation transaction/deduplication unchanged | Concurrent identical creation yields one Session, one NPC state and seven initial private records; retry returns original creation result |
| Revisit / move / reload / restart | Reads persisted rows; current location joins `actors`; no Package reseeding | `test_revisit_movement_reload_and_fresh_process_restore` compares actual private rows and complete views/Snapshot in a fresh Python process |
| Old Package compatibility | Optional Profile, explicit unconfigured state; three controller kinds are inert metadata | Original Fixture accepted unchanged; absent Profile/Runtime remain null, private collections empty; full SM-1–13 regression retained |
| Atomic SQLite initialization | Six new tables participate in existing short transaction with state/RNG/Event/Outbox/receipts | Six table-specific insert failures, lazy Scene failure and deferred source FK commit failure leave no partial state or creation mapping; same request retries |
| Schema 1–9 migration | Lock/version recheck preserved, atomic Schema 10; legacy NPCs explicitly unconfigured, no cognition invented | Nine actual historical-code databases, concurrent three-instance upgrades, old columns/rows preserved, repeated upgrade/restart unchanged |
| Interrupted migration recovery | Existing transaction rollback protocol includes all new DDL/backfill | SQL-error injection and real `os._exit(23)` during migration retain exact Schema 9 data, then successfully upgrade |
| Closed validation and history constraints | `npc_schema.py`, typed record JSON checks, ownership/source/Plan→Goal FKs, immutable historical content/provenance | 25 invalid template cases plus nine direct SQL constraint cases; loader returns structured invalid and no approved partial Package |

## Files and compatibility

New runtime files: `npc_state.py`, `npc_schema.py`, `_migration_10.sql`.
Existing production changes are confined to `package.py` and `service.py`:
optional templates/static facts/controller metadata, Schema 10 registration and
initialization at the existing Actor creation fence. No Engine dependency, rules,
command protocol, Snapshot/Delta, Perception or combat authorization source changed.

New test assets: `tests/test_npc_state.py`, `tests/_npc_resume_worker.py` and original
CC0 `tests/fixtures/npc_station.yaml`. Six existing test files only update the
latest-schema assertion (or, in `test_package.py`, the invalid shape/unknown
controller expectations now that reviewed metadata is supported). No existing
test was removed. README and `docs/SM14_NPC_STATE.md` describe the actual boundary.

Schema 10 adds `npc_states`, `world_facts`, `npc_known_facts`,
`npc_private_records`, `npc_record_sources` and `npc_plan_goals`. All primary and
foreign keys are Session-scoped; source Memory and Plan→Goal relations are also
NPC-scoped. SQL JSON/text bounds complement Package/list/type validation. The
existing migration lock, WAL, foreign_keys and short-transaction protocol remain.

Already instantiated legacy NPCs are marked `legacy/unconfigured`, not silently
enriched from an old or newly supplied Package. Missing facts/history remain
unknown. Package digest mismatch still rejects access/recovery. Downgrading a
Schema 10 database with old code is unsupported; no downgrade migration is supplied.

## Executed validation

Final complete regression: **462 passed** (396 existing + 66 new SM-14 cases),
2 upstream `lark` deprecation warnings, 114.89 seconds. Locked dependency sync,
Ruff check, Ruff format check (70 files) and `git diff --check` all passed.
No failure remained in the final run.

Commands:

```powershell
uv sync --locked --group dev
uv run --locked python -m pytest -q
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
```

Historical migration tests use `git archive` of the recorded original commits,
run those original Python modules to create actual databases, and inspect original
columns/rows after upgrading. Schema 8 uses the existing locked legacy environment;
Schema 9 uses the current pinned Engine environment. No fabricated user_version
or Mock substitutes for an old database. The restore worker opens the committed
database in a new process, resumes the noncombat Session and reads both complete
authorized NPC views. Failure tests assert database contents, not just error text.

During development the expanded attendant Fixture exposed a test's array-position
assumption; the isolation assertion now selects by stable Memory ID. This was a
test correction, with no runtime ordering change.

## Remaining scope and OPEN items

- Runtime Meta writes, NPCEvaluationTask/EvaluationReceipt, seven-field submit,
  Atomic Meta Commit, Mode switching, Gate, Schedule Tick, LLM Runtime/Prompt
  Builder, cognition generation, Action Intent handoff and extra ACK are not
  implemented. No proposal_id or mode_epoch was added.
- This Batch initializes only reviewed static Package and same-NPC Memory sources.
  Runtime Event/Observation source variants, evidence authorization, expected
  NPCStateVersion, Task validity, whole-Meta append semantics, Result idempotency
  and reliable output handoff must be reviewed together in the subsequent Task
  Batch. The shared record model is not a claim that runtime evaluation is present.
- NPCStateVersion starts at 1; content version is separate. Runtime version
  advancement and world_version policy for Meta-only/event-only commits remain
  OPEN. Schedule ticks/confidence units/private field encoding are local choices.
- No semantic truthfulness checking is claimed: explicit references/permissions
  are checked, subjective text can be wrong, and approved private Profile content
  remains the Package author's review responsibility.
- Existing bounded real Engine/F6 support and active-combat recovery exclusions
  are unchanged. This Batch does not advance full MVP or autonomous NPC acceptance.

## Findings and recommendation

Within this Batch's reviewed source, SQL, permissions, transaction and recovery
scope, no remaining P0/P1/P2 issue was confirmed. No duplicated identity/Inbox,
extra authority, arbitrary Host Meta writer or unapproved Task/Mode contract was
introduced. The explicit legacy-unconfigured behavior and runtime evidence-source
deferral are documented boundaries, not fabricated decision capability.

Recommend merging the **SM-14 persistent-state/authorized-read foundation** after
human review and successful final checks. This does not recommend enabling NPC
autonomous evaluation or claiming Task/Mode/Meta Update implementation. This work
does not merge main or create a PR.
