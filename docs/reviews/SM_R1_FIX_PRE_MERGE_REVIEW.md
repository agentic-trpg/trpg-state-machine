# R1 Fix & Pre-Merge Review

日期：2026-10-10。分支：`feat/sm-b6-b9-real-engine-integration`。
修复基准：`4aa696d345905aa4dd0a2da093f823a5c592dc63`。

**R1 Fixed。新增 40 项、全量 313 项测试通过。针对本次 Diff 未确认新的
P0/P1/P2；建议人工审阅后合并受限真实基础攻击集成。B1 继续 Blocked，
F6 整体继续 Partially Resolved；不能据此宣称完整 SM-9 或正常战斗结束后安全续玩通过。**
本轮不 Merge、不创建 PR，仅修改 State Machine 仓库。

## 1. 依据与审阅范围

- 开发前读取本地 Meta 文档 ADR-001、MODULE_CONTRACTS §4–§6、
  STATE_MACHINE_ARCHITECTURE；Accepted ADR 和已确认约束优先。
  SQLite 仍是唯一权威状态，Engine 仅产生候选 Typed Delta。
- 核对原 Global Review 的 R1/F6/B1，以及完整
  `delta_changes → validate_dnd_result → _submit_dnd → _apply_dnd_changes`
  路径。另外读取锁定 Engine 的 Delta、Projection、Context 和死亡记录实现，
  以实际合法 Result 判断身份约束，未在 SM 重算伤害、死亡或结束条件。
- Engine / SRD 继续固定 `a7225ebaa4914f6db4ae72f3f6454c355ea7baac`；
  pyproject.toml、uv.lock、SQL Schema、公开接口及事务实现均未修改。
- 实现和测试完成后，单独进行最终 Diff 的只读审阅；这是一次针对性审阅阶段，
  不宣称另一个人或外部服务已签署独立审阅。

## 2. 根因、修复和相邻不变量

实际业务修改仅在 `src/trpg_state_machine/dnd_runtime.py:delta_changes()`。

| 项目 | 修复前 | 修复后及证据 |
|---|---|---|
| R1 ProcessedDamageUpdate | 只检查 value 中 Actor 属于 roster，允许向攻击者或其他 NPC 添加记录；也把旧历史重新按当前 roster 校验 | 保留 expected 子集约束，仅检查 `value - expected` 的 target 等于请求 target；旧历史保持原值，不重新授权 |
| DeathLedger 授权 | roster 内无关 Actor 可进入死亡账本，未约束新增记录归因 | 新 dead_ids 差集和 records 后缀都限本次 target；新记录 killer 等于攻击者、location 等于当前 scene、kind 等于目标实体类型 |
| DeathLedger 历史和完整性 | 已有历史前缀不可改，但新增 ID 与记录可不配对 | 保留集合单调和精确历史前缀；新增记录与新增 dead_ids 一一对应，拒绝缺失、孤立及重复记录；旧场景/旧参与者历史不因本次攻击被拒绝 |
| TurnUpdate 内部 Actor 引用 | combat_id 校验无法发现内部引用无关 Actor | 只对改变后的非空 monster_turn_start_done、last_ended_turn、departed_actor_id 校验 Actor 属于本次作用范围（攻击者、声明目标、候选下一回合 Actor）；未变的旧引用保留 |
| TurnUpdate ended | 合法 Schema 可携带伪造 ended=true | 基础攻击切片明确拒绝 ended 改变；不自行计算结束条件，B1 保持阻塞 |
| 合法目标预算更新 | NPC 击倒 PC 时，若下一回合是第三 Actor，目标 movement_remaining=0 的 ActionBudgetUpdate 被误拒绝 | 仅 ActionBudgetUpdate 的允许 Actor 增加声明目标；MovementLedgerUpdate 仍限攻击者/下一回合 Actor。真实三 Actor NPC→PC 击倒攻击验证成功 |

所有上述范围违规均抛独立 `invalid_rule_result`，不变成业务 rejected。
`validate_dnd_result()` 继续在求值后、写入前执行；写事务中通过 Snapshot/RNG/OCC
重验后再执行一次。校验失败不会进入 `_apply_dnd_changes()`，也不会保存回执。
合法状态、RNG、Events、Outbox、Receipt 仍在原有同一短事务提交；没有新服务、
状态模型、ACK、整份 Snapshot 回写或第二份权威状态。

同一 Delta 其余封闭操作已核对并注入测试：HP、Temp HP、DeathState、Conditions
限声明 target；DamageAttribution 的目标和来源受约束；ActionBudget/MovementLedger
受 Actor 范围约束；Combat 操作受 combat_id 约束，所有写入保留前置值和重复字段校验。
这些检查验证写入许可与机械不变量，不证明 Engine 的所有 D&D 计算均正确。

## 3. 真实 Result 和 SQLite 测试证据

新增 `tests/test_dnd_ledger_scope.py`，共 **40 项参数化实例**：

- 8 项合法场景：PC→NPC 普通伤害、PC→NPC 致死、NPC→PC 击倒、NPC→PC
  instant_kill 致死，各自分别有/无旧历史。真实 `dnd5e_engine.evaluate()` 的
  accepted Result 实际提交；验证完整前后 Snapshot Delta、RNG successor、world_version、
  Damage Sequence、适用的 ProcessedDamage / DeathLedger 更新和重启后的持久回放。
- 19 项账本故障：ProcessedDamage 添加自身/其他 NPC/不存在 Actor、删除旧项；
  DeathLedger 未授权新 ID/目标，错误 killer/location/kind，历史删除/篡改，
  重复记录或缺少配对 ID/记录。故障基于真实合法攻击 Result 修改，
  再通过上游 `RuleEvaluationResult.model_validate_json()`，确保是合法 Schema 的恶意候选。
- 13 项相邻故障：HP、Temp HP、DeathState、Conditions、DamageAttribution
  目标/来源、ActionBudget、MovementLedger、combat_id，以及三个内部 Turn Actor 引用
  和 ended 改变；同样对真实 Result 注入，不构造替代 Evaluator 业务逻辑。
- 每项非法候选连续执行两次同一 command_id，必须得到 `invalid_rule_result`。
  每次通过**独立 SQLite 连接读取全部持久表并逐行比较**，验证 HP/机械组件、RNG、
  world_version、两份账本、正式 Events、Outbox、Receipt、Evidence/Perception/Inbox
  均不变，同时检查外键和回执不存在。修复返回结果后原请求和原 command_id 提交成功，
  重复提交及关闭/重建运行实例后的回放不产生新副作用。
- 旧合法历史包含另一 Actor 的 processed damage，以及不在当前 encounter、属于另一
  scene、killer 为另一 Actor 的死亡记录；当前合法攻击仍成功并保留这些历史原值。

修复前的真实执行已复现 `processed_self`、`death_killer_other`、`death_no_record`
三项本应抛错却提交的失败。相邻 Turn 引用及 ended 故障也在收紧前实际复现。
三 Actor 的合法 NPC 击倒目标预算更新在修复前实际失败，修复后通过。
没有以字符串断言、仅检查内存或 Mock 调用次数替代数据库证据。

## 4. 完整验证

原有 273 项测试保留，新增 40 项，**313 passed, 2 warnings in 89.67s**，无 skipped。
两项警告来自既有 lark-parser 的 sre_parse / sre_constants 弃用导入。
针对新测试、test_dnd_runtime、test_dnd_hardening 的检查为 **80 passed**。
完整回归包括 SM-1～SM-9 的 NPC 授权、Temp HP、故障回滚、并发 Migration、
真实旧库升级、RNG、重启审计、Event/Observation 和 F5 来源关联测试。

实际执行命令：

```powershell
$env:UV_CACHE_DIR = "$PWD/.uv-cache"
$env:PYTHONUTF8 = "1"
uv sync --locked --group dev
uv run --locked python -m pytest -q --tb=short --basetemp=.test-tmp/r1-full
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
```

Locked sync、Ruff check、format check（48 文件）和 Diff check 均通过。
Windows 沙箱限制 asyncio 本地 socketpair，真实 Engine 测试在允许该 OS 功能的
环境执行；未使用网络规则服务。临时日志/数据库保留在忽略的 `.test-tmp/`，
Git 文件检查无测试 DB、WAL、缓存或上游临时 checkout。

## 5. 兼容性、剩余依赖与合并意见

| 验收项 | 当前状态 | 边界 |
|---|---|---|
| R1 新伤害账本项写入范围 | Resolved | 仅声明 target 可新增，违规无持久副作用，可原 ID 重试 |
| F6 受限基础攻击 StateDelta 身份/账本授权 | Resolved | 封闭类型、预值、目标/归因和原子性；不覆盖未支持规则机制 |
| F6 整体跨模块契约 | Partially Resolved | 原矩阵其余结论保留；正式共同 wire Schema、NonCombat、复杂 Continuation 等未冻结/未实现项不能因 R1 修复而关闭 |
| B1 正常结束后安全续玩 | Blocked | 固定 Engine 缺正式 stateless combat.end；拒绝伪造 ended，活跃战斗跨进程新命令继续明确阻塞 |
| PendingOperation / 完整 Reaction / Spellcasting | Explicitly Out of Scope | 本轮不新增；仍按原前置能力策略拒绝不支持的请求 |
| F5 Event 来源关联 | Resolved（新记录） | 原实现/Schema 不变，完整回归通过；不可恢复的旧来源仍明确标记 |

没有 Schema Migration 或数据重写；正常旧库按既有迁移流程读取。本轮不会追溯修正
之前若已提交的非法历史，也不会借当前攻击删除、重解释旧账本。既有持久 Receipt
继续遵循原幂等回放协议；本次新提交的非法 Result 不会形成永久业务拒绝。

针对本次 Diff 未确认残留 P0/P1/P2。可以建议**仅将受限真实基础攻击集成**提交人工
合并审阅；B1 不属于已交付能力，完整 SM-9 和战斗结束后 save→restart→resume
仍未通过验收。NPC EVA、Memory/Belief、完整感知传播、全机制规则适配及已列 OPEN
保持原范围，不因本轮建议而成为已实现能力。提交和推送后停止，等待人工审阅。
