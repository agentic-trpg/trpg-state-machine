# SM-10–SM-13 Global Review

日期：2026-10-10。结论：**建议将本分支的受限真实基础攻击、战斗关闭与通用非战斗检定合并到 main**，不代表完整 TRPG MVP 或完整 D&D 支持已完成。没有执行 Merge 或创建 PR。

这是四个开发提交完成后的单独只读复核阶段，由同一执行者重新检查实际代码、Schema、上游接口与数据库测试证据；不是另一位人工审阅者的签字。本阶段没有修改业务代码。未发现新增 P0/P1/P2 或阻塞性验收失败。

## 1. 基线、依赖与交付

* 初始 State Machine origin/main：`d8abc41cdebe8554d26821001e4c3106778295c0`，开始前核对远端。
* 分支：`feat/sm-b10-b13-combat-close-checks`。
* Engine main 当时为 `a7225ebaa4914f6db4ae72f3f6454c355ea7baac`；候选分支 `feat/re-eval-07-noncombat-checks` 为 `602dcb8d448e670049427bbb751d6ed226005298`。
* Engine/SRD 依赖与 lock 均固定上述候选 SHA。实际版本组合：`dnd5e-evaluation/8`、`engine-evaluation/4`、`engine-snapshot/2`；旧 Snapshot /1 基础子集仍由 Engine 明确兼容，不等于升级旧 Session。
* 只读导出并核对候选 Engine 的 contracts/state/delta/RNG/attack/weapon/check/closure 实现。实际运行上游对应六组测试 **685 passed**；它们与 SM 的测试数量分别报告。
* 候选 [CI run 38050625923](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38050625923)：Fast / Focused Validation 成功，Full Validation **skipped**。没有声称上游整个候选已通过完整稳定版验收。
* Meta 与 Rule Engine checkout 未被本任务写入。Engine 原有未提交内容未参与固定 SHA 的导出或依赖安装。

| 批次 | Commit（均已单独 Push） | 新增测试实例 | 当批最终全量结果 |
|---|---|---:|---:|
| SM-10 | `1dab83d2fb1ab72f46245e544ed9f99bb669a624` | 13 | 326 passed |
| SM-11 | `7b8f22ce521821984807f86ff5c396a55358e46f` | 18 | 344 passed |
| SM-12 | `3e8fdb6ecba97c6206f7c7ba3cfc2516f6e5d1f1` | 36 | 380 passed |
| SM-13 | `5d1f02ef15df44f65b1d45f459d9d92ba7da9f8f` | 16 | 396 passed |

未修改依赖前，原 main 在旧锁定环境的 **313 项**全部通过。累计新增 **83** 项，没有删除历史验收行为。SM-13 同时加强此前新增的故障注入，见第 5 节。Review 的独立回归再次 **396 passed**；本报告的独立提交 SHA 在交付消息中提供。

## 2. 架构与 Scope

开发前读取本地 Meta docs 的 ADR-001、MODULE_CONTRACTS、STATE_MACHINE_ARCHITECTURE、AGENT_ARCHITECTURE、ADVENTURE_PACKAGE_SCHEMA、MVP_SCOPE；复核时再次对照 Accepted ADR、Module Contracts §4–§6 和状态事务/权限边界。

* SM 独占权威 SQLite 状态、版本、RNG、事件、回执、Outbox。Engine 只对复制的输入求值。
* HP/库存/XP 的唯一写入所有者为 `characters`。能力、装备握持、法术位等其他机械事实为 `mechanical_actors`；机械 Scene 为 `mechanical_scenes`；`combats` 只含 Encounter 组件，无第二套 HP 或事件日志。
* Snapshot 是从 SQL 重建的只读输入；候选 Snapshot 仅在内存校验，不整份替换数据库。初始化不会覆盖已提交角色事实或重置 RNG。
* 攻击/伤害/技能 Modifier、优势/劣势、终局与 XP 由真实 Engine 计算。SM 只校验结构、前置值、身份/范围、余额一致性与存储不变量。
* `noncombat_foundation` 是本地受审 Package 初始化扩展，不是另一套正式 Engine ABI。直接复用上游 CheckPayload、CombatClosePayload、Snapshot、Result、Delta、RNG 类型。
* 没有新增独立服务、ACK、游戏队列、规则算法或抽象框架。未把 Meta 的候选字段/消息版本、OPEN RNG 编码或 PendingOperation 冻结为公共协议。

## 3. 逐项 Implementation 验收

Fully 表示在本轮明确受限 Slice 内已实现且有实际行为证据；不表示全规则覆盖。

| 要求 | 状态 | 实际代码与证据 |
|---|---|---|
| SM-10.1 新 Request/Result、Snapshot、Binding、RNG | Fully | `dnd_runtime.py:validate_dnd_result/_submit_dnd`，上游真实类型；`test_real_checks`、`test_combat_closure` |
| SM-10.2 完整能力/装备/XP 初始事实 | Fully | `combat.py:validate_noncombat_foundation/_store_initial_mechanical_actor`；`test_versioned_state` 缺字段、握持、HP、XP、引用拒绝 |
| SM-10.3 唯一字段所有权 | Fully | `characters` 与机械 JSON join；独立 SQL 断言机械 JSON 没有 HP/库存/XP 副本 |
| SM-10.4 首次 Combat 前 NonCombatSnapshot | Fully | `_noncombat_snapshot/read_noncombat_snapshot`；无 combats 行时创建、读取与真实检定成功 |
| SM-10.5 旧 /4 与新 /8 边界 | Fully | `_session` 固定值核验，`_submit_dnd` 独立绑定错误；真正 Schema 7/8 旧环境数据库升级保留全部原列数据和回执 |
| SM-10.6 RNG 不重置 | Fully | 初始化只更新 algorithm=unselected 的 RNG；跨阶段测试从检定 RNG 版本 1 继续攻击，关闭后版本 3，新进程检定到 4 |
| SM-10.7 Migration/并发/中断/隔离 | Fully | `_migration_9.sql`、`service.py:_migrate`；`test_migrations`、`test_versioned_state`、真实旧库测试 |
| SM-11.1 真实 combat.close 与 Actor 授权 | Fully | commands 上游 Typed Payload 分支与既有 NPC grant，非虚构系统 Actor；撤销权限拒绝 |
| SM-11.2 终局/奖励归 Engine | Fully | `evaluate()` 原样求值；SM 未调用或重写终局/XP 规则算法 |
| SM-11.3 Closed Closure Delta 一致性 | Fully | `dnd_closure.py:closure_changes`；combat ID、ended、既有死亡、残余 HP/Temp HP/资源、XP Owner、事件与 RNG 校验 |
| SM-11.4 XP 增量、无二次支付、Loot 边界 | Fully | XP 只写 core row；既有余额只核对；上游 schema 禁止非空 loot；SQLite 整数溢出作为 invalid_rule_result 回滚 |
| SM-11.5 单事务提交 | Fully | `_submit_dnd` 同事务写 Delta/XP/ended/RNG/版本/Events/Receipt/Outbox；五个真实 SQL 故障点全回滚 |
| SM-11.6 关闭后新进程恢复/世界操作 | Fully | `_modern_resume_worker.py`、`_cross_phase_worker.py` 真子进程恢复，场景操作与检定继续 |
| SM-11.7 活跃战斗中断策略 | Fully | `process_id` guard、`_require_noncombat_save`；既有中断测试保留，不能强制结束或重初始化 |
| SM-11.8 胜利/TPK/非终局/错误/重复/故障 | Fully | `test_combat_closure`、跨阶段响应丢失测试及既有冲突/故障回归 |
| SM-12.1 CheckPayload 与原命令协议 | Fully | `commands.py` exact payload 类型、Fingerprint、原回执查询与 async 分支 |
| SM-12.2 裁决来源与 GM 权限 | Fully | `TrustedContext.can_adjudicate_checks`，默认 False，可信适配层授予；不从 payload 读取权限，不自行选 DC |
| SM-12.3 四种结果与独立故障 | Fully | 原 Engine Result 严格校验；规则 rejected/unsupported 在既有 Receipt rejected 分支保留原因码；Schema/Binding/runtime 独立；unexpected choice 不落回执 |
| SM-12.4 accepted 空 Delta/合法失败 | Fully | `dnd_checks.py:check_changes`；真实失败与成功都 committed，有一个 CheckRolled 与完整 RNGTransition |
| SM-12.5 原子 RNG/Event/Receipt/Outbox/版本 | Fully | 受限检定因 RNG 状态变化推进一次 world_version；event_seq 独立；SQL 故障/重试验证无重复掷骰 |
| SM-12.6 缺 DC 前置选择边界 | Fully | 求值前 `pending_choice_unsupported`，无 RNG/Event；新裁决用新 command_id；正式 PendingOperation 不实现 |
| SM-12.7 玩家/GM/NPC 读取隔离 | Fully | Session principal 校验、NPC grant、授权事件/回执；检定不生成机械详情 Signals/NPC Observation |
| SM-12.8 真实检定差分与恢复 | Fully | 成功/失败、模式、熟练/专精、缺 DC、授权撤销、错误 Result、冲突、故障、新进程均有数据库证据 |
| SM-13.1 同一角色事实与身份 | Fully | `test_complete_real_cross_phase_pipeline_and_new_process`，按 Actor ID 比较 Combat/NonCombat 状态，独立 SQL 审计 |
| SM-13.2 武器握持输入/输出 | Fully | rapier（另一手占用/空闲）、longsword 一/双手、greatsword 双手真实事件/Delta；dagger 明确 unsupported 无副作用 |
| SM-13.3 资源与 RNG 交接 | Partially | RNG 实际变化的连续性已证明；初始非空 spell_slots/custom_counters 余额保持。非战斗资源消耗未受支持，granted-die 请求显式 unsupported，无伪造资源变化 |
| SM-13.4 完整 E2E | Fully | Create→Explore→Check→World→Initialize→NPC/PC Attack→Close→Save→新进程 Resume→Explore/Check |
| SM-13.5 每阶段 SQL/版本/Event/Receipt/Outbox/Observation | Fully | `test_cross_phase.py:audit`、独立连接全表比较、事件顺序/Outbox 两消费者/私有 Inbox 断言 |
| SM-13.6 失败路径 | Fully | 重复、unsupported、错误 typed Result、XP、装备、SQL、响应丢失、重启、第二 Session、授权撤销，实际持久数据核验 |
| SM-13.7 F1–F5/R1 回归 | Fully | 全量历史测试保留通过，ProcessedDamage/DeathLedger 写范围未放宽；见第 5 节 |
| SM-13.8 文档/矩阵/迁移/示例 | Fully | README、四份批次说明、`examples/real_cross_phase.py`（测试真实运行并查数据库） |

没有缺失的受限关键路径；资源消耗型非战斗能力没有被计作 Fully，也未为此在 SM 补算法。

## 4. B1 与 F6 验收矩阵

**B1 — Resolved（本轮受限胜利/TPK）**：真实 Engine 返回 CombatClose；SM 原子落 ended/XP/CombatEnded 等；之后真正新进程可 resume、探索、检定。中断活跃 Combat 精确续玩仍是既定 Post-MVP，不属于 B1 被关闭的能力。

**F6 — 整体 Partially Resolved；下表 admitted slice 已对齐。**

| 契约项 | 状态 | 代码/测试证据与边界 |
|---|---|---|
| Request/Result Required、版本、四种结果、独立错误 | Resolved（slice） | 上游类型直接复用；verify_request、read_set、schema 验证；真实错误 Result/Binding/runtime/preflight 测试；未冻结 Meta 示例消息版本 |
| Combat/NonCombat 判别与完整机械事实 | Resolved（slice） | Snapshot V2、SQL builders、Required 字段与设备事实测试；不伪造 Combat 来掷非战斗检定；缺完整当前机械事实显式失败 |
| read_set/Binding/RNG 提交前核验 | Resolved（slice） | pinned Engine 要求唯一 whole-session fence；写事务重读版本、snapshot、RNG 与权限；Result 绑定/输入流验证；实际并发/重启/RNG 差分 |
| Closed Delta、前置值、归属与原子性 | Resolved（slice） | 原攻击 R1 验证器、独立 closure/check 验证器、XP 存储边界与一个提交器；合法 Schema 的故障注入、全 SQL 回滚 |
| needs_choice/PendingOperation | Partially Resolved | 缺 DC/缺武器选择执行前拒绝；unexpected choice 不持久为业务拒绝；正式可恢复 Continuation/PendingOperation 为 Explicitly Out of Scope |
| Synthetic 与真实 ABI 隔离 | Resolved（slice） | synthetic.atomic 独立绑定/类型/测试；真实路径直接使用 Engine DTO，不复制为合成公共协议 |
| 全机制依赖闭包/复杂组件/来源权限 | Explicitly Out of Scope | 完整 Reaction、effects、rules.effect、系统无 Actor 来源、Spellcasting/Save 等不在本轮；不得将受限对齐写成全部 F6 消失 |

## 5. 正确性、回归与问题分类

* F1：synthetic/real 部署绑定错误仍是独立错误，没有 receipt、RNG、Event；配置修复可用原命令重试。`test_mechanics.py`、`test_dnd_runtime.py`、`test_dnd_hardening.py`。
* F2：历史不可恢复 perception 边界依旧持久且可审计；新版本真实未完成 Work 不被跳过，Cursor 缺口仍阻塞交付。`test_migrations.py`。
* F3：user_version 仍在 BEGIN IMMEDIATE 后读；新库/旧库并发、中断与重复打开测试通过，未使用 IF NOT EXISTS 掩盖升级错误。
* F4：确定性 Event Cascade 业务拒绝使用原 savepoint 回滚后落拒绝回执；非业务故障不永久保存。真实 attack/world cascade 回归通过。
* F5：触发源关联精确已提交事件；历史不明来源继续明确审计。`test_event_traceability.py`。
* R1：新增 damage ledger target、death target/killer/location/kind 仍仅限授权攻击；历史账本保留。`test_dnd_ledger_scope.py` 的合法 Schema 注入回归全部通过。
* SM-13 修复的持久化边界：XP 超出 SQLite 整数可表示范围（单次增量或累计）现在独立拒绝，所有候选修改回滚，没有 REAL 值退化或 OverflowError 泄漏。
* SM-13 修复的测试证据盲点：此前部分新 fault tests 返回 JSON dict，严格 tuple schema 可提前拒绝。现在先构造 Schema-valid RuleEvaluationResult，再验证语义写范围与余额不变量；69 项真实 closure/check/cross-phase 组通过。旧证据没有被当作该层语义覆盖的充分证明。

最终未确认新增 P0/P1/P2。没有将明确 unsupported、OPEN 或缺少全面稳定版上游验收误报为已确认实现故障。

## 6. Persistence / Security / Recovery 限制

Schema 9 只增加 XP 与 snapshot schema 元数据；历史值默认 XP=0、机械 schema=NULL（旧 /1）。不改 Package Digest、RulesetBinding 或已提交历史。真实旧代码/锁定 /4 环境创建的 Schema 7/8 非空数据库升级后，逐列保存原 World/Scene/Actor/Character/RNG/Events/Receipts；迁移中断回滚和并发串行性通过。Schema 1–5/7 的既有迁移回归仍通过。

可信上下文由外部已认证适配层创建；本库没有互联网身份认证、GM 账号或令牌服务。GM grant 不能绕过 Session principal/player，NPC grant 撤销后不能 replay 该 NPC 命令。私有 NPC API 只读有权限 Inbox/View，无法调用全量 Session 读取；CheckRolled 不生成 NPC 机械细节信号。

只实现一个受审 authored Encounter。关闭后不能以初始化重置资源/角色；多 Encounter 启动仍后续工作。所有当前相关 Actor/Scene 都需要完整经审核机械事实；缺失时明确失败。未实现备份工具、硬件断电/磁盘破坏证明或活跃战斗跨进程精确续玩。

## 7. 执行的验证命令与数量

每个 Batch 均运行以下锁定同步、全套 pytest、Ruff check/format 和 Diff 检查。Windows 测试使用仓库忽略目录作为 pytest basetemp；真实 Engine asyncio 测试获准使用 OS 本地 socketpair，不调用外部规则服务。

```powershell
uv sync --locked --group dev
uv run --locked python -m pytest -q --tb=short --basetemp=.test-tmp/b10-full-final  # 326
uv run --locked python -m pytest -q --tb=short --basetemp=.test-tmp/b11-full        # 344
uv run --locked python -m pytest -q --tb=short --basetemp=.test-tmp/b12-full-final  # 380
uv run --locked python -m pytest -q --tb=short --basetemp=.test-tmp/b13-verified-full # 396
uv run --locked python -m pytest -q --basetemp=.test-tmp/global-review            # 396
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
```

Review 全量结果：**396 passed, 2 warnings, 129.82s**；Ruff 全通过；Diff 无错误。两条 warnings 是上游 lark 对 sre_parse/sre_constants 的弃用警告。无数据库、WAL/SHM、缓存、虚拟环境或候选源归档进入 Git。

上游独立证据：精确候选源的 test_evaluation_contracts、test_evaluation_review_findings、test_stateless_attack、test_stateless_closure、test_stateless_weapons、test_stateless_checks：**685 passed**。这不是整个 Rule Engine Full CI 的替代声明。

## 8. 剩余 MVP / OPEN / 合并建议

本轮减少了：完整机械事实初始化/重建、旧绑定边界、战斗终局落盘与安全普通恢复、受信通用检定、跨阶段 RNG 与幂等的一组真实集成工作。

仍需独立推进：生产 Host 的身份/裁决与交互、NPC EVA/Meta/Memory/Belief、真实 adventure 内容与导入审阅、完整 PC 构建验证、规则能力扩展、多 Encounter、资源/库存消费与正式跨领域效果、完整感知传播、保存备份与恢复产品流程。没有依据给出剩余工程百分比。

OPEN：正式跨模块 ABI 与字段冻结、一般 event-only/Meta-only 版本政策、无 RNG 消耗时通用流版本策略、跨版本 RNG 回放、PendingOperation/Continuation、复杂 Effect/Reaction Delta 与受信系统来源编码。上游候选的全面稳定版验收仍不在本次 SM 任务内。

建议人工审阅后合并**已测试的受限功能**；没有 blocking 代码问题要求先扩展范围。保持固定 SHA 和显式 unsupported 边界，不将本建议解释为完整 MVP、所有 F6 分支或上游全部规则已验收。按指令保留分支并停止，等待人工确认。

## 9. 实际修改清单

相对初始 main 的 32 个实现/测试/说明文件，加本 Review 共 33 个文件：

```text
README.md
pyproject.toml
uv.lock
docs/SM10_VERSIONED_STATE.md
docs/SM11_COMBAT_CLOSURE.md
docs/SM12_REAL_CHECKS.md
docs/SM13_SUPPORT_MATRIX.md
docs/reviews/SM_B10_B13_GLOBAL_REVIEW.md
examples/real_cross_phase.py
src/trpg_state_machine/_migration_9.sql
src/trpg_state_machine/availability.py
src/trpg_state_machine/combat.py
src/trpg_state_machine/commands.py
src/trpg_state_machine/dnd_checks.py
src/trpg_state_machine/dnd_closure.py
src/trpg_state_machine/dnd_runtime.py
src/trpg_state_machine/models.py
src/trpg_state_machine/package.py
src/trpg_state_machine/service.py
tests/_check_resume_worker.py
tests/_cross_phase_worker.py
tests/_dnd_legacy_worker.py
tests/_modern_resume_worker.py
tests/conftest.py
tests/modern_support.py
tests/test_combat_closure.py
tests/test_commands.py
tests/test_cross_phase.py
tests/test_dnd_hardening.py
tests/test_event_traceability.py
tests/test_migrations.py
tests/test_real_checks.py
tests/test_versioned_state.py
```
