# Runtime instance ownership — 2026-10-22

## 2026-10-22 shared passage reverse association

Architecture Decision **[DECIDED]**, existing MC §2.3a (World cross-scene
associations and mutation-maintained validity); Implementation **[IMPLEMENTED on
fix/runtime-local-query-reuse]**. This follow-up starts at reviewed SM
`9627eb0c9b906bb7a5d2e477539e586e7d58ac5a`, paired docs
`docs/runtime-local-query-reuse@64cd6801ed127e02226eefc419ebc4ffc5f8c53a`.
Fetch confirmed both remote heads unchanged. Existing SM untracked worktrees and
text file are preserved. No main merge/push, RE/Pages edits, skill, schema or
dependency changes. The preceding batch's measurements below remain historical.

### Cause, owner and maintenance

The actual public chain was SetObjectState → SessionMemory.changed →
_light_candidate_changes → _light_door_scenes. Its filtered json_each SELECT
expanded all persisted Session passages even for a box without any association;
OLD/NEW also requested the same object twice. A per-object query-cache hit hid
later repeats but did not share the negative result across different objects.

Existing InstanceData.passages was already the common by-door/scene/ID entry used
by Openable occupancy, spatial restrictions, interactions, Light and Activity/NPC
consumers; it also filtered through json_each. No reusable passage reverse index
existed (World.mechanisms.index is the separate mechanism component index).

WorldRuntime.passages now holds one FieldValue containing immutable PassageIndex:
the ordered frozen PassageDefinitions and read-only door/scene/passage-ID buckets,
plus each door's ordered, deduplicated endpoint scene IDs. Buckets share the same
records. InstanceData.passages and _light_door_scenes both use passage_index();
the latter directly selects door_scenes, including a valid empty tuple. OLD/NEW
object IDs are deduplicated before lookup. Ground-blocker geometry maintenance
remains independent. There is no Light-only passage directory or writable copy.

The authoritative source is instance_configs' Session passages, never Package.
First use, related invalidation or eviction prepares the complete shared layout
once, from one persisted projection, across subsequent objects/commands. Valid
resolve_field reads compare only the maintained passage_index:<session> stamp
(existing generation plus candidate counter). The immutable index's deepcopy
returns itself; lookup never copies the directory or checks individual configs.
By-door combined filters inspect only matching records; full/scene outputs still
have their requested output cost. Complete navigation output/audits are retained.

SessionMemory._passage_changes, in the existing TEMP change callback, compares
OLD/NEW Session layout configuration on controlled config writes only. Passage
addition/removal, door rebinding, endpoint/scene changes and config identity or
row insertion/deletion advance the registered stamp. Unchanged passage metadata
and ordinary object config/state writes do not advance it. REPLACE's existing
invalidate_table fallback also covers this stamp when SQLite omits delete hooks.
Controlled layout replacement retains the existing affected Light-field
invalidation. No object mutation enumerates Session passages to maintain validity.

Index values and stamps use the same UnitOfWork pending owner/version root as
facts. Savepoint/full rollback restore them together, including repeated numeric
counter values; old index references retain old bindings/endpoints. Unload,
budget eviction and recovery use existing release/generation handling. No new
cache lifetime: the field is charged to the existing entry/32 MiB payload budget.
COMMIT→publish failure/unknown outcome still pauses current access; another
connection's recovery fences both contexts before serving persisted new relations.

### New deterministic measurements

Before editing production, the real public-command test reproduced the reviewed
query's **39 / 79 / 439 SQLite VM steps** with +0/+10/+100 unrelated passages;
the true door measured **58 / 98 / 458**. These are helper-query opcodes, not
SELECTs/rows. Baseline reproduction tests intentionally failed the new zero-scan
assertion. Subsequent warm operations below use two different ordinary boxes and
a real shared door, closing and opening each via public commands. Ambient bright
keeps reopening authorization independent of the closed threshold's LOS.

| Work inside the candidate helper, +0 / +10 / +100 unrelated passages | Before | After |
|---|---|---|
| First ordinary box close, SQLite VM steps | 39 / 79 / 439 | 0 / 0 / 0 |
| First shared door close, SQLite VM steps | 58 / 98 / 458 | 0 / 0 / 0 |
| OLD/NEW door-association lookups per changed object | 2 | 1 |

After shared preparation, all public box/door close/open operations have zero
passage JSON parses, PassageDefinition decodes, full-layout record visits and
index builds. Each helper uses one passage-version check. Whole-command counts
(including command construction, authorization, evidence and persistence) are
identical at all three growth sizes:

| Operation in sequence | SQL SELECTs / rows | Total SQL VM steps | RAM queries / consumed rows | All version checks / passage checks |
|---|---|---:|---|---|
| First box close | 20 / 3 | 1559 | 96 / 55 | 199 / 8 |
| Different box close | 7 / 2 | 1393 | 68 / 52 | 194 / 8 |
| Each box open | 6 / 2 | 1394 | 64 / 49 | 195 / 8 |
| Shared door close | 21 / 3 | 1595 | 101 / 59 | 202 / 7 |
| Shared door open | 6 / 2 | 1394 | 66 / 52 | 173 / 5 |

These still persist each command (UPDATE×3, INSERT×7); zero helper SQL does not
mean zero command SQL. The counts also include BEGIN×5, PRAGMA×16, SAVEPOINT×2
and RELEASE×1, unchanged across the growth sizes. No admission checks removed.

Separate cold preparation after unload: +0/+10/+100 means 2/12/102 total passage
records, **one SELECT / one row, one JSON parse, one index build**, decoding and
visiting exactly **2 / 12 / 102** records. Returned JSON bytes are **723 / 4223 /
36003**. Total cold SQL VM steps are 45 each, including BEGIN×1/PRAGMA×3; this
does NOT make SQLite's internal JSON extraction or Python decoding constant cost.
Three later different bucket/helper requests together use three scalar passage
version checks, zero SELECT/rows, parsing, layout visits or builds. Outside an
existing scope they still open three read scopes (81 VM steps including their
BEGIN/PRAGMA); the public mutation helper runs inside its existing scope (0).

### Executed checks and limits

- `uv run --locked --no-sync ruff check src tests examples` and
  `ruff format --check src tests examples`: exit 0, 223 formatted files.
- Runtime selection: test_runtime_passage_index, test_runtime_local_query_reuse,
  test_runtime_local_query_correctness, test_runtime_field_maintenance,
  test_runtime_instance_ownership, test_runtime_transaction_failures,
  test_runtime_four_fixes, test_session_memory_runtime,
  test_session_memory_rng_failures and test_scoped_runtime_validation:
  first run **136 passed / 2 failed** (exit 1). The two old placement/step tests
  counted cold complete-layout bytes as local action bytes. They now explicitly
  prepare the shared association before measuring the local action; their byte,
  row, query, construction and no-audit assertions remain. New cold tests above
  independently measure the complete preparation instead of hiding its cost.
- Final new tests plus those two cases: **18 passed**, exit 0; separately the
  final counter run is **16 passed**. Covers empty/negative results, multiple
  passages/three scenes, light uncertainty and reopening, immutable old results,
  binding/endpoint edits, add/delete/REPLACE, unchanged metadata, rollback with
  repeated numeric stamp, unload/eviction/restart and cross-connection recovery
  after failed publication/unknown commit.
- test_world_lights, test_lockable_passages, test_spatial_world,
  test_world_spatial, test_object_spatial_service, test_navigation_activities,
  test_navigation_boundaries, test_navigation_faults and
  test_instance_config_migration: **153 passed**, exit 0. The real historical
  Schema38 migration test ran and passed; no environment-blocked migration here.
- Latest-result JUnit union: **292 distinct selected tests, all passed**;
  this is not a full-suite or remote CI claim. Reused lifecycle cases include
  partial/completed publication failure, old-reader fencing, permissions,
  disclosure, idempotency, RNG and SQL persistence. No unrelated full suite run.
- Both repositories' `git diff --check` and the changed Markdown link/anchor/fence
  check passed (7 files, 117 local links); no unverified remote CI claim.

Remaining costs: first/cold/invalidated layout preparation and its budget charge
are O(layout size); controlled layout replacement may inspect its full persisted
configuration. An index evicted or too large for the existing budget needs cold
preparation again. Necessary scene/full output and checking actual related doors
or scenes retain their real scope cost. No public map-edit API, general navigation
redesign, multiplayer or unbounded-layout retention was added or verified.

## 2026-10-22 local query reuse

Architecture Decision **[DECIDED]**, MC §2.3a. Implementation **[IMPLEMENTED in
fix/runtime-local-query-reuse]**; Acceptance below covers these five consumer
groups, not every runtime scan. Starting SM/refetch/source HEAD:
`a2a2fe00d5f051978a6f4738df25fc81d838361e` on
`origin/refactor/session-memory-runtime`. Paired documentation starts at
`6b0cb55320aec840e57d71d686d696b5773f49e8` on
`origin/docs/runtime-versioned-cache-keys`, in a new
`docs/runtime-local-query-reuse` worktree. Neither source had later remote
commits. Existing untracked SM `.worktrees/` and text file, and documentation
`.worktrees/` / `FIRST_BLUSH_MVP_REQUIREMENTS.md`, remain untouched. No main
merge/push, RE/Pages edit, reset, dependency/schema upgrade or parallel authority.

### Actual consumers and maintenance

1. Public get_illumination → Light.current_cell → sources used resolve_field's
   generic dependency leaves, despite zero recomputation. Sources now use a
   mutation-maintained source stamp; candidates/geometry use a geometry stamp;
   cell/topology/projection depend on their constant-size composite. Computation
   leaves do not escape to upper parsed readers. Both child stamps are registered
   even for zero enabled sources (ambient map lighting still matters), and retained
   when a composite survives version pruning. Geometry shares a safely immutable
   value; full source lists still return detached copies and disclose each source.
2. Public PlaceObject query/commit → _validate_placement now obtains actors from
   target cell_occupants, backed by existing RAM/SQLite position indices. It no
   longer obtains unused whole-scene object/actor lists. Existing local_topology,
   actor-at-destination refusal, reach, shared doors and candidate checks remain.
3. GridView prepares read-only Cell→occupant ordinal and (kind,ID)→cells maps once.
   occupied sorts only matched ordinals, preserving directory order and mult-cell
   deduplication; entity_cells directly selects the ID. Movement uses an immutable
   blocked overlay. Cover precomputes the maximum per-cell independent source;
   max-across-sources commutes with RE's max-along-ray. Optional creature half cover
   has its own prepared layer and an O(1) target override, preserving endpoint
   exclusion and real target-cell object cover. The RE ray/step/cover algorithms
   remain unchanged. Hidden preparation is outside the five DTO/asdict fields;
   deep copies retain the same fixed read-only preparation.
4. RepositionObject → validate_placement → check_candidate → _check_shape reads
   grid_edge_walls from the prepared topology, avoiding scene_grid/copy/format
   loops. Preparation checks all static walls once; unsupported integer/axis
   qualification still refuses. A bounding hierarchy selects inclusive ray/edge
   wall candidates, then invokes pinned RE's exact algorithm, including touches,
   diagonal/fractional/degenerate cases. Candidate cells, reserved ports, bounds,
   rigid internal edges and blockers remain checked locally.
5. prepare_scene_protection → _protection_input now shares fixed width/height per
   needed Scene in one existing read scope. _project_protection_input consumes
   these dimensions, not per-object grid JSON. Explicitly empty rain/precipitation
   AND wind properties are recognized before map access; unsupported pose/door
   checks retain precedence. None, partial unconfigured, unsupported and explicit
   none remain distinct. Registered provider identity/version/time and unknown
   provider opt-out are unchanged; different directions use the same fixed batch.

Source validity follows actual emitter state/membership, moved roots and carried
subtrees, relevant ancestor transmission/open/destruction and light/container
configuration. Existing member/parent buckets identify actual contributors;
negative empty-root tests use two indexed buckets, not Scene discovery. Geometry
validity follows real ground blocking footprints, maps and both shared-door
endpoints. Quantity/irrelevant object metadata, nonblocking unrelated motion and
empty unrelated transfers do not expire all lighting. Configuration source and
geometry effects are separate. Controlled Session layout replacement still
invalidates loaded relevant field metadata; ordinary object updates never do that.

Every stamp is maintained inside the same existing TEMP change callback/candidate
version root as its facts and associations. A callback-only point-read memo shares
unchanged ancestor objects between its maintenance passes; nested callbacks have
their own memo and it is discarded before any later statement. This is not a
cross-statement cache or transaction checkpoint. Savepoint/full rollback discard
candidate facts and stamps together. Existing COMMIT→publish failure pause,
unknown commit reconciliation, cross-connection generation fencing, unload and
eviction remain in force; old Views/inputs keep their own fixed data. No new
writable position, capability or map facts are introduced.

### Same-fixture deterministic before/after

The new test file was first executed against archived, unmodified `a2a2fe0/src`.
Baseline performance assertions fail as expected; these failures are reproduction
evidence, not passing validation. Earlier independent review numbers retain their
original meaning. Measurements below are newly run with test-side instrumentation:
SQL execution/fetched rows, SessionMemory.select/version and actual occupant
iteration, PreparedGrid.copy_data, the old protection-input encoder, map parses,
RE segment tests, wall qualification coordinates, field/model/topology builds.
No production test switches or removed admission checks.

| Fixed target / growing irrelevant input | Baseline | Repaired |
|---|---:|---:|
| Public same-cell dark light, +10/+100 disabled emitters: version checks | 43 / 313 | 9 / 9 |
| Same public light: field builds | 0 / 0 | 0 / 0 |
| PlaceObject, +0/+100 far actors: actors copied from scene directory | 2 / 102 | 0 / 0 |
| Same placement: unused object directory records | 5 / 5 | 0 / 0 |
| Fixed View occupied + entity_cells together: occupant visits | 13 / 213 | 0 / 0 |
| View can_step, 8×8 / 64×64: copied static blockers | 24 / 3776 | 0 / 0 |
| View cover_between, same maps: copied static blockers | 24 / 3776 | 0 / 0 |
| Each View geometry query: exact wall segment tests | 16 / 3712 | 0 / 0 |
| Public RepositionObject same maps: copied static blockers | 24 / 3776 | 0 / 0 |
| Same shape query: wall qualification coordinate checks | 64 / 14848 | 0 / 0 |
| Protection batch +10/+100 empty contributors: map copies / encodings | 14 / 104 each | 0 / 0 each |
| Same batch followed by two directions: map parses | 28 / 208 | 0 / 0 |

The View point row is **two calls together**; it does not relabel the earlier
independent occupied-only 7/107 observation. Both geometries still use their own
fixed snapshot, not a current scene lookup. The map fixtures place irrelevant
static blockers at y≥5 and wall segments at y≥6, leaving the requested local
geometry unchanged. Cold hierarchy/index preparation is outside hot-query rows.

| Repaired operation (sizes small / large) | SQL SELECTs / fetched rows | RAM calls / rows | Version checks | Field builds |
|---|---|---|---|---|
| Public hot light | 0/0 at both sizes | 1/1 at both | 9 / 9 | 0 / 0 |
| Public different dark cell | 0/0 at both sizes | 1/1 at both | 11 / 11 | 1 / 1 |
| Public hot placement query | 0/0 at both sizes | 7/4 at both | 65 / 65 | 0 / 0 |
| Fixed View point/geometry | 0/0 at both sizes | 0/0 at both | 0 / 0 | 0 / 0 |
| Public hot shape query | 0/0 at both sizes | 3/2 at both | 22 / 22 | 0 / 0 |
| Cold protection input batch, two directions | 15/15; 105/105 | 15/0; 105/0 | 106 / 736 | 0 / 0 |

Cold protection still reads every actual object/provider/pose and explicit
unsupported shared door; its linear work was not hidden as a cache hit. Only
redundant map transfer was removed. The batch now has BEGIN×1 / PRAGMA×3 rather
than 75/525 BEGINs from independent out-of-scope leaf calls. Public read rows also
have BEGIN×1 and PRAGMA×3; zero SELECT is not zero SQL. View-only methods open no
scope. SQL row counts are distinct from RAM returned rows and copied directories.

Environment's registered-provider fixture separately records first preparation,
real provider-version invalidation, four directions from a detached prepared batch
after provider mutation, and hot hit: map copy/encode/parse all zero. Cold phase
has 24 SELECTs/20 rows, 27 RAM calls/20 rows, 64 version checks and 14 field builds;
rebuild has 0 SELECTs, 4 RAM calls/5 rows, 35 checks and 3 builds; hot has 0 SELECTs,
2 RAM calls/3 rows and no checks/builds; fixed directions read no facts. These
registered-provider numbers are a separate fixture, not the +10/+100 batch.

The existing four real PutItem/TakeItem empty-bag operations with +0/+100 unrelated
objects still pass their original bounds: RAM calls 280/280, rows 168/168, version
checks 844/844, source/geometry/index rediscovery or recomputation zero. Required
container/weight facts, evidence and SQLite persistence remain; SQL has 36 SELECTs
/16 rows at both sizes. No existing assertion was relaxed for the new protocol.

### Validation and limits

The 99-file collection contains **2147 unique cases**. Disjoint whole-repository
groups executed 703, 713 and 729 cases; the two excluded real compatibility/fresh
process cases ran separately and passed with their original assertions/timeouts.
Group 0 initially had 701 passes and two AttributeErrors because the now-unused
world_spatial.canonical_json import had also been removed. Its two existing map
update fixtures use that module export. Restoring the explicit compatibility
re-export fixed both: the complete environment file plus new correctness/counters
and existing field-maintenance tests then passed **68/68** on final source.
Group 1 passed 713/713, group 2 passed 729/729, isolated passed 2/2. The earlier
group-0 exit 1/log is retained, not relabeled as an original all-pass run. No
unresolved failure, skipped case, missing case or duplicate exists in the final
coverage audit; supplementary reruns are not added to the unique count.

Between the first two groups and final source, the only Python byte difference
was that one compatibility import line. Removing it reproduces the earlier file's
exact SHA256; runtime function bodies and the other 221 Python files are identical.
Final 222-file hashes remain unchanged through the final focused/isolated/group-2
runs. This qualifies revision attribution rather than silently claiming the first
two groups started on the later import revision. The unused export performs no
protection map encoding. An earlier sandboxed broad attempt was interrupted while
stalled in asynchronous NPC setup and is excluded; the isolated NPC regression
passed 2/2 and the executed complete groups used the approved local environment.

Commands use the locked existing environment: `uv run --locked --no-sync pytest
-q` with selected file groups, `-o faulthandler_timeout=60`, repository-local
basetemp and JUnit; the final 68-case run also used `-s` for actual counters.
`uv run --locked --no-sync ruff check src tests examples` and `ruff format --check
src tests examples` both exit 0 (222 files). Both repositories' diff checks and
13 changed Markdown files / 186 relative links, anchors and fences pass. Historical
Git migration cases were actually executed within the complete groups; no missing
commit/environment case is labeled passed. No remote CI or multiplayer run.

Evidence under ignored `.test-tmp/`: local-reuse-baseline-evidence.log/xml;
local-reuse-complete-{0,1}.log/xml; local-reuse-acceptance-2.log/xml;
local-reuse-isolated.log/xml; local-reuse-final-focused.log/xml;
local-reuse-counts.json; local-reuse-audit.json; local-reuse-acceptance-hashes.json;
local-reuse-doc-check.json. They retain baseline failures, the two intermediate
compatibility failures, recovered final outcomes and revision checks separately.
Wall-index counters also verify one hierarchy node / zero leaf records in each
fixed local View geometry sample, rather than moving work to another whole scan.
New correctness covers ambient map changes with zero emitters, shading candidate
savepoint/full rollback, irrelevant object motion, config blocker changes, both
shared-door endpoints, unsupported wall qualification rollback, map replacement,
unload/restart, fixed DTO/deepcopy, independent/creature cover equivalence and
post-COMMIT publication / unknown-commit recovery through another connection.

Necessary remaining work: cold Scene source discovery, provider/object qualification,
full GridView preparation and requested complete outputs, new-cell illumination
against actual emitters, and the RE ray's relevant geometry. The BVH is a candidate
filter; heavily overlapping wall bounds can still yield many candidates. Nonfinite
legacy walls retain original RE fallback; they do not acquire shape qualification.
The adapter calls pinned RE's private _step_is_legal/_in_bounds and existing backing
fields; a future dependency upgrade must recheck this narrow compatibility seam.
GridView.entities_within_range retains its prior entity-footprint algorithm; this
batch changes occupied/entity_cells and fixed movement/cover, not all range/path
algorithms. Explicit Full Audit still performs its declared full work. Unsupported
dynamic vision, mechanical integration and external-provider boundaries are not
expanded; no multiplayer load or remote CI claim is made.

Latest follow-up: [local query reuse](#2026-10-22-local-query-reuse).
Earlier acceptance counts remain dated evidence for their original source.

## 2026-10-22 four fixes: transaction and local costs

Implementation scope: `refactor/session-memory-runtime`, starting at reviewed and
remote HEAD `5137742d8126588047f04ff4699ee14a94d3058b`. Paired documentation uses
the existing `docs/runtime-versioned-cache-keys` worktree, starting at
`8b18c198a9d724c1bb5b48b25d1911b0ba338a5e`. Both were clean of tracked changes;
SM's pre-existing untracked `.worktrees/` and text file are preserved. No new
branch, main update, merge, RE/Pages edit, schema/DTO/pin change or parallel state
manager is part of this delivery. Architecture remains MC §2.3a; implementation
is the following concrete repair, not a general claim that every cost is constant.

### Statement failure protocol (P1)

The original CHECK-constraint reproduction was run against archived `5137742/src`:
one UPDATE closes the first container, then gives the second an invalid state;
catching IntegrityError and exiting the outer transaction normally published
`get_object().open_state == closed` while independent SQLite returned `open`.
The final regression instead returns `open / open` and records commit refusal.
The prior section's **unresolved** finding is superseded only by this new evidence.

Chosen protocol is **B: fail the current write scope until a matching rollback**.
RuntimeConnection.execute catches statement failures; executemany also catches a
parameter source that raises after a successful earlier item. They mark the same
UnitOfWork failed and clear reading memo. RAM guards, write requirements and outer
pre-COMMIT check reject `transaction_failed`, even if callers catch SQLite errors.
No per-statement Session/cache/version checkpoint is introduced.
Store's first persistence upgrade (BEGIN IMMEDIATE, snapshot pin and savepoint
recreation) follows the same failure marking: it may already have ended the
prepared SQL snapshot, so a caught upgrade error cannot reuse that candidate or
commit it. Separate lock-acquisition and snapshot-pin faults cover this boundary.

RELEASE in existing participants' finally blocks is permitted as cleanup so it
does not mask the original storage_error. RELEASE does not reset the failed flag
or permit subsequent reads/writes/COMMIT; a dedicated test proves that distinction.

Existing SAVEPOINT stores candidate pending values, immutable version root, change
log length and prior failure flag. A successful ROLLBACK TO restores all of them,
clears memo/derived candidates and discards nested savepoints. Work before that
savepoint is preserved and execution can continue. The savepoint context checks
failure even when the inner SQL exception was swallowed, rolls back and raises.
Without such recovery the outer scope rolls back completely. SQLite ROLLBACK may
already destroy the entire transaction/savepoints; cleanup then skips nonexistent
SQL controls and discards all RAM candidates. Failure never poisons later Session
operations. ABORT, FAIL (which can retain earlier rows) and ROLLBACK are all tested,
including trigger errors and real FK cascades. Existing unknown-COMMIT and
post-COMMIT publication pause/reconciliation are unchanged and tested separately.

Production mutations still use RuntimeConnection execute/executemany and existing
Store/UnitOfWork. Direct native SQLite cursors/executescript are not a supported
runtime mutation bypass; existing migration/native maintenance retains its
explicit controlled initialization/reload boundary.

### Inventory fields and one complete typed validation (P2)

Actual path: StepActor → actors position UPDATE → changed/_inventory_changes →
public read_carried_items → CharacterFacts.inventory_model. OLD/NEW identity and
relevant fields now decide invalidation. Actor coordinates, character HP/max HP/XP
and object-shaped mechanical actor action/equipment metadata do not change the
current six-field InventoryEntry result. The precise source-field and consistency
list is in [Session Management](../SESSION_MANAGEMENT.md#2026-10-22-sql-失败协议与局部元数据).
Core carried slugs, rejection of legacy resources/effects, owner existence,
mechanical object shape, portable item facts, old/new ancestry access, destruction
boundary and relevant configs still invalidate. Cross-owner transfers maintain
both owners; unattributed component changes retain the conservative shared stamp.

`_validated_rule_components` caches the complete strictly validated typed result
and remaining component projections. rule_components produces its dictionary
output from that result; inventory_model uses the already validated owner subset.
Filtering an existing validated unique set cannot introduce invalid scalar values
or duplicate IDs. No strict=False or JSON scalar coercion is introduced. Portable
facts/projection equality, owner/slug consistency and duplicate ID checks remain.
A regression invokes both consumers cold and observes exactly one full inventory
validation; an actual aggregate quantity/charges change also rebuilds exactly once.
Complete public inventory outputs still copy/serialize their 11 / 101 entries.

### NPC target identity and persistent version roots (P2)

The real handoff → dispatch_npc_output → combat guard path queries
`actors WHERE session_id=? AND actor_id IN (?,?)` with acting and target IDs.
The existing RAM identity range supplies those two facts; no all-actor dictionary
is prepared. Both before-evaluation and before-apply checks remain, as do grants,
perception, target existence/same Scene, fixed inputs and OCC/replay handling.

The existing version mappings are now private VersionMap root handles over frozen
AVL nodes. Beginning a read/write scope, taking/rolling back a savepoint and
accept_read/publish fork roots in O(1). Lookup/update is O(log N); an update copies
only that key's balanced search path. Roots have no previous-root/overlay links;
only actual old scopes/savepoints keep old nodes alive. A fixed-seed mixed
insert/delete/fork test audits ordering, heights, balance, sizes and old roots;
4096-key testing bounds copied nodes by the search height. Normal reads/writes/
savepoints/commit do not enumerate version records. Candidate pending dictionaries
are still checkpointed according to this transaction's pending work.

Existing pruning now builds a new root only over retained dependencies (including
inventory's three shared stamp components) above capacity×32. If the retained set
exceeds capacity×16, rebuildable caches are released, leaving at least half-budget
headroom. Otherwise at least that much new metadata must accumulate before another
full prune. Eviction/explicit unload/recovery are lifecycle operations; there is no
unbounded overlay chain or per-operation high-watermark full rebuild. Old roots,
read-publish fences, memory generation, external data_version and cross-connection
recovery remain part of the original isolation protocol.

### Matched deterministic measurements

Final performance fixtures run on unmodified archived `5137742/src` and delivery
source in the same locked environment. All six old-source performance assertions
fail as expected after legitimate successful StepActor/dispatch/transaction work.
The separate original atomicity probe fails with the precise closed/open mismatch.
These failures are reproduction evidence, not current regression failures.
Previous `e776fab1` figures (298/798) are not reused as this baseline.

| Production slice / counter | 5137742 before | This repair |
|---|---|---|
| After Step, target inventory 11 / 101: full validations; entries validated | 2 / 2; 22 / 202 | 0 / 0; 0 / 0 |
| Same: RAM queries / rows | 26/25 ; 206/205 | 2/2 ; 2/2 |
| Same: version checks; inventory field builds | 154 / 1324; 1 / 1 | 6 / 6; 0 / 0 |
| Same: complete output entries | 11 / 101 | 11 / 101 retained |
| Local box read, +0 / +100 loaded unrelated objects: version records copied | 300 / 900 | 0 / 0 |
| Local write + savepoint rollback + write + commit: version records copied | 900 / 2700 | 0 / 0 |
| Both local slices: full version records enumerated after repair | not claimed | 0 / 0 |
| NPC dispatch, +0 / +100 remote actors: records returned by two target checks | 6 / 206 | 4 / 4 |
| Same entire dispatch: RAM rows | 76 / 276 | 70 / 70 |

All Step-following and local box read slices use zero SQL SELECTs/returned rows.
Local reads retain RAM 31 calls/13 rows and 155 version checks at both sizes,
not a claim that get_object performs only one scalar check. Local writes retain
RAM 6/2, 2 checks, two UPDATE executions and no domain SELECT. New AVL nodes for
that write/savepoint/rollback/commit slice are 208 / 266 in the final recorded run:
this is bounded path copying over actual version advances, **not** zero allocation
or constant update cost independent of N. No record enumeration/copy occurs at
transaction startup, query, savepoint or publication. Node totals can vary with
tree insertion order; the regression asserts structural/logarithmic bounds, not
one hardcoded node total.

Whole NPC dispatch SQL SELECTs/rows remain 28/25 at both sizes; it still validates
one one-entry snapshot inventory and constructs one snapshot. Its target slice
remains two checks (four records), and version checks 141 → 143 remain constant
across growth; whole-dispatch RAM calls remain 114, accessed rows become 70 at
both sizes. All target/local read weight/mechanism rebuilds and JSON/model
parses are zero. These categories are instrumented separately: a cache hit or
zero SQL alone is not used as proof. Cold data, actual dependency closure, full
outputs/evidence, aggregate ComponentInputs preparation and aggregate SQLite
inventory updates keep their necessary existing costs. Controlled REPLACE/table
maintenance retains conservative invalidation. Multiplayer/sustained load and
unsupported combat continuation recovery are not new guarantees.
Whole-Session configuration replacement still enumerates Store-retained version
records to select that Session's light-candidate stamps; this pre-existing broad-source
maintenance is not part of ordinary local read/write measurements or claimed fixed
cost. Direct REPLACE/table invalidation likewise retains its existing broad scope.

SQL counters use the existing real Connection.execute / fetched-cursor wrapper,
not trigger trace repeats or RAM row counts. Step-following/local read each has
BEGIN×1 and PRAGMA×3 in addition to zero domain SELECTs. Local write slice has
BEGIN×2, PRAGMA×5, SAVEPOINT×2, ROLLBACK TO×1, RELEASE×1 and UPDATE×2.
NPC dispatch has BEGIN×8, PRAGMA×24, SAVEPOINT×2, RELEASE×1, SELECT×28,
UPDATE×59 and INSERT×49 at both sizes. Native commit()/rollback() method calls
are not counted as execute statements; real durable commit/failure semantics are
verified independently. These metadata/control statements were not called zero SQL.

### Validation of the four fixes

Final focused acceptance: **75 passed, exit 0** in the locked environment, with
real counts captured in `four-fixes-complete-target.log` / `.xml` and
`four-fixes-complete-counts.json` under `.test-tmp/`. The six files are
test_runtime_four_fixes.py, test_runtime_transaction_failures.py,
test_version_map.py, test_versioned_runtime_inputs.py,
test_runtime_instance_ownership.py and test_runtime_field_maintenance.py.
Command: `uv run --locked --no-sync pytest -q -s` with those paths, unique local
basetemp, JUnit output and `-o faulthandler_timeout=60`. The separate **35-passed**
upgrade/RNG/publication subset is not added to the final unique count. Full union
results follow below. New tests cover the original CHECK case, all three
SQLite error policies with/without recovery, caught error read/write/commit,
prior legal work, trigger/FK cascades, executemany/parameter failure, actor movement,
HP/actions, first-persistence lock/pin failure, once-only typed validation,
true quantity/charge change, composite
pruning and AVL snapshots. Existing tests jointly cover transfers/access/config,
strict negatives, other-owner growth, savepoint/outer rollback, old readers,
unload/restart, authorization, RNG, idempotency and commit/recovery failures.

The older sections below, including their independent OPEN statements, remain
historical records for their original SHA. This section supersedes those four
findings only in its demonstrated scope; it does not rewrite earlier acceptance.

### Final complete-source acceptance

**2,119 collected / 2,119 unique executed tests pass**, exit 0. Disjoint sorted-file
partitions contain **739 / 703 / 675** cases, plus **2** separately executed original
cases. Collection/JUnit union has no missing, unexpected, duplicate, failed,
errored or skipped cases. All **220** Python source/test/example hashes remain
unchanged throughout this final complete-source run, including the last
first-persistence-upgrade failure protection. Historical migration tests are in
the union; none is labeled passed while blocked by unavailable Git history.

Actual invocations use `uv run --locked --no-sync pytest -q`, files assigned by
sorted test filename modulo three, `-o faulthandler_timeout=60`, unique local
basetemps and JUnit paths. The main partitions use
`-k 'not test_live_compatibility_baseline_vs_sm_frozen_verification and not test_real_vertical_pipeline_committed_observations_fresh_process_audit'`.
Those exact two original node IDs in test_frozen_ruleset_runtime.py and
test_dnd_hardening.py run separately with their original assertions and timeouts,
not skipped or weakened. Their final isolated result is **2 passed, exit 0**.
The 75-test target and 35-test fault subset are overlapping evidence, not extra
tests added to 2,119.

Intermediate runs are retained: one concurrent corpus test died with Windows
native access violation `0xC0000005` while Path.read_text was reading SRD files;
one fresh-process worker exceeded its existing 30-second timeout. Neither is
recorded as a passing intermediate run, nor as a demonstrated Python assertion
defect. Both original cases pass in the final isolated process. The native
failure's root cause was not assigned or repaired by this SM change; repeatable
parallel whole-corpus stress is not an acceptance claim. Early fixture assertion
mistakes and the superseded RELEASE-cleanup compatibility failure also remain
intermediate evidence, not the final results.

Evidence: `.test-tmp/four-fixes-complete-delivery-{0,1,2}.{log,xml}`,
`four-fixes-complete-delivery-collection.txt`, `four-fixes-complete-isolated.*`,
`four-fixes-complete-hashes.json`, `four-fixes-complete-audit.json` and
`four-fixes-complete-counts.json`. Matched old source reproduction is
`four-fixes-final-before.log` (seven expected failing new assertions); the archived
source remains `four-fixes-baseline/src`. Intermediate native/timeout logs are
`four-fixes-final-delivery-2-native-crash.log` and `four-fixes-final-delivery-1.log`.

`uv run --locked --no-sync ruff check src tests examples` and
`uv run --locked --no-sync ruff format --check src tests examples` both exit 0
(220 files). Changed Markdown links/anchors/fences pass in both repositories:
9 files / 137 local links; staged and unstaged diff whitespace checks pass.
Only the two existing Lark deprecation warnings remain in final pytest groups.
No dependency upgrade/synchronization, remote CI or multiplayer/sustained-load
validation is claimed. Cold aggregate/output costs and controlled broad
configuration maintenance remain the concrete limits documented above.

## Earlier acceptance records

The follow-up [mutation-maintained validity acceptance](#2026-10-22-follow-up-mutation-maintained-validity)
below supersedes the original warm-read cost claims only within its measured scope.
All original counts and acceptance dates below retain their original meaning.

Start: `refactor/session-memory-runtime@16c305999e5ac17ef4194a3113eeb60b9ed36902`;
`origin/main@3aaceccf1b1b492f8dd99e3bec16b9379ac6dc77`. The untracked user text
file is excluded. Architecture baseline: `agentic-trpg@5619e7a`; its isolated
documentation branch is `docs/runtime-instance-ownership`. RE and Pages are excluded.

## Pre-change ownership and migration inventory

| Data / current production reader | Before | Intended instance field / maintainer / dependencies |
|---|---|---|
| Object row, effective configuration | WorldFacts / InstanceData; indexed SessionMemory rows and generic parsed cache | Object runtime facts/configuration; original Facts remain the only writers; instance row/config dependencies |
| Direct contents and recursive weight / object view, capacity admission | ContainmentState adjacency and operation weights; generic `total_weight` key sorts every candidate row | Object.container.contents, own weight and subtree total; ContainerService computes on demand from parent-indexed Facts; quantity/config/children dependency closure |
| Scene direct objects | `scene_object_rows` assembles roots and descendants | Scene.objects.direct contains only direct ground objects; effective contributors include carrier/container links separately |
| Trigger and component links / automatic, damage | generic Session-wide `_build_index`, batch dict | Scene.mechanisms.index; object mechanism configuration/links; cross-scene component lookup remains World-owned; changes to membership/config/pose invalidate relevant relationships |
| Parsed map/topology, occupants / Grid, views, RE | GridMaps content pool and generic scoped reads | Scene.spatial.map/occupants; indexed cell lookup remains the local consumer; maps immutable, positions remain actor/object facts |
| Environment / area, public cells, height queries | service DerivedCache and repeated preparation wrappers | Scene.environment inputs/protection/cell/range fields; preserve source/protection versions, provider identity, heights, disclosure and bounded capacity |
| Light sources/results / cell query, GridView, snapshot | generic `sources`/`project`; cell query calculates whole grid | Scene.lighting sources/cell/full projection; source state, position, enclosing transmission and geometry dependencies; public visibility still checked |
| Recovery/current readers | shared pause flag; only recovering connection clears RAM | shared recovery generation; each connection invalidates on its own thread before current reads; old read scopes cannot republish |

All runtime fields are derived representations under the existing SessionMemory,
UnitOfWork/savepoint and durable-COMMIT/publication boundary. Creation and recovery
load persisted instance facts; cold fields are lazy. Rollback discards candidates;
unload/LRU/close release fields. No second Session/transaction executor, Cell system,
rule protocol, periodic-only saving or Package fallback is introduced.

## Implemented owner fields and actual consumers

`runtime_instances.py` defines immutable ObjectRuntime, SceneRuntime and WorldRuntime
owners with typed component groups. Existing SessionMemory stores their field values
under the same entry/byte budget; existing UnitOfWork stages and publishes replacements.
No new transaction, authority, persistent total-weight column or public protocol exists.

- WorldFacts.object / InstanceData.object_definition now resolve Object fields.
  Direct contents belong to Object.container; Scene.objects.direct contains ground
  IDs only, while contributors follow explicit carrier/container relationships.
- `get_object` and `list_scene_objects` read contents and derived weights directly.
  They do not construct ContainmentState. `total_weight` still supports detached
  candidate admission but has no scoped-read key; `runtime_key` is removed.
  Candidate relocation marks original/new ancestors; only an untouched subtree in
  the exact originating UnitOfWork and mutation stage can reuse instance weight.
  Changed ancestors read candidate adjacency, with existing fit/capacity/unknown,
  access/binding/cycle/nesting checks. Weight/config/quantity dependencies and the
  original destroyed-shell semantics are retained; partial quantity transfer remains unsupported.
- Automatic mechanism events use Scene.mechanisms.index after event-kind/phase
  filtering. Object.mechanism holds static trigger/component links, without
  fabricating a current status. Activation/disarm/reset/broken state remains a
  runtime prerequisite. World.mechanisms owns cross-scene component reverse links.
  Its cold scope is configured mechanisms, not all objects or unrelated capabilities.
- GridReader prepared/current maps, object positions, occupants and cell lookup
  are Scene.spatial fields. `local_topology` follows the existing actor-position and
  Schema40 cell indices for the requested cells and diagonal neighbors. It no
  longer uses a whole-scene occupant list. Mechanical maps do not imply fine
  spatial grants. Object-only geometry consumers do not acquire actor-position dependencies.
- Environment public cell/area/unit/height and internal projections use
  Scene.environment facts/inputs/protection/cells/ranges. The existing registered
  source/protection/provider version protocol remains authoritative; incidental
  decoder reads do not make lock metadata a rain-protection dependency. Unregistered
  providers still opt out. Area admission checks only the target instance and map
  bounds, without composing environment or loading all Scene configurations.
- Light sources, geometry, bounded cell results and explicit full-map projection
  belong to Scene.lighting. Public single-cell queries compose just that cell.
  Grid base topology, noncombat RE Snapshot and noncombat staged evidence share
  the current projection. Explicit combat/fixed-map inputs keep their supplied
  geometry and are not aliased to the current scene map. Visibility, sources and
  uncertainty still follow the original public/RE rules.

  Targeted object/door/area visibility uses only the requested cells' lighting.
  `ReadOnlyTopology.with_lighting` shares immutable parsed walls/blockers and adds
  a detached read-only lighting overlay; it does not parse/serialize the map again
  for each object. The pinned RE `can_see` algorithm is unchanged. Step/transfer
  bounds checks do not prepare lighting they do not consume. Complete scene views
  and mechanical/evidence inputs retain their explicit full-map scope.

### Relationship and validity maintenance

Simple RAM queries record predicate membership plus the columns actually selected;
unselected status changes do not invalidate identity-only association reads.
`SessionMemory.changed` advances affected projection dependencies and scene capability
association versions using the changed position and necessary parent/carrier chain.
Those chain reads use the same RAM/candidate interface, rather than bypassing loaded
facts with raw SQL. New/old scene associations, actor scene changes, capability
insert/delete and controlled REPLACE are covered. Savepoint restoration restores
the same sparse candidates and versions.

Cold capability discovery follows indexed ground roots/carriers/parent links and
filters identities before reading configuration or capability state. Its resulting
field depends on a maintained Scene association version and the selected instances,
not one validity probe for every unrelated object. Empty mechanism results remain
valid until a relevant relationship/configuration change. This is lazy association
loading, not a claim that cold directory construction is constant-cost.

Fields are immutable replacements; returned mutable values are detached. Nested
computations preserve other fields filled in the same scope. They participate in
the existing memory_pending publication and shallow savepoint snapshots safely.
Source/protection/height/provider identities still distinguish environment results;
clear/release epochs prevent pending old values from being considered current.
Per-field bounded lists and the original 2,048-entry / 32 MiB retained-payload budget
permit cold reload and eviction. This is not a complete Python heap upper bound.

### Cross-connection recovery

The shared database coordinator records Session recovery generations. Pause and
reconciliation advance the relevant generation. Each connection synchronizes it
on its own thread before opening a current reading scope, releasing that Session's
old fields/maps and obsolete TimeDriver. A read scope captures its generation and
cannot republish after a later recovery, even when SQLite data_version is unchanged.
Read-only recovery does not erase unrelated Session fields. No connection is used
from another thread; stale drivers are detached and require explicit reattachment.

Existing pre-commit rollback, unknown commit, committed-publication-pending,
receipt replay and shared pause semantics remain. Failed recovery leaves all
contexts paused. Fixed historical inputs remain valid historical values. SQLite
and RAM are still not a cross-medium ACID transaction.

## Deterministic public-path measurements

Executed `uv run --locked --no-sync python tests/_session_memory_benchmark.py
--output .test-tmp/runtime-instance-delivery-measurements.json`, exit 0. Both groups
warm two Put/Take cycles, measure 10 public object reads and 20 public Put/Take
commands. Growth adds 150 unrelated objects and 150 remote actors; objects occupy
both existing scenes. Separate scoped tests add 20 scenes and exercise cold local
admission and loaded-cell reuse. These are synthetic local measurements, not a
multiplayer load/SLO claim.

| Independent count | Growth 0 | Growth 150 |
|---|---:|---:|
| 10 hot reads: SQL SELECT / fetched rows | 0 / 0 | 0 / 0 |
| Hot JSON / model parses / fixed Snapshot builds | 0 / 0 / 0 | 0 / 0 / 0 |
| Hot instance field computations / hits | 0 / 360 | 0 / 360 |
| Hot RAM row accesses | 130 | 130 |
| 20 commands: SQL SELECT / fetched rows | 180 / 80 | 180 / 80 |
| SQL returned encoded bytes | 69,350 | 69,350 |
| Command RAM row accesses / published row updates | 829 / 160 | 829 / 160 |
| Command instance field computations / hits | 117 / 890 | 117 / 890 |
| Four commands: SQLite VM instructions | 8,356 | 8,356 |
| Retained scalar payload bytes | 673,359 | 836,074 |
| tracemalloc added retained / peak bytes | 1,117,579 / 1,232,898 | 1,117,619 / 1,232,556 |

Hot scopes still execute 10 BEGIN and 30 PRAGMA metadata fences plus permission /
disclosure checks. Commands retain 160 json.loads calls for new event persistence,
event reads and perception proof (40/80/40); zero model/content-cache parses does
not mean all JSON work disappeared. `parsed_builds=138` is generic wrapper work,
not a JSON counter. Necessary fit/aggregate checks, persistence and reliable keys
remain; there is no zero-SQL write claim.

The existing 100-content public cases (direct and nested) now construct **zero**
ContainmentState candidates for get/list, while preserving weights 100 / 102.
The full-candidate key constructor no longer exists. New public field tests forbid
the old `_rows` / `_weight` path during object reads. Single-cell light composition
observes exactly the one requested cell and then reuses it. Empty mechanism index
builds once across two real StepActor commands. Timing was collected under
instrumentation/concurrent validation and is not used as scale evidence.

The supplied historical 10,302 figure describes records visited while building
candidate cache keys, not SQL statements; it is not relabeled or claimed as a
fresh baseline measurement here.

## Acceptance and remaining boundaries

Final executed collection, disjoint JUnit accounting and checks are recorded in
the delivery entry below. Early runs exposed old tests that observed removed
candidate/project/cache internals; those assertions were migrated to production
field bodies and actual owner values, retaining behavior, authorization and
old-reader/version checks. The Environment input counter improves from two source
preparations for two cells to one shared input preparation. Test fixtures that
alter instance config perform explicit structural acceptance before public reads;
this does not create a new live configuration-edit API.

The restricted environment stalled before real RE execution in Windows
asyncio `_fallback_socketpair` / `socket.accept`. Those runs were stopped and the
same locked environment was run outside the sandbox. An initial measurement found
40 unnecessary position SELECTs in change callbacks (220 SELECT / 120 rows); those
reads were moved to RAM before the final 180 / 80 measurement. Interrupted full
runs and intermediate failures are not counted as final acceptance.

A subsequent complete-call-chain check found the old single-object visibility
consumer still entering a full light projection. The target-cell overlay above
closes that path; a public get_object test forbids whole-map projection and checks
that the old base geometry/lighting stays immutable. Final checks below include
this final consumer migration; earlier runs are intermediate evidence.

Cold association discovery and requested complete geometry/projections retain
their declared directory/mechanical scope; this work does not invent a constant-cost
full-map algorithm. Provider production support is unchanged. There is no separate
unload_scene API; Session unload/LRU/close govern retained fields, and unloading
does not destroy persistent actors/items. Full Effects/Activity, arbitrary Cell
instances, multiplayer protocol, partial item splitting and active-combat crash
continuation remain outside this change. Schema40, DTO/RE pin and rules are unchanged.

## Final verified delivery

Named fields/consumers/recovery fixes: Implementation Status **[IMPLEMENTED]**;
Acceptance Status **[VERIFIED]** for the scope above. This does not close the
larger unsupported systems listed above. The final runtime, tests and examples
were held fixed throughout this complete acceptance (SHA256 comparison: zero changes).

| Executed check | Exit / evidence |
|---|---|
| `uv run --locked --no-sync ruff check src tests examples` | 0 |
| `uv run --locked --no-sync ruff format --check src tests examples` | 0; 214 files |
| `uv run --locked --no-sync pytest --collect-only -q` | 0; 2,059 tests in 92 files |
| Complete file partition 0 | 0; 633 passed; JUnit 801.461 s |
| Complete file partition 1 | 0; 738 passed; JUnit 539.861 s |
| Complete file partition 2 | 0; 688 passed; JUnit 467.461 s |
| JUnit / collection / SHA256 audit | 0; 2,059 unique tests; missing / unexpected / duplicate / failure / error / skip all zero |
| Final public-path measurement | 0; counts in the table above |
| Both repositories' changed Markdown links / anchors / fences | 0; 18 files / 172 local links |
| Staged and unstaged `git diff --check` | 0 |
| Dependency/lock/SQL migration diff | Empty |

The three complete partitions are disjoint sorted test files at indices `part,
part+3, ...`; this is local acceptance grouping, not a new CI tier. Each used
`uv run --locked --no-sync pytest -q @taskSubset`, independent
`--basetemp=.test-tmp/instance-complete-N`, `-o faulthandler_timeout=60` and
`--junitxml=.test-tmp/instance-complete-N.xml`. The existing locked `.venv` was
reused, with repository UV_CACHE_DIR, PYTHONUTF8 and cleared process PYTHONPATH
for historical subprocess isolation. Historical fixtures retain their own old
locked bindings; no fixture or production assertion was weakened to accept a new pin.

Final raw outputs are `instance-complete-N.log/xml`, `instance-complete-collection.txt`,
`instance-complete-hashes.json`, `instance-complete-audit.json`,
`runtime-instance-delivery-measurements.json` and `instance-doc-check.json` under
this checkout's ignored `.test-tmp`. Only the same two existing Lark deprecation
warnings occur in each partition. Focused runs are not added again to 2,059.
These are local checks, not GitHub Actions results.

Delivery keeps SM on its original branch and the docs on the new isolated branch;
only named task files are committed. The user text and original docs requirements
remain unstaged. The docs worktree remains under
`.worktrees/runtime-instance-ownership/agentic-trpg` for review. Final commit IDs
and normal push verification are supplied in the delivery response; neither main
branch is merged or pushed by this task.
## 2026-10-22 follow-up: mutation-maintained validity

Start/review baseline: `refactor/session-memory-runtime@c64f9d7975cad55e536e0e4ea7a056fed79bd384`.
Paired docs baseline: `docs/runtime-instance-ownership@80bd39c551dd62cb53bfd2d4a18f4c4c3a00182a`.
Both existing branches matched their remote refs after fetch. Original untracked
user text and nested worktrees are preserved. No new branch, main integration,
RE change, Pages edit, schema/DTO/dependency/rule change is part of this follow-up.

### Actual maintenance and isolation

- **Weight:** the three Object.container scalar fields use one object weight
  version. `SessionMemory.changed` invalidates the changed item's fields and the
  old/new ancestor chains for parent membership, item JSON (including quantity),
  creation/removal and persisted object configuration changes. Valid reads check
  that field version once; computation collects its leaves privately, so enclosing
  get/list, capacity and parsed caches inherit the field version rather than all
  descendants. Missing/stale fields compute from their actual candidate stage and
  retain that stage's stamp. Unknown remains `None`. Destruction still retains the
  supported shell/contents semantics; actual relocations/configuration changes,
  rather than the word "destroyed", determine weight effects.
- **Mechanisms:** Scene.mechanisms.index uses a maintained scene index version.
  Membership/effective-scene and mechanism configuration changes invalidate it;
  arming/status/uses and eligibility are still checked on the selected mechanism.
  `TriggerIndex` wraps a private read-only mapping of tuple buckets and frozen
  trigger records. `automatic` gets the event/target bucket directly. Sharing an
  index never copies its records or exposes writable nested state. A relevant
  change may rebuild the actual scene mechanism index lazily; ordinary hits do
  not validate every configuration. The World component reverse index keeps its
  existing separate dependency/isolation contract.
  Region buckets are keyed by destination cell, and object-operation buckets by
  the concrete event type and object ID. An event of the same category at another
  target therefore selects zero records too; matching entry order/conditions stay
  unchanged. Region preparation/storage costs the configured region-cell links.
- **Capability associations:** Scene.objects.capabilities holds the two immutable
  light/mechanism ID tuples. They derive solely from objects' authoritative
  parent/carrier/scene facts, actors' scenes and capability membership tables.
  Cold construction follows existing root/carrier/parent indexes. Normal cross-scene
  movement follows only the moving subtree (or an actor's carried subtrees), checks
  their capability member buckets and patches loaded old/new scene associations.
  No scene scan decides whether a moved bag has relevant descendants. Same-scene
  transfer does not change membership. Selected source/ancestor/configuration,
  enabled/mode/HP and carrier dependencies still invalidate light contributions
  when their position, shading or accessibility actually changes. Capability
  insertion/removal patches only its effective scene. No independently writable
  membership truth or new Session/transaction manager is introduced.
- **Indirect light costs:** lighting.candidates separately holds ground-object
  uncertainty. Its version changes with ground pose/state, object configuration,
  durability, scene acceptance lifecycle and controlled layout configuration.
  Nonground-to-nonground empty-bag transfer does not change it. This removes the
  negative spatial/configuration dependency expansion through cached light
  geometry, projection and visible_cells. Door, map, source and shading inputs
  keep their original separate checks; no geometry/rule behavior is removed.
- **Retention cost:** each immutable FieldValue computes its conservative scalar
  payload charge when produced/replaced. Owner budget accounting sums those
  charges without formatting retained values again. DependencyGroups shares the
  fields' immutable dependency tuples without flattening every leaf on each
  owner replacement; trimming still retains the actual dependencies. The existing
  entry/byte limits remain, with an assertion that the trigger payload is charged,
  rather than hiding it behind a short repr. This removes a separately reproduced
  10/100-record budget repr traversal even after index deepcopy was eliminated.

Facts, parent buckets, association patches and invalidation versions all use the
existing sparse UnitOfWork, savepoint restoration and post-COMMIT publication.
Object DELETE capture runs before removal so necessary position/contribution facts
are available; failure still rolls back the same candidate. Published owners are
immutable replacements. Missing/evicted fields stay cold; patching never stamps
an already-invalid association as valid. Controlled REPLACE conservatively
invalidates affected loaded protocols because SQLite may omit victim DELETE
callbacks; this is a maintenance compatibility path, not a normal command scan.
Unload, restart and all-context recovery discard/rebuild these derived fields
from persisted instances. The prior shared recovery generations, paused Sessions,
old-read publication fence and driver invalidation remain intact.

### Matched deterministic measurements

`tests/test_runtime_field_maintenance.py` counts real `memory.version` calls,
RAM query calls/visited rows, SQL SELECTs/fetched rows, resolver compute bodies,
association discoveries and copied trigger records independently. All are
production paths; wrappers observe work without replacing results. Baseline source
was loaded from a read-only `git archive c64f9d7 src` in the same locked environment.
The eight new performance cases fail on that baseline as expected; the original
baseline reproduction without the new assertions passed all six cases.

RAM calls mean the SessionMemory.select interface (including cold requests that
also execute SQL); visited RAM rows and fetched SQL rows are separate counters.
Copied records mean scene trigger-index records, not necessary complete-map or
event-output copies. Trigger __repr__ calls separately detect record traversal
during retention accounting.

| Scope / count | Before small / large | After small / large |
|---|---:|---:|
| One warm weight, 10 / 100 contents: dependency probes | 31 / 301 | 1 / 1 |
| Same weight: SQL SELECTs, fetched rows, RAM queries, computations | all 0 / 0 | all 0 / 0 |
| Real nonmatching StepActor, 10 / 100 mechanisms: copied trigger records | 10 / 100 | 0 / 0 |
| Same StepActor: trigger records formatted for budget accounting | 10 / 100 | 0 / 0 |
| Same-category region event outside all configured cells: selected candidates | 10 / 100 | 0 / 0 |
| Same StepActor: all dependency probes | 121 / 211 | 111 / 111 |
| Same StepActor: scene-index probes / index rebuilds | per-member dependencies / 0 | 1 / 0 in both |
| Same StepActor: SQL SELECTs / fetched rows | 26 / 5 in both | 26 / 5 in both |
| Same StepActor: RAM calls / rows | 78 / 32 in both | 78 / 32 in both |
| Four empty-bag Put/Take commands, growth 0 / 100: association discoveries | 8 / 8 | 0 / 0 |
| Same commands: RAM calls | 350 / 750 | 284 / 284 |
| Same commands: RAM rows | 202 / 202 | 180 / 180 |
| Same commands: all dependency probes | 1,902 / 6,302 | 972 / 972 |
| Same commands: scene mechanism index / light source rebuilds | 4 / 4 in both | 0 / 0 in both |
| Same commands: SQL SELECTs / fetched rows | 36 / 16 in both | 36 / 16 in both |
| Same commands: all instance computations | 28 / 28 | 16 / 16 |

Growth adds 100 unrelated objects (50 in this scene) **and 100 remote actors**.
The light, target operation and actual target contents remain fixed. Both groups
warm two Put/Take cycles. Subsequent commands use receipt versions; a separately
requested complete SessionView is outside this measured ordinary-command scope.
StepActor fixtures use one legal nonmatching trigger on each of 10/100 mechanisms,
testing both a different event type and a same-category different destination cell,
respecting the sixteen-trigger-per-mechanism limit. Weight repeats also prove
three reads perform exactly three probes and no computation; warmed public get/list
checks prevent upper-layer dependency expansion while allowing complete contents
output. The 16 remaining command computations are necessary facts/configuration,
direct contents and candidate weight work; there is no zero-work/zero-SQL write
claim. SessionMemory content JSON/model decoding and its fixed Snapshot wrapper
construction counters are zero in these hot measurements. New event JSON work is
retained and is not counted as zero by this table. Full event evidence/persistence
and required admission remain.

The historical 15/115 RAM-query discovery reproduction, 31/301 dependency counts,
10/100 copied records and earlier 2,059-test/SQL measurements keep their original
scope/time. They are not retroactively relabeled as this acceptance.

### Validation and remaining scope

Final executed tests and checks are recorded at delivery below. New regressions
cover nested quantity/mass changes, unknown weights, multiple mutation stages,
candidate relocation, savepoint/outer rollback, unload/restart; nested light and
mechanism carrier scene changes, opaque-container shading, capability membership
remove/add and fixed old indices/inputs. Existing cross-connection publication
failure tests now also assert old/current weights. Existing public capacity,
matching trigger qualification/order/uses, destruction, authorization/disclosure,
idempotency, RNG, commit failure and recovery tests remain in acceptance.
Final candidate tests also cover positional/keyword capability-query identity and
retained payload charging. The earlier full 2,067-test run preceded the last
retention-cost fix and is intermediate evidence; final acceptance is rerun against
the frozen final sources, not inferred from that run.

Cold association preparation and invalidated whole-map geometry have their actual
directory/map cost. Cross-scene maintenance traverses the moved subtree, not an
unrelated scene; association tuple replacement and changed mechanism index rebuilds
cost their actual capability members/triggers. Ground geometry invalidation is
conservative for ground-object configuration/state and scene-instance metadata changes. Full lists, complete
mechanical/evidence maps and necessary aggregate admission remain output/dependency
sized. No public partial-stack transfer, general live capability editor or standalone
scene unload API is added. These are bounded local counters, not multiplayer/SLO
evidence or a complete Python heap bound.

### Final follow-up acceptance

Final fixed sources/tests/examples pass **2,069 unique tests in 93 files**:
three disjoint sorted-file groups of **650 / 725 / 694**, each exit 0. Collection
versus JUnit union has zero missing/unexpected/duplicate/failed/errored/skipped
nodes. All **215 Python source/test/example hashes** are unchanged across this
final run. Historical migration cases are included with no environment-blocked or
skipped cases. The separately executed final mechanism/field/recovery slice passes
60 tests (exit 0); it is not added again to the full-suite total. Earlier runs,
including the superseded interrupted run, are not final acceptance evidence.

Executed with the existing locked environment and repo-local basetemps:
`uv run --locked --no-sync pytest -q` over each sorted test-file partition with
`-o faulthandler_timeout=60` and separate JUnit outputs. The eight old-source
performance cases intentionally fail the new assertions, while their final
production-path counterparts pass. Reproduction and counter definitions live in
the [production-path regressions](../../tests/test_runtime_field_maintenance.py).
SQL, RAM interface/rows, version probes, copied/formatted trigger records,
candidate selection, discovery/rebuild and compute-body counters are separate.

`uv run --locked --no-sync ruff check src tests examples` and
`uv run --locked --no-sync ruff format --check src tests examples` both exit 0
(215 files). Both repos' modified Markdown links/anchors/fences (12 files / 148
local links) and staged/unstaged `git diff --check` pass. Only
the existing two Lark deprecation warnings occur per pytest group. These are local
checks; GitHub Actions and sustained multiplayer load were not executed. Delivery
uses the two original branches and separate commits/pushes; no merge or force push
is authorized.

## 2026-10-22 follow-up: managed cache inputs

Start: SM `refactor/session-memory-runtime@e776fab1ede6ec8fc8ca5a46cb34410a85031de1`;
Doc `a593a9d91b1268bba26805b48b3d2cf4b5f13942`. Fetch/ls-remote confirmed both
reviewed heads. The Doc predecessor is not an ancestor of remote main `5619e7a`;
new branch `docs/runtime-versioned-cache-keys` therefore starts from its latest
head in a new worktree. SM main remains `3aacecc`; existing worktrees, untracked
text and primary Doc files are preserved. RE, Pages, merges and main pushes are
excluded. No Schema, lockfile, rule binding or public DTO change.

### Implemented consumers and validity

| Owner / actual field | Entry and identity | Source / maintenance |
|---|---|---|
| Scene.spatial.record, map, mechanical_map | GridMaps.scene; GridReader.prepared_scene/current_grid; Store + Session + Scene | Existing SessionMemory projection versions for mechanical_scenes state_json/spatial_enabled; actual row changes invalidate, actor/object motion does not |
| Scene.spatial plus local blocker overlay | GridService.local_topology/base_topology/exploration_topology | PreparedGrid shares ReadOnlyTopology; Schema40 cell and Actor position indices, relevant door states and endpoint conditions supply dynamic blockers |
| Scene.lighting.sources/geometry/cells/projection/topology | query_illumination/current_cell/current_project/current_topology | Existing actual source, mode, ancestor transmission, map, ground geometry and door dependencies; no full-map content key for managed input |
| CharacterRuntime.inventory | CharacterFacts.inventory_model, public read_carried_items; Session + actor | Maintained inventory stamp: affected owner changes, aggregate fallback, actor membership and controlled layout version; model schema remains InventoryState |
| CharacterFacts component/selection cache | rule_components, _inventory_selection; Session + explicit owner and item-ID sets | Existing component row/relationship dependency capture; indexed ComponentInputs selects actual closure; no inventory structure-to-JSON-to-model-cache round trip |
| Scene.spatial.sensory | GridMaps.mechanical_grid, EventCapture._perception_geometry; Session + Scene + MechanicalGrid type | Complete MechanicalGrid validation of original prepared data on cache miss/map change; GridScene defaults do not fill missing sensory fields |
| Event stage result | EventCapture stage evidence, signals and persisted evidence JSON | Detached full stage output from that candidate/read range; later map/model changes cannot modify old evidence |

`local_topology` shares the base blocked set through an immutable membership
overlay. Creating it visits only supplied dynamic cells; it never thaws, unions,
sorts or re-encodes the full base map. RE algorithms still decide walls, corners,
diagonals, bounds and cover. Existing exclusion, shared-door and multi-cell
semantics remain. Full exploration/path consumers still read their required
dynamic scope. Public Light retains current authorization, LOS and source
disclosure; its visibility topology comes directly from the same prepared map.
Full Light projection shares base topology and produces a detached output map.
Event capture overlays projected lighting on the already validated stage topology.
Current combat-position consumers also pass PreparedGrid; initial/fixed foundation
maps keep their supplied content identity.

Inventory mutation capture follows old/new container ancestry to actual carriers.
Objects/quantity/unit metadata, actor/core/mechanical facts, object configuration,
durability, bars and locks invalidate affected owners. `sync_inventory_change`
already knows the old/new owners: its synchronous `inventory_projection_write`
capture hint advances only their stamps for the validated inventory patch. This
hint is not a writable fact, another directory or transaction. Other aggregate
SQL/import/schema/resource/casting/effect changes use a conservative shared
fallback stamp; mechanical actor membership and Session layout remain covered.
No global inventory enumeration is added to ordinary mutation maintenance.
Trimming retains composite stamp dependencies; controlled REPLACE compatibility
also invalidates these fields. Unknown/malformed source changes cannot hide behind
a previously valid model. Inventory's JSON array is normalized to a tuple only;
strict scalar checking and duplicate-ID validation remain enabled.

All fields use the existing immutable owner replacements, SessionMemory budget,
UnitOfWork savepoints and durable-COMMIT/RAM-publication boundary. Versions come
from the caller's scope, never a fresh DB read or payload digest. Rollback restores
both fields and stamps, including repeated numeric candidate versions. Bounded
legacy content pools still support explicit unversioned fixed inputs; they cannot
select a current map on scene_id alone. Eviction, unload and recovery reconstruct
from persisted instances. Per-connection recovery generation and old-reader
publication fences are retained.

### Comparable deterministic evidence

`tests/test_versioned_runtime_inputs.py` measures actual production readers and a
real StepActor command. Baseline source is an archived, unmodified `e776fab1/src`,
selected explicitly by pytest pythonpath; both runs use the same locked environment
and test fixtures. The fourteen performance cases intentionally fail their new
assertions on that old source. Fixture correction/early implementation failures
are intermediate evidence only.

Growth 10 / 100 means that many distant static blocked cells, unrelated objects,
remote actors and another actor's inventory entries; the local target stays fixed.
Local/Light slices perform two repeated queries and one new local target; inventory
and sensory slices perform two repeated queries. The larger target-inventory slice
separately returns 11 / 101 entries (10 / 100 portable units plus one legacy entry).

| Slice / counter (10 / 100) | e776fab1 before | This follow-up |
|---|---|---|
| Local movement input: cache-key encoded blocked records | 10 / 100 | 0 / 0 |
| Same: full-map copies; sorted records | 1 / 1; 22 / 112 | 0 / 0; 12 / 12 local cells |
| Public single-cell Light: encoded blocked records; full Scene copies | 30 / 300; 3 / 3 | 0 / 0; 0 / 0 |
| Sensory map: encoded blocked records; full Scene copies | 20 / 200; 2 / 2 | 0 / 0; 0 / 0 |
| Target inventory output: encoded inventory records | 11 / 101 | 0 / 0 |
| Same: dependency version checks | 41 / 311 | 4 / 4 |
| After another actor's validated transfer: target RAM calls/rows | 18 / 108 | 2 / 2 |
| Same: target inventory records re-encoded; all version checks | 4 / 4; 33 / 33 | 0 / 0; 4 / 4 |
| Real StepActor: internal cache-key blocked records | 40 / 400 | 0 / 0 |
| Same: topology constructions; necessary full evidence encodings | 1 / 1; 1 / 1 | 0 / 0; 1 / 1 |
| Same: blocked records in persisted stage evidence | 10 / 100 | 10 / 100 (retained) |

Cache-lookup encoding call counts (equal at both sizes) are respectively:
local 1 -> 0, public Light 3 -> 0, small inventory 2 -> 0, sensory 2 -> 0,
full target inventory 1 -> 0, target after foreign transfer 5 -> 0, and movement
4 -> 0. Movement's one additional evidence encoding remains 1 -> 1; it is
separately classified, not removed or counted as a cache key.

SQL / RAM / checks are separate: local slice SELECTs/SQL rows = 3/0 and RAM
calls/rows = 10/2, checks 136, unchanged at both sizes; these include cold local
occupancy for the new target. Light uses 0 SQL, RAM 6/6 -> 3/3, checks 41 -> 44.
Sensory uses 0 SQL, RAM 2/2 -> 0/0, checks 0 -> 4. Small inventory uses 0 SQL,
RAM 4/4 unchanged, checks 30 -> 8. Full target inventory uses 0 SQL, RAM 2/2
unchanged. Movement SELECTs/rows = 6/2 unchanged, RAM 49/29 -> 46/26, checks
133 -> 137. Each figure is equal in the two growth groups. All warm-reader
slices have zero topology constructions and zero JSON-model parses before/after;
instrumented Python InventoryState/MechanicalGrid validation is also zero on
delivery warm reads. No-parse alone was insufficient: old encoding counts above
remain nonzero. Legitimate output copies/serialization and cold preparations are
not relabeled as cache lookup.

Dependency-check figures count actual SessionMemory.version calls in both runs,
not SQL or scalar CPU comparisons. The inventory token reads a fixed four
version components plus the existing memory generation; its comparison does not
enumerate inventory entries or a variable dependency collection.

Prior field-maintenance cases still show weight 1 check, no mechanism index copies
or rediscovery, and fixed unrelated growth. New record validity raises mechanism
command checks 111 -> 114 (RAM 78/32 -> 76/30); four bag commands checks
972 -> 1000 (RAM 284/180 -> 280/176), identically at both sizes. Their thresholds
track this measured constant overhead; the original dated figures above stay
unchanged. No work was shifted into an added full-scene scan.

### Validation and retained boundaries

Final targeted suite: 47 passed, exit 0, including all 22 new cases plus prior
owner/field-maintenance tests. New cases cover distinct item/owner selection keys,
unprojected invalid source rejection, multi-stage map/inventory changes, savepoint
and whole rollback, repeated candidate stamps, old fixed maps, full sensory
requirements, unload/restart, and map commit failure/unknown commit/publication
failure with recovery on another connection. Existing four publication-failure
modes now also assert old/current inventory model isolation. Final complete-regression
and documentation validation results are recorded below.

Remaining serialization is deliberate: persisted facts/deltas, receipts, evidence,
protocol snapshots, idempotency fingerprints, source bindings, existing JSON
decoders and unversioned external/fixed maps. Casting/effect selections retain the
pinned strict JSON-model compatibility route when rule_components actually rebuilds;
its effective outer cache still reuses valid results. Complete inventory/evidence/
projection outputs retain proportional output work. Cold aggregate ComponentInputs
index preparation and SQLite aggregate inventory UPDATE retain their existing
aggregate storage cost; the measured target read after an unrelated domain patch
does not reconstruct them. Unattributed aggregate SQL remains conservatively
invalidating. Actual light-source/door and scene-passage dependency checks remain;
this is not a constant-cost claim across growing relevant geometry or sources.

NPC all-actor reads and transaction-wide version-dictionary copying are unchanged,
separate tasks. The independently reported **caught SQL statement error followed
by continued commit can diverge RAM/SQLite** issue remains **unresolved and outside
this acceptance**. Cache/commit fault tests here do not establish its repair.
No general map-edit command, live stack editing, multiplayer load guarantee or
broader recovery capability is introduced.

### Final managed-input acceptance

Full locked-environment regression: **2,091 unique tests**, disjoint sorted-file
partitions **665 / 757 / 669**, all exit 0. Collection/JUnit union has no missing,
unexpected, duplicate, failed, errored or skipped cases. All 216 Python source,
test and example hashes are unchanged during the final run. Historical migration
tests are included; no environment-blocked case is reported as a pass. The final
47-test targeted run also exits 0 and is not added again to the full-suite count.

Actual commands use `uv run --locked --no-sync pytest -q`, repository-local unique
basetemps, JUnit output and `-o faulthandler_timeout=60`. The focused files are
`test_versioned_runtime_inputs.py`, `test_runtime_instance_ownership.py` and
`test_runtime_field_maintenance.py`; the full suite assigns every sorted test file
to exactly one of three modulo partitions. Evidence: `.test-tmp/versioned-final-target.*`,
`versioned-delivery-{0,1,2}.*`, `versioned-delivery-collection.txt`,
`versioned-delivery-audit.json` and `versioned-counts.json`. Old-source runs are
`versioned-before-comparable.log` and `versioned-before-commands.log`.

`uv run --locked --no-sync ruff check src tests examples` and
`uv run --locked --no-sync ruff format --check src tests examples` pass (216 files).
Both repositories' changed Markdown links/anchors/fences pass: 13 files, 170 local
links; staged/unstaged whitespace checks pass. Only two existing Lark deprecation
warnings occur per pytest group. No dependency synchronization/upgrade, remote CI
or sustained multiplayer load was performed. The explicit independent SQL-error,
NPC, aggregate storage and genuine dependency limits above remain open.
