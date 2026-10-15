# World W1–W8 review and delivery evidence

Reviewed in the isolated `feat/world-w1-w8` checkout against
`1fa7cea5dab7bb95f3f6fa975f8e681c691937a6`. Original GC5 work is excluded.
Architecture reference is the user-specified fixed top-level commit; RE pin and
dependencies are unchanged. This review covers bounded local noncombat World
capabilities and does not certify production Host/authentication or full D&D rules.

## Authority and shared commit

Session identity/pins/version/RNG/receipt remain in existing infrastructure.
WorldFacts owns object state, locks, parent placement, HP, emitter and mechanism
facts. CharacterFacts owns actual actor positions/HP/mechanics and projects the
portable inventory subset from World relations. Activity owns navigation plans and
cursors. Effects and CombatRecords responsibilities are not duplicated.

All new player commands use the existing typed registry, closed payload unions,
serialization, TrustedContext, authorization and world-version/fingerprint receipt.
Internal mechanism actions use the same transaction and reviewed capability methods;
no nested command submission or separate commit occurs. Stage evidence and trigger
adoption share the command savepoint and outer transaction. No persistent candidate
cache can survive rollback. Durability application remains private and synthetic
tests do not prove a real object attack/repair path.

## Confirmed findings fixed

| Finding | Fix and evidence |
|---|---|
| Legacy state setters could bypass new jam/bar/destroyed conditions | WorldFacts.switch/validate_door_switch checks the same inhibitors; legacy and PackageEffects paths retain them |
| A multi-lock object could be treated as one legacy bool operation | Stable lock IDs, independent rows and credentials; old single-lock commands reject ambiguity; actual same-slug keys do not substitute |
| Parent movement/closure could leave stale owner or accessible projections | Complete portable subset rebuilt from unique parent facts, in the shared transaction; nonportable entries/balances and carried order preserved |
| Closed container IDs/weights/source IDs could leak through new queries | Object contents/weight and inaccessible contained carried entries filtered; hidden contributor IDs filtered; trusted snapshots keep inaccessible mechanical entries |
| W7 light evidence extension was rejected by existing strict Perception parser | Producer uses event-evidence/6, reader validates /6 and compatible W7 /5 metadata, older /4 and /5 remain readable; actual project_event→authorized Observation test added |
| Dynamic movement geometry was treated as known light at a blocked ray endpoint | Explicit endpoint/ray uncertainty, unknown public result, perception refusal for uncertain cells, mechanical snapshot rejects uncertainty |
| New geometry could overlap existing blockers or shared passage reserved cells | Common spatial audit includes legacy blockers and reserved ports; shared door geometry restricted until orientation is supported |
| WorldHandler independently composed without the facade lacked its new services | Capability composition moved into WorldHandler.__init__; facade reuses it. Existing domain-runtime and navigation-boundary regressions rerun |
| Unknown optional limits and template references needed stronger semantics | Explicit unknown capacity/threshold values, rejection of unknown-capacity transfers, consistent definition refs and one legacy/new lock-ID namespace |
| Settlements/read queries accepted malformed identities/version/input types | Bounded types checked before application/query; no public result/HP setter was introduced |
| Historical tests equated exact old schema shape with preserved facts | Migration tests retain old columns/rows, verify new tables empty, compare receipt replay against the upgraded snapshot; genuine old runtimes still create fixtures |

## Public and internal support

- **Real player paths:** public object/illumination queries; SetObjectState open/close,
  BarObject/UnbarObject, LockObject/UnlockObject, unique-lock legacy commands,
  PutItem/TakeItem and existing pickup/drop, ordinary scenario light enable/mode,
  one-shot ActivateMechanism, navigation over current dynamic movement projection.
- **GM path:** RepositionObject requires an authenticated placement grant and an
  explicitly configured GM placement policy. This proves management placement,
  not unlimited player lifting, pushing or strength-based movement.
- **Internal synthetic path:** typed admitted durability settlement, stale/target/HP
  checks, result adoption, failure rollback, destroyed shell/source/spatial behavior.
  No fake player attack command, Dice/HP formula or validated=true bypass exists.
- **Existing NPC paths:** existing door/unique-lock/pickup/drop/navigation Evaluation
  conversion and reliable handoff remain. New bar/multi-lock/container/light/lever
  Evaluation intent DTOs are not implemented. Trusted controlled-NPC typed command
  support does not imply new LLM intent conversion exists.

## Scenario and recovery

Run from the isolated checkout:

```powershell
uv run --locked python examples/world_station.py
```

The standalone original YAML and example perform pickup, nesting, take/put, closed
key denial, case lock, shared door lock, failed locked lever, light/shading, then
close the first runtime. A fresh Python process resumes the database, replays the
original receipt, unlocks case/door, consumes the lever, traverses the same door,
encounters a multi-cell obstacle, explicitly uses the GM placement grant and
navigates through the cleared route. Output separates player, GM and synthetic
evidence. Demonstrated final state: workshop `(1,0)`, world version 32, event seq 49,
bright local illumination, all eight reported object IDs unique and preserved.

Additional tests cover independent-process definition/lighting/container recovery,
genuine committed Schema25 nonempty save upgrade, legacy historical migrations,
SQL fault injection, stale versions, retry/replay, credential identity, graph
validation, locked/jammed/barred/destroyed/occupied targets and trigger adoption.

## Validation record

No AGENTS.md, check-fast or CI workflow exists at the verified baseline. Validation
uses the repository's pytest and Ruff configuration. The isolated environment was
installed with `uv sync --locked --group dev --python <verified Python 3.12>`.
No dependency/lockfile upgrade was made.

W1: 24 focused cases passed. W2: 26 focused cases passed. W3: 29 focused cases
passed. W4: all 1,227 collected cases covered by four full file partitions and the
corrected historical replay assertion retest. W5: 72 legacy/item/capability cases
and 21 World/recovery cases passed. W6: 13 initial cases and 16 spatial/durability/
navigation cases passed. W7: 85 combined regression cases passed, its one endpoint
uncertainty failure was fixed and all six light cases then passed. W8: 13 mechanism/
example/durability, 25 combined World and seven light/real-perception cases passed.

Final whole-suite validation is partitioned across every `tests/test_*.py` file,
with a separate basetemp per partition. First final run collected 1,256 tests:
1,254 passed, two independent-composition failures identified the same missing
WorldHandler service wiring. Partitions 2 and 4 completed with 396 and 306 passing
cases. After fixing wiring, complete affected partitions 1 and 3 are rerun; their
definitive results are recorded in the final delivery section of the implementation
record. No failed run is reported as a clean full-suite result.

Checks: `ruff check src tests examples/world_station.py`,
`ruff format --check src tests examples/world_station.py`, `git diff --check`.
Full command logs live under `.test-tmp/w8-final*.log` and
`.test-tmp/w8-repaired*.log` in the isolated checkout.

## Remaining boundaries and merge conditions

Player attacks/repair need real RE object-target settlement support. Combat world
actions need eligibility/cost/timing/reaction and environment input integration.
Picking/forcing need real tools/Activity/rule semantics. Finite fuel needs one
authoritative balance and tick/accounting path. Magical light/darkness/sunlight
belongs with Effects. Dynamic visual occlusion/Cover, rotating door geometry,
automatic pressure/weight mechanisms and new NPC Evaluation intents remain open.

Old IDs/quantities/charges and supported behavior are retained; new capabilities
require explicit definitions. No inferred discovery/knowledge/memory is attached
to objects. The branch remains local-runtime work and requires review of these
declared limits before merge. No main merge or main update was performed.
Combining the excluded GC5 work requires explicit migration renumbering/testing:
its uncommitted migration 26 is unrelated to this branch's committed World 26–31.


Final frozen-code result: **1,259 passed**, partition counts 314/396/242/307, all exit
0. All four partitions were rerun after the last semantic changes and three new
unknown-field/template regressions. Static checks pass, including format of 148
files. The complete public scenario passed inside this suite. The implementation
is ready for bounded-scope review with the integration limits listed above.
