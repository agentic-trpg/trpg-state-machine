# ITEM-01 Batch B: combat healing potion

## 2026-11-18 evaluator binding

The SM adapter matches the inventory change and the `IntentSubmitted` item
identity to the selected instance on the fixed Snapshot. The bound RE decides
whether its healing activity is supported. The locked RE `/42` evaluator admits the
canonical Greater, Superior and Supreme potions through the same self-healing
profile in exploration and combat. The original compatibility cleanup was
verified with Engine and SRD at RE `ca49928`.

## 2026-11-15 original scope

2026-11-15. Architecture decision: the existing World, Character, Combat and
SessionRuntime owners remain authoritative. Implementation status: the selected
PC may drink one owned, accessible, ordinary `potion-of-healing` instance in
active combat through typed `combat.item` and the pinned RE item evaluator.
This is a narrow combat command, not general item use.

The command carries an exact `instance_id`, the item's `activity_id`, and the
PC's own `target_id`. `RuleExecution` prepares fixed `/19` Snapshot and RNG
inputs, evaluates outside the SQL write scope, rechecks authorization, command
replay, world version, Snapshot and RNG, and validates the accepted result. The
item validator requires the selected one-unit inventory consumption, self HP
healing matching the RE `HealingApplied` roll capped by current maximum HP,
the paid bonus action, and only supported turn/ledger changes. Existing attack
Delta validation remains strict about healing and has no inventory removal.
The RE result is uncommitted until WorldFacts, CharacterFacts, CombatRecords,
RNG, events, receipt and outbox share the command's write transaction and RAM
publication succeeds. A fault rolls that transaction back; an ordinary live
binding refusal records a rejected receipt after the effects savepoint rolls
back. Events retain the exact selected instance ID alongside the RE event.

Schema51 adds `consumed_items(session_id, object_id, command_id,
authored_scene_id)`, tied to the command receipt by a deferred foreign key.
WorldFacts checks the current row, configuration, children, credentials,
capabilities, casting material and selected actor equipment before deleting
the exact object/config rows. For an authored scene item, it retires only that
item's object/config integrity dependencies first. Scene certification and
explicit lifecycle validation accept the missing authored object only with
its matching durable consumption record; other missing rows and reintroduced
consumed objects remain invalid. The ordinary command does not recertify the
scene. Old pre-release Schema50 saves are refused; fresh current-schema saves
must be used.

Focused evidence: `tests/test_combat_healing_potion.py` uses two same-slug
authored bottles, real RE evaluation, HP/budget/RNG/receipt and replay checks,
SQL receipt failure after candidate effects, malformed accepted results,
foreign target refusal and a fresh connection's scene integrity validation.
Active combat is process-bound and `resume_session` still refuses it; opening
the durable database does not claim active-combat recovery. Exploration use,
feeding another actor, NPC item policy, general item use and an item
availability API remain outside this batch.
