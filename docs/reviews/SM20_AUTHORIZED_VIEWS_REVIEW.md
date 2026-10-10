# SM-20 Batch Review

Meta baseline fc41d3c, Module Contracts 3.5/7/9.7 and SM architecture 6.1–6.2.

Actual changes: perception.py adds local game-view/1 contexts/projections to the existing
get_scene_view entry, with exact Session/owner/player/role verification. Shared Package
pin checks are extracted in service.py; no Player/Narrator conversion to TrustedContext.
NPC current views keep their existing authorization and now report a snapshot version;
unsupported mechanical visibility returns unresolved with no entities or object state.

No migration or duplicated knowledge, observation, event or world authority. Existing
TrustedContext is explicitly an internal privileged runtime identity and must not be
issued to player/narrator clients. GameViewContext roles are authenticated by the adapter,
not self-asserted JSON. This library does not implement login/authentication.

16 new SQLite tests cover role separation/GM secrets, own PC parity, cross-Session/pin
checks, dual NPC access/revocation, actual positive-only Signal/Event provenance, repeated
projection, historical-vs-current views, read non-mutation and fresh-process reads.
Unsupported current mechanical state is injected after valid initialization to check
fail-closed read admission, not presented as a supported rule evaluation.

Scope: plain declared world entities and already committed state; no secret discovery,
complex spatial propagation, whisper audience or mechanical Perception Check. Wrong
beliefs remain private cognition, never projected as objective world facts. Existing
SM14–18 tests retain their reference/Belief and event-time historical checks.

Separate review of actual diff and SQL paths found no new confirmed P0/P1/P2. Broader
player event-observation discovery and real Host authentication remain integration work.

Validation: uv sync --locked --group dev passed; full pytest **612 passed** (596 + 16),
2 upstream warnings, 190.50s; Ruff check and format --check (91 files) passed;
git diff --check passed. Actual final diff contains only the scoped view implementation,
Package pin extraction, tests and documentation; no new schema or unrelated changes.
