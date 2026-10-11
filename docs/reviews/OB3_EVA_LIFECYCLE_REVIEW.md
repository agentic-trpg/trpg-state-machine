# OB-3: EVA authorization and Observation recovery

Baseline: OB-2 `e6949755f3d5044c30ce159e5a2dc35736e75b9d`; Meta architecture
`307cde1`, MODULE_CONTRACTS §7.4, §9.6–9.9. The branch, clean tree and previous
actual commit were checked before implementation. This is a batch review;
the separate cumulative Global Review follows its verified Commit + Push.

## Implemented behavior

The existing `route_npc_observations(..., classification="eligible")` lets a
trusted Host request domain authorization from current persisted eligibility.
Urgent is selected first, then earliest deadlines; a bounded set of at most 100
Work items is associated in original committed order. Inactive creates Background,
Interactive creates Interactive, through the same `_create_evaluation` and
single-running-Task constraint. Occupied NPCs return queued scope, never another
valid EVA. New arrivals do not expand an in-flight Task's Work, Recent grant or
completion scope. A queued/no-eligible response is durable for its request key;
a later authorization uses a new request key, not replay to change an old result.
Normal player interaction can batch Pending without mandatory Gate. Existing
explicit legacy and mode-switch requests retain their behavior.

Schema 18 adds only `npc_evaluation_recent_scope`: immutable own-Task references
to the existing Inbox, with identity/foreign-key constraints, not another OB
store or Work queue. First Task authorization captures all currently valid Recent
OB IDs, including Completed. Old Tasks gain no fabricated Recent grants: their
original Work scope and all old records remain. A task read returns complete
authorized NPC Meta, its valid Pending Work through the compatible `observations`
field, and separate `recent_observations` filtered by both fixed grant and current
validity. Merely reading Completed background information never reopens its Work.
The existing Meta validator accepts original Task Work evidence, plus explicitly
authorized Recent background OB still valid at commit; it never admits arbitrary
same-NPC history, in-flight arrivals, foreign NPCs or archived secrets.

`get_npc_evaluation_recovery_input(evaluation_id, ..., context=...)` explicitly
returns only that valid, version-current, principal-bound Task's expired/unknown
Pending evidence, marked with lifecycle status and original adoption/deadline
(NULL for unknown history). It does not regrant Recent validity. Expiry alone
neither completes Work nor cancels a valid Task. Work evidence from a legitimately
authorized in-flight/recovery Task may still support its final Meta, under original
version/identity/result checks. Cancelled/failed/completed Tasks cannot use this
recovery interface or choose a new Result. Already selected Results retain their
original idempotent Receipt even when sources expire.

`get_npc_observation_expiry(..., observation_ids=None)` is a repeatable own-NPC
metadata query for the next Host invocation. It does not publish archived text,
advance a Cursor, ACK Work, run EVA, modify a Context/KV Cache or promise physical
forgetting. Unknown timing is explicitly `legacy_unknown`, never falsely expired.
The Host controls Context rebuild/invocation; ordinary reconstruction excludes
expired Recent OB and may deliberately request protected Work recovery separately.

Five-kind Meta, immutable sole Result/Receipt, reliable Dialogue/Action handoff,
Work completion and Completed Cursor still commit in the original single SQLite
transaction. Command execution remains independent, including stale-world rejection.
No Rule Engine calculation, JEV, Prompt, model, polling, physical execution queue,
Context manager or new ACK is introduced.

## Evidence and limits

22 added actual SQLite cases cover both modes, Completed Recent used as authorized
Meta background without repeated Work, periodic batch/normal interaction, Urgent
arrival during EVA, fixed source grants and invalid foreign/new/expired sources,
expiry metadata/recovery and late selected-Result replay, budget failure and switch,
revocation, three authorization write failpoints, five Result/Meta/Output/Work
failpoints, mode/expiry races, fresh-process recovery and receipt reconciliation.
Archived OB-2 code creates an actual Schema-17 save with classified Work and a
running Task; all original columns/rows survive upgrade, without invented grants.
The lawful synthetic end-to-end path covers Event projection → Gate → mode-correct
Task → five-kind Meta / empty Result → atomic completion → independent handoff.

The snapshot guarantee concerns Task Work/Recent authorization and an already
returned invocation input. A later explicit NPC View read may report changed
World location; SM does not freeze a second authoritative World/NPC snapshot or
modify an executing Host invocation. Source eligibility is rechecked at commit.
Deadline policy remains the local OB-1/OB-2 candidate, not a cross-module ABI freeze.
Resources, budgets, crashes and game-time jumps can exceed it; evidence is retained,
not falsely completed. Physical cleanup and actual Host runtime remain unimplemented.

Full locked regression on 2026-10-11: **715 passed**, 2 existing Lark warnings,
272.35s (693 previous + 22 added). Commands passed:

```text
uv sync --locked --group dev
uv run --locked python -m pytest -q
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .  (107 files)
git diff --check
```

Targeted source/SQL
review found no new confirmed P0/P1. Perception Judgment and Event Evidence code
remain unchanged from SM-22.

**PER-19-22-01 remains an unresolved P1 / Global Merge Blocker.** This independent
batch can be committed/pushed after verification; Merge/PR are not authorized.
