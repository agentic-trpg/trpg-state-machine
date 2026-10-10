# SM-6 — authoritative combat foundation

Upstream: `agentic-trpg/trpg-rules-engine` commit
`a7225ebaa4914f6db4ae72f3f6454c355ea7baac`, candidate branch
`refactor/re-eval-04-npc-and-availability`. At implementation-time verification,
upstream main was `64dd920507de29602908b4bea1635eedc4385370`; the candidate was
not merged. Both Engine and SRD packages are pinned by full SHA in `pyproject.toml`
and `uv.lock`. The local schemas are `engine-snapshot/1`, `engine-evaluation/1`,
`dnd5e-evaluation/4`; this adapter does not freeze a new public cross-module ABI.

## State mapping and restrictions

Every Engine schema field is required, including empty collections and nulls.
The Package's `rule_bindings.combat_foundation` contains an explicitly authored
complete initial snapshot and separate `rng_state`. Package validation invokes
the actual upstream strict Pydantic schema, its snapshot capability guard, and
SM reference validation. Initialization additionally verifies the effective data
binding and upstream monster-template capability guard outside the write lock.

| Engine component | Required origin / SQLite owner | Unsupported closure |
| --- | --- | --- |
| Snapshot identity/discriminator/version | `sessions`, constant pinned schema and `combat` discriminator | Other schema versions; missing dependencies |
| `character_states[*].entity_id` | Existing Session PC / Package NPC; selected template ID maps once to the generated PC ID | New actors supplied by the caller |
| `hp_current`, `hp_max`, `carried_item_slugs` | Existing `characters.current_hp`, `max_hp`, `inventory_json`; initialization cannot reset PC values | Implicit conversion of legacy resource/effect data |
| All remaining CharacterState fields | Explicit Package values, then `mechanical_actors.state_json` | Features, conditions, concentration, extended attack grants, class/species mechanics, complex budgets rejected by upstream capability guard |
| All CombatState fields except `event_log` | Explicit Package values, then `combats.state_json` | Reactions, active effects, areas, objects, summons, transforms, spell and special-action sidecars |
| `CombatState.event_log` | Ordered committed `events` entries of type `combat.engine_event`; initial history must be empty | Independent mutable Engine history / registry |
| Scene/grid, including lighting and geometry | Explicit Package `SceneState`, then `mechanical_scenes.state_json`; existing Scene must match | Non-bright or complex geometry not admitted by the candidate |
| `effect_states` | Explicit validated empty initial field; empty follows the restricted capability invariant | Nonempty effect state |
| RNG stream/version/state | Existing separate `rng_states` table; Engine `stdlib-mt19937/1` encoding | Pickle, shared RNG, RNG in snapshots, fabricated draw counts |

The JSON components contain the complete typed state, not defaults recovered from
an Engine handle. HP/equipment and mechanical history are excluded from those
components to avoid duplicate authority. `position` in the legacy RNG table stays
zero for MT19937; its internal index is in the versioned Engine encoding, and no
claim about a count of random draws is made.

## Interfaces

`TypedCommand(command_type="combat.initialize", payload=InitializeCombat(), ...)`
selects only the encounter embedded in the already approved, digest-pinned Package.
It accepts no replacement snapshot or arbitrary mechanical updates. The selected
PC's authenticated principal/player controls this command. Existing command
version gates, receipt fingerprints, event expansion and short SQLite transactions
apply. Initialization is one-time even under a new command ID.

`read_combat_snapshot(...)` and `read_rng_context(...)` are trusted internal audit
reads with owner/package/rules binding verification. They return detached actual
upstream types. They are not player/NPC views or resume authorization.

This restricted authored initialization is explicitly permitted by SM-6. It does
not implement the proposed `combat.start`/`combat.create` protocol, roll initiative,
or claim general Encounter setup. It imports no Legacy registry API.

## Data, migration, and recovery

Schema 7 adds three component tables and rebuilds the existing RNG table with one
additional closed encoding alternative. Existing RNG rows and other tables remain
unchanged. The established migration write lock, version recheck, transactional DDL
and foreign-key audit apply. Tests create actual Schema 1–6 databases with archived
old runtime code, then verify row preservation and SQLite integrity after upgrade.

The original Lantern fixture contains fully explicit initial mechanical values.
Its static starting baseline was authored with the candidate Engine's initializer,
then reviewed and fixed as JSON; production and tests never initialize via a Legacy
handle. The effective test corpus contains an original simple weapon and the real
bundled SRD Bandit. `ruleset_binding()` hashes this exact effective corpus. These are
real D&D evaluations/data bindings, distinct from `synthetic.atomic`, while the
original encounter is a test fixture and not a complete official adventure.

ADR-001 and State Machine Architecture SM-02 remain binding: stored state can be
audited after restart; an active combat cannot use ordinary save/resume to claim
safe continuation. No committed fact is rolled back. Exact active-combat recovery,
combat-start fields and no-draw RNG version policy remain OPEN/out of this batch.

## Verification

`tests/test_combat_foundation.py` covers complete schema round-trip, independent
RNG, Session isolation, one-time initialization, real old-code migrations, missing
mechanical dependencies, data/configuration mismatch, transaction fault/retry,
restart audit consistency, active-combat save/resume refusal and public YAML loading.

Run the locked full suite and Ruff commands documented in README. Windows' sandbox
blocks the local socketpair used to create an asyncio event loop; real Engine
execution tests require that OS facility, without external service/network access.

SM-6 validation: **208 passed** (191 existing + 17 new), `uv sync --locked --group dev`,
`uv run --locked python -m pytest -q --basetemp=.test-tmp/sm6-all`, Ruff check/format
and `git diff --check` passed. The two deprecation warnings originate in d20's
transitive `lark-parser` dependency.
