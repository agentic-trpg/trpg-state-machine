# Agentic TRPG — SM-6～SM-9 Real Rule Engine Integration

## 一、任务目标

Repository：`agentic-trpg/trpg-state-machine`

工作分支：`feat/sm-b6-b9-real-engine-integration`

本轮目标：在 SM-1～SM-5 的权威状态、事务、Event/Outbox 和 NPC Perception 基础上，将真正的 D&D Rule Engine 的受限攻击求值能力接入 State Machine。

严格顺序执行：

1. SM-6：Cross-Repository Contract & Combat State Foundation
2. SM-7：Real PC Attack Evaluation & Atomic Commit
3. SM-8：NPC Combat Intent & Action Availability
4. SM-9：Integration Hardening & Global Review

每个 Batch 独立 Commit + Push。四个 Batch 完成后进行一次完整独立 Review，仍不 Merge main、不创建 PR。

## 二、最高优先级架构约束

1. 首先读取并遵循本地 `D:\Projects\agentic-trpg\agentic-trpg\docs\` 中的 ADR-001、MODULE_CONTRACTS、STATE_MACHINE_ARCHITECTURE、AGENT_ARCHITECTURE、ADVENTURE_PACKAGE_SCHEMA、MVP_SCOPE。

2. 同时检查本地 `D:\Projects\agentic-trpg\trpg-rules-engine` 和 GitHub 上 Rule Engine 的 `refactor/re-eval-04-npc-and-availability` 分支，重点检查真实 Evaluation、Snapshot、RNG、StateDelta、NPC Intent、Availability API 和测试。

3. 当前已发现 Rule Engine 的候选实现 Commit 为 `a7225ebaa4914f6db4ae72f3f6454c355ea7baac`。开始开发前重新确认其状态；若尚未合并 main，使用该固定 SHA 建立可复现的测试依赖，不得默默改用旧 main、追踪浮动分支或直接修改 Rule Engine 仓库。审查候选接口兼容性，发现阻塞性冲突则报告并停止相关开发。

4. State Machine 是 World / Character / Combat / RNG / NPC State 唯一权威拥有者。Rule Engine 仅在写事务外使用明确 Snapshot 和 RNG 求值，返回非权威结果；不得在 SM 中保留第二个 `_LiveCombat` 权威实例或重复实现 D&D 公式。

5. 必须区分 `[DECIDED]`、`[PROPOSED]`、`[OPEN]`、`[NOT IMPLEMENTED]`。不得为了使测试通过而静默冻结尚未批准的跨模块契约、伪造机械能力、添加完整 Snapshot 覆盖更新或绕过权限。

6. 优先复用既有 Session、Command、Receipt、SQLite、Migration、Events、Outbox 与 Perception。禁止重复建设已存在的机制；尽量用封闭类型和必要的真实持久化扩展实现。

---

## 三、Batch SM-6 — Combat State Foundation

**目标：从 SM 权威持久化状态构建真实 Rule Engine 所要求的完整受限 CombatSnapshot。**

1. 对照上游 `evaluation_state.py`、`evaluation_contracts.py`、`evaluation_delta.py`、`evaluation_rng.py`，建立严格的字段与能力映射，列出 Required、可推导、暂不支持的状态，不得默默填充缺失的机械信息。

2. 扩展必要的 CharacterState / CombatState / SceneMechanicalState 持久化，支持受限基础攻击所需的 Actor、HP、行动经济、位置、回合、装备、RNG 及相关机械组件。明确拒绝无法完整表示的 Effect/Reaction/Area 等机制。

3. 使用真实规则数据绑定构建原创 D&D 合成测试 Encounter，固定实际 RulesetBinding / DataRevision / EvaluatorVersion。不得直接复用 `synthetic.atomic` 冒充真实 D&D 规则。

4. 从 SQLite 读取版本化权威状态并构建完整 CombatSnapshot；RNGContext 作为独立输入。必要的 Combat 初始化只能通过可信、受限的 SM 路径完成，不得借测试辅助接口引入第二份权威状态。

5. 提供向上游真实 Pydantic Schema 的严格验证和序列化测试。保持旧 Session、旧 Package、合成规则测试及 Schema 1–6 数据兼容。

6. 确保新增数据库 Schema Migration 可恢复、可审计，禁止通过破坏旧数据或直接整体覆盖 Session Snapshot 实现。

7. 完成测试，至少验证完整 Snapshot 重建、缺字段拒绝、RulesetBinding 不匹配、RNG 独立、重启后 Snapshot 一致及旧存档迁移。

8. 本轮不要求执行真实 Attack，不实现完整 Encounter、法术或复杂 Reaction。若无法构建完整受限 Snapshot，应明确报告阻塞，不能制造简化的伪 Snapshot。

完成后运行全部测试、Ruff 和 Diff 检查，并执行：

`git add .`

`git commit -m "feat: add authoritative combat snapshot foundation"`

`git push origin HEAD`

确认成功后进入 SM-7。

---

## 四、Batch SM-7 — Real PC Attack & Atomic Commit

**目标：真正调用上游 D&D Rule Engine，完成一次 PC 基础攻击的合法求值和原子提交。**

1. 在 SM-6 的完整 Snapshot 基础上，实现真实 `combat.intent` TypedCommand 与上游 RuleEvaluationRequest 的适配。Actor 身份来自可信 SM 授权，不允许请求自行冒充其他 Actor。

2. 调用上游真实 `evaluate()`，使用当前 Session 固定规则数据、完整 CombatSnapshot 与独立 RNGContext。不得调用 Legacy Registry-Backed Combat 作为权威。

3. 对返回的 accepted/rejected/unsupported/needs_choice 及 Schema/Ruleset/运行时错误作准确区分；没有合法 PendingOperation 时，必须采用明确的受限能力拒绝，不得将 needs_choice 伪装为已执行行为。

4. 验证完整 Typed StateDelta，包括 HP、Temp HP、行动预算和该受限攻击实际改变的全部受支持机械状态。提交时验证 read-set、版本、Actor/Target、预期值、权限和相关不变量，不允许整份 Snapshot 覆盖。

5. 在一个 SQLite 事务中提交机械状态、RNGTransition、正式 WorldEvent、CommandReceipt 和可靠 Outbox。Engine ProposedEvents 不具有正式 event_id，不得直接发布给 NPC 或 Narrator。

6. 验证命中、未命中、合法攻击消耗、伤害减免/免疫（上游支持范围内）、资源与行动预算、不合法目标、规则不支持和版本竞争。未命中仍可能是合法 accepted，不得混同 rejected。

7. 测试同一 Command ID 重试、并发攻击、响应丢失、Evaluator 异常、错误 Delta、SQLite 故障、重启恢复，确保规则求值不会重复提交或重复推进权威 RNG。

8. 与原 Rule Engine 的对应受限基础攻击结果进行差分测试，验证 StateDelta 应用后的状态及 RNG 后继与上游实际求值一致，不能仅测试调用成功。

9. 保留合成执行路径和既有全部回归。不得自行加入尚未支持的完整回合循环、攻击附加效果、复杂武器特性、施法或 Reaction。

完成后运行完整验证，并执行：

`git add .`

`git commit -m "feat: integrate real dnd attack evaluation and atomic commits"`

`git push origin HEAD`

确认成功后进入 SM-8。

---

## 五、Batch SM-8 — NPC Combat Intent & Action Availability

**目标：使 PC/NPC 共用真实机械攻击路径，并提供不改变状态的行动可用性查询。**

1. 复用 SM-7 的真实 Rule Evaluation/Atomic Commit，不为 NPC 复制一套攻击或骰点逻辑。

2. 支持受限敌方 NPC 的显式 Typed Combat Intent。NPC 具有真实 actor_id、可信控制 Grant、完整机械状态和规则绑定；Agent DM 不代替 NPC 私自生成个人决策。

3. 在上游支持范围内接入 NPC 普通武器攻击及精确 stat_block_action_id 的基础攻击。未知 Action、Multiattack、特殊 Recharge、Spellcasting 等机制必须明确 unsupported。

4. NPC 攻击和 PC 攻击使用相同授权、版本、StateDelta、RNG、CommandReceipt、Outbox 机制，不能直接改 HP 或内部 CombatState。

5. 接入上游 `query_action_availability` 的受限只读路径。查询必须验证可信 Actor、完整 Snapshot、RulesetBinding，并经授权过滤原因；不得掷骰、消耗 RNG、修改状态、产生 Delta/Event/Receipt。

6. Availability 的 available/unavailable/unknown 严格区别；available 仅表示当前 Snapshot 下可执行，正式 Command 必须重新鉴权并验证版本。

7. 测试敌方 NPC 攻击 PC、PC 攻击 NPC、NPC 错误控制主体、被撤销 Grant、行动预算不足、未知 stat-block action、重复命令以及查询与执行间的版本变化。

8. 保持 NPC Combat Intent 与 NPC Perception/EVA 分离。当前仅提供可信提交接口，不实现 LLM NPC 战术决策、Interactive/Background EVA 或 Gate。

完成后运行完整验证，并执行：

`git add .`

`git commit -m "feat: support npc combat intents and action availability"`

`git push origin HEAD`

确认成功后进入 SM-9。

---

## 六、Batch SM-9 — Integration Hardening

**目标：加固 PC/NPC 真实 D&D 攻击的纵向闭环，并解决影响后续集成的剩余问题。**

1. 完整复核 SM-6～SM-8 的真实规则执行链，确认 SM 是唯一权威且无 Legacy Registry / 第二份 Combat State。

2. 增加完整的原创合成 Encounter 纵向测试：创建 Session → 初始化受限 Combat → PC/NPC Typed Attack → Rule Evaluation → Atomic Commit → WorldEvent / Outbox → NPC Perception → 保存与新进程恢复。

3. 加强跨模块故障测试：版本竞争、请求重试、错误规则绑定、RNG 状态冲突、SQL 回滚、失效 Actor、重复事件和恢复后重新提交。测试必须直接核对 SQLite 权威状态及回执。

4. 对齐受限真实 RuleEvaluationRequest / Result、Typed Delta、read-set、RNGTransition 与既有契约，不把本地 Synthetic 类型扩张为第二套正式机械协议；避免不必要的统一大重构。

5. **F6 — 跨模块 Rule Evaluation Contract 对齐与验收**

   对照 Meta Repository `MODULE_CONTRACTS.md` §4–§6，以及 Rule Engine 已实现的真实 Evaluation API，逐项检查并对齐：

   1. `RuleEvaluationRequest / Result` 的 Required 字段、Schema Version、四种结果与独立错误类别。
   2. `CombatSnapshot / NonCombatSnapshot` 的类型判别、必要机械状态和版本信息。
   3. `read_set`、RulesetBinding、RNGContext / RNGTransition 的一致性及提交前重验证。
   4. `StateDelta` 的封闭类型、操作前置值、Actor/资源归属和事务原子性。
   5. `needs_choice` 与 `PendingOperation`：未实现正式 Continuation 时，必须在执行前明确拒绝不支持的能力，不得把已经发生的部分求值伪装成正常拒绝，也不得产生部分机械副作用。
   6. Synthetic Evaluator 与真实 D&D Evaluator 必须明确隔离，不能让合成协议成为第二套正式公共 ABI。

   最终在 Global Review 中建立 F6 契约逐项验收矩阵，标记 **Resolved / Partially Resolved / Blocked / Explicitly Out of Scope**，并提供相应代码及测试证据。

   不得为了关闭 F6 而实现完整 Reaction、复杂 Continuation 或超出本轮 MVP 基础攻击范围的机制。

6. 修复旧审查遗留的 F5：Trigger History 准确关联触发它的 CommittedWorldEvent，而不只是 source_command_id。保留去重、触发顺序及 Schema Migration 兼容。

7. 检查 NPC Observation 是否仍严格基于已提交可感知 Signals，不能直接看到 Engine 内部秘密、失败 Action Intent 或未提交事件。

8. 若部分 D&D 机制、Combat 继续操作或完整 PendingChoice 尚未支持，明确报告并拒绝执行，不扩张到不完整的全战斗实现。

9. 更新 README 和必要的 Batch 文档，标明真实支持的动作、规则版本、限制与测试证据，不夸大为完整 D&D Engine 集成。

---

## 七、Global Review — 四轮整体审阅

SM-9 开发结束后，进行一次独立的全局代码审阅，覆盖 SM-1～SM-9 的全部现有能力。

重点检查：

1. **Implementation**：核实四个 Batch 的真实代码路径，是否存在空接口、虚假集成或遗漏的状态字段。
2. **Correctness**：验证真实 D&D 规则语义、HP/资源、行动经济、RNG、Delta 和合法失败处理。
3. **Architecture**：验证 ADR-001 单一权威、SM ↔ RE 模块职责、Schema 和权限边界。
4. **Atomicity / Recovery**：检查 SQLite 事务、Receipt 幂等、事件顺序、Outbox、Migration 和崩溃恢复。
5. **Regression**：运行全部既有测试及新增真实跨模块测试，检查旧 Synthetic Foundation 和 Legacy Session 的兼容性。
6. **Security**：检查 NPC 控制权限、Snapshot 隔离、WorldEvent / Observation 信息泄漏。
7. **Progress**：识别真正减少 MVP 工作量的实现，以及仍需真实 Engine、Host 或 NPC EVA 配合的能力。
8. **Defects**：区分 Confirmed P0/P1/P2、潜在风险、未冻结 OPEN 和明确不支持的功能，不以测试通过代替实现证据。

生成：

`docs/reviews/SM_B6_B9_REAL_ENGINE_REVIEW.md`

包含各 Batch Commit SHA、测试结果、实际支持能力矩阵、跨仓库接口差距、问题清单、下一步开发顺序及是否建议 Merge。

审阅默认只读业务代码，不自动进行大规模修复；发现阻塞性问题则记录并建议单独 Fix Batch。

Review 完成后单独执行：

`git add docs/reviews/SM_B6_B9_REAL_ENGINE_REVIEW.md`

`git commit -m "docs: review real rule engine integration batches sm6 through sm9"`

`git push origin HEAD`

---

## 八、全局执行纪律

每个 Batch 必须先运行：

```powershell
uv sync --locked --group dev
uv run --locked python -m pytest -q
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
```

若新增真实规则依赖需要额外的集成环境，必须明确记录固定 Commit、安装命令和实际执行的跨仓库测试，不允许仅使用 Mock 或跳过测试宣称成功。

严格保持：

- 每个 Batch 独立 Commit + Push。
- 四个 Batch 顺序执行。
- 测试失败或关键依赖缺失时停止，不能带病进入下一轮。
- 不修改其他 Repository。
- 不 Merge main，不创建 PR。
- 不把受限真实基础攻击宣称为完整 Combat / D&D MVP。
- 不重新设计已有 Gate、EVA、Memory、Perception 架构。

最后报告全部 Commit SHA、Push 状态、测试、实际集成程度、OPEN / Blocked，以及：

`git status -sb`

`git log --oneline origin/main..HEAD`

完成四轮与 Global Review 后停止，等待人工审阅。