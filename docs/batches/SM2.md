# SM-2 — typed world commands

Base: 182efbe. Architecture: Meta main fe6a1e9, ADR-001 and Contracts §2/§10.

Implemented: closed local TypedCommand payloads, PC control and explicit trusted NPC grants, schema/target/current-scene/precondition/version validation, durable CommandReceipt and authorized query. Internal compatibility writes route through submit_command. Receipt keys are session-scoped and fingerprints bind trusted identity and fixed package/ruleset. Replays reauthorize before lookup. Expected versions are rechecked under BEGIN IMMEDIATE. State, version, events, event-time actor positions and receipt commit together. Savepoint rollback prevents business rejection from retaining effects. Storage failures rollback the whole transaction and may be retried using the original command.

Schema 1 -> 2 migration preserves Batch 1 data, identity/digest and pins; old events deliberately have no fabricated historical evidence. No new Rule Engine, Combat, NPC cognition or arbitrary update interfaces.

Validation: uv sync --locked --group dev; uv run --locked python -m pytest -q --basetemp=.test-tmp/sm2b (112 passed); uv run --locked python -m ruff check .; uv run --locked python -m ruff format --check . . Tests include independent SQLite reads, reopen/replay, concurrent writers, receipt-insert fault rollback and actual schema migration.

Local choices, not global frozen contracts: sm-command/1 encoding and receipts; actor branch requires a real actor and does not yet admit system commands; unsupported schema/type errors are structured exceptions before a receipt. No pending choices. Event-only/Meta-only version policy remains OPEN. Authentication and construction of trusted grants remain adapter responsibilities.
