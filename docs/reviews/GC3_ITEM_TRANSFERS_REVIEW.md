# GC-3 implementation and independent review

Repository: `D:/Projects/agentic-trpg/trpg-state-machine`.
Remote: `https://github.com/agentic-trpg/trpg-state-machine.git`.
Branch: `feat/sm-gc3-item-transfers`.
Clean baseline: `0e380cc27e6c20516fe0f3f4c81b895310124f62`.
Fetched origin/main equals the baseline and includes GC2 `5ff7a807`, RF4
`4e4e678e` and RF4-REV-01 `7da11370`. No applicable AGENTS.md was present.

Scope: only SM implementation, tests, an original executable Package/example,
README and this repository's docs. No RE/orchestration repository writes, new
framework, dependency upgrade, main merge or next batch.

## Facts, implementation and compatibility

The pre-implementation responsibility table is in
[GC3 design](../GC3_ITEM_TRANSFERS.md). WorldFacts owns original portable object
identity, unit metadata and exclusive ground/carrier location. CharacterFacts
owns existing RF4 inventory and generates/checks the portable subset and affected
carried slug projection. WorldHandler coordinates one reusable transfer executor
inside the existing command transaction. No second Handler independently writes
a competing backpack. Equipment and current resources/casting/effects keep their
existing ownership.

Schema 24 preserves all original object columns and adds explicit portable
metadata/carrier fields, physical-location CHECK constraints and same-Session
foreign keys. Old migration files are untouched. Old rows receive no portable
capability or invented instance ID. Package /4 requires complete /17 noncombat
facts and reviewed public portable configuration; /1-/3 raw digests and prior
admission remain compatible. Initial configuration runs only during fresh
creation. Current Snapshots and combat initialization read persisted facts.

The two local TypedCommand operations and NPC proposals are closed and typed.
They do not admit owner claims, accessibility grants, quantities, charge changes,
arbitrary properties, scripts or patches. Actor identity/control, local visible
scope, walls/doors, reach, busy state and current physical ownership are checked.
Pickup does not equip. Ambiguous held/equipped/bound instances cannot be dropped.
Carried objects have no stored world coordinates; source position derives from
the real carrier. Small ground objects introduce no cover or movement obstacle.

Pickup evidence freezes before removal; drop evidence freezes after placement.
Physical action events, complete fact changes, queue/Outbox, version and receipt
share the outer transaction; ordinary transfer leaves authoritative RNG and time
unchanged. Historical Evidence /4-/5, old receipts and closed-combat records are
preserved. No observations are fabricated from refusal or inferred from success.

## Executed development gates

Baseline Fast, before implementation, passed **162 tests in 38.46 s**:

```powershell
.venv/Scripts/python.exe -m pytest -q --tb=short --basetemp=.test-tmp/gc3-baseline-host tests/test_package.py tests/test_commands.py tests/test_spatial_world.py tests/test_navigation_activities.py tests/test_history_free_rules_adapter.py tests/test_rf4_position_evidence.py
```

Two environment attempts preceded it: uv could not persist its interpreter cache;
direct pytest without a workspace basetemp produced 142 temporary-directory
setup errors / 20 passes. The restricted workspace-temp run stalled at real
async tests and was stopped after the equivalent host-permission run passed.
Host execution supplies the Windows local socketpair required by the installed
RE; tests do not call an external service or LLM. The passing baseline uses the
existing fixed .venv, not a new resolution or install.

Development GC3 runs exposed and corrected migration-script registration,
strict Pydantic JSON/tuple decoding and fixture assumptions (occupied navigation
cells, required NPC navigation controller, public DTO field names and lighting
enum). The final example contains exactly its two mechanically initialized
Actors, allowing actual NonCombatSnapshot reads after Scene traversal. Success
paths use public APIs; direct SQL is confined to explicit negative fault injection
and verification. No existing failed assertion was skipped or threshold lowered.

The first broad focused run passed **316 / 317 in 86.76 s**. The remaining test
was the archived RF4 missing-evidence comparison: Schema24 legitimately adds two
object columns. It now uses the existing exact original-column/row comparator
and asserts that both new capability columns remain null. Original Evidence byte
equality, unresolved historical judgment and queue assertions remain intact.

The subsequent GC3 plus RF4 position suite passed **82 tests in 43.18 s**.
The expanded GC3 suite covers 59 cases, including both actual NPC Task/handoff
directions, current CombatSnapshot and supported real RE availability, plus a
real accepted generic check that captures the submitted request. Requests contain
the acquired original instance IDs and carried slugs; a later call after drop
excludes the ground instance and never restores the Package inventory.

SQL trigger failures at objects, mechanical inventory, character projection,
Evidence, Outbox and receipt writes compare every table before/after and retry
the original command. Genuine archived Schema23 creation tests inject failures
through the new migration, compare all original facts and preserve audit rows.
Independent SQLite connections race PC/NPC pickup; at most one commits and the
loser has no instance residue. Receipt reconciliation covers committed lost
response, exact retry, changed-content conflict and stale world version.

The standalone public example was executed successfully with its final Package:
PC retained `inventory.lantern_blade` and `object.rope`; NPC acquired original
`object.dagger_a`; world version 20. Its authored navigate/transfer decisions do
not claim autonomous LLM planning. A separate Python process resumes a carried
save through public reads and leaves every row unchanged.

## Independent review

A separate Codex reviewer inspected the complete production diff, new files,
tests and example. It did not modify or submit files. Review covered fact owner
direction, dual location, same-type identity, stock/equipment conservation,
query authorization, activity qualification, template reapplication, migrations,
RF4 current Snapshots, actual event coordinates, transaction faults and reliable
NPC outputs. It reported no remaining production finding after corrections.

Two concrete findings were fixed before final validation:

1. Drop lacked a safe decision when `other_hand_occupied=True` could not identify
   the held instance. Such Drop now refuses explicitly; its regression compares
   every persistent fact, and the reviewer independently confirmed refusal.
2. Legacy PlaceObject initially checked portable capability before establishing
   local visible target authorization. This distinguished a hidden remote
   portable ID from an absent ID. Authorization now comes first. The reviewer
   reproduced the original issue with real SQLite/NPCContext, then verified
   both references return `invalid_target`; remote/wall query regressions cover it.

The reviewer also confirmed the RF4 migration assertion adjustment preserves
strict equality of every original table/column/row. A candidate concern about
Scene.objects tuple membership was withdrawn after verifying that SceneInstance
uses dictionary keys; SpatialView has the separate tuple representation.

## Final gate

After all production/test/example edits, the complete suite passed
**1,082 tests in 574.58 s (9:34)**, including the 59 new GC3 cases and all 1,023
existing cases. There were no skips or failures; the two existing Lark
`sre_parse` / `sre_constants` deprecation warnings remain. This gate includes
the final two-Actor Package and post-traversal current Snapshot assertions.

```powershell
.venv/Scripts/python.exe -m pytest -q --tb=short --basetemp=.test-tmp/gc3-full tests
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
git diff --check
git diff --exit-code 0e380cc27e6c20516fe0f3f4c81b895310124f62 -- pyproject.toml uv.lock
```

Ruff passed; **174 files** passed formatting. Whitespace and unchanged dependency
checks passed. Both new Markdown documents' local links and fences were checked.
Final example execution passed. Logs are retained locally under the ignored
`.test-tmp/gc3-logs/` directory, including `.test-gc3-baseline-host.log`,
`.test-gc3-focused.log`, `.test-gc3-final-special.log` and `.test-gc3-full.log`.
The earlier environment/development logs are retained there as well.

Engine and SRD remain **0.6.0** at
`675552406e67470bf3456fe90e88c4f1595bf324`. `git diff --exit-code` against the
baseline for `pyproject.toml uv.lock` passed, and the existing installed
`direct_url.json` full-SHA assertion runs in the suite. Historical migration
files have no diff. No remote CI result is claimed; the repository has no CI
workflow.

Remaining scope limits: portable quantity one, ordinary public noncombat scope,
no contested-property adjudication, containers, locks, equipment changes,
encumbrance, consumption or stack operations. Legacy inventory without approved
world instance identity is retained and cannot use GC3 Drop. These are explicit
capability refusals, not implemented gameplay or an ABI certification.
