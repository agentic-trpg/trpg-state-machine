# OB-2: Memory Gate and domain eligibility

Baseline: OB-1 `413165c26f01c59c6d3d061a95059447f0b39857`; architecture Meta
`307cde1`, MODULE_CONTRACTS §9.8–9.9. Branch/clean tree and actual prior commit
were checked before this sequential batch. This review does not claim OB-3 routing.

## Local contract and compatibility

`get_npc_memory_gate_input(..., request_id, observation_work_ids=None)` authorizes
an exact, bounded, projection-visible Pending/Recent/unoccupied subset. It persists
the typed `npc-memory-gate/2` input and original scope in the existing
`npc_routing_requests`, bound to the existing NPC grant, Session and principal.
It returns NPC identity/current mode, actual authorized OB and Work timing, never
unobserved facts. Replay keeps the original scope, and refuses to re-expose expired
input. No new queue, task ID, ACK, model execution or Observation storage is added.

The existing `route_npc_observations` accepts optional exact
`tuple[MemoryGateJudgment, ...]`. Perception `JudgmentResult` is not this type.
Every requested Work must occur exactly once; unknown, duplicate, missing or
invalid entries reject the whole candidate. First adoption revalidates current
grant, Package, ownership, projection watermark, Processing, validity and in-flight
occupation. A mode change cannot restore old Task eligibility. Replayed adopted
judgments return the persisted result under current authorization, including after
expiry; changed judgments conflict. The original input, adopted items, fingerprint
and result are durably auditable in the existing routing record.

Urgent makes Work immediately eligible. Deferred and Uncertain retain Pending.
Irrelevant is an explicit authorized `no_op` adoption, including in Interactive
mode, and atomically completes only its stated Work; it creates no Meta. Completed
OB remains Recent until TTL. Uncertainty/failure/malformed input never means No-op.

Legacy `background`, `safe_retention`, `no_op`, `uncertain` remain a separate,
unchanged input path: Inactive background authorizes EVA, safe/no-op completes
bounded Work, uncertainty retains it; legacy Interactive routing still authorizes
Interactive EVA instead of silently renaming its behavior. Mixing legacy and new
inputs is rejected. New classifications in OB-2 expose eligibility, not physical
model jobs; domain task routing is the next batch.

## Schema 17 and deadlines

The existing Work receives nullable classification, periodic eligibility and
latest-start ticks. Local candidates: periodic 20, maximum scheduling wait 60,
execution margin 10, TTL 100 (`60+10<100`). They are domain game ticks, not seconds
or rounds and do not freeze OPEN policies. Times are anchored to first adoption;
batching/classification/retry cannot postpone them. Urgent may only bring eligibility
forward. Unknown legacy timing remains unknown, with explicit recovery eligibility
and no invented historic deadline. Host discovers domain eligibility through
`get_npc_evaluation_eligibility`; absent a model, unclassified/uncertain Pending
Work still becomes periodically due. Results sort Urgent first, then earliest
deadline and stable Work ID; occupancy is exposed, not resolved by a second EVA.

All candidate adoption/No-op/Cursor/routing persistence is one existing short
transaction. SQLite triggers protect deadlines; failed adoption leaves Pending
and the original candidate available. No background polling, timer, worker, Context
or physical LLM queue exists. Periodic eligibility is not a guarantee that a Host
has resources or that processing finishes before expiry.

## Evidence

25 added real SQLite cases: four classes, exact/reordered replay and conflict,
mixed batches, six invalid-result shapes, five invalid input scopes (including
unpaired surrogate), expired/completed/occupied late candidates, foreign NPC/Session
and revoked grant, earliest deadline under batching/new OB/restart, three SQL
failpoints with safe retry and concurrent same-candidate No-op adoption.
Existing Task/Mode/retention tests run unchanged except latest-schema expectations.
Full locked regression on 2026-10-11: **693 passed**, 2 existing Lark warnings,
233.99s (668 previous + 25 added). All required commands passed:

```text
uv sync --locked --group dev
uv run --locked python -m pytest -q
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .  (104 files)
git diff --check
```

Targeted review found no new
confirmed P0/P1. No Event Evidence or Perception algorithm is modified.

**PER-19-22-01 remains unresolved P1 / Global Merge Blocker.** Model APIs, Prompts,
JEV execution, actual EVA execution and Context/KV management belong to future Host
integration. No Merge or PR is authorized.
