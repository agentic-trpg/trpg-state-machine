# RF-RE4: history-free Rules adapter

2026-10-20 follow-up: Engine/SRD now both pin
`a011eba3ea2226e22d8081294ee38d3ec193b325`: Evaluation `/24`, Snapshot `/17`,
evaluator `/37`, Stage Facts `/2`, Openable `/1`, Availability `/13`.
SM retains one RE FrozenRuleset per selected corpus. See
[current production paths, counters and old-save policy](INSTANCE_CONFIGURATION_PROGRESS.md).
Existing `/36` saves keep their binding: audit/instance upgrades remain available,
while rule execution/save/resume under `/37` explicitly refuses the mismatch.
No automatic live-save repin or new combat/magic capability is provided.

2026-10-17 follow-up: the current RE pin and bounded Openable additions are in
[Openable progress](OPENABLE_SERVICE_PROGRESS.md); the RF-RE4 baseline evidence below
remains historical. Combat/magic Openable gaps remain explicitly unfinished.

## Delivery boundaries and progress

This batch changes `trpg-state-machine` only, on
`refactor/sm-rf4-rules-adapter-upgrade`. Actual implementation baseline:
`5ff7a807d5b29cceb4311dcb2fe5b1182ead6bd1` (the separately committed GC2 work).
The initial inspection saw `9ea750bab2c1ff6d990a02b13b10ffff1c81ed88` plus that work;
RF4 preserves its commit and does not rewrite or include it in RF4's own diff.
The architecture repository is unchanged.

RF-RE1, RF-RE2 and RF-RE3 were independently validated, committed and pushed before
SM dependency installation. RE's actual starting SHA was
`38fcfcd4c3a744b30f792775415e798b323d4331`. The three batch commits are
`b7f891ee83cafdc619ffc929917f693951beff86`,
`0da207ddf504fb17b48b424b787c6962422a351b`, and
`675552406e67470bf3456fe90e88c4f1595bf324`.
The [RE audit and validation record](https://github.com/agentic-trpg/trpg-rules-engine/blob/675552406e67470bf3456fe90e88c4f1595bf324/docs/dev/rf-history-free-evaluation.md)
contains the history-consumer map and preceding batch evidence.

Both `pyproject.toml` and `uv.lock` fix Engine and SRD to the final full SHA above,
using remote Git subdirectories, never a path or moving branch. Both packages
remain distribution version 0.6.0. Local contracts are Evaluation `/23`, Snapshot
`/17`, Availability `/13`, and evaluator binding `dnd5e-evaluation/36`.
SQLite storage schema is 23. These local versioned contracts are not a frozen
cross-module protocol or a full stateless integration certification.

## Current facts and controlled writes

All snapshots are assembled inside one consistent read transaction. Evaluation
runs outside SQLite transactions. A short write transaction rechecks identity,
NPC grants/handoffs, idempotence/fingerprint, world version, the complete projected
current snapshot and RNG. The checked Delta is converted to admitted component
edits; no whole candidate Snapshot is persisted.

| Fact and lifetime | Authoritative source / reader | Delta owner and admitted scope |
|---|---|---|
| Session pins, world version, RNG stream | SessionAccess: `sessions`, `rng_states`; Session lifetime | SessionAccess advances version/RNG and records durable receipts |
| HP/max HP, carried equipment, XP | CharacterFacts: `characters`; Session lifetime | CharacterFacts applies checked HP and bounded XP increments |
| Abilities, equipment grip, action budgets, mirrors | CharacterFacts: `mechanical_actors`; initialized once from approved complete facts, then current | Closed actor metadata field set; no arbitrary JSON patch |
| Inventory instances | CharacterFacts: `mechanical_components.inventory_json`; explicit typed initial stock | No consuming/item Delta is admitted by this SM batch |
| Resource pools / casting repertoire | CharacterFacts: same row's `resources_json` / `casting_json`; explicit typed stock or explicit contract `null` | Read dependencies only; no new resource payment, spell or feature command |
| Effects | Same row's `effects_json`; explicit empty list for current foundation profile | Nonempty initial effects and effect mutation are not admitted |
| Actual current positions | CharacterFacts: `actors.current_scene_id/cell`; Session/world lifetime | Existing spatial service validates and writes actual positions |
| Scene grid/obstacles | WorldFacts: current `mechanical_scenes`, with existing spatial admission guards | Existing world/spatial writers; no obstacle inference from prose |
| Turn, death/damage and movement ledgers | CombatRecords: `combats`; encounter lifetime | Closed combat/ledger edits with expected values and ownership checks |
| Ordered per-call Engine events | Result `proposed_events` only | EventPersistence appends new events, Evidence, Outbox and NPC work in the same transaction |
| Historical audit / cognition | Existing events, Evidence, queues/barriers, Meta and receipts | Original authorized audit/projection APIs; not an evaluator input |

The new components row requires all four explicit JSON values. Its absence is
unsupported; it never means empty. Each component is fully type-validated before
owner filtering, and unknown owners are rejected. The projected Snapshot checks
cross-component actor/resource/casting closure again. This batch provides no old core-to-typed mapping: nonempty old core resource
or effects JSON is refused even when a separate typed stock exists. Existing explicit character
spell-slot/custom-counter mirrors remain preserved; `null` is the upstream bounded
contract's intentional absence of a separate pool/repertoire component, not a
claim that those mirrors are zero. Explicit nonempty typed pool/repertoire stock
is also preserved without enabling its spending or casting.

Combat and noncombat foundations must agree on actors, scene and the four stocks.
Noncombat `combat_setup` must explicitly be `None`: combat.start is not admitted.
First combat-only initialization reads existing core HP/max HP. Changed equipment
without provable current inventory instances is rejected rather than reconstructed.
Later exploration/combat reads use the same current rows. No template reload
resets HP, equipment, resources or positions.

Explicit approved combat initial cells fill only missing positions during fresh
Session creation before its first event. Already known actual cells take priority
over template cells; inconsistent scenes are refused. All later preparation and OCC read actual
positions, including internally mechanical scenes. Missing/overlapping/out-of-scene
positions fail closed. Active combat does not persist a second `actor_zone` copy;
closure freezes final cells only for closed-combat historical reads.

`CombatClose` permits exactly one closure operation: only ended and positive,
representable XP increments mutate current state. Its `historical` deaths, residual
HP/temp HP and expended resources must equal previously committed facts and are
not written again. The new RE commits a monster's dead flag at lethal damage;
closure no longer accepts the old normalization operation.

## History removal and retained boundaries

Neither snapshot builder nor request/OCC loads, concatenates, truncates or
reconstructs the historical combat event stream. SQLite trace regression checks
this through availability, real evaluation, atomic commit and retry. Event/queue
persistence may read individual newly appended events by primary key. Authorized
`read_events` still reads history deliberately; historical migrations/projections
retain their existing audit semantics. No performance or constant-complexity claim
is made.

SM retains its authored encounter initialization, explicit basic PC/NPC attack,
trusted GM ability/skill check and bounded victory/TPK close. Other RE intents
remain outside SM admission, including pass, dash, spell/item/feature operations,
rest, reactions and continuations. Availability reports the same SM capability
boundary. Unknown Delta operations, invalid pre-values, ownership, duplicate writes,
bad dependency fences and RNG transitions cannot commit.

Engine Result acceptance is a proposal. SM alone commits all controlled facts,
ordered Events, RNG/version and receipt atomically. Existing fault, revocation,
NPC handoff, response-loss and fingerprint tests remain. Engine intermediate
Events still lack per-stage mechanical evidence: their evidence remains explicitly
Unresolved; final world state is never substituted for event-time facts.
Active-combat cross-process continuation is still unrestorable. No new Evidence
protocol, perception queue/watermark policy, evaluator service or registry is added.

## Explicit compatibility policy

Schema 23 widens only the existing mechanical-version constraint and adds the
component table in the existing locked migration transaction. It preserves old
mechanical version values and every original row/column, including HP/equipment,
RNG, events, Evidence, receipts, cognition and queue/barrier proofs. No old Session
is marked `/17`, repinned or supplied fabricated component rows. Crash/fault
rollback and reopening are covered by actual Schema 22 runtime databases; the
existing real Schema 7/8 combat archives retain their original mechanics and audits.

Default loading rejects old D&D `/4` and `/8` Package bindings. Explicit
`load_adventure_package(path, allow_legacy_audit=True)` retains their original
content and digest for the same authorized Session audit, including receipt/RNG
reads and existing historical delivery/projection. Old mechanical Snapshot reads
return `unsupported_mechanical_version`. New game commands, new Session creation
and pending dialogue publication are refused; already published output results
remain readable/replayable. This flag does not loosen Session ownership or pins.

`trpg_state_machine.rules_migration.migrate_package_16(data, loader=...,
package_version=...)` is an explicit source conversion helper. It accepts only a
complete `/16` Package with the original `/35` same-effective-data binding, all
required facts, valid typed history/references and consistent ended state. It
returns new source content with a new Package version/digest and `/36` binding;
it does not mutate its input or access/repin a live Session. The complete converted
source must pass ordinary Package validation before a new Session can use it.
Earlier `/1`/`/2` facts, missing fields, contradictory history and changed data are
refused. There is no general old live-save upgrade. Original audit history remains
with its original owner.

Legacy ledger regression fixtures now supply complete terminal actors, sides,
positions and matching mechanical kinds. Missing former-participant facts are
not inferred from a death-record label. Ownership fault fixtures keep a living
next-turn target so the unrelated third actor cannot become legitimately eligible
when the new RE skips a dead actor. No fault assertions or historical-row checks
were removed.

## Executed validation

Development baseline with the old installed pin: 90 real attack/check/closure/
cross-phase tests passed in 25.90 s. Remote pin resolution and locked installation
succeeded. After adapting typed closure and death flags, 97 focused tests passed
in 27.74 s. Development failures were repaired before the final gate, including
strict JSON tuple parsing, required noncombat setup, current death-ledger closure
and tests using an unrelated weapon loader.

The first complete new-pin run found 24 failures and 36 setup errors, with 939
passing tests in 340.92 s (343.69 s outer). Repairs preserved existing assertions:
actual positions now take priority over authored template cells; spatial fixture
conversion is explicit rather than changing every perception fixture; the NPC
custom-weapon test supplies equipment and inventory ownership; migration table
expectations include the new empty table. The affected rerun passed 271 tests,
with one remaining fixture race failure in 134.08 s: preparation now owns a read
transaction, so the OCC race was moved to a genuine second SQLite connection.

Read-only review also found a receipt ordering regression in the new early NPC
scene check. That check now runs after replay misses in both transaction phases;
original grant/fingerprint checks still precede replay. A real committed-attack,
response-loss, changed-scene retry regression guards reconciliation without
another mechanical mutation.

The final affected rerun passed **74 tests in 23.45 s** (26.36 s outer),
including actual-position OCC, real NPC handoff response loss, and the new
history-free adapter/migration cases. The independent read-only reviewer
rechecked the corrected two-phase ordering and found no remaining blocker.
The reviewer performed static review only and did not edit or execute tests.

Final local commands, executed after all source/test edits:

```powershell
uv sync --locked --group dev
uv run --locked python -m pytest -q --tb=short
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
```

The locked remote installation had already succeeded before development and
was not repeated on unchanged dependencies. The full pytest gate passed
**1,000 tests in 344.34 s** (347.48 s outer stopwatch), with two existing Lark
`sre_parse`/`sre_constants` deprecation warnings. Ruff check passed, all **163**
files passed format checking, and whitespace checks passed (1.98 s combined).
The full suite includes real SQLite and the actual installed remote Engine,
subprocess examples/reopening, invalid Delta/pre-value/RNG injection, rollback,
fingerprint/replay, revocation, closure/XP, Evidence, Outbox, perception queue
and watermark checks. No assertion or coverage threshold was removed.

RE final Integration gates had independently passed in 924.32 s (RF1),
776.01 s (RF2), and 792.62 s (RF3), using the batch-specific base with
`uv run --no-project python tools/validate.py fast --plan --base <batch-start>`
then `integration --base <batch-start>` (GNU Make unavailable). The RE report
records suite counts and development failures separately. Native Actions were
subsequently verified successful for each exact pushed commit:
[RF1](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38295994130),
[RF2](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38297934591),
and [RF3](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38301229415).

RF4 source and contract validation is complete. Its containing commit is the
SM delivery commit; branch and remote SHA are verified after commit/push in the
final handoff. Only the requested feature branches are pushed, with no merge,
rebase or force push. The RE unrelated untracked worktree and pre-existing SM
GC2 baseline commit remain preserved.
No GitHub Actions workflow is configured in this SM repository; local evidence
must not be reported as a remote CI result.


## RF4-REV-01 follow-up

Review found one omitted downstream position reader: EventCapture still read
removed active `actor_zone` for mechanically complete Scenes with spatial
admission disabled. The [position evidence repair record](reviews/RF4_REV01_PERCEPTION_POSITION_SOURCE.md)
records the real public-API red regression, both actor roles and Evidence shapes,
consumer audit and validation. New captures now use actual CharacterFacts
positions against complete current mechanical geometry regardless of movement
admission; closed-combat positions remain historical only. Existing missing-coordinate
Evidence is never repaired from the present. Engine intermediate-stage Evidence
remains explicitly unavailable and is outside this repair.

Repair validation completed: 244 focused tests passed in 77.63 s; the final full
SM gate passed 1,023 tests in 457.41 s (23 new SQLite regressions), with only the
two existing Lark warnings. Ruff, format (165 files), whitespace and unchanged
RE/SRD pin checks passed. The repair was self-reviewed; no new independent agent
review or remote CI success is claimed. See the linked record for exact commands,
red/green evidence, historical limitations and delivery boundaries.
