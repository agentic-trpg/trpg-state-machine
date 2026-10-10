# SM-21: conditional world effects

Local `conditional-world-effects/1` is an optional `TypedCommand.check_world_effects`
proposal, not a newly frozen cross-module ABI. `CheckWorldEffects` fixes a bounded
trusted-GM source reference, preconditions and both candidate branches before
evaluation. The authenticated adapter supplies `can_adjudicate_checks`; a payload
cannot grant GM authority. Both branches must use existing current-scene
`SetSceneFlag` / `SetObjectState`, including valid references in the unselected
branch. No mechanical state can be written through this proposal.

`dnd_checks.world_effects_ready` checks references and exact preconditions before
RNG evaluation and under the write fence. `dnd_runtime._submit_dnd` uses only the
validated official `CheckRolled.succeeded` to choose a branch. An accepted failed
check commits its failure branch. SM does not compare a roll to DC. RNG, existing
world operations, bounded trigger cascade, event evidence/outbox, world version
and original receipt share the existing savepoint/transaction. SQL/protocol
faults leave the original command retryable; deterministic cascade/precondition
rejections have durable receipts. No new trigger engine or separate action path.

Existing command fingerprints are preserved when the optional field is absent.
Schema remains 14; Package/Engine bindings and historical data are unchanged.
No continuation, saving throw, tool execution or mechanical world effect added.
Unsupported official check capabilities retain their existing admission errors.

Evidence: `tests/test_conditional_world_effects.py` adds 18 real Engine/SQLite
cases: success/failure, explicit object and flag branches, all-candidate admission,
GM grant/source, precondition rejection, four SQL failpoints, concurrent world
conflict, bounded real package cascade rollback, fingerprint compatibility and
fresh-process lost-response reconciliation without a second evaluation. All table
comparisons use an independent SQLite connection and include FK validation.

Validation: full locked pytest **630 passed** (612 previous + 18), with the two
existing Lark deprecation warnings. Locked sync, Ruff check, Ruff format check and
`git diff --check` pass. Review checked the complete prepare/evaluate/write path
and savepoint boundary; no confirmed new P0/P1/P2 in this bounded slice.
