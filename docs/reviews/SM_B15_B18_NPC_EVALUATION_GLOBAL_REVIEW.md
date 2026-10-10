# SM-15–SM-18 NPC Evaluation Global Review

审阅日期：2026-10-11。分支：`feat/sm-b15-b18-npc-evaluation-runtime`。

**结论：受限 NPC Evaluation 领域闭环已通过真实 SQLite 与跨进程验收；发现一项尚未修复的 P2（GR-01），没有确认新的 P0/P1。建议先完成 GR-01 的小范围修复与回归，再合并。当前不建议直接 Merge；也不将本轮称为真实 Host/LLM MVP。**

## 1. 基线、独立性与实际范围

- 起点：已合并 SM-14 的 `277229f970b89f05966d15e2de5bf4ce97d1ef65`。
- 审阅代码：四轮独立 Commit + Push 后的 `c0924c9f815e92cbf06088abe47ce391a8ecfd7a`。
- 再次从 Meta remote main 读取，仍为 `fc41d3c584816198ba4f23bb4d326655ea3c774d`。依据 MODULE_CONTRACTS §3/§7/§9/§10，STATE_MACHINE_ARCHITECTURE §3.4/§3.7–3.9/§6/§8，AGENT_ARCHITECTURE §4–§6/§8–§10，ADVENTURE_PACKAGE_SCHEMA §5/§9 和 MVP_SCOPE。
- Module Contracts `[DECIDED]` 职责为最高约束；§9.2–§9.7 具体接口/生命周期及本地 Schema 仍属 Proposed。本轮没有修改或冻结 Meta 契约，没有使用已删除 ADR 覆盖最新要求。
- “独立”指四轮推送结束后，对固定提交重新开展只读代码/SQL/测试审阅和全量回归；不是另一个人工审阅者或另一模型的背书。本阶段没有修改业务源码、测试或 README，仅新增本报告。
- 审阅前业务 Diff 为 29 个文件：四个运行时模块、Migration 11–14、原有权限/初始化/Perception/Command 的小范围接入、四组测试和三个子进程/测试辅助文件、批次文档与 README。`pyproject.toml`、`uv.lock`、`dnd_runtime.py`、`mechanical_runtime.py` 均无 Diff；Engine/SRD 固定依赖未变。

## 2. 五阶段完成矩阵

Fully 只指本任务明确受限的 State Machine 领域范围；不代表完整游戏机制或外部 Host 已交付。

| 阶段 | 状态 | 实际能力与证据 | 差距 |
|---|---|---|---|
| SM-15 Task / Work | Fully | `npc_evaluation.py:95–269`；Migration 11；真实授权输入、持久请求幂等、唯一 running Task、观察溯源、进程中断恢复；17 项新增测试 | 本地 Schema 待跨模块批准；没有模型执行 |
| SM-16 Atomic Meta / Receipt | Partial | `npc_results.py:265–391`；Migration 12；五类历史/来源/版本、一个最终 Result、输出/回执原子提交、重复与纠错；33 项新增测试 | GR-01：非法 UTF-8 候选未转换为结构化验证错误，修正预算漏计 |
| SM-17 Mode / Gate | Fully（有 GR-01 的输入预算关联风险） | `npc_modes.py:39–286`；Migration 13；Interactive 独占、两向原子取消替换、Inactive Gate、有界失败、串行竞争；18 项新增测试 | 三次预算/分类枚举是本地政策；Host 物理重试未实现 |
| SM-18 Output / Completion | Fully（受限命令子集） | `npc_outputs.py:25–248`；Migration 14；稳定独立 Command、Dialogue 授权、原回执对账、连续前缀、真实进程退出恢复；33 项新增测试 | Action 仅映射已有 `move_actor`；Dialogue 仅同 Scene 全体受众或空受众 private |
| 独立 Global Review | Fully | 固定四轮提交重读调用链/SQL/Diff，563 项完整回归、静态检查、额外恶意文本实库探针，本报告独立提交 | GR-01 只报告，不在 Review 阶段修业务代码；合并等待人工审查和后续小修 |

没有用类型定义、Mock、跳过测试或文档声明代替上述可运行能力。

## 3. 调用链、权限与事务审阅

| 检查领域 | 实际实现及测试证据 | 审阅结论 |
|---|---|---|
| Task 身份与授权 | `_evaluation_row` 重验 NPCContext、Session/Package pins、当前 `npc_access` 与 Task 绑定 principal；`_evaluation_actor` 拒绝 unconfigured；`test_task_request_conflict_active_and_revoked_access`、双 NPC/Session 测试 | evaluation_id 不是凭证；撤销后读取/提交拒绝；未复制 NPC Identity |
| 输入可见性 | `get_npc_evaluation_task` 在一致读事务返回完整授权 NPCView 与该 Task 的观察；事件/Signal 溯源留内部；`test_task_full_input_repeat_restart_and_internal_provenance`、`test_two_npcs_and_sessions_cannot_use_foreign_observation_or_memory` | 不返回 DM Secret、他人私有 Meta 或隐藏事件语义；没有检索/Ranking/压缩 |
| 创建幂等与唯一任务 | `(session,npc,principal,request_id)` + 稳定内容指纹；部分唯一索引限制一个 running Task；Task 与 Work 关联同事务；四连接并发与 `os._exit(23)` 插入中断测试 | 不以 Invocation/Context 充当领域幂等键；取消后的旧身份不会复活 |
| Meta 结构与历史 | `_validate_meta` 使用封闭 SM-14 记录类型；下一版本、唯一 current、来源范围、Plan→Goal；Memory 来源必须已提交且属于同 NPC/Session；五类联合提交/非法字段/九个 SQL 故障点测试 | 旧 Belief 历史不改写；错误 Belief 不修改 World Fact；不做语义真实性验证；GR-01 例外见下 |
| 原子 Result 与回执 | `submit_npc_evaluation_result` 在一个 BEGIN IMMEDIATE 中验证任务/版本、追加五类 Meta、选择 Result/指纹、写可靠输出/Receipt、完成 Task/Work/Cursor；相同已选结果返回原回执，不同结果冲突 | SQL 故障回滚 current markers、NPC version、Result、输出与完成状态；存储故障不消耗修正预算 |
| Mode / Gate | `_switch_mode` 原子取消旧任务、保留 Meta/Work、切换并新建；`_evaluation_mode_check` 阻止直接 Background；Idle Interactive 仍独占；Gate safe/no-op 只有审计/Work，无主观 Meta；四次真实双连接 Commit/Switch 竞争与两向切换测试 | 不等待物理调用/失败回执/ACK；先提交者由 SQLite 写锁决定；新观察不修改在途请求输入 |
| 失败与重试 | `report_npc_evaluation_failure` 用 expected_attempts 去重；一次失败保持 running，三次耗尽；非法 Meta 可以在预算内修正；failed/cancelled/completed 不接受新 Result | 正常结构化错误路径成立；非法 Unicode 在 GR-01 中漏出该协议。相同非法 Result 重新提交本地视为新失败尝试，非已选 Result 幂等回执 |
| Action 交接与执行 | 每个获准 intent 保存独立 `command.npc.*`；`dispatch_npc_output` 释放交接事务后走原 `submit_command`；`commands.py:204` 在世界写锁内重验来源/授权；撤销间隙、冲突、SQL 回滚、四连接并发测试 | 无第二执行器/CommandReceipt；View/Task 权限不等于控制权；Meta 完成不意味着动作成功 |
| Dialogue | `_publish_npc_dialogue` 重验来源、当前 Scene 和完整受众；private 空受众不产生事件；发布原子写 Event/Evidence/Signal/Outbox/status；五处 SQL 失败和并发发布测试 | 只证明台词被说出，不证明命题为真；拒绝远程/子集/不支持的 private 受众，不提前公开候选内容 |
| Observation Completion | Result 可靠记录交接后，同事务完成关联 Work；safe/no-op 同事务完成；`_refresh_npc_completion` 上限是 Perception 连续前缀，遇最早 pending Observation 截止；真实双事件投影/认知缺口测试 | 读、模型返回、取消不能完成；失败/等待的独立 Command 不重开已完成 EVA；没有 ACK |
| 故障恢复 | 实际新解释器读取 Task/Meta/Mode/Receipt/Cursor；在 Task 创建、Meta 提交中退出恢复；Command 已提交、交接写回前退出，再次派发得到原 Receipt | 不重复认知、移动或奖励；没有 Host 影子状态接管。活跃战斗精确续玩仍为既有 Post-MVP 限制 |

所有新写入使用既有 SQLite/WAL、foreign_keys 和事务边界。调用中没有 LLM、网络、Rule Engine 求值或 Host 模型等待位于新写事务中；分类候选与手工 Result 的业务验证在本地执行。JSON/文本数量和大小受限，但生产压力/p95/恶意超大对象构造未做性能验收，不宣称具有生产容量指标。

## 4. SQLite Migration 与兼容性

| Schema | 新增/修改 |
|---|---|
| 11 | 新增 `npc_observation_work`、`observation_signal_sources`、`npc_evaluation_tasks`、`npc_evaluation_observations`；Work 只引用既有 Inbox，不复制 Observation；仅从已保存的 positive judgment/Signal 回填溯源 |
| 12 | 重建既有 `npc_record_sources` 并保留旧列/行，新增 generated Observation FK；新增 `npc_evaluation_receipts`、`npc_record_evaluations`、`npc_output_handoffs` |
| 13 | 新增 `npc_modes`、`npc_mode_requests`、`npc_routing_requests`、`npc_gate_decisions`；优先按旧 running Task 恢复模式，否则采用最新 Task 模式；无任务为 inactive |
| 14 | 新增 `npc_completion_cursors`、交接身份/终态不可变触发器；只为已有 completed Task 且存在真实 Receipt 的 Work 回填 cognition completion |

共新增 12 张表；没有第二份 NPC Meta、Inbox、World/Combat/RNG 或 CommandReceipt。现有 NPCRuntime 在原 Scene/Session 初始化事务中创建唯一 Mode/Cursor，不在重访/移动/恢复时重新初始化认知。

`service.py:168–221` 保留跨进程写锁内重读 user_version、连续执行所有迁移、FK 检查后统一提交、失败统一回滚；没有以 IF NOT EXISTS 掩盖中断。Schema 1–9 既有真实旧代码建库/并发/中断测试保留；新测试用 `git archive` 的 SM-14（Schema 10）和 SM-15/16/17（Schema 11/12/13）代码实际建库，再升级并比较旧 World/Scene/Character/RNG/Receipt/Event/Meta/Task/输出行与外键完整性，重复打开不重跑升级。

Schema 14 对 Work 的预期变化独立检查；其余旧表比较保留。`stored_rows` 改用 table_xinfo，是为了把 generated provenance column 纳入实际行比较；没有删数据以满足测试。历史 latest-schema 断言由 10 改为 14，其他旧状态/事件/RNG 预期保留。

Legacy Perception 的既有可审计边界保留，不补造历史 Evidence/Signal/Observation；对未知感知不声称 false。旧未配置 Profile 不获得自主评价能力。Package pins/digest 与 RulesetBinding 不静默升级，静态 Package 不随 Meta 更新写回。

## 5. 实测发现：GR-01 / P2 / Open

**未配对 Unicode surrogate 绕过 Result 结构化错误及失败预算。** 位置：`src/trpg_state_machine/npc_results.py:93–98`、`:287–292`。

`canonical_json(asdict(result))` 使用 ensure_ascii=False，Python 字符串可以含 `chr(0xD800)`。该数据是 JSON 转义字符串可解码出的候选，不能假定 Host 永不传入。`_result_json` 捕获 TypeError/ValueError/RecursionError，但 UTF-8 `.encode()` 位于其捕获范围之外，直接抛 UnicodeEncodeError；外层只捕获 StateMachineError，因此没有 `invalid_npc_result`，也不记录失败尝试。

2026-10-11 在固定审阅提交上执行一次独立 `uv run --locked python -` 探针，使用原创 fixture、真实授权 Task 和临时 SQLite，而非 Mock。复现核心步骤：

```python
# 使用 tests/test_npc_evaluation_tasks.py 的 evaluation_package/prepare/create_task
# 与 tests/test_npc_results.py 的 full_result，创建真实数据库和授权上下文。
good = full_result(sm, sid, package, npc, observation_ids)
bad = replace(
    good,
    memory_updates=(replace(good.memory_updates[0], text=chr(0xD800)),),
)
sm.submit_npc_evaluation_result(task.evaluation_id, bad, package, context=npc)
```

实际输出/断言：

```text
exception=UnicodeEncodeError; domain_code=null
all SQLite table rows unchanged=true; task.attempts=0
corrected same evaluation_id -> completed; npc_state_version=2
```

影响：非法模型候选无法通过统一结构化反馈纠错，重复发送不计本地失败预算；调用方可能将其误判为运行异常。**没有观察到 Meta/World/RNG/Event/Receipt 的部分提交或权限绕过**，当前事务回滚有效，所以定为 P2，而非 P0/P1。此探针不计入 563 项正式测试。首次探针已经复现行为，但临时清理遇 Windows 文件占用；改为显式关闭探针查询连接后，复核退出 0、结果一致。

最小后续修复建议：在本地 Result 字符串/UTF-8 编码边界将 UnicodeError 转成 `invalid_npc_result`，进入既有有界纠错路径；新增真实 SQLite 测试覆盖非法 Memory/Dialogue 文本、预算推进、全表未写、相同任务修正和已选结果重试。还应核对 Task/player-input 等其他本地文本入口的同类错误；本报告未把未复现的其他入口列为额外确认 Bug。无需新增服务、Schema、ACK、幂等键或语义判断器。

Review 按要求不修改业务代码，GR-01 **未关闭**。P0：0；P1：0；确认 P2：1。没有将未冻结 Schema、显式排除机制或 Post-MVP 功能计为已确认缺陷。

## 6. 测试、提交与执行证据

所有正式全量运行均执行 `uv sync --locked --group dev` 后的 `uv run --locked python -m pytest -q`，并执行 Ruff check、Ruff format --check 和 git diff --check。Windows 的实际子进程/异步 Engine 测试使用所需 OS 权限，未调用外部模型服务。

| 阶段 | Commit SHA（已推送） | 新增 | 总数 | 最终完整测试耗时 |
|---|---|---:|---:|---:|
| SM-15 | `c94955f1f12edb00931244a1b2ddedcaed4511dd` | 17 | 479 | 121.33 s |
| SM-16 | `44f8e9004d3a1c984b9c7084e1762d60ac6d92e6` | 33 | 512 | 142.44 s |
| SM-17 | `2ddd79e369e59b6acc1110b8144139177fe22393` | 18 | 530 | 151.05 s |
| SM-18 | `c0924c9f815e92cbf06088abe47ce391a8ecfd7a` | 33 | 563 | 162.54 s |
| 独立 Global Review | 本报告的独立 docs commit；SHA 由最终 Git 报告给出 | 0 | 563 | 176.72 s |

最终独立运行：**563 passed, 2 warnings in 176.72s**。基线 462 项保留，新增共 101 项，无 skip/xfail。两条 warnings 是既有上游 Lark 对 sre_parse/sre_constants 的弃用提示。Ruff check 通过，format 检查 84 files already formatted，git diff --check 通过。未跟踪数据库/WAL/SHM/虚拟环境或缓存。

```powershell
uv sync --locked --group dev
uv run --locked python -m pytest -q
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
```

早期失败及修复没有隐去：SM-15 先修正过时 Schema 断言和编辑期间的子进程导入错误；SM-16 恢复两条被误改的历史事件计数后重跑全量成功，之后只做格式修正；SM-18 修复 generated-column 比较辅助函数与测试片段位置后，全量成功。详细次数/边界见各 Batch 文档。SM-17 首次 Push 的 GitHub 连接超时后重试成功。上述均不是最终测试或未关闭的产品故障；GR-01 则是本次新增、尚未处理的产品输入验证问题。

## 7. 剩余契约、外部依赖与下一步

| 项目 | 状态 | 实际边界 |
|---|---|---|
| GR-01 Result UTF-8 错误分类 | Confirmed P2 / Open | 合并前建议小修，不能以绿色既有测试宣称无问题 |
| Host wire schema、认证与恢复调用约定 | OPEN / Proposed | 当前是可信 Python 进程内接口；Task/Result/Receipt/Mode/Gate 枚举、大小上限、三次预算及稳定指纹不是已批准公共 ABI |
| 当前 NPC 输入与历史 Context | Explicitly out of scope | Task 读取完整当前授权 Meta 与 expected version/status，不保存物理 Prompt/Context，也不替 Host 恢复旧 KV Cache；调用方应读取 Task/Receipt 的当前状态，不能仅凭早期 routing response 的快照重启模型 |
| 完整 Action Intent 映射 | Partial beyond this task | 本轮只允许 NPC 本人场景移动；战斗/检定/法术 Intent 映射未实现，必要规则仍必须经过原 Engine 管线 |
| Dialogue 空间/隐私传播 | Explicitly unsupported | 不支持 whisper/子集/远程受众；不得借当前 scene Signal 冒充严格私聊传播 |
| 撤销后的旧 pending 输出 | Fail closed / policy OPEN | 当前授权重新核验，不自动交给新 Controller 执行旧提议；可信权限解决前保持 pending/blocked。已提交 Command 可通过原回执审计，不重新执行 |
| 泛化版本策略 | OPEN | 专用 NPC Meta 非空批次推进 NPCStateVersion；专用 Dialogue 发布只推进 event_seq。没有冻结所有 Meta-only/event-only 世界命令的 world_version 策略 |
| 真实 Host / LLM、Context、Token/cache、自动重试、模型分类 | NOT IMPLEMENTED / 外部依赖 | 测试 Result 与分类为手工合成数据；没有真实主观决策、语义筛选、智能 Memory 管理或模型验收 |
| Schedule Tick、复杂社会模拟、PendingOperation/Reaction、活跃战斗精确续玩 | Explicitly out of scope | 沿用既有受限规则/恢复政策，不为本轮虚构支持 |
| Legacy Profile 升级、长期容量/备份、First Blush 验收 | OPEN / 后续依赖 | 未补造旧人格；未做生产性能/硬件断电或正版素材端到端验收；未提交版权素材 |

本轮真正减少的 MVP 剩余工作，是把 NPC 的授权任务、认知历史提交、模式资格、可靠输出、处理水位和重启对账从 Host 预想职责落实到唯一 SM 数据库。后续 Host 可以调用该领域闭环，不能接管上述权威状态；但真实 LLM 和玩法端到端验收仍全部未完成。

建议后续 State Machine 工作顺序：先以独立小修关闭 GR-01 并补回归；再和 Host 审阅本地 wire/schema/输入与反馈恢复契约、撤销输出处置政策；按一个受审操作一次的方式扩展 Action Intent 映射，复用现有 Typed Command/Rule Evaluation；最后安排真实 Host 调用及获授权素材的联合验收。不要为这些准备工作添加新状态权威、Proposal/ACK 或规则计算。

**本轮四个 Batch 与报告已分别交付；SM-16 的 GR-01 仍为 Partial，合并建议为暂缓。没有 Merge main、创建 PR 或修改其他仓库；停止，等待人工审阅及下一轮修复授权。**
