# SM-2～SM-5 Global Review

审查日期：2026-10-10。结论：**暂不建议 Merge main**。四个开发 Batch 均有独立提交并已 Push；168 项自动化测试与 Ruff 通过，但独立审查确认 2 项 P1 和若干 P2。成功路径的单一权威、事务原子性、持久回执、合成 RNG 和新 Session 感知链路成立，不代表完整跨模块协议、真实 D&D 集成或旧存档感知启用已完成。

本阶段只读业务代码、运行测试及一次性复现，唯一新增受跟踪文件为本报告。未自动修复下列问题，未修改验收标准，未 Merge、未创建 PR、未修改 Meta 或 Engine Repository。

## 1. 基准、提交与审查方法

| 项目 | 实际值 |
|---|---|
| Branch | `feat/sm-b2-b5-runtime-pipeline` |
| Base / origin/main | `182efbe6b6b331fea274b5ae7863a8888e791339` |
| SM-2 | `86ac1bf816d917c155cadf436097eb6b6f81a6f4` |
| SM-3 | `1e289a9b57d72d955777ff1d781b1ba90f89112f` |
| SM-4 | `16291de08b41ef147a2389bdb3d02235fa65cb0e` |
| SM-5 / reviewed HEAD | `53c97e9e1aada6d92280653ebcddbc8c504766c6` |
| Meta main | `fe6a1e9edeaf2a3e120aa669b6abfe1e819d87f1` |
| Engine main | `64dd920507de29602908b4bea1635eedc4385370` |

已检查 `git diff origin/main...HEAD`、文件树、独立提交、源代码/SQL/断言和故障路径。`git ls-remote` 确认远端 main 与 Base 相同，远端功能分支与 reviewed HEAD 相同；开发区间没有 Merge Commit。四轮均在 Push 成功、工作区干净后才进入下一轮。开发提交合计 26 个文件、3119 insertions / 69 deletions；本报告另计一个文档文件。

完整读取本地六份架构依据：ADR-001、MODULE_CONTRACTS、STATE_MACHINE_ARCHITECTURE、AGENT_ARCHITECTURE、ADVENTURE_PACKAGE_SCHEMA、MVP_SCOPE。ADR-001 与 DECIDED 语义为约束；未将全部 PROPOSED 字段冻结，也未把 OPEN 或 NOT IMPLEMENTED 宣称现有能力。三条独立只读审查分别覆盖命令/事件/迁移、机械边界、感知/授权；主审补跑跨 Batch 实际数据库和新进程验证。

## 2. 原始要求完成矩阵

Fully 表示所述受限能力有实际代码和测试，不能外推为完整 MVP。Partial 明确标出已存在实现及剩余缺口。编号对应任务附件各 Batch 的“实现内容”。

| 要求 | 状态 | 实际实现、证据与边界 |
|---|---|---|
| SM-2.1 统一 TypedCommand | Fully Implemented（本地子集） | `commands.py:22,49,110`；封闭类型、可信主体、真实 PC Actor、预期版本；不支持无 Actor 系统分支或任意 GM 操作 |
| SM-2.2 复用基础操作及 NPC 授权 | Fully Implemented | `service.py:490` 的 `_apply` 路由同一入口；`commands.py:83` 验证 NPC 控制 grant，无测试绕过 |
| SM-2.3 Schema/权限/目标/前置条件 | Fully Implemented（受限操作） | 类型/字段检查、当前 Scene、声明 Flag/Object、合法出口；未知 Patch/SQL/复杂行为拒绝 |
| SM-2.4 持久幂等/冲突 | Fully Implemented | `command_receipts` 唯一键；稳定规范化 Fingerprint；同键重试/不同内容冲突/并发测试 |
| SM-2.5 原子提交与事务内重验 | Fully Implemented | `commands.py:124–185`；Savepoint + BEGIN IMMEDIATE，状态/版本/事件/回执/工作同事务；SQLite 故障全回滚 |
| SM-2.6 授权回执/兼容恢复 | Partially Implemented | 回执重验权限、旧世界状态及摘要保留；并发迁移启动缺陷见 F3，升级后感知启用缺口见 F2 |
| SM-3.1 CommittedWorldEvent | Fully Implemented | 稳定 SM-owned event_id/seq/source_command/type/payload；仅提交事务生成 |
| SM-3.2 Signals 与受限 Scope | Fully Implemented | 同事务存储 visual/auditory、scene scope、clear/blocked/undetermined；无自然语言传播解释 |
| SM-3.3 事件时证据/工作 | Fully Implemented（新事件） | `service.py:418–485` 保存位置、显式合成 senses、Scope 和 Outbox；历史缺证据不补造，但迁移启用受 F2 限制 |
| SM-3.4 Outbox/至少一次/缺口 | Fully Implemented（普通投递） | `events.py:105–173`；持久 attempts、完成和连续 Cursor，sink 按 event_id 去重；无 exactly-once/认知 ACK 声称 |
| SM-3.5 EventDefinition | Fully Implemented（最小子集） | authoritative trigger + flag_equals + 两种 world effect；严格 Loader，无 eval/脚本/独立 Event Engine |
| SM-3.6 触发历史/关联效果/后续命令 | Partially Implemented | once 历史及同步效果原子提交；历史缺确切触发 event_id（F5）；后续机械命令/交接未实现且 Loader 明确拒绝 |
| SM-3.7 循环/奖励/独立版本 | Fully Implemented（本地子集） | per-session once + 32 后果上限；超限全回滚；world_version 与 event_seq 独立 |
| SM-4.1 真实 Engine 审计 | Fully Implemented | main/API/_LiveCombat/RNG 已实际核对，真实 `resolve_check`/rest smoke，见 §6 |
| SM-4.2 Request/Result/四态/Snapshot | Partially Implemented | 四态 Synthetic 边界、非战斗支持状态和独立 RNG；无 CombatSnapshot/可信无 Actor 分支；共同 Schema/read_set/needs_choice 语义未完整对齐（F6） |
| SM-4.3 封闭 Delta 验证 | Fully Implemented（Synthetic） | HPChange/ResourceSpend/SetSceneFlag；目标归属、范围、HP/资源/重复写检查；不宣称真实规则操作 |
| SM-4.4 事务外求值与原子提交 | Fully Implemented（支持路径） | copied input；事务内重验完整 snapshot digest、版本、权限、pins、RNG、Delta；HP/资源/世界/RNG/事件/回执/Outbox 同事务 |
| SM-4.5 非 accepted/失败不耗 RNG | Fully Implemented（已测试路径） | rejected/unsupported/choice/conflict、非法结果和 SQL 故障无 RNG/事件；合法机械失败扣资源并提交 |
| SM-4.6 完整错误映射/持久幂等 | Partially Implemented | 已提交/丢响应/重启重试不求值；绑定配置错误被固化为拒绝（F1），确定性后果拒绝缺回执（F4） |
| SM-4.7 Synthetic 与真实集成区分 | Fully Implemented | 代码/README/SM4.md 清晰标注 Synthetic；真实 D&D Adapter = NOT IMPLEMENTED / Blocked |
| SM-4.8 上游缺口/复杂流程 | Fully Implemented（边界披露） | 未修改 Engine，未使用 registry 作为第二权威；Combat/Reaction/Concentration/continuation 明确不支持；真实适配仍 Blocked |
| SM-5.1 PER 决策与边界 | Fully Implemented（Foundation） | 复用 Contracts §7 / SM §6 / Agent §5；无 Gate/EVA/Memory/认知 ACK |
| SM-5.2 事件时 Scope/感官/遮挡 | Fully Implemented（合成模型） | 事件时位置和明确 senses；限定 Scene、已知遮挡；不读取后来位置倒推历史 |
| SM-5.3 聚合/全部 false | Fully Implemented | 每 observer/event/version 一条 Observation；all-false 完成不建 Observation；内部 decisions 的 signal_id 与来源 Signals 可 JOIN 精确追溯 |
| SM-5.4 来源/可见知识隔离 | Fully Implemented（支持模型） | Public Observation 仅 opaque ID、observer、channel/content；内部保留 source seq；不附原事件类型/真实 Actor/秘密目标 |
| SM-5.5 受限 JEV 校验 | Fully Implemented（本地边界） | 同 observer/event 未决 Signals；逐 ID 严格 bool；缺失/重复/未知/异常/超时保持未决；无真实 LLM Host |
| SM-5.6 Inbox/重试/恢复 | Partially Implemented | 新 Session 并发去重、重启/新进程、原子 Inbox 通过；旧历史缺口阻断后续读取（F2） |
| SM-5.7 授权读取及角色分离 | Fully Implemented（本地信任模型） | persisted NPC grant + NPCContext；每次重验；Snapshot/read_events 拒绝 NPC，get_scene_view 返回受限视图 |
| SM-5.8 当前视图/历史/认知区分 | Fully Implemented（受限视图） | CurrentPerceptualView 与 EventObservation 独立；当前 presence 不推断过去 causal actor；无 Belief/Goal/Plan/执行更新 |

## 3. 实际数据流与模块边界

```text
approved immutable Package + trusted principal
  -> create_session -> SQLite Session/World/Scene/PC/RNG + creation receipt
  -> submit_command -> schema/auth/actor/fingerprint/replay
       world operation -> current-scene preconditions -> bounded synchronous effects
       rule operation  -> detached Snapshot + separate RNG -> Synthetic evaluate OUTSIDE write tx
                          -> recheck authority/read-set/RNG/pins/Delta INSIDE short write tx
  -> one commit: world/mechanical state + version + RNG + facts/Signals/evidence
                 + trigger history + command receipt + delivery/perception Outbox
  -> delivery sink OUTSIDE write tx -> durable delivery completion/contiguous cursor
  -> event-time deterministic/JEV projection OUTSIDE write tx
  -> one projection commit: decisions + aggregate Observation + NPC Inbox + completion
  -> authorized NPC read / separate current perceptual view
```

StateMachine 及一个 SQLite 文件拥有唯一权威游戏状态；返回对象为 detached 值。Events/Outbox/Observation 是提交事实与派生投影，不用事件重算 HP，不是另一份可写游戏 Snapshot。Package 静态模板与 SceneInstance/唯一 Actor 行分开。规则求值不调用 `_REGISTRY`；临时 request/result 不持有跨请求权威状态。

模块拆分为具体职责 mixin，均有真实调用路径，没有插件注册、通用脚本/事件框架或未来空 Stub。`_apply` 是一次兼容包装，仍经过公开命令入口。当前持久化是单 SQLite WAL，不包含网络服务、生产 worker 调度或多库事务。

## 4. 测试、组合验证与回归证据

| 阶段 | pytest 结果 | Sync / Ruff / Format / Diff |
|---|---:|---|
| 原 Batch 1 基线 | 98 passed | 本轮开始时基线复核 |
| SM-2 `86ac1bf` | 112 passed | 均通过 |
| SM-3 `1e289a9` | 123 passed | 均通过 |
| SM-4 `16291de` | 146 passed | 均通过 |
| SM-5 `53c97e9` | 168 passed | 均通过 |
| Global Review 同一 HEAD | 168 passed in 10.54s | Sync、Ruff、Format 均通过 |

每轮实际命令（受限 Windows 环境使用仓库内临时目录）：

```powershell
$env:UV_CACHE_DIR = "$PWD/.uv-cache"
uv sync --locked --group dev
uv run --locked python -m pytest -q --basetemp=.test-tmp/<batch>
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
```

Global Review 使用 `.test-tmp/global-review`；SM-2/3/4/5 最终目录分别为 `sm2b/sm3final/sm4final/sm5final`。独立审查另外执行 commands/events 25 项、perception 22 项，均通过。部分独立检查首次遇到沙箱系统临时目录或 uv cache 权限失败，改用既有 venv / 仓库 basetemp 后成功；这些 fixture/setup 环境错误不作为产品回归或成功证据。

原 Batch 1 集成测试仍实际运行独立子进程和 `os._exit(0)`，验证 `create -> enter -> mutate -> leave -> revisit -> save -> restart -> resume` 的真实 SQLite 行、版本、Package/Ruleset pins 与创建幂等。新增测试验证 receipt/outbox/RNG/inbox 等表的故障回滚、并发双连接、reopen/replay 和来源隔离；不是仅 Mock 调用。

主审另运行一次不修改受跟踪测试的跨四批真实集成：从原创 YAML 加载 synthetic.atomic Package，创建 visitor PC（HP 11/12、tokens=3），`synthetic_attempt` 成功消费 token/恢复 HP/置 flag，同命令触发开门和 chime；随后进入另一 Scene、移动 NPC、离开及重访，投递所有事件并投影 NPC Inbox，保存、关闭并在全新进程恢复和重试原规则命令。新进程的 Evaluator 故意在被调用时抛异常，重试仍成功返回原回执，证明没有再次求值。

实际独立 SQL/子进程输出：

```json
{"world_version":5,"event_seq":13,"rng_position":1,"receipt_status":"committed","observations":7}
```

另核验 HP=12、tokens=2、门=open、rng_version=1、规则命令回执恰好一条、26 条 delivery/perception Work 均完成，`integrity_check=ok`、`foreign_key_check=[]`。原始命令回执仍为 world_version=1，当前恢复 Snapshot 为5；这是原结果回放，不是错误更新回执。该证据只证明合成闭环和进程恢复，不证明断电、文件系统损坏、真实 Host 或 D&D 规则正确性。

## 5. 发现与最小建议

没有已确认 P0。以下发现不因绿色测试被忽略，也未在 Review 中修复。

### F1 — P1 / Confirmed Defect：Evaluator 绑定配置错误变成永久游戏拒绝

证据：`src/trpg_state_machine/mechanical_runtime.py:145–146,171–172,219–229`。匹配失败使 result 保持 None，然后统一写 durable `rejected / unsupported_ruleset`。`MODULE_CONTRACTS.md:285` 和 §4.3 要求 RulesetBinding mismatch 使用独立错误，不得改报规则 rejected。

真实复现：合法 synthetic.atomic Session，Evaluator 误配 synthetic.foundation binding；有效请求首次得到 rejected。修复 Evaluator binding 后相同 ID 原请求仍回放拒绝，Evaluator 从未被调用。

```json
{"first_status":"rejected","error":"unsupported_ruleset","after_fix_status":"rejected","evaluator_calls":0,"rng_position":0}
```

影响：可修复的部署/适配器配置错误永久污染命令结果，需要换 ID 才能执行；这违反错误分类与可恢复重试语义。没有 RNG/资源部分写入。

最小建议：在求值前独立校验实际 Evaluator 的绑定；不匹配返回 `ruleset_binding_mismatch` 类独立错误、不保存业务拒绝。区分不支持的 Package/operation 与运行配置故障，并增加“配置修复后原 ID 可重试”的测试。若上游结果的 binding 不匹配，也保持独立协议错误。

### F2 — P1 / Confirmed Compatibility Limitation：旧历史缺口阻塞 Session 感知交付

证据：`_migration_3.sql:11–13` 将历史来源标为 legacy.unavailable；`_migration_5.sql:2–3` 为全部历史事件建立未完成 perception work；`perception.py:180–184` 缺证据永久 unresolved；`events.py:111–122` 连续 cursor 停在首个缺口；`perception.py:279–290` 用该 Session 全局水位屏蔽后续 Inbox。

真实旧源码复现：从 Base `182efbe` 只读导出 src，在独立子进程用原 Batch 1 Loader/Service/Fixture 创建真实 Schema 1 DB；当前 HEAD 自动升级、resume 并提交新 Flag 命令：

```text
project 1 unresolved
project 2 unresolved
project 3 resolved
perception_cursor 0
outbox [(1,0),(2,0),(3,1)]
new command committed, world_version=1, event_seqs=(3,)
```

另以有明确 senses 的合成 Package 将真实状态列复制进原 Schema 1 的迁移 fixture，验证新观察确实落盘时的读取后果（这是迁移 fixture，不能冒称旧 Loader 支持 senses）：新 event_seq=3 的 Observation 和 Inbox 各一条，cursor=0、`read_observations=()`；重启仍为空。

影响边界：**旧世界状态 resume 与新命令成功，没有发现世界状态丢失**；问题是旧 Session 缺少可用的感知启用/交付边界。原旧 Package 也缺 senses，且 Package Digest 固定，无法简单修改模板。README/SM5.md 已披露保守阻塞，但没有恢复或明确 blocked 查询路径。不能据此宣称旧存档的新感知可靠可用。

最小建议：先明确结构化反馈“历史证据不足导致 perception blocked”，区分空 Inbox 与不可交付。随后由架构确认持久的感知启用 epoch/起点，或对确实可判定的旧内部审计/无 Signal 工作定义可靠 no-op 政策。保留不可投影历史审计，禁止伪造历史证据或静默跳 cursor。增加真实旧源码建库 -> 升级 -> 新感知可交付/明确拒绝的验收。

### F3 — P2 / Confirmed Defect：并发启动重复执行迁移

证据：`service.py:94–122` 在迁移写锁外读取并缓存 user_version；`_migration_2.sql:1–2` 获取锁后直接 CREATE，没有重新读取版本。

实际复现：真实 Schema 1 DB，两连接通过 barrier 安排均已读到版本1后开始迁移。一个打开成功，另一个 `storage_error`，cause 为 `OperationalError('table command_receipts already exists')`；最终 user_version=5、FK check 正常。没有数据损坏，属于连接启动/部署并发失败。

最小建议：统一迁移串行边界内重新读取版本并选择步骤；等待者发现已完成迁移后重新核验并继续。不要仅加 IF NOT EXISTS 掩盖不完整升级。增加并发首次打开/旧版升级测试。

### F4 — P2 / Confirmed Defect：机械后果业务拒绝缺持久回执

证据：`mechanical_runtime.py:186` 的 `_expand_events` 异常直接回滚并抛出；对照 `commands.py:156–162` 对普通命令的确定性业务拒绝保存 rejected receipt。

实际复现：合法 Package 有33个 once 的 scene.flag_changed 定义，difficulty=1 的 synthetic_attempt 触发级联上限；相同命令重试两次均抛 event_cascade_limit，Evaluator calls=2、receipt=None、version=0、rng=0、tokens=3。

影响：原子回滚正确，无重复资源消耗/事实；确定性业务拒绝的持久幂等和错误映射不一致，导致不必要重复求值。

最小建议：机械候选使用 Savepoint 区分可持久业务拒绝与独立协议/存储/求值故障；前者回滚所有候选状态/RNG/后果后保存原拒绝回执，后者保持独立可重试错误。增加跨域级联拒绝重试测试。

### F5 — P2 / Architecture Completeness：触发历史无确切触发事件引用

证据：`events.py:55–58`、`_migration_3.sql:31–35` 只存 source_command_id；SM Architecture §5 的触发历史要求关联 trigger_event_id，AP §4 也要求可追溯来源。单命令产生多个事件时，只能定位候选集合。

影响：当前 once-per-session 去重和原子效果正确，未发现重复奖励；审计不能精确回答是哪条事件触发了定义。

最小建议：在同事务内将 pending event 序号/身份关联到触发历史，保存准确 source event；不新增公共 operation ID，也不声称有后续机械交接。增加一个命令多事件的来源审计断言。

### F6 — P2 / Contract Coverage Gap：局部合成协议未完整对齐跨模块语义

证据：`mechanics.py:26–65,108–121`、`mechanical_runtime.py:173–181`；Contracts §4.2/§5.1/§6.2。

NonCombatSnapshot 没有 snapshot_kind/snapshot_schema_version，Result 使用整个 Snapshot digest 而非实际依赖实体/版本 read_set；没有 CombatSnapshot 或可信无 Actor 机械来源。needs_choice 虽在 evaluator 四态中真实返回，提交侧却转成 rejected/pending_choice_unsupported，没有 pending_choice / PendingOperation 生命周期。全量 digest 对当前本地竞态有效；具体字段名/API Proposed 不一概视为冻结，但分支/错误/选择语义与已确定契约仍需明确对齐。

影响：可以证明 Synthetic 事务 Foundation，不能宣称完整 Module Contracts P0 Adapter 或完整四态提交协议。README 已披露主要限制，无隐藏的真实 D&D 集成。

最小建议：下一轮明确版本化非战斗协议及 read-set 表达，能力层拒绝不能承诺的 choice，或完成最小 pending_choice；真实 Combat 仅在上游完整无权威 Snapshot/Delta 可用后接入。不要以部分字段或空 Combat 类型替代实现。

**不作为已确认 Bug：** Public PerceivedSignal 不直接带 signal_id，但 `perception_results.decisions_json` + Observation 来源 seq + `event_records.signals_json` 保留精确内部追溯；不能说 Signal ID 数据丢失。真实 JEV、距离/LOS、动态 senses、秘密内容创作审核、EVA/Memory/认知完成是明确未实现范围，也不伪报为本轮运行缺陷。

## 6. 真实 Rule Engine 集成程度

**Synthetic Rule Evaluation Boundary：主路径测试通过，受 F1/F4/F6 限制仍是局部 Foundation。Real D&D Rule Engine Integration：NOT IMPLEMENTED / Blocked。**

实际 Engine main 已核对为 `64dd920`，版本0.6.0。公开 API 仍为 start_combat / submit_player_intent / get_live 等 Handle 路径；`orchestrator.py:3107` 的 _LiveCombat 持有 HP sidecar、资源、回合、effects、reaction、区域、RNG 等，`:3485` 有 _REGISTRY，`:11815` 从 Handle 取 live。`views.py:114` 是投影，不能作为完整恢复 Snapshot。没有目标 evaluate、RuleEvaluationRequest/Result、CombatSnapshot/NonCombatSnapshot、StateDelta 或 RNGTransition 公共类型。

实际 read-only `python -B` smoke：seed47、Strength14、DC12 的 resolve_check 返回 natural12/total14/success=true；恢复 getstate 后结果与后继 RNG state 相同；short rest 示例 healed2、dice_remaining1。真实 fragment 可复用，但没有填补完整事务协议。Engine 工作区已有脏数据未作为 main 的固定 DataRevision，未写任何 Engine 文件。

最小后续适配路径：上游提供完整依赖闭包 Snapshot、临时执行上下文与显式 RNG 输入/后继；从现有 resolver 输出封闭 Delta/候选事件/四态结果和 read-set；盘点全部 sidecar；固定实际 data/evaluator binding；复杂 continuation 不完整时 unsupported。之后在本仓库适配同一 SM 提交器并做真实规则对照测试。禁止临时引入 registry-backed 第二权威。

## 7. 架构、安全、事务与恢复评估

- **所有权与权限：** ADR-001 主路径成立；可信主体不从 command payload 获取；PC/NPC 分支明确、Actor grant 撤销后不能重试越权。NPC observation principal 有单独持久 grant；每次访问核验 session/observer/pins。Python 上下文由认证适配层构造的信任假设明确，本库没有认证服务或网络入口。
- **信息隔离：** Observation 不附完整事件、source command 或真实因果 Actor；JEV 仅收到同 observer/event 的未决 Signals 与必要事件时证据。当前视图只处理声明的合成 clear scene，没有秘密 Actor/复杂遮挡支持，不能推广为生产安全 LOS。内容本身由 Package 作者提供，尚无秘密文本创作审查器。
- **SQL 与 Schema：** 参数化业务值、封闭更新，不接受任意 SQL/Patch。会话复合键/外键和唯一键覆盖主要归属与去重。Schema 1→5 保留世界状态、原 pins/digest；RNG 表重建仅在启动迁移中临时关闭 FK，结束核验 FK ON/foreign_key_check；并发启动问题见 F3。全库 FK 检查在大数据库启动的成本尚未测量。
- **事务：** WAL/FULL、单实例单线程连接、BEGIN IMMEDIATE 短写事务；规则/JEV/sink 在写事务外。动态 Package 级联有后果上限，但最大 Package/Actor 数量下事务延迟未基准测试。所有支持写路径只提交真实数据库状态，非 accepted 不推进权威 RNG。
- **恢复：** 无进程内 authority cache，world/scene/character/pins/RNG/receipts/outbox/inbox 从 SQLite 读取。实际新进程和 os._exit 提交后恢复通过；未验证断电、损坏介质、网络文件系统、在线备份、跨主机或多进程冷启动迁移压力。旧感知启用不完整见 F2。
- **投递/处理：** 至少一次 callback 可能重复或并发到达，sink 必须按 event_id 幂等；不保证外部 exactly once。连续 cursor 保护缺口，read/inbox append 不代表认知完成。没有 worker lease/scheduler、真实 LLM timeout 管理或 NPC activation，均为明确范围外。

## 8. 文件与进度评估

相对 Base 实际变更：

```text
README.md
src/trpg_state_machine/__init__.py
src/trpg_state_machine/_operations.py
src/trpg_state_machine/models.py
src/trpg_state_machine/package.py
src/trpg_state_machine/service.py
src/trpg_state_machine/commands.py
src/trpg_state_machine/events.py
src/trpg_state_machine/mechanics.py
src/trpg_state_machine/mechanical_runtime.py
src/trpg_state_machine/perception.py
src/trpg_state_machine/_migration_2.sql
src/trpg_state_machine/_migration_3.sql
src/trpg_state_machine/_migration_4.sql
src/trpg_state_machine/_migration_5.sql
tests/conftest.py
tests/_restart_worker.py
tests/test_package.py
tests/test_commands.py
tests/test_events.py
tests/test_mechanics.py
tests/test_perception.py
docs/batches/SM2.md
docs/batches/SM3.md
docs/batches/SM4.md
docs/batches/SM5.md
docs/reviews/SM_B2_B5_GLOBAL_REVIEW.md
```

未变更依赖锁、基础 Schema 1、原创 Fixture 或其他 Repository；未将测试数据库/缓存/旧源码导出纳入 Git。无版权冒险素材新增。

这些 Batch 实际减少了 MVP 的事务/恢复/感知基础工作：现在可把一个受信合成命令从世界或机械边界提交为事实、可靠工作及授权观察。仍不能直接承接完整 Agent DM：真实规则、公开协议对齐、旧存档感知迁移、pending choices、完整权限视图与 Host 消费者尚缺。此次没有为未来建立通用插件/脚本框架；技术债主要是局部协议对齐、迁移政策和错误分类，而非类型数量本身。

## 9. OPEN、Blocked 与下一步顺序

1. **先做修复 Batch：** F1 独立 binding 错误及原 ID 可重试；F2 结构化 blocked 反馈及经确认的旧历史投影启用政策。这两项通过专门回归后，再评估 Merge。
2. 修复 F3 并发迁移重验、F4 确定性拒绝持久化、F5 来源事件关联；补足跨批失败路径断言。
3. 对齐受限非战斗 Request/Result/read-set 和 capability/choice 语义（F6），清楚区分本地测试编码与跨模块 Required 语义。
4. 与 Engine 上游协作完成无权威 Snapshot/Delta/RNGTransition 最小真实机械接口，再复用本仓库原子提交器；不在本轮擅自修改 Engine。
5. 最后接入真实 Host/JEV/Agent Inbox 消费与认知生命周期；距离/LOS、EVA/Memory/Goal 等继续按既有架构后续开发，不在 Foundation 中伪造。

仍 OPEN：全局 RNG 算法/编码与 no-draw 版本、event-/Meta-only world_version、复杂 continuation、完整世界条件/效果、历史不可投影事件的启用边界、实际生产投递调度与备份/升级运营策略。Blocked：真实 D&D Adapter 依赖尚不存在的完整上游接口；旧 Session 的可靠感知启用依赖 F2 政策，当前没有自动恢复路径。

**Merge 结论：暂不适合 Merge main。** 绿色测试证明受限成功路径及多类故障原子性，未覆盖 F1/F2 已复现问题；四轮独立提交和审查交付完成，但 SM-4 的完整错误语义、SM-5 的旧存档感知兼容仍为 Partial。建议下一轮先修复并复核，保持本次功能分支供人工审阅。
