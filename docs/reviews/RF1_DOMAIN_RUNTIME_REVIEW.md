# RF-1 implementation review

Repository: `agentic-trpg/trpg-state-machine`, branch
`refactor/sm-rf1-domain-runtime`. Start: clean `802599a78c24df7f99381f88c7c4ba91a2c92cc9`,
already containing GC-1 and `9e9eac8`. `origin/main` matched that baseline after
fetch. Read-only architecture main `8141402` includes decision `e6cde66`; no
architecture or RE repository was modified. No merge, reset, rebase or dependency
upgrade is part of RF-1.

The [design and field audit](../RF1_DOMAIN_RUNTIME.md) records actual owners,
dependencies, settlement chain, preserved GC-1 behavior and remaining debt.

## Executed evidence

Commands use `.venv/Scripts/python.exe -m pytest -q`, `PYTHONUTF8=1` and file
SQLite; RE/subprocess/concurrency suites run outside the restricted sandbox.
No core transactions were mocked, tests skipped or thresholds lowered.

| Stage / command targets | Result |
|---|---|
| Before code: package, commands, sessions (Fast), spatial, GC-1 doors/effects, conditional effects, real checks, closure, NPC outputs | 288 passed, 65.58 s |
| Storage phase: package/commands/sessions/migrations | 127 passed, 61.40 s |
| Storage scope tests | 2 passed, 0.51 s |
| World/character phase: spatial + GC-1 + conditional effects | 91 passed, 21.53 s |
| Combat phase: conditional effects, real checks, closure, Dnd runtime, NPC combat handoff | 104 passed, 35.87 s |
| Fast + standalone domains + Dnd NPC availability + game time | 162 passed, 37.49 s |
| migrations + NPC perception queue + efficiency + standalone domains | 71 passed, 128.30 s |
| GC-1 + spatial + conditional effects + standalone domains after private-injection migration | 97 passed, 20.27 s |
| Independent-review fixes: standalone domains + combat foundation | 24 passed, 8.14 s |
| Full before the final two review fixes | 919 passed, 390.23 s |
| Final complete acceptance, after all review fixes | 920 passed, 388.26 s |
| Existing spatial_world examples: default crate and stool, both with fresh-process read | Both passed |
| Existing real_cross_phase example: exploration, real checks, combat, closure and fresh-process resume | Passed |
| Ruff, format, git diff --check | Passed; 144 Python files already formatted |

The final suite command is `.venv/Scripts/python.exe -m pytest -q
--basetemp=.test-tmp/rf1-acceptance`. Focused rows name the corresponding
`tests/test_*.py` targets. Examples were run with the same Python executable:
`examples/spatial_world.py --directory .test-tmp/rf1-example-crate`,
`examples/spatial_world.py --directory .test-tmp/rf1-example-stool --object stool`,
and `examples/real_cross_phase.py --directory .test-tmp/rf1-example-cross-phase`.
Static checks are `.venv/Scripts/ruff.exe check .`,
`.venv/Scripts/ruff.exe format --check .` and `git diff --check`.

The two warnings are existing Lark imports of deprecated `sre_parse` and
`sre_constants`. Baseline and post-change runs above are distinct. Earlier
intermediate failures included missing extraction imports, a mechanical schema
literal and private tests still calling/injecting old facade helpers; these were
corrected and rerun. In particular, the first full run was **917 passed / 2
failed** because the CheckWorldEffects tail-failure injection still patched
`sm._expand_events`. It now patches the actual PackageEffects implementation;
all original rollback assertions remain. Geometry assertions likewise read the
owning WorldHandler instead of retaining unused facade methods.

New boundary tests construct actual components without a StateMachine, prove
world/real-RE composition, session isolation, one execution through the public
entry, replay, pure reads, no early commit, scope/thread/close guards, and full
character/combat/world/event rollback after an Outbox SQL failure. The AST check
verifies table/class writer ownership, active-write guards and dependency
direction. Existing full regressions supply authorization/OCC/retry/lost-reply,
NPC lifecycle and exploration/combat continuity evidence.

## Independent review

An independent read-only reviewer inspected the complete baseline diff and new
files, including AST semantic comparisons of migrated behavior. Review found:

- A scope returning a raw connection could outlive its transaction. Fixed:
  UnitOfWork exposes checked execute/fetchone, returns only rowcount/detached row,
  expires on exit and rejects writes through a read scope.
- Transaction-control SQL through scope.execute could commit early. Fixed:
  scope accepts the bounded mutation/DDL statement set and rejects control SQL,
  comments used to conceal it and PRAGMA; only outer transaction/savepoint
  infrastructure controls commit/rollback. Added explicit refusal assertions.
- Sharing actor initialization could change legacy resource/effect refusal from
  unsupported_combat_foundation to invalid_combat_foundation. Fixed: the encounter
  path retains original priority/codes; a public Package/create/initialize
  regression verifies the original rejection and no partial initialization.

Final independent review: **no remaining blocking findings**. The reviewer also
independently ran the 2 Store boundary tests, checked the complete diff and
confirmed dependency pins unchanged. Full-suite results are reported separately
above by the implementing agent. Remaining architecture debt is explicit in
the design document: transitional NPC and
synthetic settlement, pinned history dependency, unsupported combat policies
and active-combat recovery. These are not claimed as completed.

## Dependency and schema evidence

`git diff -- pyproject.toml uv.lock` is empty. Both current Git blob hashes equal
their `802599a` values:

- pyproject.toml: `9c2c2209812fdf6a987ca56294abf2dd1b491777`
- uv.lock: `3a97f45be12dfb595251b48f8109a33ed3668003`

RE/SRD remain 0.6.0 at `602dcb8d448e670049427bbb751d6ed226005298`.
No schema SQL or migrations changed; schema 21 remains authoritative. No whole
RE test suite or performance benchmark is claimed.
