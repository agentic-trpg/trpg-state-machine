# SM-8 — NPC intents and action availability

NPC `combat.intent` uses the same pinned Engine evaluation, complete snapshots,
closed Delta validation, RNG transition, atomic commit, events, outbox and durable
receipt path as PC attacks. The Session owner and trusted `controlled_npc_ids`
grant must authorize an existing NPC; this is checked again immediately before
commit and before replay/query. A revoked grant cannot retrieve its NPC receipt.
Source IDs cannot override the authenticated envelope actor.

Ordinary explicit NPC weapons and exact `stat_block_action_id` basic attacks are
supported. Real tests use bundled SRD Bandit `scimitar` and `light-crossbow`, plus
the original simple weapon. Unknown action IDs are a pre-evaluation unsupported
capability; a known `multiattack` ID is rejected by actual upstream admission.
No prose/slug inference, Monster AI, extra attack planner, spellcasting, recharge
or full combat loop is implemented.

`query_action_availability(session_id, actor_id, CombatIntentPayload, package,
context=...)` calls actual upstream `query_action_availability` with the same
authorized Snapshot and rules binding. Its output is the real versioned
`ActionAvailabilityResult`, with free-text details replaced by generic authorized
status descriptions. `available`, `unavailable` and `unknown` remain distinct.
Unknown unsupported action/choice and interrupted-combat capabilities return
`unknown`. There is no command key, authoritative RNG input, receipt or mutation.

An availability result reserves nothing. Submission repeats Actor/Grant checks,
reads fresh state and uses the normal expected-world-version gate. An available
old snapshot therefore cannot authorize a stale attack or revoked NPC grant.

Tests in `test_dnd_npc_availability.py` exercise PC→NPC then NPC→PC attacks using
actual upstream resolution; ordinary/stat-block attacks; missing/wrong/revoked
grants; budget exhaustion; unknown and known unsupported actions; receipt replay;
all three query statuses; source/configuration errors; and query/execution races.
Queries compare all authoritative tables and SQLite `total_changes` before/after.
NPC Intent remains separate from Perception, EVA, Memory and LLM tactical choice.

Validation: **246 passed** (229 existing + 17 new), locked dependency sync,
`uv run --locked python -m pytest -q --basetemp=.test-tmp/sm8-all`, Ruff check,
Ruff format check and `git diff --check` passed. No existing test was removed.
