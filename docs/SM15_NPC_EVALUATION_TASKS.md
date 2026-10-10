# SM-15 durable NPC Evaluation tasks

Architecture baseline: Meta main `fc41d3c` (MODULE_CONTRACTS sections 3, 7, 9, 10). The local `npc-evaluation/1` types and database schema are an implementation proposal, not a frozen cross-module ABI.

## Ownership and interfaces

`create_npc_evaluation_task(session_id, package, *, context, request_id, mode, reason, expected_npc_state_version, source_observation_ids=(), player_input=None)` authorizes a durable task for the configured NPC in the trusted `NPCContext`. `request_id` deduplicates the controller authorization request; SM assigns the independent `evaluation_id`. Same request returns the original task; changed content conflicts. One running task per Session/NPC is enforced by a partial unique index. Background and Interactive are task modes, not separate NPC state.

`get_npc_evaluation_task(evaluation_id, package, *, context)` reads a consistent SQLite snapshot containing task, complete authorized NPC Meta and its observations. `list_npc_evaluation_tasks` supports restart discovery for the same currently authorized controller. Evaluation IDs do not grant access. Replacing the existing `npc_access` grant revokes the old principal. Legacy/unconfigured NPCs cannot create cognition tasks.

## Storage and initialization

Migration 11 adds `npc_evaluation_tasks`, `npc_evaluation_observations`, `npc_observation_work`, and `observation_signal_sources`. Work is one reference per existing Inbox Observation, not a second Inbox. A task carries ordered source IDs and corresponding Work IDs; a batch may reference multiple observations, so the local projection uses tuples rather than one ambiguous work identity. Task creation is one short transaction with version, grant, availability and unique-key checks.

Existing observations are retained. Migration backfills Work from the Inbox and internal Signal provenance only from saved positive judgments and committed Signals. Event identity/sequence and Signal IDs stay internal; the authorized input exposes only existing Observation IDs and perceived content. Projection gaps block task association. Reads do not finish Work, consume the Inbox, mutate Meta or advance any cursor.

## Verification and boundary

Real SQLite tests cover configured inputs, two NPCs/Sessions, current authorization and revocation, request replay/conflict, missing observations and projection gaps, SQL insertion failures, four concurrent connections, actual SM-14 databases and a fresh interpreter recovery. Existing migrations 1–9 remain in the regression suite.

Result submission, Meta updates, mode coordination/Gate, outputs and Completed Cursor are subsequent batches. No LLM, Context, semantic retrieval, controller execution, rule computation or ACK is implemented here. Attempt counters are stored for the later bounded lifecycle; no public status mutation is exposed.

Final verification: `uv sync --locked --group dev`; `uv run --locked python -m pytest -q` — **479 passed**, 17 added, 121.33 s, two existing upstream Lark deprecation warnings. `ruff check .`, `ruff format --check .` (74 files) and `git diff --check` passed. First full run found four obsolete latest-schema assertions; two subprocess imports also encountered an editing-time syntax error. Those were corrected and the complete unchanged-source run above passed. No historical tests were removed or skipped.

Batch source/SQL review: authorization and version checks run under the same SQLite write lock as task insertion; detached NPC input reads share one read transaction; one partial unique index prevents dual valid tasks; new projection associations commit with the existing Perception result/Inbox. No new authority, model, command executor or completion claim. No confirmed P0/P1/P2 in the implemented SM-15 scope.
