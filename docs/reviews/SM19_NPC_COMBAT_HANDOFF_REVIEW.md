# SM-19 Batch Review

Baseline b3f350f; Meta fc41d3c (Module Contracts 3, 4–7, 9–10; SM architecture 5–8).

Changed: npc_results.py adds a local typed combat envelope over the existing Engine
payload and preserves old Result canonical encoding. npc_outputs.py restores either
move or combat commands from the same immutable handoff. dnd_runtime.py checks origin
and controller before evaluation/replay and again under its write lock; admitted target
visibility is checked before evaluation and again before applying the accepted Result.

No schema migration. No rule calculation, LLM, new queue/executor/receipt, ACK, mode epoch
or proposal ID. Unsupported target perception is a separate authorization failure; the
selected Meta/Result remains committed. Original command rejection/conflict semantics
remain unchanged. Missing pre-choice/complex actions stay explicit Unsupported.

Test evidence: test_npc_combat_handoff.py (11 cases) uses actual evaluate, SQLite triggers,
revocation during evaluation, current target scope, original command replay and a new
process reconciling a committed command after lost response. Active-combat exact resume
remains rejected. Existing NPC tests cover whole-Meta/output separation and mode races;
existing real attacks cover budget/resource ownership and ledger scope.

Review: checked actual diff, write-lock revalidation, immutable output identity, fingerprint
compatibility and rollback/receipt lifecycle. No confirmed new P0/P1/P2 within the admitted
slice. Broader perception and real LLM/First Blush acceptance remain outside this evidence.

Validation result is recorded after the full regression run below.

Validation: locked sync passed; full pytest **596 passed** (585 baseline + 11 new),
2 existing upstream lark deprecation warnings, 168.41s. Ruff check passed; Ruff format
--check passed (89 files); git diff --check passed. No temporary DB/cache/log is staged.
