# Mechanism effects — mechanism-effect/1

## Verified baseline and implementation status

Branch: `feat/world-mechanism-effects-contract`; starting SM commit:
`f2cd863811475ee9b5571641e58a181a73b1d6ec`. Architecture authority:
`agentic-trpg/docs/MODULE_CONTRACTS.md` at
`98e8d36b03bdc6b9578082bcdae1ed99ccbc5997`, AP-15, §§4–6, C14 and the current
Turn/Round Episode decisions. No applicable SM AGENTS.md, CI workflow, Makefile
or check-fast configuration was found. Checks: pytest, Ruff lint/format, diff.

The unchanged lock and installed distribution identify RE
`d596161060555630019163ab301e1d25fd816549`: engine-evaluation/23 and
engine-snapshot/17. RuleEvaluationRequest permits combat.intent, rules.check,
combat.close, combat.item, rules.save and rules.rest, with required actor_id.
It has **no rules.effect or world-source branch**. Its four-state Result is
bound to that request ABI. An object ID cannot legally replace a character ID.

**IMPLEMENTED:** separate SM strong types, compatible configuration, trusted
target/request assembly, bounded result/owner validation, ordered transaction
integration, original-command outcomes, replay and rollback.
**NOT IMPLEMENTED:** real RE mechanism evaluation. The production adapter is
UnsupportedMechanismEffects and returns unsupported. No Package/command/public
constructor selects a fixture. Accepted outcomes are verified using fixtures
installed explicitly by tests; they are not real RE effect integration.

This is SM's local versioned proposal for RE adaptation. It does not freeze the
architecture's OPEN world-source ABI across repositories.

## Configuration

Package /10 admits mechanism/3 alongside compatible /1-/2 definitions. /3 adds
`rule_effects` and ordered `rule_effect` actions mixed with existing open/light
actions. Bounds remain eight actions/operation and sixteen operations, plus at
most sixteen effect definitions. All shown fields are required, including nulls.
Unknown fields/versions/effect references, malformed parameters, noncanonical,
duplicate or out-of-scene cells refuse during load.

Add the following fields to the existing /2 declaration and change its version:

```json
{
  "schema_version": "mechanism/3",
  "rule_effects": [{
    "effect_ref": "effect.dart",
    "schema_version": "mechanism-effect-definition/1",
    "attack": null,
    "save": {"ability": "dex", "dc": 12, "on_success": "half_damage"},
    "damage": {"dice": "1d4", "bonus": 0, "damage_type": "piercing"},
    "status": null, "magical": false
  }],
  "operations": [{
    "operation_id": "up", "consume_on_noop": false,
    "actions": [{
      "kind": "rule_effect", "effect_ref": "effect.dart",
      "targets": {
        "scope": "trigger_actor", "cells": [], "actor_kind": "all",
        "living_only": true, "exclude_trigger_actor": false,
        "selection_time": "before_consequence"
      }
    }]
  }]
}
```

These are trusted Package definitions, never caller-supplied DCs/dice/targets.
effect_ref resolves in the immutable mechanism catalog; it does not certify an
RE implementation or mean an arbitrary spell slug. SM validates shape, not rules.

| Parameter | Closed definition/1 shape |
|---|---|
| attack | null or bonus (-100..100), mode (melee/ranged), range_feet (1..10000) |
| save | null or ability (str/dex/con/int/wis/cha), dc (0..100), on_success (none/half_damage) |
| damage | null or bounded NdM expression (d4/d6/d8/d10/d12/d20/d100), integer bonus, standard damage_type |
| status | null or standard condition, positive/null duration_seconds, repeat_save (SaveRequirement/null), end_condition (duration/effects_explicit/source_destroyed) |
| magical | explicit Boolean |

Damage or status is required. Combined attack+save needs a future profile.
half_damage requires damage; repeated status saves cannot scale damage.
Only end_condition=duration permits/requires duration_seconds. These engineering
bounds are not D&D adjudication. Status durations/repeat saves/end conditions
are request semantics until Effects/RE supports adoption.

Single target selects the trusted triggering actor. Area selects an explicit
absolute region in the source's current scene (at most 256 cells). Filters:
actor_kind=all/pc/npc, living_only, exclude_trigger_actor. Sorted unique targets
are real actors (at most 128). Empty selection refuses the command. Portable
source pose and current scene bounds are rechecked. Faction, immunity, sight,
range legality and save calculations remain RE responsibilities.

Targets are selected immediately before **each consequence**, after preceding
world changes and adopted deltas. World preflight preserves the existing ordered
ObjectSpatialState candidate map; rule steps do not clear it or confer a world
capability. Queries never write. Production query/execute refuse unsupported
effects before any step. With a fixture, query checks world admission without
predicting later rule-dependent targets. An accepted miss/empty delta still counts
as an evaluated consequence and consumes one activation under the existing policy.

## Request, result and adoption

Strong Python types are in `trpg_state_machine.mechanism_effects`. JSON schemas
are available through MechanismEffectRequest/Result.model_json_schema(); strict
fields and JSON round trips are tested.

| Request field | Source/invariant |
|---|---|
| schema_version / operation_kind | mechanism-effect/1 / rules.effect |
| session_id / command_id | Original admitted command, including automatic and failed-check consequences |
| source | kind=mechanism; actual object_id/definition_ref, scene/cell, activation/consequence identity, source_event_seq, package_digest |
| effect / selector / target_ids | Typed resolved immutable definition, selector and trusted candidate targets |
| state_snapshot | Complete existing NonCombatSnapshot /17 with inventory/resource/casting/effects and known projected lighting |
| world_dependencies | Actual owner-provided actor poses and current per-object OSS contributions |
| ruleset_binding | Session-fixed Package binding verified against actual evaluator/data |
| rng_context | Current authoritative stream/version/state, including prior consequences |

There is **no actor_id/caster_id on the request**. The original controlling actor
remains separately in its command envelope and activation record. Incomplete or
uncertain mechanics refuse. Environment-dependent rules, remote dependency closure
and combat snapshots require additional certified profiles. OSS contributions
are not claimed to be merged RE Terrain/Light/Combat support.

Activation retains the existing object/command/source-event/operation hash;
consequence appends its declared action index, including intervening world steps.
Every request has the original command ID; no independent external commands,
receipts or RNG streams are created.

Result fields: schema_version, session_id, command_id, source, request_digest,
input_world_version, input_ruleset_binding, target_ids, status, read_set,
state_delta, proposed_events, rng_transition, error, choice. request_digest is
SHA256 over foundation-json/1 of the complete request. Correlation/source/targets/
binding/read fence must match exactly; read_set is the pinned adapter's complete
whole-session world fence. RNG separately validates its input state/version.

accepted requires a possibly empty Delta and valid RNG transition, null
error/choice. rejected/unsupported require error; needs_choice requires a bounded
preflight choice identity. Nonaccepted results require null Delta/RNG and empty
audit events. No partial continuation is represented.

Closed Delta wire variants reuse RE HPDelta, TempHPSet and DeathStateUpdate DTOs;
adoption currently permits **only bounded nonzero-HP damage/TempHP reduction**.
Death packets, zero-HP settlement, targets with conditions, existing effect
dependencies and status/lifecycle adoption explicitly refuse as unsupported owner
capabilities. Parsability does not imply adoption support. Unknown kinds, arbitrary
paths/SQL, healing, TempHP grants, wrong targets/pre-values/arithmetic, duplicate
fields and malformed candidate state produce invalid_rule_result. RE DTO and
NonCombatSnapshot validators are reused; the actor-attack authorization validator
is not called with a fabricated attacker. RulesAdapter._apply_dnd_changes routes
approved edits to CharacterFacts.

proposed_events are typed audit summaries (attack/save/damage/status), restricted
to declared requirements and selected targets. They never drive HP writes.
Accepted results/audits are nested in the existing activation payload; there is
no new per-consequence WorldEvent, OB, JEV or LLM path. Combat commands/hooks remain
unsupported. Future combat adoption must aggregate participants by Turn Episode
and outsiders by Round Episode.

## Original-command outcome and transaction

| Boundary | Original-command outcome |
|---|---|
| accepted, all owners succeed | committed; one version increment and shared events/receipt/outbox |
| rejected | rejected + mechanism_rule_rejected; never a character save failure |
| unsupported | rejected + unsupported_mechanism_effect or explicit owner capability code |
| needs_choice | pending_choice + mechanism_needs_choice, no partial effects/RNG; continuation is OPEN |
| stale/candidate dependency conflict | conflict + version_conflict |
| malformed result/binding/evaluator/storage fault | Independent error, rollback, no cached refusal; original command key can retry |

The synchronous port is bounded and pure: no IO, waits or authoritative writes.
Production never invokes RE. Fixtures use the existing command savepoint; before
adoption the binding and complete current request are rebuilt/compared. Later
failure rolls back earlier HP, world operations, RNG, uses/count, activation
history, events and outbox. Receipt replay/reopen never reruns evaluation. Rule
results count toward the existing command cascade bound.

Real ordinary RE checks remain evaluated outside the write transaction. Their
candidate RNG is adopted inside the savepoint **before** direct failed-check
consequences or automatic hooks. Later effects read its successor and cannot be
overwritten by the earlier check transition; rollback restores both.

## Remaining RE / Effects interfaces — OPEN

1. Real versioned rules.effect with authenticated mechanism/world source,
   certified effect catalog/parameters and complete dependency semantics.
2. Source-aware attacks/saves/damage, complete multi-target and zero-HP/death/
   condition/concentration/effect packets, certified Delta owner adoption.
3. Effects source/lifecycle identities, duration clocks, repeat saves and explicit
   end conditions. Reset/re-prepare stays MechanismService-owned. Disarm/destruction
   must not clear effects with unrelated end conditions; no blanket clear/timer
   was added here.
4. Prepare/evaluate/OCC adoption outside the write transaction before connecting
   an async/remote evaluator; fixture injection does not authorize locked IO.
5. Durable preflight choices/continuations and authorized choice presentation;
   pending_choice here is a command result, not a resume API.
6. Combat admission/costs and Turn/Round Episode aggregation before combat hooks.

This delivery does not claim full SM/RE reconstruction or real effect integration.
Validation evidence: [MechanismService progress](MECHANISM_SERVICE_PROGRESS.md).
