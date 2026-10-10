# SM-19–SM-22 implementation plan

Baseline: origin/main b3f350f9a318270afbbbd00cb7857706cdbd2e1f (SM15–18 and GR-01 merged).
Meta main: fc41d3c584816198ba4f23bb4d326655ea3c774d; local five architecture files match.
The latest Module Contracts DECIDED constraints govern; local schemas do not freeze Proposed/Open ABI.

| Batch | Expected paths | Invariants and validation | External dependency |
|---|---|---|---|
| SM19 | npc_results.py, npc_outputs.py, dnd_runtime.py, dedicated tests, README/review | Persisted output uses original command receipt, own NPC, current grant and clear-scene visible target; real Engine outside write transaction; fault/retry/process reconciliation | Pinned Engine attack; complex perception and active combat exact resume unsupported |
| SM20 | perception.py, models.py, authorized view tests, documentation | Role/session scoped reads, no GM/private/source semantic leakage, no read side effects, historical observations separate | Trusted adapter issues contexts; complex perception fails closed |
| SM21 | commands.py, dnd_runtime.py, dnd_checks.py, world effects tests, documentation | Predeclared typed effects/preconditions; official CheckRolled.succeeded only; same RNG/world/events/receipt transaction | Pinned Engine generic checks, no continuation |
| SM22 | service.py, commands.py, NPC runtime, next migration, clock/schedule tests, documentation | One monotonic clock; command idempotency, no teleport/Meta rewrite, mode exclusivity, rollback/restart/legacy preservation | Existing Gate/EVA, no LLM execution |

Each batch runs locked sync, full pytest, Ruff check/format and diff check, then its own review, commit and push.
Global Review follows all four pushes, reads actual final code/SQL/tests, and only changes its report.
