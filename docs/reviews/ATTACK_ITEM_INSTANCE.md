# Ordinary attack item identity

## Scope and bases

- SM started at `009fb32ccc38f284748b01f4e3a057b134c74342`. The feature
  checkpoint `256613b` merged the approved cleanup main
  `1692ad322131cc5ea054acb36fb6084dd2db257a` in `3bca3fb`.
- RE starts at `8013916fc2bd7a0958810e36df73afa70b20ffa4`.
- The new attack protocol proves the held item identity. It preserves the
  current combat snapshot scope. Shield, armor, off-hand and reaction
  identity changes are outside this batch.

## Development checkpoint

The SM facts writer records the held instance. Snapshot assembly derives its
weapon type from the owned item and the transfer guards compare instance IDs.
RE validates the fixed snapshot's item identity and uses shared preflight for
availability and execution. The new snapshot /18, evaluation /25 and
availability /14 are selected together. Schema 42 extends the persisted
snapshot version checks. The evaluator binding is /38.

The first linked verification found missing SQLite version admission. Schema 42
now extends those constraints while preserving /17 rows. After merging cleanup
main, initial actor comparison was normalized for an absent held binding on
the earlier protocol. The synchronized SM focused run passed 74 tests in 78.37
seconds. It covered two matching rapiers, moving the spare, restart, rejected
selection with unchanged RNG, successful real attack and replay, and injected
held-binding change between evaluation and commit.

The earlier RE targeted run passed 234 tests in 16.63 seconds. RE integration
type checking identified a legacy capture boundary that must preserve the new
explicit binding. The adapter now reattaches it from the supplied snapshot,
and integration type checking subsequently passed.

## Independent review and dependency lock

The first review found extra null fields in the earlier typed request,
snapshot and choice. The fix omits the new fields when absent and excludes
the original binding from the working combatant conversion. The independent
rerun passed 212 SM tests and 59 RE tests using the candidate sources. The
reviewer also migrated a real Schema 41 save with an engine-snapshot/17
session to Schema 42, checked every original actor JSON row and mechanical
field, and verified SQLite foreign keys.

The reviewed RE candidate is `fdff8d0396565e52653c9308bb7e93ad09b752a3`.
Both SM RE dependencies use this SHA. `uv lock` and a fresh `uv run --locked`
installation resolved it from the local Git repository via a command-local
URL rewrite. The manifest and lock retain the canonical GitHub URLs. Remote
resolution stays pending until the RE commit is published.

The committed attack facts and fixed request prove the held instance. The
existing combat event payloads still expose attack and roll data without an
item instance ID. The complete combat snapshot and its existing dependency
scope are retained.

## Locked validation and test-only follow-up

- The locked SM install passed 74 focused tests and the real cross-phase
  example through combat, save and a fresh-process resume. Both installed RE
  distributions identify the full `fdff8d0` SHA. The RE event-stage demo
  evaluated and verified its response successfully.
- An initial Windows RE integration run decoded UTF-8 capability text as GBK.
  With UTF-8 mode and subprocess access, the engine run completed with 9,048
  passed and three outdated assertions failed. The follow-up keeps required
  fields and unknown-field rejection covered and asserts both supported
  protocol versions in validation and serialization schemas. Commit
  `0b633e264d79019b13e06421e457e02c761e0746` changes only those two RE test
  files. Independent retesting passed 137 tests and the exact final two mode
  variants. Bridge (142 tests), demo (54 tests), type checks and strict docs
  build also passed.
- The first SM broad run flagged fresh-process NPC receipt recovery. The
  case passed in isolation under the locked environment and passed in the
  later broad run, which completed with 2,065 tests passed and one failure.
  SM updated that test's old dependency SHA in
  `cd875f3e7a61779ec30b582d3db2926faebf3505`; the reviewer passed all
  23 tests in the affected file with the locked install. The follow-up
  changed no production code in either repository.
