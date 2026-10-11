# NPC Observation Lifecycle: Global Review

Reviewed on 2026-10-11 after the three independent implementation commits and
pushes. Runtime baseline: SM-22 `491e98c86ce1584e21af47acfd8fc32bcc3efbbc`.
Architecture baseline: Meta `307cde1d36170ff021564ff5155ba6f349f35e80`.
Branch: `feat/sm-npc-observation-lifecycle`.

**Do not merge this branch yet. PER-19-22-01 remains an unresolved P1 / Global
Merge Blocker.** No new confirmed P0/P1/P2 was found in this cumulative review.
The three bounded batches are implemented and tested; this conclusion does not
claim a working Agent Host, JEV model or autonomous NPC runtime.

## Commits and evidence

| Batch | Actual commit | Added cases | Full locked pytest | Push |
|---|---|---:|---|---|
| OB-1 | `413165c26f01c59c6d3d061a95059447f0b39857` | 9 | 668 passed, 215.85s | Successful |
| OB-2 | `e6949755f3d5044c30ce159e5a2dc35736e75b9d` | 25 | 693 passed, 233.99s | Successful |
| OB-3 | `82978907d00f4bd7978b7e11ed150e766bed5bf7` | 22 | 715 passed, 272.35s | Successful |

The starting suite had 659 cases. There are **56 added cases, 715 total**. Each
batch independently passed all five required commands:

```text
uv sync --locked --group dev
uv run --locked python -m pytest -q
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
```

Ruff formatting checked 102, 104 and 107 files respectively. Each full run had
two existing Lark deprecation warnings (`sre_parse`, `sre_constants`), no failures.
After all three commits, this additional committed-code combination check passed
**56 cases in 23.68s**, with the same two warnings:

```text
uv run --locked python -m pytest -q tests/test_observation_retention.py tests/test_memory_gate.py tests/test_observation_eva_lifecycle.py
git diff --check 491e98c..HEAD
```

Individual reviews retain the batch-specific evidence:
[OB-1](OB1_OBSERVATION_RETENTION_REVIEW.md),
[OB-2](OB2_MEMORY_GATE_REVIEW.md),
[OB-3](OB3_EVA_LIFECYCLE_REVIEW.md).
This final review adds documentation only; it does not change tested runtime code.
Final Ruff check, Ruff format check (108 files) and diff check also passed before
the documentation commit; only this review document is staged for that commit.

## Independent cumulative inspection

The review inspected the actual three commits and cumulative diff, all three SQL
migrations, read/authorization paths, Gate adoption, Task creation and source
validation through the existing Result/Meta/output/completion transaction. It
compared behavior with MODULE_CONTRACTS §7.4, §9.6–9.9 and C-A36–C-A44, plus the
Agent Architecture Gate/EVA/Context Lifecycle and State Machine Architecture
Processing/Retention/Game Time requirements.

Before this review document, the cumulative diff contains 25 files, 2,193
insertions and 35 deletions, all in this repository. Runtime changes are confined
to the new lifecycle module, three migrations, `npc_modes.py`, `npc_evaluation.py`,
one source-scope line in `npc_results.py`, and migration/MRO registration in
`service.py`. README, tests, migration workers and batch reviews account for the
other paths. There is no dependency/version change, Rule Engine change or Meta
Repository edit. No test database, cache, model or generated asset is tracked.

Baseline comparisons verified unchanged Perception, Event, World Command,
game-time/schedule, output dispatch, D&D runtime and Package source files, and
unchanged `service.py:_append_events`. Thus the inherited Event-time Evidence
defect has not been repaired or worked around. Existing test edits mostly advance
the latest-schema expectation. Two compatibility assertions now distinguish
preserved original Work columns/rows from added unknown timing, and retained
legacy Pending evidence from ordinary Recent input. Historical source-version
assertions and migration preservation checks remain.

## Functional and DECIDED contract matrix

"Implemented" below means the tested local State Machine domain behavior. It
does not freeze an OPEN cross-module wire format or imply actual model execution.

| Contract / capability | Status | Actual implementation and test evidence |
|---|---|---|
| §7.4, C-A36: independent Processing and Retention; Completed + Recent | Implemented | `observation_lifecycle.py` derives validity independently of Work status. `test_pending_completed_recent_boundary_and_original_receipt` and `test_four_classifications_processing_recent_deadlines_and_idempotency` verify No-op/empty Result completion, Recent reads and stable replay without reopening Work. |
| §7.4, C-A37: authoritative Game Time and exact expiry | Implemented, local policy | Migration 16 records first durable Work adoption and expiry; reads use only `world_states.game_tick`. Boundary/jump, time-transaction rollback and fresh-process tests exclude expired OB without completing Pending Work. No wall-clock or combat-round conversion exists. |
| §7.4, C-A38: multiple Meta sources and history | Implemented | Existing `npc_record_sources` includes all five Meta kinds and historical Belief versions. `test_multi_source_history_archives_without_text_reinjection` verifies protected evidence and sources remain in SQLite while Recent text disappears. |
| C-A39: cleanup safety, identity, Receipt, Cursor and Work protection | Implemented conservative boundary | No physical cleanup is attempted. Migration 16 prevents Observation/Inbox/Work deletion; existing source/FK, Task, output, receipt, dedupe and cursor rows remain. Projection gaps, rollback/retry and old-save tests verify preservation. Discardable calculation/GC is explicitly out of scope. |
| §9.8, C-A40: separate Perception and Memory Gate contracts | Implemented Gate domain boundary | `MemoryGateInput` / `MemoryGateJudgment` use the existing routing record and trusted NPC grant. Exact coverage, foreign/unknown/duplicate/missing/type errors, Perception-type misuse, revocation, stale input and SQL-failure tests reject candidates without defaulting to Irrelevant. Perception Judgment is unchanged. |
| §9.9, C-A41: Inactive routing and bounded batch eligibility | Implemented authorization | `get_npc_evaluation_eligibility` and `route_npc_observations(classification="eligible")` prioritize Urgent, then deadlines; Deferred/Uncertain remain Pending until authorized processing. Both-mode and periodic-batch tests verify Background authorization and controlled Irrelevant No-op without mandatory Memory. |
| §9.9, C-A42: Interactive routing, single EVA and new arrivals | Implemented authorization | Existing Task creation and single-running constraint are reused. Interactive gets Interactive Tasks, ordinary interaction can batch without Gate, and occupied NPCs return queued scope. `test_new_urgent_during_task_never_expands_input_or_completion_scope` verifies immutable Work/Recent grants and later priority. |
| §9.9, C-A43: waiting deadlines, failure and recovery | Implemented bounded domain policy | Migration 17 anchors eligibility/latest-start to first adoption, prohibits postponement and enforces execution margin below TTL. Deadline/restart, mode/expiry race, budget failure, occupied Task and recovery tests cover delayed execution. Eligibility is not a completion guarantee or physical job queue. |
| §9.6–9.7, C-A44: expiry synchronization before later invocation | Implemented domain query | `get_npc_observation_expiry` returns own-NPC validity metadata, without ACK or Context effects. Ordinary Task reads exclude expired text; explicit `get_npc_evaluation_recovery_input` supplies only active, version-current, principal-bound Task Work marked expired/unknown. Expiry/recovery/revocation/fresh-process tests verify both paths. |
| Authorized Recent background and long-term Meta provenance | Implemented | Migration 18 stores immutable Task-to-existing-Inbox references. `NPCEvaluationInput.recent_observations` includes authorized Completed Recent OB separately from Work. `_meta_observation_scope` permits original Work plus fixed Recent grants still valid at commit. Foreign/new-arrival/expired-Completed source tests reject unauthorized Meta. |
| Atomic Result/Meta/output/Work and durable idempotency | Preserved and regression tested | Existing sole selected Result and EvaluationReceipt commit with Meta, reliable handoffs and Work completion. Three authorization and five Result write failpoints verify real SQLite rollback and safe retry. Repeated selected Result after expiry/restart returns the original receipt without new writes. |
| Old-save, concurrent migration and restart compatibility | Implemented | Real archived Schema-15 and Schema-17 runtimes create databases with actual records. Tests upgrade/reopen them, preserving original columns, rows, Gate results, Tasks, sources, RNG and receipts; concurrent/repeated migration and fresh-process recovery are verified. The complete older migration suite also passes. |
| Existing PER-19-22-01 | Blocked, inherited P1 | Preserved evidence in [SM-22 Review](SM22_GAME_TIME_SCHEDULE_REVIEW.md#blocking-cross-batch-finding-per-19-22-01-p1); excluded from this repair scope and still blocks Global Merge. |
| Actual JEV/EVA, Host Context, timers/workers/model queue, physical forgetting | Explicitly out of scope | No model/API/Prompt/Context/KV implementation or new physical execution queue exists. All production model invocation and resource management remain Host work. |

## Storage, ownership and compatibility

Schema 16 extends only existing `npc_observation_work` timing and adds retention
guards. Old Work stays `NULL/NULL` (`legacy_unknown`), never given fabricated
event time or a fresh TTL. Schema 17 adds classification and domain scheduling
ticks to that same Work. Known first-adoption times get anchored deadlines;
unknown legacy times remain unknown and are explicitly recovery-eligible.
Schema 18 adds `npc_evaluation_recent_scope`, an immutable ownership-checked
reference relation to existing Task/Inbox, not another Observation store or queue.
Old Tasks retain original Work and gain no fabricated Recent grants.

All three migrations run under the existing cross-process SQLite migration write
lock with schema version recheck and foreign-key validation. Candidate adoption,
Task authorization and Result commit reuse existing short atomic transactions.
There are no external model/network calls inside them. Failed Result validation
uses the existing bounded failure counter; it cannot partially append Meta or
outputs. Projection's existing independently committed attempt counter is
explicitly allowed to advance on a failed projection attempt; the failure test
checks all other authoritative rows remain unchanged and Work can retry.

There is still one authoritative World/NPC state, one Observation/Inbox/Work
identity and one cognitive Task mechanism. Gate request snapshots/results in
`npc_routing_requests` provide authorized scope, audit and idempotency, not a new
authoritative perception store. No Gate Task ID, Proposal ID, mode epoch or ACK is
introduced. Reads neither consume observations nor advance Completed Cursor.

Legacy `background`, `safe_retention`, `no_op` and `uncertain` remain separate
from the new four-class typed path. Existing Interactive legacy routing keeps
its original semantics; the new explicit Irrelevant candidate can authorize
No-op in either mode. Mixed protocols are rejected. Replayed adopted Gate/route
results remain fixed, including queued/no-eligible results; later authorization
uses a new request key. Grant and Package checks still apply on replay.

## Findings, risks and OPEN decisions

**Confirmed existing bug:** PER-19-22-01 is P1 and remains a Global Merge Blocker.
An original dark-scene reproduction shows Current View unresolved but visual
Event projection confirmed from insufficient event-time mechanical evidence.
The original audit/reproduction is retained verbatim in the SM-22 review. Green
lifecycle tests do not establish historical perception correctness. New tests
start from lawful synthetic observations and do not treat that defect as an
acceptable visual fact. Its independent repair must capture sufficient committed
event-time evidence and preserve explicit unknown handling for legacy evidence.

**New confirmed bugs:** none found in this review (P0: 0, P1: 0, P2: 0). This is
source/SQL/behavior evidence within the stated scope, not proof of absence of all
possible defects or production load certification.

**Potential operational risks, not confirmed bugs:** conservative retention grows
storage; full Recent reads and persisted Gate input snapshots may be expensive
with large histories. Gate input uses a local size bound and batch limit, but
production scale/cost has not been load-tested. Game-time jumps, Host outages,
model latency, Context budgets and an occupied EVA can miss domain deadlines;
the implemented response protects evidence rather than claiming success.

**OPEN / local candidate decisions:** C-20 retains TTL start/length/encoding,
per-type policy, archive granularity and cleanup design as OPEN. `ob-retention/1`
uses 100 abstract ticks from first durable Work adoption, explicitly not historic
event occurrence. C-21 retains Gate wire format/error shape and scheduling policy
as OPEN; local `npc-memory-gate/2` uses periodic 20, latest start 60 and margin 10
ticks (`60 + 10 < 100`). These reviewed constants are bounded implementation
policy, not a frozen public ABI. Batching/retries never reset the earliest origin.
C-22 expiry notification/recovery transport remains a local typed candidate, not
a requirement that a Host mutate existing Context or forget previous tokens.

Physical Discardable/GC, semantic memory retrieval/ranking/summary, Context
compaction, actual model decisions, background execution and broad perception
propagation remain unimplemented. No unused framework was added to prepare them.

## Next integration and merge recommendation

The delivered domain interfaces remove the need for a future Host to invent
another short-term memory, expiry authority, Gate persistence or EVA queue. The
Host can read bounded authorized Gate inputs, submit typed judgments, discover
eligibility, request mode-correct Tasks, obtain distinct Recent/Work/recovery
inputs, and reconcile existing receipts/output handoffs after a crash.

Next, repair PER-19-22-01 independently and verify dark/condition/partial-Signal
and legacy/restart cases without current-state backfilling. Then review C-20–C-22
wire/policy decisions against an actual Host integration. The Host must execute
both JEV purposes with separate schemas, run Interactive/Background EVA, enforce
invocation/resource limits, query expiry before later invocations, rebuild Context
appropriately and reconcile durable Task/Result/Command outcomes. None of this
requires another authoritative state or queue in the State Machine.

**Recommendation: keep the independently tested commits pushed for human review;
do not merge to the SM-19–SM-22 branch or main while PER-19-22-01 is unresolved.**
No merge or PR was performed. This review is a separate documentation commit.
