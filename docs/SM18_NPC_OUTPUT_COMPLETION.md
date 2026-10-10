# SM-18 reliable NPC output and processing completion

Meta main `fc41d3c`, MODULE_CONTRACTS sections 3/7/9 controls ownership and transactions. This is a local bounded NPC domain implementation, not a frozen Agent Host ABI or real LLM acceptance.

## Reliable output and independent Commands

`dispatch_npc_output(evaluation_id, ordinal, package, *, context=TrustedContext(...))` dispatches one durable handoff. It requires the Session owner, an explicit existing NPC command-control grant, and the task controller's current persisted `npc_access` grant. NPC Meta/View access alone grants no command authority. `get_npc_output_status(..., context=NPCContext(...))` returns the task's own delivery status and the existing independent CommandReceipt, without duplicating its storage.

The local approved action subset is `NPCActionIntent('move_actor', destination_scene_id, expected_world_version)` for its own task NPC, mapped to the existing `MoveActor` / `TypedCommand` path. SM-18 does not add mechanical/combat intent payloads. Every approved intent has a persisted independent `command.npc.*` ID. The existing command transaction rechecks the handoff payload/Actor/version and current controller binding under its write lock; normal command authorization, OCC, events, RNG and CommandReceipt behavior remain authoritative.

Dispatch runs `submit_command` outside the handoff transaction. If Command commit succeeds but response or handoff writeback is lost, the pending handoff still points at the original receipt; retry uses the same command ID, returns that receipt and reconciles delivery. A rejected/conflicting command does not redo EVA/Meta or reopen Work. Storage failure leaves pending dispatch for recovery. Current authorization is required even for replay; a revoked output remains pending/blocked until trusted authority resolves its grant, never executes under stale authority. There is no shadow executor, Host game queue, proposal ID or ACK.

## Dialogue

Dialogue is a separate publication lifecycle, not action success. Local speech requires the speaker's current Scene and an exact audience matching all other Actors there. The existing scene-only Signal scope cannot guarantee a whisper/subset audience, so those publications are rejected. Private dialogue with an empty audience stays in the authorized handoff, creates no WorldEvent and conveys no public fact. Task Observation sources are rechecked; invalid/remote audience or source gets independent rejection without undoing legal Meta.

Approved speech atomically writes one `npc.dialogue_published` event, event-time evidence, an auditory Signal, Outbox and final publication status. The source field uses stable `dialogue.<evaluation_id>.<ordinal>` as an internal output audit reference; it does not pretend there was an Action CommandReceipt. The content only proves speech occurred, never that its claim is true. This specific event-only path retains world_version, while event_seq advances; generic event/Meta-only version policy remains OPEN.

## Work and Completed Cursor

EVA's existing Result/Meta/receipt/handoffs transaction now marks its associated Observation Work completed with disposition `evaluation`. Gate safe-retention/no-op updates Work, its audit and cursor in its existing transaction. Neither waits for action execution. Empty Result can complete Work; invalid/failed/cancelled evaluation, reads and model returns cannot.

`npc_completed_cursor(session_id, package, *, context=NPCContext(...))` reads the internal continuous event-order prefix for that NPC. It is capped at Perception's completed projection prefix and stops before the earliest pending Observation for that NPC. Later completed Work cannot skip earlier cognition or projection gaps. All-false/no-observation projections can advance without inventing evidence. Projecting new events cannot bypass pending cognition. Authoritative events, Inbox, Work and Meta stay separate.

Migration 14 adds `npc_completion_cursors`, immutable handoff identity/final-delivery triggers, and backfills completion only for existing completed tasks with a real selected Result/receipt and reliable handoffs. Running/failed/cancelled tasks remain unfinished. Existing Schema 1–13 data and the audited Legacy Perception boundary are retained. Each new NPC initializes one cursor; no second NPC identity or inbox is created. Replay does not rerun migrations.

## Evidence and boundaries

`tests/test_npc_outputs.py` covers both Interactive and Background real SQLite vertical paths with hand-built seven-field Results, all five histories, Dialogue and an independent action, then fresh-process resume. It verifies World/RNG/version/receipts/events/outbox/Work/cursors, command-commit response loss via actual process exit, concurrent dispatch, private/invalid dialogue, action conflict, SQL rollback at publication/command/completion boundaries, source/controller isolation, two NPCs/Sessions, cancellation after committed action, and actual archived Schema 11/12/13 runtimes. Earlier real Schema 1–10 migration/recovery tests remain.

The generic migration evidence helper now uses `PRAGMA table_xinfo` so generated provenance columns participate in real row preservation comparisons. This corrected an evidence-helper mismatch, not dropped data. Model behavior is simulated solely by original typed fixtures; no Agent Host, LLM, Prompt/Context/cache/retrieval, automatic retry, schedule simulation or new rule mathematics is implemented. Combat action mapping, broader dialogue scopes, production budgets and wire-schema approval need separate work.

## Batch validation and review

Final full run: **563 passed, 33 added over SM-17, 162.54 s**. Commands executed: `uv sync --locked --group dev`, `uv run --locked python -m pytest -q`, `uv run --locked python -m ruff check .`, `uv run --locked python -m ruff format --check .`, `git diff --check`. All passed; Ruff format checked 84 files. Two upstream Lark deprecation warnings remain. Windows subprocess/asyncio tests ran with the required OS permissions, against actual SQLite files; no external model service was called.

Targeted new tests: **33 passed in 21.01 s**. Earlier targeted attempts exposed the generated-column comparison helper mismatch and a misplaced test suffix; both were corrected before the successful targeted and full runs. No historical test was deleted or skipped. Existing latest-schema assertions now expect 14; their state, receipt, RNG and event expectations are retained.

Batch Diff review checked transaction separation, command-control versus view permissions, current authorization under the command write lock, stable output identity, projection/cognition prefix gaps, migration preservation and unchanged pinned Engine dependencies. No confirmed new P0/P1/P2 was found in this Batch check. Global Review is a separate phase after this commit and push; these results do not constitute real Host/LLM acceptance.
