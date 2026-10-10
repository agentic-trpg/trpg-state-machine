# SM-7 — real PC attack and atomic commit

`TypedCommand(command_type="combat.intent", payload=CombatIntentPayload(...))`
uses the actual pinned upstream type. `submit_command` provides a synchronous
adapter; asynchronous callers use `await submit_command_async` with the same
command/receipt keys. Calling the synchronous adapter inside an event loop raises
`async_context_required` before evaluation. At this batch, authorization admits
only the selected PC. NPC grants are the next batch.

The adapter calls actual `dnd5e_engine.evaluation.evaluate()` outside any SQLite
transaction. It sends a detached complete `CombatSnapshot`, Session-pinned binding,
separate `RNGContext`, and trusted envelope Actor. There is no registry or combat
handle in SM. Missing weapon selection is refused before calling the Evaluator;
unexpected `needs_choice` after explicit admission is a protocol error, not a
durable business rejection. Formal PendingOperation/Continuation is not implemented.

Result validation uses the upstream strict model and `verify_request`, the pinned
whole-session `world` read fence, all typed operation pre-values, ownership and
final mechanical invariants. HP/status changes belong to the declared target;
budget/ledger changes belong to the acting/next-turn actor; Combat changes must
reference this Combat. Unknown/duplicate operations and healing via Attack fail
closed. No whole Snapshot is written: only the closed Delta's fields update the
existing character and mechanical component tables.

After evaluation, the short write transaction rechecks authorization, fingerprint,
receipt, full authoritative Snapshot/RNG and version, then validates the result
again. Component edits, RNG transition, world_version, ordered WorldEvents,
Outbox and CommandReceipt commit together. A conflicting candidate is discarded.
Only deterministic event-cascade overflow is caught inside the effects savepoint;
it rolls back all candidate effects and then stores the rejection. Storage/schema,
binding and Evaluator failures remain independent errors with no durable rejection.

Accepted miss/immunity still commit the Engine's actual budgets, events and RNG;
they are not rejected attacks. Retries return the original persisted receipt before
calling the Evaluator. Synthetic Evaluator and real D&D imports/types stay separate.
No generic Patch/SQL interface is exposed. Event-only and no-draw future-operation
version policies remain OPEN; this adapter only executes the candidate's ordinary
explicit attack slice.

## Actual upstream limitations discovered

The candidate has no stateless `combat.end` operation. A lethal monster attack
records death in `dead_ids` and `deaths_recorded`, but retains the monster's
`is_alive` field and does not mark CombatState.ended. SM preserves that actual
Engine result; it does not invent a second death/end calculation. Subsequent
unsupported/dying-state operations fail closed. An active combat remains ineligible
for safe save/resume under SM-02. Normal completed-combat save/resume is therefore
blocked on an upstream completion contract. Audit reads and receipt replay after
restart still preserve every committed fact. This limitation was reported before
proceeding with the affected SM-9 recovery acceptance.

## Evidence

`tests/test_dnd_runtime.py` compares a direct actual Engine evaluation against the
complete persisted post-Delta state, event history and exact RNG successor. Cases
cover hit, miss, resistance, immunity, vulnerability and Temp HP; lethal damage
checks the real death ledger and recovery limitation. Other cases verify capability
and target refusals, actor spoofing, async usage, configuration repair/retry,
different/same command concurrency, lost-response/reopen replay, bad binding/RNG/
read-set/Delta, Evaluator exceptions and SQL failures at Events and Receipt writes.

Fault tests alter a real evaluation result or inject a SQLite trigger; they do not
substitute a mock for successful D&D execution. Every failure checks actual
authoritative table contents, not just an exception string.

Validation: **229 passed** (208 existing + 21 new), locked dependency sync,
`uv run --locked python -m pytest -q --basetemp=.test-tmp/sm7-final`, Ruff check,
Ruff format check and Diff check passed. After making every varied fixture pass
the complete Package validator with its actual digest, the 21 integration cases
were rerun and passed. No existing test was removed.
