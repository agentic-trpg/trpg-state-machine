# SM-PER-1: event-time Visual / Auditory admission

Baseline: `4745f17` on `feat/sm-b19-b22-gameplay-domain-bridge`; initial tree clean.
Only State Machine is modified. MODULE_CONTRACTS §7 and the installed pinned
Engine/SRD at `602dcb8d448e670049427bbb751d6ed226005298` are the design evidence.
The Engine repository, dependencies, mechanical rules and existing NPC domain
Task/Meta/Mode/Output/Retention protocols are unchanged.

## Implemented local schema and ownership

`Signal` retains signal_id/channel/content/scope and adds optional typed
`SignalOrigin(scene_id, entity_id=None, cell=None)` and
`SignalStimulus(kind)`. Visual kinds are `ordinary_activity` / `light_emission`;
Auditory admits `sound`. These are objective source/nature fields, not a detection
result or an invented intensity, distance or DC. Only existing Scene Scope is
supported. Package validation rejects unknown fields, mismatched channels,
foreign Scene/entity references and noncanonical cells. Runtime evidence checks
entity ownership/location and available cell consistency.

Old four-field Package inputs remain valid without changing their canonical
digest. At NEW commit only, a missing origin is bound to the trusted event's
Scene/entity and a missing stimulus to ordinary activity or sound. No natural
language parsing is used. Stored old Signals decode with absent metadata; they
are never enriched from current state. This is a local candidate, not a frozen
cross-module ABI.

`events.py:_event_perception_evidence` captures `event-evidence/3` in the existing
World/Event/Receipt/Outbox transaction: world version, game tick, RulesetBinding,
Actor identities/Scene/sensory capability, mechanical Conditions/special senses,
alive/death-ledger context,
supported spatial positions, Scene Grid and unavailable/unsupported environment
evidence. Object identities provide source references without exposing World
facts or private Meta. This is sensory evidence, not another mutable World state.
Noncombat state currently has no authoritative cell positions; they stay unknown.
Active-combat coordinates come only from the stored actor_zone, and are not reused
after combat ends or after an Actor changes Scene.

SQLite storage remains Schema 18: existing JSON records are explicitly versioned,
so no DDL migration is required. `perception-decisions/2` records Boolean answers
separately from internal tri-state/reason audit entries. Old completed OB and
receipts remain immutable. Incomplete legacy Evidence / partial decisions cannot
inherit an unsafe old Positive or be backfilled with current conditions; they
stay unresolved behind the original projection watermark. Known version 1/2
empty-Signal events can complete without inventing an Observation; absent or
unknown-version Evidence cannot silently complete even an empty-Signal event.

## Conditions and actual Engine evidence

| Branch | Outcome / actual source |
|---|---|
| Candidate Scene | `discover_signals` selects only Scene candidates; no sensory judgment. Out-of-Scene handling is an explicit discovery exclusion. |
| Explicit absent channel / channel-specific blocked scope | Negative from authored sensory capability / trusted channel obstruction, not a geometric sound calculation. Special visual-sense conflicts remain unknown. |
| Blinded without a supported Blindsight interaction | Negative via canonical Engine `conditions_auto_fail_check(..., "sight")`; Truesight is not an exemption from Blinded. |
| Blinded with Blindsight | Unresolved: no copied composite condition/range logic. |
| Ordinary visual target with complete grid/positions/senses | Engine `GridTopology.can_see()` supplies mechanical admission. False is Negative; true is **Unresolved**, not perceived=true. |
| Darkness / Darkvision / Truesight / heavy obscurement / Blindsight / walls / total cover | Real pinned GridTopology functions are exercised, including their ranges and exceptions; SM copies no distance or light formula. |
| Self-emitting light | Explicit kind; known Engine LoS/total-cover obstruction can be Negative. Otherwise Unresolved because creature-visibility API is insufficient to model luminous phenomena without invented radii/intensities. |
| Deafened | Negative via canonical Engine `conditions_auto_fail_check(..., "hearing")`. Canonical Deafened does not block the separate visual branch. |
| General sound / visual wall | Unresolved: the Engine has no general acoustics/automatic-noticing API; a visual wall is not silently an auditory barrier. |
| Observer recorded inactive/dead at commit | Unresolved: the stored result does not establish noticing before death inside that command; no inferred death or consciousness rule. |
| Missing origin/state/grid/positions, unknown Conditions/traits/effects or unsupported combinations | Unresolved with reason code. Unknown data is never silently false or an automatic Positive. |

There are **no deterministic Positive leaves justified by the installed Engine**.
This is an intentional bounded capability statement: can_see describes ability,
not actual noticing. Eligible unresolved questions use the existing batch
JudgmentRequest/Result callback outside the write transaction. It now carries
only the relevant copied event-time evidence and reasons. Accepted Positive or
Negative candidates record `validated_judgment`; malformed, missing, duplicate,
unknown or failed batches do not complete work. Definitive Negative answers are
never sent to that callback and cannot be overridden by it. No JEV/model or
formal Perception check is implemented.

Missing D&D mechanical state, nontrivial lighting/occlusion without coordinates,
unsupported mechanics, and unknown Evidence cannot be repaired by a noticing
callback. Only the two explicitly synthetic RulesetBindings retain hand-authored
adjudication without D&D state. Event-time world version / game tick accompany
callback evidence. Unknown stored Signal fields/types raise `invalid_event` before
any projection write; malformed/unsupported Evidence remains unresolved.

## Verification and compatibility

`test_perception_conditions.py` tests the real Engine admission functions and
actual SQLite. It covers light levels, special senses, Conditions, occlusion,
missing/unsupported evidence, strict Signal schema, Scope separation, mixed
Signal aggregation, reason audit, restart, source identity, Inbox/Work/Outbox,
watermark gaps, replay and Event-transaction rollback. A real old runtime creates
the legacy save used for missing-evidence compatibility checks.

The original PER-19-22-01 reproduction is now
`test_per_19_22_01_dark_scene_no_false_positive_sqlite_restart`: an original NPC
Package plus real noncombat foundation, dark initial grid, legal object-state
command, database close/reopen and asynchronous projection. It asserts no visual
OB/Inbox/Work/source row, a held Perception cursor, independent successful delivery,
unchanged world save/receipt replay and persisted dark evidence. No fake Rule
Evaluation Result is used. The original historical audit is retained in the
[SM-22 review](SM22_GAME_TIME_SCHEDULE_REVIEW.md).

A separate executed comparison used archived `4745f17` source and current source
against the same original NPC fixture with the admitted dark noncombat grid and
the same legal object command (independent temporary SQLite files):

| Runtime | Receipt | Projection of Events 1–3 | Actual OB rows | Perception cursor |
|---|---|---|---:|---:|
| `4745f17` | committed | resolved / resolved / resolved | 2 | 3 |
| SM-PER-1 | committed | resolved / unresolved / unresolved | 0 | 1 |

This establishes the original failure as well as the new regression's behavior;
no evaluator result or sensory answer was injected for this comparison.

Tests that previously assumed `senses + clear => perceived` now explicitly supply
the existing hand-authored Judgment callback when testing downstream synthetic
Inbox/EVA behavior. They do not bypass the condition tree or mock Engine rules,
and retain their database/rollback/idempotency assertions. Such callback tests
verify protocol adoption, not autonomous perception correctness. Engine attack
initialization still rejects dark/Condition-bearing snapshots; sensory tests do
not weaken that gate and use admitted noncombat data or direct real GridTopology
inputs instead.

The real cross-phase and original combat-only tests now also retain genuine
missing-state gaps: a Workshop NPC without mechanical state and an entry event
before combat initialization cannot receive fabricated perception. Later valid
Observations remain persisted but cannot cross those gaps. The lethal attack's
observer state likewise stays unknown rather than manufacturing pre-death notice.

Final locked validation (2026-10-11):

| Command | Actual result |
|---|---|
| `uv sync --locked --group dev` | Passed; 19 packages checked, pins unchanged. |
| `uv run --locked python -m pytest -q` | **768 passed**, 2 upstream Lark deprecation warnings, 326.58 s. |
| `uv run --locked python -m ruff check .` | Passed. |
| `uv run --locked python -m ruff format --check .` | Passed; 112 files already formatted. |
| `git diff --check` | Passed. |

The original 715 cases remain, with **53 new collected cases**. The final focused
conditions / cross-phase / D&D hardening / perception run was **110 passed**.
The table includes definitive Negative and Unresolved branches, while Positive
adoption is tested only as an explicit Judgment protocol boundary, never as a
fabricated deterministic Engine result.

## Changed files

- Runtime: `src/trpg_state_machine/package.py`, `events.py`, `service.py`,
  `perception.py`, new `perception_conditions.py`.
- New regressions: `tests/test_perception_conditions.py` (53 collected cases);
  explicit synthetic protocol helper: `tests/perception_support.py`.
- Existing tests updated for unsafe automatic Positive assumptions:
  `tests/test_perception.py`, `test_migrations.py`, `test_cross_phase.py`,
  `test_dnd_hardening.py`, `test_npc_evaluation_tasks.py`, `test_npc_modes.py`,
  `test_npc_outputs.py`, `test_npc_state.py`, `test_observation_retention.py`,
  `test_observation_eva_lifecycle.py`. No test was deleted.
- Documentation: `README.md` and this review. No SQL, lockfile, dependency,
  Rule Engine source or Meta Repository file is changed.

## Remaining boundaries and review conclusion

The reproduced **new-event PER-19-22-01 false Positive is fixed**. This does not
certify or retract historically committed bad Observations/Meta: silently
rewriting their provenance, Work or receipts would violate existing contracts.
Legacy incomplete events can consequently hold a cursor until a separately
reviewed recovery policy exists. No new bypass or fabricated legacy boundary is
introduced in this batch. Earlier review reports retain their original findings.

Targeted source/SQL/diff review found no new P0/P1: Event evidence and command
writes remain atomic; callback work is outside write transactions; durable
answers/OB/Inbox/Work retain their existing identities and authorization; missing
judgments preserve gaps; no second state owner, ACK or rule calculation is added.
Previously contaminated saves and incomplete legacy projection still need an
independent recovery policy. This is a remaining compatibility/merge-review
boundary, not a claim that every historical Global Merge Blocker is cleared.

Automatic noticing, actual JEV/Host, formal Perception rolls, general acoustics,
distance/intensity thresholds, luminous-effect propagation, extra Channels/Scopes,
composite special-sense Conditions and active-combat restart remain unsupported.
Within-command changes to observer capability can require finer temporal
evidence; this batch does not reconstruct intermediate mechanical snapshots.
Unresolved and storage/protocol errors must remain distinguishable from Negative.
This change alone is not certification of full perception coverage or all legacy
saves. Human review is required; no Merge or PR is performed.
