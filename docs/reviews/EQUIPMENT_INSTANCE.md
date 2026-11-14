# Equipment instance bindings

## Current status

Independent local review passed in round 2. Both candidates are held locally for the user's merge decision.

## 1. Scope and baselines

The user authorized paired SM and RE implementation for worn armor, an equipped shield and a held offhand item, including exact-instance transfer guards and acceptance with duplicate equipment. Opportunity attacks, runtime equipment commands and new dual-wield combat behavior are outside this batch. Wait for the user's confirmation before merging. Code graph indexes remain on their verified main baselines during implementation.

| Repository | Baseline and merge target at start | Work branch |
| --- | --- | --- |
| SM | `main` at `d3b048547380afedb4028a4d3ad8c6c5295fe5fc` | `codex/equipment-instance` |
| RE | `main` at `0b633e264d79019b13e06421e457e02c761e0746` | `codex/equipment-instance` |

Health checks confirmed both exact remote main commits. Worktrees were created independently below the SM workspace. Two implementation agents own the repository sides. The parent independently reviews their handoff.

SM CharacterFacts owns the equipment bindings. WorldFacts owns each item's identity and placement. RE validates fixed input and decides rule outcomes while SM keeps authority over authorization, factual changes and publication. Current snapshot and save fixtures must exercise the new facts, with older development save migration outside the acceptance scope.

## 2. Planned validation

- Run SM public drop and container transfers with fixed target items, identically typed spares, an occupied offhand, and an unrelated item. Verify equipment, casting and other existing transfer guards, affected container subtrees, replay, rollback and reopening. Compare deterministic local work counters on fixed-scope operations with growing unrelated inventory.
- Exercise RE snapshot identity, ownership and accessibility, duplicate bindings, hand/grip consistency, AC, stealth and attack parity in the supported rules profile. Check protocol contracts and real SM → RE → SM requests with the exact dependency revision.
- Use the repositories' Ruff, focused, and broader validation commands. Review the complete paired diffs and record exact commits and completed validation before requesting a merge decision.

## 3. RE development handoff

On 2026-11-14, the RE implementation agent froze the candidate diff from
`0b633e2`. Snapshot /19 checks that equipment references distinct, accessible,
single-unit owned items, resolving categories against the bound rules data.
Evaluation /26 and availability /15 share these facts. The supported equipped
attack profile uses selected bound equipment for AC and grip checks. Identical
spares do not acquire the role of an equipped item. The evaluator is /39.

The developer's focused suite passed 184 cases in 42.26 seconds, covering the
new equipment bindings, attack, availability and openable integration.
An initial default-sandbox launcher stalled during startup. A rerun with the
same locked RE interpreter and process access executed successfully. Full
integration and independent validation are tracked separately.

## 4. SM development handoff and paired lock

SM persists armor, shield and offhand identities in the actor's existing
mechanical record. Snapshot assembly reads the bound World items and projects
the derived armor category and hand/slot flags. Drop and container transfers
share the binding check. Schema 43 admits the new protocol while retaining
the already-supported earlier protocol shapes.

The developer reported 10 passing new cases and 30 passing schema, migration
and adapter cases. The executable /19 cross-phase example completed. A fixed
bound Put preview with 0 and 80 unrelated objects/actors read the same counts
of SQL SELECT statements, rows fetched and SessionMemory JSON calls (0, 0, 1).
The same comparison on a successful spare Put gave (12, 4, 11) at both sizes.
These counts measure the public command paths, not whole combat evaluation.

The pinned RE implementation is `9c31a60b674f4c68b3beb9e7c8f441df7e8bb10c`.
The parent ran `uv lock` then `uv sync --locked` using a process-local Git
URL mapping to the local object database. Each of the two RE distributions
installed from that SHA. The manifest and lock retain the GitHub URLs.
Remote package resolution remains a separate publishing check.

## 5. Paired independent review, round 1

- Candidate production and test commits: SM
  `ba7a610d8b8ae336395c561fd6a4a4221d1f7a4d`, RE
  `9c31a60b674f4c68b3beb9e7c8f441df7e8bb10c`.
- EQ-SM-01 (P2, introduced in this batch): unifying the Drop guard removed
  still-relevant actor/effect admission from the separate Pickup path. The
  late ownership checks do not provide equivalent public preview or effects
  protection. Restore the Pickup guard in the common public path while
  retaining the one shared Drop/container check and validate both paths.
- Independent RE focused regression passed 207 tests, including new/previous
  wire protocol and attack profile checks. Independent SM tests passed 140
  cases across equipment, existing transfers, container admission, adapter
  and pre-release save policy. The broader gates continue. No merge
  requested or performed.

## 6. Round 1 corrections

- EQ-SM-01: SM `b7d527855685e2601a6a16c412f04954a033a533` restores the
  missing Pickup guards and adds public preview/submit cases with absent
  actor state and an active effect. The developer's 12 selected equipment
  and Pickup cases passed. Earlier transfer/container development regression
  passed 117 tests.
- EQ-SM-02: the broad run found two direct composition tests using the old
  `CharacterFacts` constructor. Their test composition already provides a
  frozen ruleset to adjacent services. Pass the same loader to CharacterFacts
  and verify both public and direct composition paths. Development commit
  `0ce9e6330a2841bbab6c50eff6204ed7cea04ba0` updates just that composition and
  passed 19 focused tests. `6c01af42d135294e8c1e929efcd63940e3d28974` synchronizes
  the constructor docstring with its shared frozen-loader dependency.
- EQ-SM-03: concrete equipment admission preceded the World materialization
  of approved inventory entries for ordinary /2 foundations. The fix resolves
  equipment after item creation within the same transaction for both initial
  paths. Six acceptance cases cover armor, shield and offhand across
  noncombat and combat-only initialization, including an NPC's bound item.
  The original /2 example shape now works with /19. Invalid concrete equipment
  still causes atomic initialization refusal.
- RE broad pytest initially reported 9,057 passed and three failures. Two
  version-enum tests had been loaded before updating the expected versions.
  The third is the shipped example's stale evaluator binding /38. Update that
  example to /39 and verify it through the real subprocess entrypoint.

## 7. Fixed candidates and independent verification

- SM production and test head `323e416cec023e48e40221b0fcc9029d0c96ad93`.
  The same-transaction initialization correction is `5806949`. The test-only
  follow-up uses a file-based example import so the configured `uv run --locked
  pytest` entry collects it without relying on the launcher search path.
  Independent locked runs passed 140 cases on the first candidate, 53 cases
  after the Pickup/composition fixes and 53 on the final candidate, including
  the six new ingress cases. Earlier and later selections overlap.
- RE head `76f76a91be0b41303733a09ed70cb32cd302b023` includes the example-only
  evaluator binding fix. The SM lock still references the exact production
  commit `9c31a60`. The independent focused run passed 207 cases, and all
  three cases flagged by the engine broad run passed in the independent final
  rerun. Bridge 142, demo 54, strict docs, engine Ruff and mypy passed in
  developer validation. The complete RE gate was not rerun after the two
  test expectations and the example binding were updated.
- The new wire shape still carries the existing inventory list. RE can now
  look up individual equipped slots by ID, while snapshot validation and the
  reviewed class profile continue to examine their inventory scope. This batch
  changes equipment identity, not general snapshot request locality.
- The SM broad run started before the fixes, with 2,067 passed and nine
  failures. Eight shared the direct composition loader issue, and one loaded
  the prior dependency SHA assertion. All nine passed on the exact final
  candidate in an independent locked run. Neither broad suite was rerun
  from scratch on the final pair. The independent code review and focused
  acceptance are complete. Neither feature branch was pushed or merged.
