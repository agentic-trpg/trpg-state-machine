# SM-16 atomic NPC Meta and selected Evaluation Result

Meta main `fc41d3c`, MODULE_CONTRACTS section 9 is the authority. `npc-evaluation/1`, these Python records and their SQL representation are local versioned proposals, not a frozen Host ABI.

## Result and authorization

`npc_results.NPCEvaluationResult` has exactly seven fields: dialogue, memory_updates, belief_updates, relationship_updates, goal_updates, plan_updates, action_intents. Lists use bounded tuples of SM-14 record types; dialogue uses `NPCDialogue`, and the minimal action candidate is `NPCActionIntent(operation, destination_scene_id, expected_world_version)`. A trusted adapter constructs these types from untrusted data; a dict, extra identity envelope or wrong record type is rejected. All fields can be empty. Result encoding is canonical JSON (sorted keys, compact separators, no nonfinite numbers), UTF-8 SHA-256, maximum 256 KiB. Each list has at most 100 items.

`submit_npc_evaluation_result(evaluation_id, result, package, *, context)` derives all associations from the durable Task and rechecks its current NPC grant, principal binding, running status and NPCStateVersion. `get_npc_evaluation_receipt` queries the original persistent feedback after a lost response. Replay also rechecks current authorization; another Result conflicts.

## Transaction and history

One BEGIN IMMEDIATE validates all five Meta categories, appends immutable versions, records evaluation provenance, selects one Result/fingerprint, persists every output handoff, completes the Task and saves EvaluationReceipt. SQL failure rolls everything back, including current-version markers. Invalid Meta selects no Result and changes no Meta/output; only the bounded error attempt is persisted. Three failed attempts exhaust the local task budget; storage/configuration failures do not consume that budget. Cancellation and stale versions cannot submit.

Records append exactly the next content version with one current marker. Observation sources must be in the Task's authorized input, Memory sources must already be committed to this same Session/NPC, and Package sources explicitly identify this NPC's approved initial template. Same-batch forward Memory sources are unsupported; plans may reference a same-batch validated Goal. SM does not check prose fidelity or decide beliefs. Incorrect Belief text remains private and does not alter World Facts.

A nonempty approved Meta batch advances NPCStateVersion once; an empty batch retains it. This dedicated cognition lifecycle changes neither WorldState nor world_version/RNG/events; the generic Meta-only command version policy remains OPEN.

## Migration and outputs

Migration 12 extends the existing `npc_record_sources` with scoped Observation references and preserves old Package/Memory rows and columns. A generated nullable Observation FK prevents cross-NPC/Session references without weakening Memory FKs. It adds `npc_record_evaluations`, `npc_evaluation_receipts`, `npc_output_handoffs`. Existing private histories remain the sole authority.

Illegal action candidates get independent structured rejection while legal Meta commits. The supported proposal subset is existing `move_actor` for the task's own NPC; each approved intent receives a separate stable `command.npc.*` identity. Dialogue carries audience, visibility and source IDs for later publication authorization. Pending handoffs are not executed or published in SM-16. No CommandReceipt is duplicated; output dispatch, observation completion and mode coordination follow in SM-17/18.

## Test and review evidence

`tests/test_npc_results.py` verifies all five updates/history, illegal updates in every category, closed result type, nine actual SQLite failure points, empty results, output independence, cancelled/stale/revoked tasks, four concurrent submits and receipt recovery in a fresh interpreter. No model, semantic validator, alternate command executor or NPC authority is introduced.

Final verification: full `uv sync --locked --group dev` / `uv run --locked python -m pytest -q`: **512 passed**, 33 added, 142.44 s, two upstream Lark deprecation warnings. Ruff check passed; one formatting-only correction was applied after the full run, then Ruff check, format check (77 files) and `git diff --check` passed. An earlier full run caught two accidentally changed historical event-count assertions; the original assertions were restored before this successful full run. No tests were removed/skipped.

Batch review: one SQLite transaction owns selection, version/current-marker transitions, provenance and handoff; storage failure persists neither Result nor attempt. Replays reauthorize before reading. No confirmed P0/P1/P2 in this implemented scope. Mode switching and Work completion remain subsequent batches.
