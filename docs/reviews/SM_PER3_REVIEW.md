# Batch C — Per-NPC Perception Event Queue & Cursor

Date: 2026-10-12. Baseline: `e88efa2`, merged Batch B on
`feat/sm-b19-b22-gameplay-domain-bridge`. Work branch:
`feat/sm-per3-npc-perception-queue`; initial working tree clean.
Only State Machine is changed. Engine / SRD pin, Package Digest, Signal Schema
and Batch B `event-evidence/4` producer-stage capture are unchanged.

## Ownership and implementation

There remains one Global CommittedWorldEvent Log, one Evidence record per Event,
and the existing Event Delivery/Perception Outbox. No NPC receives a copied
WorldEvent, hidden payload, another NPC's evidence, identity or Inbox.

Schema 19 adds three structures:

| Table | Meaning and constraints |
|---|---|
| `npc_perception_queue` | PK `(session_id,npc_id,event_seq)`, FKs to existing Actor and Event; unique typed Signal ID array, completion Boolean and `discovery` / `legacy_projection` provenance. Identity/scope and completed state are immutable; rows retained. Completion requires that NPC's own complete `perception_results` row. |
| `npc_perception_cursors` | PK Session/NPC; greatest event-sequence boundary before that NPC's first incomplete candidate or applicable quarantine. Noncandidate events need no judgment; ordering uses authoritative event_seq. |
| `perception_event_barriers` | One Session/Event quarantine with reason when stage/candidate completeness cannot be proved. References the original log; does not invent candidates, Boolean decisions, OB or per-NPC world copies. |

`perception_queue.py` contains reference creation, migration, boundary calculation
and authorized worker metadata. `service.py:_append_events` creates references
from frozen `/4` only, in the original Event/Signals/Evidence/Outbox/World/RNG/
Receipt transaction. It then refreshes NPC projection and cognition boundaries.
The same NPC's multiple Signals create one queue row in original Signal order.
Complete empty Signal/candidate sets create no queue or quarantine.

Public local worker interfaces (not a frozen network ABI or LLM Context):

- `get_npc_perception_queue(session_id, package, context=NPCContext)` returns
  ordered `NPCPerceptionEvent(event_seq, signal_ids, complete, provenance)` refs.
- `npc_perception_cursor(...)` returns only that authenticated NPC's projection
  boundary. The trusted principal / Session / observer grant is checked.
- `project_npc_event(session_id, event_seq, package, context=NPCContext, judgment=...)`
  independently returns resolved / unresolved / already_complete / not_candidate /
  legacy_unavailable. A missing historical scope is unresolved, not not_candidate.
  Authorization is rechecked after the callback, before writing.

`perception.py` shares the existing judgment and atomic OB/Inbox/Work/source writes
between owner aggregate projection and NPC-only projection. Positive/Negative/
Unresolved and partial answers stay in the existing `perception_results` audit;
queue completion commits with the complete own result and any OB. A worker for
one NPC neither calls another NPC's judgment nor completes its queue. Repeated or
concurrent workers adopt one durable result/OB/Work. No extra ACK is introduced.

## Cursor dependency audit

| Consumer | Current fence |
|---|---|
| `read_observations`, Recent / Retention / Expiry / Pending queries (`observation_lifecycle.py`) | Own NPC perception boundary, then the unchanged retention/processing projections. |
| Memory Gate input/adoption, eligibility and eligible routing (`npc_modes.py`) | Same own-NPC observation rows; no hidden later OB, other-NPC Work or expired text enters classification. |
| Legacy mode routing / interaction (`npc_modes.py:_pending_npc_observations`) | Own NPC boundary and original pending/occupied/completed constraints. |
| EVA source authorization (`npc_evaluation.py:_create_evaluation`) | Own NPC boundary, original ordered Work and task ownership checks. |
| EVA Recent Scope, input/recovery, Result provenance | Existing queries through own observation rows, frozen Task scope and unchanged TTL checks. |
| `npc_completed_cursor` (`npc_outputs.py`) | Own perception boundary and earliest Pending cognition Work; no advancement through the NPC's projection gap. |
| `perception_cursor`, Event Outbox Perception Done | Original Session aggregate completion audit / compatibility only. |
| Delivery Cursor | Unchanged Event Delivery Outbox protocol. |

An NPC's later candidate can finish and durably store OB/Inbox/Work first. These
remain hidden from its normal reads, Gate and EVA until its own earlier candidate
finishes. Another NPC's incomplete candidate does not fence these queries.
Negative completion releases order without an empty OB. Processing Completed,
Perception complete and Recent/Expired are separate: Completed Recent stays
readable; Pending Expired stays protected; neither TTL nor a read completes work.

## Unknown history and recovery policy

An incomplete Discovery, unavailable Engine stage, invalid/missing stored
evidence or unproven legacy stage creates durable quarantine. Every NPC without
its own completed proof is fenced there, including an NPC instantiated later.
No current Actor/Scene/senses query reconstructs a historical candidate set.
The original Evidence and reason remain available across restart; retries cannot
silently skip the gap. This intentionally preserves conservative blocking where
NPC independence cannot be proved.

Migration can reuse an existing completed `perception_results` for its own NPC
only (`legacy_projection`), preserving an already selected historical result.
This is not a claim that an old `/3` result used correct event-local evidence,
nor that another NPC completed. Global Outbox Done alone grants nothing. Known
empty historical Signal events need no candidates; the explicit pre-perception
legacy archive boundary remains the previously accepted compatibility policy.

Known `/4` queues with incomplete judgment are immediately recoverable by retry
with the original evidence, including after restart. Unknown-stage/scope
quarantine is restart-safe but cannot manufacture missing facts: release needs
future trusted historical evidence/proof and a reviewed recovery migration or
protocol. This batch adds no arbitrary release/skip/ACK or Evidence mutation API.
The Engine intermediate-stage protocol and legacy controlled recovery remain
OPEN; full combat observation delivery is not claimed.

## Migration and compatibility

`_migration_19.sql` plus `_migrate_perception_queues()` execute inside the existing
SQLite migration write lock and transaction, after older legacy boundary setup.
The SQL creates refs/constraints; Python strictly decodes stored Events/Evidence,
reuses frozen candidate mappings or own completed proofs, creates quarantine and
calculates NPC boundaries. It does not change old world, RNG, receipt, OB, Inbox,
Memory/Belief, Task, Gate, output or cognition completion records. Global cursors
are retained. Reopening version 19 never reruns backfill.

Actual archived `e88efa2` and `5deea78` code creates Schema-18 SQLite saves with
two Sessions, old Tasks/selected Results/private records, completed history and
one event partially completed across two NPCs. Three simultaneous openers upgrade
once; repeated restart preserves every original table row except explicit retry
attempt audit. `/4` pending work resumes; `/3` unknown remains quarantined while
the NPC with its own completed proof can read its OB. Failure during DDL or Python
backfill rolls back Schema 19 and all derived rows to the original Schema 18;
reopening repairs normally. Existing actual Schema 1–17 upgrade tests are retained.

Compatibility risks: older runtimes cannot open Schema 19; back up a closed DB
before upgrade. Migration enumerates stored event history under the write lock,
with no network/model work; no large-save performance benchmark is claimed.
Prior completed cognition cursors are retained as historical audit, even if an
unknown legacy event gives a stricter new projection boundary; new advancement
uses the NPC boundary and cannot reopen old completed Work. This is not data
clearing or historical re-evaluation.

## Test and code review evidence

`test_npc_perception_queue.py` exercises real SQLite and real producers:
cross-NPC Positive/Negative/Unresolved; strict same-NPC out-of-order gating;
multi-Signal aggregation; noncandidate Scene independence; empty Signals/candidates;
actual Engine unknown phase; late NPC quarantine; queue constraints; worker
authorization/revocation and Session isolation; concurrent same/different NPC
workers; idempotency/restart; Recent TTL / Pending expiry; Memory Gate and EVA;
event/queue/cursor creation and projection rollback; real history migration,
concurrent upgrade and DDL/backfill interruption. No fake WorldEvent/Evidence is
used to prove producer timing. Batch B and SM-PER-1 tests remain active.

Existing tests only change current-schema assertions 18→19 and the authorized
watermark semantics: three projection-gap injections now target the own-NPC
cursor, retaining rejection checks; the Batch B crossing test now proves NPC B
reads its completed entry while NPC A's exit is pending. Historical preservation
checks compare all old tables while admitting the three new derived tables.
No test is deleted and no mechanical/visibility gate is relaxed.

Executed on this branch with the locked dependencies:

| Check | Command / scope | Result |
|---|---|---|
| Dependency sync | `uv sync --locked --group dev` | Passed; no dependency changes. |
| Fast | `uv run --locked python -m pytest -q tests/test_commands.py tests/test_events.py tests/test_perception.py tests/test_game_time.py` | 76 passed, 29.63s. |
| Focused | `uv run --locked python -m pytest -q tests/test_npc_perception_queue.py tests/test_event_local_evidence.py tests/test_perception_conditions.py tests/test_observation_retention.py tests/test_observation_eva_lifecycle.py tests/test_memory_gate.py tests/test_npc_modes.py tests/test_npc_outputs.py tests/test_npc_evaluation_tasks.py` | 229 passed, 109.76s. |
| Full | `uv run --locked python -m pytest -q` | 820 passed, 313.79s; baseline 792 plus 28 new cases. |
| Lint | `uv run --locked python -m ruff check .` | Passed. |
| Format | `uv run --locked python -m ruff format --check .` | Passed, 118 files already formatted. |
| Diff | `git diff --check` | Passed. |

Each pytest run reports the same two existing Lark `sre_parse` / `sre_constants`
deprecation warnings. No failures or skipped regression cases are hidden in the
reported counts.

## Architecture synchronization required

Readonly Meta evidence is MODULE_CONTRACTS v0.10 §7 and
STATE_MACHINE_ARCHITECTURE v0.12. This user-authorized Batch C implements NPC
ordering without changing single ownership, Event-local capture, truthful
Unknown, atomic commits, existing OB/Gate/EVA or no-ACK contracts. The Batch A
statement that its own documentation work did not expand global Cursor design
does not freeze a shared Session fence as the only permitted implementation.
No conflicting accepted ownership/transaction requirement was found.

Meta should be synchronized in a separate documentation change:

- MODULE_CONTRACTS §7.2–§7.3: synchronous fixed candidates, per-NPC Event reference
  queue / completion prefix, aggregate Outbox audit, conservative unknown quarantine.
- §7.4 / §9.6 / §9.8–§9.9: Recent/Pending, Gate, EVA and cognition completion use
  that NPC's perception fence while Processing/Retention stay independent.
- §12.5: add independent NPC release, same-NPC ordering, own-proof legacy migration,
  unknown quarantine, concurrent recovery and atomic queue acceptance evidence.
- STATE_MACHINE_ARCHITECTURE §3.6–§3.7a, §6.2, §8, §10–§11: reflect current queue /
  query / migration responsibilities and distinguish this runtime from old baseline.

No Meta Repository or Rule Engine code is changed. Concrete field names, public
worker shapes and quarantine recovery protocol remain local candidates / OPEN.
Final targeted review found no new confirmed P0/P1/P2 in this batch. Source,
SQL constraints and real-database tests support independent NPC release,
same-NPC ordering, atomic result/queue completion, authorization and migration
preservation. Engine intermediate phases, unknown legacy scope recovery and the
Meta synchronization above remain OPEN; this does not claim that all historical
or combat perception can be delivered. No merge or PR is performed.
