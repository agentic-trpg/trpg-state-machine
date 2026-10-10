# SM-6～SM-9 Real Rule Engine Integration — Global Review

日期：2026-10-10。分支：`feat/sm-b6-b9-real-engine-integration`。
业务审阅冻结点：`eeb5b8fcd63222342e9dced7295130e8e50a85f7`。

**结论：SM-6/7/8 的受限能力已实现并验证；SM-9 为 Partial，F5 Resolved，
F6 Partially Resolved。暂不建议 Merge main。** 全量 273 项测试通过，但独立
只读审阅另确认一项 P2 写入范围缺陷（R1），并保留正常战斗结束后安全续玩的
上游能力阻塞（B1）。测试通过不代表这些验收已经完成。本轮不 Merge、不创建 PR。

## 1. 依据、版本与审阅方法

- 最高依据为本地 `D:\Projects\agentic-trpg\agentic-trpg\docs\`：Accepted
  ADR-001、MODULE_CONTRACTS v0.8 §2/§4–§8/§10、STATE_MACHINE_ARCHITECTURE
  v0.10（特别是 §7.3/§7.4/§8.3 的 SM-02）、AGENT_ARCHITECTURE v0.9，
  并核对 ADVENTURE_PACKAGE_SCHEMA v0.6、MVP_SCOPE v0.11。
- Meta 当前 HEAD：`fe6a1e9edeaf2a3e120aa669b6abfe1e819d87f1`。本地文档的
  已确认要求优先；示例 `module-contracts/0.2` 及 Proposed 字段不视为共同冻结 ABI。
- 真实 Engine / SRD 依赖始终固定
  `a7225ebaa4914f6db4ae72f3f6454c355ea7baac`，由 pyproject 与 uv.lock 锁定。
  实现开始时 main 为 `64dd920507de29602908b4bea1635eedc4385370`；最终审阅再次
  `git ls-remote`，main 和 `refactor/re-eval-04-npc-and-availability` 均为
  `a7225eb...`。候选已到达上游 main，但本轮没有跟随浮动分支更新依赖。
- 实际核对上游 evaluation/state/contracts/delta/rng/preflight/availability 实现，
  而非只比对文档。上游一次求值的临时 `_LiveCombat` 属 ADR 允许的内部适配；
  SM 没有注册或保留独立权威 Handle。
- SM-9 提交后进入单独只读审阅阶段，检查 SM-1～SM-9 的实现、SQL、实际 Diff、
  测试断言、授权、事务及异常调用链；冻结点重新运行全部回归。审阅另用真实 Engine
  Result 故障注入和隔离 SQLite 复现 R1。没有在审阅阶段自动修改业务代码。
  这是一次独立阶段的代码审阅，不宣称另一个人或外部服务已签署审阅。
- 仅提交 State Machine 文件。Meta / Engine 工作区已有其他未提交文件，未被本任务
  编辑、清理或提交；依赖验证使用固定 Git 来源，不消费该脏工作区覆盖内容。

## 2. Batch 提交与验证

| Batch | Commit SHA | 新增 / 总测试 | 实际结果与状态 |
|---|---|---|---|
| SM-6 | `74368459ec805a341d9b6e9451e218444a174fa6` | 17 / 208 | 完整 Snapshot、独立 RNG、Schema 1–6 真实旧库升级；Passed |
| SM-7 | `8f24ca03e6037428bbc559d50e91bcd57669a0ed` | 21 / 229 | 真实 PC Attack、完整 Delta 差分、原子提交/竞争/故障；Passed |
| SM-8 | `7cf7d0f944b3b67aa71e3daaa72f9542a708e95d` | 17 / 246 | NPC 同路径攻击、可信 Grant、只读 Availability；Passed |
| SM-9 | `eeb5b8fcd63222342e9dced7295130e8e50a85f7` | 27 / 273 | 加固、F5、真实新进程审计；Partial（B1，及审阅发现 R1） |

四个 Batch 均独立 Commit + Push 成功。原始 168 项及 Stabilization 新增 23 项
仍运行，当前基线 191 + 本轮新增 82 = **273**，没有删除既有测试来获得通过。
既有断言的修改仅涉及新 Schema 版本、真实历史版本来源和 synthetic 前置选择策略。

SM-9 提交前：**273 passed, 2 warnings in 77.49s**。
冻结后的 Global Review：**273 passed, 2 warnings in 68.78s**，无 skipped。
两项警告来自 d20 的 lark-parser 对 `sre_parse` / `sre_constants` 的弃用导入。
Locked sync、Ruff check、format check（45 文件）和 Diff check 均通过。

实际命令：

```powershell
$env:UV_CACHE_DIR = "$PWD/.uv-cache"
$env:PYTHONUTF8 = "1"
uv sync --locked --group dev
uv run --locked python -m pytest -q --tb=short --basetemp=.test-tmp/global-review
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
```

Windows 受限沙箱阻止 asyncio 所需的本地 socketpair，因此真实 Engine 测试在允许
该 OS 功能的环境执行；未调用外部规则服务或 LLM。日志和审阅复现数据库仅位于
忽略的 `.test-tmp/`。检查 `git ls-files`，没有测试 DB、WAL、缓存或上游临时 checkout
进入版本控制。Git 历史迁移测试要求完整历史对象，源码压缩包/浅克隆须先补足历史。

## 3. 实际能力与 SM-1～SM-9 回归矩阵

| 能力 | 实际实现 / 边界 | 代码及执行证据 |
|---|---|---|
| Package / Session / Scene | Safe YAML、引用与摘要校验、可信创建幂等、隔离、一次初始化/重访及非战斗恢复 | package.py、service.py；test_package/session/scene/integration/commands |
| Command / Receipt | 强类型信封、可信主体/Actor、Fingerprint 冲突、World Version Gate、持久回放 | commands.py；test_commands、test_dnd_runtime 并发同键/不同键及重启回放 |
| 合成机械路径 | 原 synthetic.atomic 封闭操作；配置错误独立、确定性拒绝保存回执 | mechanical_runtime.py；test_mechanics；未成为正式 D&D ABI |
| 受限 Encounter 初始化 | 只能选择已批准且 Digest 固定的完整 Package encounter；不接受调用者任意 Snapshot | combat.py InitializeCombat；test_combat_foundation 17 例 |
| SQLite 权威 Combat | HP/装备仍在 characters；其余 typed actor/combat/scene 组件单独保存；历史来自正式 Events；独立 RNG | combat.py、migration_7；完整 Snapshot round-trip、双 Session、失败回滚 |
| PC / NPC 基础攻击 | 实际 evaluate()；显式普通武器或 NPC 精确 stat_block_action_id；相同提交路径 | dnd_runtime.py；test_dnd_runtime、test_dnd_npc_availability |
| 支持的实际规则行为 | 命中/未命中、Temp HP、减免/免疫/易伤、行动预算、回合推进、死亡/伤害账本按真实 Engine Delta 保存 | 完整上游前后 Snapshot / event history / RNG 差分；zero-HP PC 和 lethal NPC 测试 |
| NPC 权限 | Session owner + 可信 controlled_npc_ids；提交前重验，撤销后不可回放/查询该 Actor 回执 | commands.py；错误主体/Grant/撤销及查询后撤销测试；不实现认证服务 |
| Availability | actual upstream available/unavailable/unknown；原因文本过滤，零 RNG/Delta/Event/Receipt/SQL 写入 | availability.py；全表及 total_changes 比较、查询/执行版本竞争 |
| Event / Outbox | Engine proposals 由 SM 分配正式身份；状态/RNG/Receipt/事件/双 Outbox 同事务；可靠重投 | service.py、events.py；SQL 故障、Lost Response、级联回滚、既有 test_events |
| Trigger History / F5 | 新记录外键精确关联 source event_seq → 稳定 event_id；旧来源明确不可恢复 | migration_8、events.py；test_event_traceability 8 例 |
| Perception / Inbox | 已提交攻击只有通用可见 Signal；事件时证据、未知保持未决、顺序水位和持久 Inbox | perception.py、service.py；既有 test_perception/migrations 与真实纵向测试 |
| 重启 | 新 Python 进程读回完整机械事实/RNG/回执/Observation；原 ID 回放无需 Evaluator | _dnd_restart_worker、test_real_vertical_pipeline_committed_observations_fresh_process_audit |
| 安全战斗续玩 | 活跃战斗 save/resume 和跨进程新 Attack 明确 combat_unrestorable；正常结束后续玩未证明 | B1 Blocked；不把审计读取描述为成功恢复游戏 |

PC/NPC 普通攻击测试使用原创 Lantern encounter、原创无复杂属性武器及真实 bundled
SRD Bandit 的 `scimitar` / `light-crossbow`。有效 RulesetBinding 摘要覆盖实际生效的
测试语料；不是用 synthetic.atomic 冒充 D&D，也不声称全部 SRD 或真实 Adventure
已经验收。静态完整起始 Fixture 的编制方式在 SM6 文档披露；运行路径禁止 Legacy
registry / start_combat / Monster AI，真实成功路径不以 Mock 代替求值。

## 4. F6 — 契约逐项验收矩阵

矩阵对照 MODULE_CONTRACTS §4–§6 与固定 Engine API。Resolved 只适用于本轮
明示的基础攻击切片，不冻结跨模块的所有未来字段/规则。

| 验收项 | 状态 | 代码 / 测试证据及实际限制 |
|---|---|---|
| Request Required / Schema | Resolved | actual RuleEvaluationRequest 显式提供 schema/session/command/operation/actor/payload/snapshot/binding/RNG；engine-evaluation/1、engine-snapshot/1；CombatIntentPayload 严格验证/source 绑定 |
| Result Required / Schema | Resolved | actual RuleEvaluationResult revalidate_instances + verify_request；missing-required 6 例、错 read-set/binding/RNG/Delta 均无持久回执，原命令可重试 |
| accepted / rejected / unsupported | Resolved | accepted 不等于命中；结果→既有 committed/rejected Receipt；真拒绝不写 RNG/事实，实际 Engine 差分及拒绝测试 |
| needs_choice 第四态正常提交 | Partially Resolved | Schema 识别四态；本切片缺显式武器选择在求值前拒绝；不提供 pending_choice 成功路径，Unexpected choice 为独立协议错误（非正常拒绝） |
| Schema/Binding/运行时/存储错误独立 | Resolved | rule_schema_error / ruleset_binding_mismatch / rule_evaluation_failed / invalid_rule_result / storage_error；配置修复、7 类故障、SM9 SQL fault/retry；无永久 rejected 伪装 |
| CombatSnapshot 类型/完整依赖 | Resolved | 完整 actual Character/Combat/Scene 字段及显式空 effects；所有复杂依赖保留 DTO 字段并由 capability guard 拒绝；missing-state 7 例、SQLite重建/重启 |
| NonCombatSnapshot 判别及 Schema | Resolved | actual 非战斗 discriminator/schema 测试；不伪造 combat_state |
| 正式 NonCombat Evaluation | Explicitly Out of Scope | 只检查 Schema，不声称有真实技能/豁免/休息等执行能力 |
| read_set / 提交重验证 | Resolved | pinned Engine 的唯一 whole-session world fence 完整覆盖当前依赖；短写事务再比对完整 Snapshot/RNG/World Version；并发及独立 RNG conflict 测试；细粒度 component versions 未冻结 |
| RulesetBinding / 实际数据 | Resolved | Session pins + 实际 effective corpus hash + dnd5e-evaluation/4；求值前/求值后独立验证；提交重验 Session pins；不在写锁中哈希规则语料 |
| RNGContext / RNGTransition | Resolved | 独立 rng_states、actual stdlib-mt19937/1、流/版本/后继校验、提交原子性；完整 successor 差分与 fault/rollback；不冒称全项目 RNG 编码已冻结 |
| StateDelta 封闭类型/前置值 | Resolved | actual 11 类 typed union；unknown/duplicate/prevalue/bounds 检查，不接受 JSON Patch/SQL/整份 Snapshot 覆盖；实际故障与回滚测试 |
| StateDelta Actor/写入范围 | Partially Resolved | HP/status 限声明 target，budget/ledger 限 acting/next-turn Actor；**R1：ProcessedDamageUpdate 新账本项仍只检查 roster，未限制本次批准 target** |
| 资源池/物品消费/复杂 Effect Delta | Explicitly Out of Scope | 当前真实切片只提交基础攻击及其行动预算；未实现药水支付+治疗等契约黄金用例，不能从合成 token 测试外推为真实 D&D 资源协议验收 |
| Delta + RNG + Event + Receipt 原子性 | Resolved | 同一 SQLite 事务；级联拒绝只回滚候选后保存 Receipt；非业务故障全部回滚；跨领域 33 级联、history/receipt SQL fault、重启回放 |
| 无正式 Continuation 时前置拒绝 | Resolved | D&D 缺明确 weapon/stat-block 引用和 synthetic 缺 difficulty 在 Evaluator 前拒绝；测试零调用；无部分机械/RNG/事件副作用 |
| PendingOperation / choice.respond / Reaction | Explicitly Out of Scope | 未实现，不能把 unexpected needs_choice 变成已执行行为或正常拒绝；未为了关闭 F6 增加复杂 Continuation |
| Synthetic / 真实协议隔离 | Resolved | mechanics.py 的 synthetic-rule-* 仅本地合成；真实路径直接 import 上游模型/evaluate，独立 evaluator/loader；不会扩张 synthetic 为第二份正式公共 ABI |
| 正常战斗结束后安全续玩 | Blocked | B1：候选缺 stateless combat.end；不得在 SM 补造结束/死亡计算。此项不能由上游已合并 main 或测试通过替代 |

F6 整体为 **Partially Resolved**：受限真实适配消除了旧“只有合成协议”的缺口，
但 R1 未修复、正式 PendingOperation 未实现、完整跨模块/全机制协议不能宣称关闭。

## 5. Confirmed 缺陷、阻塞及风险

### R1 — P2：ProcessedDamageUpdate 可写入本次 target 之外的账本（Confirmed）

位置：`src/trpg_state_machine/dnd_runtime.py:123`（delta_changes）。
代码只检查旧集合为新集合子集，以及新值 Actor 属 encounter roster。该 operation
没有 `target_id` 属性，绕过循环开头对请求 target 的约束；新增项可指向攻击者或另一个
已存在 Actor。严格上游 Schema 和最终 CombatSnapshot 也不检查此授权范围。

实际复现：原创合法 Package → Session → Combat 初始化 → PC 攻击 npc.caretaker →
真实 evaluate 得到 accepted Result → 仅在 Result 中附加以下合法类型 operation：

```python
ProcessedDamageUpdate(
    kind="combat.processed_damage_update",
    combat_id=request.state_snapshot.combat_state.combat_id,
    expected=frozenset(request.state_snapshot.combat_state.processed_zero_hp_damage_instances),
    value=frozenset(request.state_snapshot.combat_state.processed_zero_hp_damage_instances)
    | {(request.actor_id, "review:foreign-target-damage")},
)
```

实际结果：`receipt_status=committed`、`persisted_wrong_scope=true`、FK 检查为空。
重新从 SQLite 构建的 Snapshot 包含该攻击者账本项；关闭运行实例后，另一独立
sqlite3 连接直接读取 combats.state_json 与 command_receipts，确认二者已落盘。
期望是独立
invalid_rule_result，所有候选状态/RNG/Events/Receipt 不写入。
审阅命令为 `uv run --locked python .test-tmp/review_delta_write_scope.py
.test-tmp/review-ledger-scope-confirmed`；脚本/输出/数据库是本地忽略的审阅见证，不属于 273 项
pytest 成功统计。复现使用 tests/test_dnd_runtime.setup/attack 和实际 Engine，
没有修改正式 Evaluator 或业务文件。

影响：客户端不能直接提交这种 Delta；当前固定 Engine 正常路径未观察到该错误。
但 Evaluator 错误/协议漂移可污染其他 Actor 的零 HP 去重历史，违反批准写入范围。
这是已复现的防御性提交校验缺陷，不等于已证明当前正常攻击会错误计算伤害。
建议单独 Fix Batch：只验证新增加的 damage-ledger 项属于本次允许 target，保留
原历史；同时检查 DeathLedger 的新增 target/来源范围，并添加真实 SQLite fault/retry
测试。不要在 SM 重新计算 D&D 伤害或创建第二个账本模型。审阅阶段未自动修复 R1。

### B1 — 正常结束/安全续玩能力缺口（Blocked，非新 P0/P1 缺陷）

上游只执行受限 combat.intent。没有 stateless combat.end；致死 NPC 攻击增加
dead_ids/deaths_recorded，但不设置 ended，is_alive 也保持上游现有语义。
SM 保留真实结果，不用本地“敌人 HP=0”公式补造结束/奖励。
本地架构 §7.3 将结束条件交 Engine，§8.3 禁止活跃战斗普通 save_session
声称成功续玩。affected acceptance 已暂停并报告，尚未收到人工澄清。
实现了持久审计/原回执对账和明确阻塞，不宣称正常战斗后 save→restart→resume 完成。

### 其他风险与 OPEN（未列为已确认缺陷）

- TrustedContext 的认证和 NPC control Grant 由可信 Host 提供。当前按命令获取的
  控制快照在提交时重验；Host 的实时权限撤销/认证目录不是本库提供的服务。
- 单进程优先，多个 SQLite 连接的竞争已测试；不声称有分布式锁/多 Host 执行支持。
  process_id 只作为中断战斗资格标记，不保存权威游戏状态副本。
- 初始 Encounter 是明确 Package 扩展，非正式 combat.start/create；战前安全 Checkpoint、
  phase 转换、奖励/库存/Quest、备份/降级尚未实现。旧 runtime 不保证识别 Schema 8。
- Engine 返回的所有复杂 Effect/Reaction/Area/资源生命周期、完整多回合机制和非战斗
  API 不在本切片；零 HP 后继续动作也不能宣称已覆盖完整死亡/回合生命周期。
- event/meta-only world_version、全项目 RNG 编码/无 draw 版本策略、细粒度 read-set、
  正式共同 wire Schema、PendingOperation 生命周期仍 OPEN。
- NPC Observation 仅验证已提交 Signals 与 event-time senses；没有实现完整遮挡/传播、
  规则感知检定、EVA、Memory/Belief 或模型战术。这里的 NPC Intent 来自可信测试调用者，
  不声称通过了独立 NPC Sub-agent 决策验收。

未确认新增 P0/P1。新增确认 **1 项 P2（R1）**。B1 和上述 OPEN/Out of Scope
独立列示，不能当作已修复或用需求扩张掩盖。

## 6. Migration、F1～F5 及信息边界复核

- Schema 7 添加 mechanical_actors / mechanical_scenes / combats，重建原 RNG 表以允许
  actual MT19937 编码；HP/装备未复制到第二份权威，event_log 由 Events 重建。
- Schema 8 重建 trigger_history；新记录 source_event_status=linked，source_event_seq
  以 deferred FK 关联正式事件。同命令 flag→object→flag 链验证不同的精确来源。
  原 once-per-session PK、顺序、命令关联保留；旧记录 NULL/legacy_unavailable
  可审计，不猜测/补造历史事件。因此 **F5 Resolved（新记录）**，历史丢失信息明确不可恢复。
- 启动在 BEGIN IMMEDIATE 内重新读取 user_version；逐步 DDL/连续版本/FK 检查统一提交，
  没有用 IF NOT EXISTS 掩盖新增迁移不完整。旧基础 schema 的原定义未擅自重写。
  正常业务 FK=ON、WAL、synchronous=FULL；长求值/语料计算在写事务外。
- Schema 1～6 由实际 Git archived 旧代码创建数据库；原 World/Scene/Character/RNG/
  Receipt 行逐列保留。另由 SM-7 commit 实际攻击生成 Schema 7 战斗库，升级后完整
  Snapshot/RNG/Receipt 不变。旧 Schema3/6/7 trigger history、新版 DDL 失败回滚、
  重启重复迁移、并发空库/旧库升级、进程中断恢复全部实际运行。
- Stabilization **F1～F4 Fixed 的行为继续通过回归**：绑定错误可修复原 ID 重试；
  Legacy Perception boundary 不将未知当 false；迁移等待者锁内重读版本；确定性
  cascade 拒绝回滚候选后持久回执。Schema8 精确事件来源不改变旧历史感知水位。
- 新 Event 仍分配正式 ID 后才进入 Perception/Outbox；仅 committed attack 的通用 Signal
  可到 NPC Inbox。失败/未提交 Intent 不生成观察。新真实未决 Work 继续阻塞连续水位，
  双 Session/缺口回归通过；未添加第二套感知状态机、服务或 ACK。

## 7. 实际文件清单

相对开始基线 e114658，业务四个 Batch 共修改/新增 31 文件；本 Review 再新增 1 文件。

- 运行/依赖：README.md、pyproject.toml、uv.lock；src/trpg_state_machine 下
  __init__.py、commands.py、events.py、mechanical_runtime.py、package.py、service.py；
  新增 combat.py、dnd_runtime.py、availability.py、_migration_7.sql、_migration_8.sql。
- 新增测试：tests/dnd_support.py、_dnd_legacy_worker.py、_dnd_restart_worker.py、
  fixtures/dnd_lantern_combat.json、test_combat_foundation.py、test_dnd_runtime.py、
  test_dnd_npc_availability.py、test_dnd_hardening.py、test_event_traceability.py。
  修改 test_commands.py、test_mechanics.py、test_migrations.py。
- 新增文档：docs/plans/SM_B6_B9_REAL_ENGINE_INTEGRATION.md、
  docs/SM6_COMBAT_FOUNDATION.md、SM7_REAL_ATTACK.md、SM8_NPC_AVAILABILITY.md、
  SM9_INTEGRATION_HARDENING.md，以及本 docs/reviews/SM_B6_B9_REAL_ENGINE_REVIEW.md。

## 8. 建议开发顺序与 Merge 意见

1. 单独最小 Fix Batch 关闭 R1 的账本写入范围及相邻 DeathLedger 校验，补充失败无回执、
   原请求可重试的数据库测试；复跑全部 273 项和新增测试后重新审阅。
2. 与上游确认受限 stateless combat.end / 正常结束条件、Delta/奖励和 phase 语义，
   在有真实 API 后实施正常战斗结束及非活跃安全 save→restart→resume 验收。
   不以任意 ended 更新或 Legacy end_combat 回写绕过 ADR。
3. 人工决定如何处理 B1 的本轮验收缺口；此决定不能使本报告未实现能力变为已实现。
4. 后续按已确认 MVP 顺序推进 Host/NPC Intent 集成、受限其他规则、正式前置选择等；
   复杂 Reaction/Continuation、视觉、完整感知传播仍保持独立范围。

当前不建议 Merge main：R1 的批准写范围校验尚未关闭，SM-9 正常结束后安全续玩验收
仍 Blocked。受限真实基础攻击及持久审计已取得实际进展，但不能宣称四轮全部验收
完成或完整 D&D MVP 就绪。四个业务 Commit 已推送；本 Review 按计划单独提交推送，
之后停止等待人工审阅，不自动进行 Fix Batch。
