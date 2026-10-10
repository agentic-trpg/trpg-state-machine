# SM-3 — durable events and outbox

Base: 86ac1bf. Architecture checked against Meta fe6a1e9, ADR-001, Contracts §2/§7/§10, SM §5–§8 and Adventure Package §4.

Implemented bounded synchronous event definitions, flag_equals and two world effects; per-session once history and 32-consequence bound. World effects, committed facts, typed scene Signals, event-time actor positions, receipt and delivery work share the command transaction. Initial entry triggers run during atomic creation. No interpreter or rule-effect/downstream command adapter is admitted. Cycles cannot award indefinitely; any cascade overflow rolls back and produces a rejected receipt.

Delivery is at least once. Stable event_id supports sink deduplication. Attempts are durable before external callback; callback and retry occur outside write transactions. Completion is stored only after callback returns; lost responses cause safe redelivery, not false exactly-once claims. The cursor records the contiguous completed prefix; later work cannot conceal an earlier gap. This is delivery bookkeeping, not Agent CompletedCursor or cognition ACK.

Schema 3 migrates previous versions. Legacy sources/signals are explicitly unavailable; historical evidence is never synthesized from current state. Read APIs are owner-only and not NPC views.

Validation: uv sync --locked --group dev; uv run --locked python -m pytest -q --basetemp=.test-tmp/sm3final (123 passed); uv run --locked python -m ruff check .; uv run --locked python -m ruff format --check . . Actual SQLite trigger faults verify rollback of world/effects/history/events/Signals/outbox/receipts. Restart tests verify retry and cursor behavior.

Remaining: physical propagation, rules-based perception, full condition AST, once-per-scene policies, downstream mechanical effects, secret-content authoring validation and production workers. Scope/channel/event format are a local bounded implementation, not a freeze of all PROPOSED APIs. Event-/Meta-only world_version remains OPEN.
