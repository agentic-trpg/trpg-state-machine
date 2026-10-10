# SM-17 NPC mode coordination and Gate

Local scheduling implementation follows Meta main `fc41d3c`, MODULE_CONTRACTS section 9 and AGENT_ARCHITECTURE sections 4–6/8–10. Concrete schemas, three-attempt budget and classification enum remain local proposals.

## Persistent modes and requests

Migration 13 adds `npc_modes`, `npc_mode_requests`, `npc_routing_requests`, `npc_gate_decisions`. New NPCs start inactive; migration preserves an existing running task's mode first, otherwise the most recent task mode, without fabricating Meta. Interactive activation remains in effect between invocations and after EVA completion.

`request_npc_interaction(session_id, player_input, package, *, context, request_id)` atomically cancels a running task, retains all committed Meta/output, selects still-unfinished visible Work, switches to interactive and authorizes a fresh task with current player input/version. `set_npc_inactive(..., evaluate_pending=False)` atomically cancels and changes mode; the explicit trusted `evaluate_pending=True` policy can approve one Background task if visible Work exists. Neither switch waits for a physical invocation or ACK. Duplicate mode request keys replay their original task identity; changed inputs conflict. `get_npc_mode` is an authorized read.

Direct Background task creation is rejected; it requires the Inactive routing path. One unique running-task index and BEGIN IMMEDIATE serialize switching, result commit and authorization. A committed result before switching survives; cancellation first invalidates the old result. No cancellation flag, generation counter, Context ID or proposal ID is used.

## Observation routing and Gate

`route_npc_observations(..., request_id, classification='uncertain')` takes at most the first 100 ordered visible unfinished observations. Extra Work stays durable for subsequent explicit requests. While a task is running, new input remains queued; its immutable input snapshot is not edited. Idle interactive routes only to an Interactive task and ignores the Gate classification for cognition policy. No Gate or second cognition call runs in that mode.

Inactive candidates are closed: `background`, `safe_retention`, `no_op`, `uncertain`. SM adopts a trusted Host classification without performing classification inference. Safe/no-op records an auditable domain decision and marks that Work complete, retaining its original Inbox/evidence. Uncertainty, authorization errors, invalid classification and projection gaps preserve unfinished evidence. Gate never appends subjective Meta. Stable routing request keys and saved responses ensure retry cannot accidentally consume newly arrived observations. Work associated with an already completed EVA cannot be cognitively processed again.

## Invocation failures

`report_npc_evaluation_failure(evaluation_id, reason, package, *, context, expected_attempts)` accepts timeout/API/context-budget failures. The expected persisted attempt count deduplicates a retransmitted failure report. One failure keeps running within the local budget; three exhaust it to failed. Schema/Meta validation failures use that same persisted budget. A valid corrected result can commit in the current task; cancelled/completed/failed eligibility cannot be revived. SQL/storage errors roll back and do not consume a model attempt. The Host performs physical retry/cancellation; SM performs none.

## Evidence and exclusions

Real tests cover active/idle exclusivity, Gate retention/no-op/uncertainty, both switches and late results, request replay/conflict, committed history retention, invocation budgets, seven SQL fault points, four commit-versus-switch races and a fresh process recovery. No LLM, Context/cache, semantic memory logic, schedule simulation or action execution was added. Continuous Completed Cursor and dispatch are SM-18 work.

Final verification: `uv sync --locked --group dev`; full `uv run --locked python -m pytest -q` — **530 passed**, 18 added, 151.05 s, two upstream Lark deprecation warnings. Ruff check, format check (80 files), and `git diff --check` passed. Source/SQL review confirms mode/Task cancellation and replacement use one write transaction, Gate does not modify Meta, request replay cannot consume newer evidence, and retry counts cannot revive cancellation. No confirmed P0/P1/P2 in this batch scope. Physical Host model retry and output execution remain outside this batch.
