# SM-10–SM-13 support and migration matrix

Historical implementation record. Current dependency/schema/compatibility and
component ownership are specified in [RF-RE4](RF_RE4_HISTORY_FREE_RULES.md).
The 2026-10-17 Openable pin, verified increments and unfinished boundaries are
recorded in [Openable progress](OPENABLE_SERVICE_PROGRESS.md).
Time support introduced in Schema 32 and its compatibility are specified in
[WorldTimeService progress](WORLD_TIME_SERVICE_PROGRESS.md).

Current storage Schema 33 and Package `/8` object orientation/geometry support are specified in
[ObjectSpatialService progress](OBJECT_SPATIAL_SERVICE_PROGRESS.md). WOS provides full-cell
movement, difficult-terrain, occlusion and cover contributions; merged terrain, dynamic visibility,
Perception and Combat consumption remain OPEN. Candidate placement and final audit share wall-edge
and occupancy rules. Old Package digests and placement receipt fingerprints remain compatible.

| Current World time capability | Actual support / boundary |
|---|---|
| Session time binding | `world-time/1`, immutable integer-second configuration persisted at creation; optional complete Package default and complete request override. Existing Sessions keep their saved interpretation. |
| Clock authority / advance | `WorldFacts.world_states.game_tick`; WorldHandler calls the real WorldTimeService inside the original command/savepoint. Same authorization, OCC, fingerprints, receipts, event evidence, Outbox and rollback. |
| Calendar / phase queries | Custom positive day length, starting day/time, cyclic day/night including midnight wrap, or no cycle. Exact seconds/minutes/hours/days, elapsed/remaining/reached, read-only queries. |
| Interval boundaries | `(start,end]`; combined simultaneous kinds; strictly exclusive continuation cursor, limit 1–1000, O(limit) work for arbitrarily large supported spans. |
| Legacy abstract time | Absent binding means `game-clock/1`; no tick scaling or history rewriting. Old command and creation fingerprints preserved; actual Schema31 save and receipt replay tested. |
| NPC schedule / OB / Gate | Existing legacy schedule sync remains in the original transaction, with NPC versions/task cancellation preserved. OB 100 and Gate 20/60/10 abstract-tick policies unchanged. Second-based configured NPC Runtime and observation access explicitly unsupported pending reviewed policies. |
| TimeDriver / Combat / other due work | NOT IMPLEMENTED: wall-clock sampling/rate/pause, Round integration, Activity deadlines, Effects, fuel/charges, weather/light changes, NPC scheduling and runtime segmentation. Manual advance during active combat remains rejected. |

| Capability | Actual status and evidence |
|---|---|
| Single authority before/inside/after combat | Supported: `characters`, `mechanical_actors`, `mechanical_scenes` share facts; no snapshot overwrite. `test_versioned_state`, `test_cross_phase` inspect SQLite. |
| Generic ability/skill check | Supported with complete Snapshot V2 and trusted GM grant; proficiency/expertise and explicit flag advantage/disadvantage are computed by Engine. `test_real_checks`. |
| Basic weapon inputs | Real rapier (including occupied other hand), longsword one/two handed, greatsword two handed tested against Engine events/deltas. |
| Other weapons/mechanics | Dagger's Light/Thrown properties and trained mastery explicitly unsupported in this candidate slice. Grip disagreement is rejected by Engine. Invalid complete equipment facts reject Package loading. |
| NPC attacks | Existing trusted NPC grants and real Bandit actions through the same committer; generic attack Signals only. |
| Victory/TPK closure | Supported with upstream CombatClose, existing final balances and new XP increments; save/new-process resume works. B1 resolved for this bounded slice. |
| RNG continuity | Same persisted stream across checks, combat initialization, PC/NPC attacks, closure and postcombat check. Closure/init/world operations do not reset or consume it. |
| Resource balances | Existing explicit spell-slot/custom-counter balances survive the complete pipeline. No admitted noncombat resource-consuming operation: granted-die/tool paths are unsupported with no RNG/resource change. Resource-consuming cross-phase mechanics remain deferred, not proved. |
| Durable failure handling | Result/schema/config/runtime/storage failures remain independent and retryable; business refusals/conflicts have receipts. Response loss after commit replays without evaluation or payment. |
| Active-combat process interruption | Explicitly unrestorable under MVP; committed attacks are preserved, no forced ending/reset. |
| Further Encounters | One authored encounter per Session in this local foundation; no reset/reinitialization after closure. |
| Missing mechanical Actor/Scene facts | Fail closed; additional scene templates initialize lazily, but do not synthesize missing NPC character mechanics. |
| Full effects/Reaction/Continuation/Saving Throws/spells | Explicitly out of scope. Host, EVA, Memory/Belief, full perception propagation and a production adventure remain separate MVP work. |

## GridService 后的空间跨系统待办

2026-10-18：GridService 完成 SM 当前读取／组合／基础几何切片，不能将此结果作为
RE／Combat／Character／Effects 的完整集成证据。当前固定依赖未变。

| 既有开放范围 | 当前限制与依赖 | 后续接入／触发条件／验收 |
|---|---|---|
| 角色 Size／多格空间与经过角色的规则 | Character 只有唯一锚点，Grid 当前只报告这一格；物体多格支持不能证明角色多格。需要 Character／RE 共同确定完整占格与规则输入 | 契约确定且实际依赖可运行后，改 Character 当前空间读取、Grid 角色组合、RulesAdapter 与 Combat；真实大体型／旋转或朝向适用范围、边界／墙／通路／角色穿越／掩护验证占用和机械规则一致，失败回滚不留下位置副本 |
| 战斗世界空间／动态障碍／费用 | 现有 Combat guard 保留；探索专用拓扑不是机械 Grid，不支持的贡献继续拒绝 | RE 稳定且能独立表达移动、视觉、掩护及困难地形后，改 RulesAdapter／CombatHandler 的输入与采用；真实移动／费用／反应／开门状态变化及 OCC／幂等／回滚验证，不能引入探索回合额度 |
| 新 Activity／Effect 契约的真实跨系统验证 | RE 重构尚未完成；没有新占位协议，未执行未来契约端到端 | 新契约发布并固定依赖后按 [Navigation 依赖](GC2_NAVIGATION_ACTIVITIES.md#gridservice-后的-activityeffect-依赖) 和 [OSS 集成](OBJECT_SPATIAL_SERVICE_PROGRESS.md#gridservice-消费与后续接入2026-10-18) 接入真实来源／生命周期／规则采用并完成所列验收，现有可运行集成不能被整体跳过 |

W-01 仅部分推进，SA-01 及上述跨系统工作仍 OPEN。没有改动 Turn Episode／Round
Episode、NPC 认知模型或增加逐机械 Event 的感知／阶段证据要求。

## Storage compatibility

Schema 9 adds `characters.xp` and `sessions.mechanical_schema_version` under the
existing serialized, transactional migration. No binding, digest, HP, inventory,
RNG, receipt or event rewriting. No migration beyond 9 was needed for closure or
checks. Phase is derived from `combats.state_json.ended`, never stored separately.

Historical evaluator /4 Sessions remain readable with their original Package and
receipts, but cannot execute new /8 operations. Historical regression uses a
separate locked environment. Snapshot V2 is initialized from approved Package
facts once; the new evaluator does not authorize template reload or save upgrades.

XP is a SQLite integer in the core character row. Fault testing confirmed that
out-of-range accepted increments could otherwise overflow/coerce storage. The
committer now rejects unrepresentable increments as invalid_rule_result and
rolls back normalization/ended/RNG/events/receipt. This is a persistence invariant,
not an XP formula or second reward calculator.

## Reproducible example and evidence

Run `uv run --locked python examples/real_cross_phase.py --directory .test-tmp/demo-new`.
The directory must be new; it retains the original generated Package and SQLite
database for inspection. The script uses real bundled rule assets, a synthetic
original adventure, and launches a fresh interpreter for resume and the next
check. Its trusted context is a demonstration Host capability, not authentication.
The example itself is covered by an automated real-database integration test.

`test_complete_real_cross_phase_pipeline_and_new_process` checks every checkpoint:
world/event versions, RNG, HP/equipment/resource balances, XP, receipt count,
outbox/evidence records, attack Observations, private check details and a second
Session. No live state is seeded with SQL or an overwritten Snapshot.

F1–F5/R1 remain in the full suite. F6 is aligned for the new bounded attack,
closure and check slice; a complete general Rule Evaluation contract is still
partially resolved because unsupported mechanics and formal continuations remain.
General event-only/Meta version policy remains OPEN.

## Batch verification

SM-13 adds 16 test instances: 15 cross-phase/weapon/fault/example cases and a
second actual historical database version. It also hardens the existing closure
and check fault tests: mutated JSON is first parsed into a valid upstream
RuleEvaluationResult so tuple/schema rejection cannot mask the intended semantic
validation. The real-result closure/check/cross-phase group passes 69 tests.

Final full suite: 396 passed using `uv run --locked python -m pytest -q --tb=short --basetemp=.test-tmp/b13-verified-full`.
Locked sync, Ruff check, Ruff format check and `git diff --check` passed.
All historical tests remain present. Two lark deprecation warnings originate
from the upstream dependency and do not indicate failed checks.
