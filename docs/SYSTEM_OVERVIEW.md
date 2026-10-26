# 系统与接口总览：State Machine

2026-10-27，按本批起始源码 `a7b0804` 核对。此页是面向 Codex 的实现导航，
不替代[跨模块契约](../../agentic-trpg/docs/MODULE_CONTRACTS.md)、
[SM Architecture](../../agentic-trpg/docs/STATE_MACHINE_ARCHITECTURE.md) 和
[World Design](../../agentic-trpg/docs/WORLD_SUBSYSTEM_DESIGN.md)。
下文 Python 文件未带目录时，均相对 `src/trpg_state_machine/`。

## 1. 定位与当前边界

本库提供可信进程内 `StateMachine`，组合领域事实、命令／查询、Session 生命周期、
SQLite 持久化和受控 RAM 发布。外部适配层负责认证后提供 `TrustedContext`，
不能从玩家或模型 JSON 直接接受该可信上下文；公开方法并非现成 HTTP 服务。

World、Character、Combat、Time、NPC 等组件已有实际实现；SessionMemory、
实例字段、重量／机关／通路关联及相关消费者已有受限生产接线。
RE 负责受支持机械规则，SM 负责输入准备、结果校验和采用，不能另算一套规则。
完整 Agent Host、通用规则采用、完整活跃战斗恢复及未来多人负载不是已验收能力。

## 2. 公开入口和主要协作模块

| 子系统 | 职责 | 关键文件／符号 | 主要协作对象 |
| --- | --- | --- | --- |
| 服务装配／生命周期 | 选择冻结规则素材、组合一个 Store 与领域服务 | `service.py::StateMachine` | 下列 Facts、Handler、Runtime |
| 创建／权限 | 类型、单玩家选择、绑定、主体和控制权检查；原子创建／重放 | `session_creation.py::SessionCreation.create`；`session_access.py::SessionAccess` | World 初始化、角色基础、SessionRuntime |
| 命令分发 | 命令形状、授权、版本／幂等、保存点和回执 | `commands.py::CommandRuntime.submit_command` | 由 StateMachine 继承暴露；领域 Handler／规则路径 |
| Session 元数据 | 运行准入、命令指纹、世界版本、RNG、事件和回执协调 | `session_runtime.py::SessionRuntime` | SessionAccess、EventPersistence、Time |
| 共享存储 | 连接、迁移、读写范围、保存点、持久提交及恢复门禁 | `storage.py::SqliteStore`、`storage.py::UnitOfWork` | SessionMemory、所有领域写入者 |
| 活跃事实／运行字段 | 固定读取范围、候选补丁、依赖版本、有界保留和发布 | `session_memory.py::SessionMemory`；`runtime_instances.py::resolve_field` | RuntimeConnection、领域 owner、查询消费者 |
| 领域事实／战斗流程 | 角色机械事实、World 事实、遭遇流程记录分别管理 | `domain_facts.py::CharacterFacts`、`domain_facts.py::WorldFacts`、`domain_facts.py::CombatRecords` | WorldHandler、CombatHandler、RulesAdapter |
| World／对象 | 世界操作和空间准入；容器、门锁、光照、机关等能力路由 | `spatial.py::WorldHandler`；`world_objects.py::WorldObjectService` | 领域 Facts、Grid、Time、SceneLifecycle |
| 空间／地图 | 固定 GridView、局部占用、拓扑、对象空间通道 | `grid.py::GridService`；`grid_maps.py::GridMaps`；`world_spatial.py::ObjectSpatialService` | 实例配置、角色／对象位置、RE 几何 |
| 场景／活动／时间 | 必要实例创建、导航执行、唯一世界 tick 与有界推进 | `scene_lifecycle.py::SceneLifecycleService`；`activity_handler.py::ActivityHandler`；`time_coordination.py::SessionTime` | ActivityRecords、WorldTimeService、TimeDriver |
| 环境／派生结果 | 环境来源、防护、光照和有界查询结果 | `environment.py::EnvironmentService`；`world_lights.py::LightService` | 来源／防护依赖版本、地图、对象能力 |
| 规则交接 | 固定机械输入、事务外 RE 计算、结果校验及事务内采用 | `rules_adapter.py::RulesAdapter`；`rule_execution.py::RuleExecution.submit` | CombatHandler、Facts、SessionRuntime |
| 事件／可靠工作 | 阶段取证、有限后果展开、持久事件／Outbox／感知队列 | `event_capture.py::EventCapture`；`package_effects.py::PackageEffects`；`event_persistence.py::EventPersistence` | 领域写入、EventRuntime、PerceptionQueueRuntime |
| NPC／感知 | 授权视图、固定评估任务、认知采用、模式和输出交接 | `npc_evaluation.py::NPCEvaluationRuntime`；`npc_results.py::NPCResultRuntime`；`npc_outputs.py::NPCOutputRuntime` | NPC state、观察保留、Perception、普通命令接口 |

常用入口：`StateMachine.create_session`、`read_session`、`save_session`、
`resume_session`、`unload_session`、`close`；规则命令另有 `submit_command_async`。
局部查询包括 `query_world_action`、`get_object`、`read_carried_items`、
`get_spatial_view`、`get_illumination` 及 `environment` 的授权查询。
`read_session` 是显式完整 SessionSnapshot 读取，普通局部操作不必先构造它。

## 3. 代表性处理流程

### 创建、加载和关闭

1. `StateMachine` 构造器在打开 Store 前选择默认或注入的冻结规则集。
2. `SessionCreation.create` 验证可信主体、单玩家模板、Package／Ruleset 绑定，
   计算创建指纹并检查持久重放；写范围内再次核对创建键。
3. 领域接口共同初始化 Session、World、必要场景／角色、RNG、配置和初始事件，
   创建结果随实际写范围提交；重试返回同一持久结果。
4. 已有 Session 的首次／冷访问和恢复读取持久实例，不重新套用 Package 模板。
5. `save_session` 验证并读取已有持久状态，不接收可写 Snapshot 覆盖事实；
   `resume_session` 在支持的非战斗边界验证，必要时进入 reconciliation。
6. `unload_session` 释放 RAM、地图／环境派生及驱动，不删除游戏事实；`close` 关闭 Store。

### 普通 World 命令

1. `TypedCommand` 先做封闭形状检查和 Session 准入，生成绑定主体／内容的指纹。
2. 在相应写范围内重验控制权、绑定、预期版本和幂等键；命中回执仍走权限边界。
3. World／Activity／对象能力处理器检查受影响依赖与候选中间状态，执行领域写入，
   在实际阶段捕获证据，按需展开有限后果；普通路径不默认执行完整 Session Audit。
4. `stage_outcome` 准备版本和正式事件写入，`finish` 写持久回执；这些仍在 SQL 事务内。
5. Store 完成 COMMIT→RAM 发布后才正常退出；调用方随后取得回执并刷新适用驱动。

普通领域拒绝可回滚效果保存点后记录拒绝／冲突／待选回执；存储、绑定或无效规则
结果等严重失败不会都被压成普通拒绝。时间准入／有界推进可能先持久化进度，
重试按已记录目标继续，不能把整个未来时间工作误解成一个长事务。

### RE 求值与采用

1. `RulesAdapter` 从一致当前范围或隔离候选构造必要完整机械 Snapshot 和 RNG 输入。
2. `RuleExecution.submit` 准备授权、目标、绑定、快照和幂等关联后离开准备读事务，
   再调用 RE；不在持久写事务中等待 RE／模型。
3. RE 返回未提交结果。`rules_adapter.py::validate_dnd_result` 检查类型／关联／
   绑定／版本／Delta；支持范围还受 SM 实际采用白名单约束。
4. 进入写范围后重验权限、重放、世界版本、快照、RNG 和领域前提，防止采用过期结果。
5. 合法变化经 Facts／Records 应用，RNG、证据、事件、回执及必要后续工作共同持久化，
   再受 Store 的发布边界约束。`accepted` 本身从未等于 `committed`。

### 事件与 NPC 后续工作

`EventCapture` 固定阶段证据，`PackageEffects._expand_events` 有界展开来源事件后果；
`EventPersistence._append_events` 分配正式序号并写事件、证据、Outbox 和可靠感知工作，
不在持久化时重新采样历史世界。`EventRuntime.read_events` 提供授权读取，
可靠投递／感知处理通过各自游标和队列推进，不是一次写入就完成全部下游任务。

NPC 评估固定授权观察和版本输入；外部模型推理后调用
`NPCResultRuntime.submit_npc_evaluation_result` 验证采用认知变化及输出交接。
`NPCOutputRuntime.dispatch_npc_output` 的动作回到普通命令接口，丢响应查原回执；
模式、Meta、Task、观察完成状态有自身版本和事务责任，不是每项都机械推进世界版本。

## 4. 数据与状态归属

| 表示 | 持有者／修改责任 | 读取与隔离边界 |
| --- | --- | --- |
| 持久事实／配置／版本／RNG | 各领域通过 SqliteStore 共同事务写 SQLite | 创建、冷读、恢复、回执及可靠查询的持久依据 |
| 已加载事实范围 | Store 的 SessionMemory；受控 RuntimeConnection 参与读取 | 支持的查询复用固定 RAM 范围及候选补丁；看到 SQL 调用形式不等于又访问数据库 |
| 对象／场景／World／角色字段 | `runtime_instances.py` 的 `ObjectRuntime`、`SceneRuntime`、`WorldRuntime`、`CharacterRuntime` | 沿现有发布机制替换 owner，不能独立写成第二套权威 |
| 索引／缓存／准备地图 | 字段 owner、GridMaps、Environment 等有界派生结构 | 维护依赖与失效，冷准备／必要全量输出保留真实成本 |
| 固定输入／对外 View | SessionSnapshot、GridView、RE/NPC 固定请求等消费者输入 | 与后续发布和候选修改隔离；视图不是修改授权 |
| 未提交候选／结果 | 当前 UnitOfWork、领域候选及 RE 返回值 | 保存点／整体回滚不能污染已发布事实和旧输入 |

容器重量看 `world_containers.py::ContainerService`；机关／通路等关联沿所属字段索引。
`automatic()` 每次处理一个来源事件，局部 `fired` 只限制该事件同机关一个匹配操作；
不同事件可在资格仍成立时再次触发。它直接使用场景索引，`prepared` 不提供跨事件去重。
`_WallIndex.query()` 只给包围盒重叠候选，后续 RE 几何仍执行精确移动／视线判断。

## 5. 事务、发布和恢复边界

`SqliteStore.transaction`／`UnitOfWork` 是已有共同范围；第一条实际写入经
`SqliteStore.begin_persistence` 转入增量持久化并重验准备版本。
`RuntimeConnection` 捕获修改与 SQL 失败，保存点回滚同步恢复候选、版本和字段；
阶段内 memo 不能跨修改或回滚失效边界继续使用。

SQLite COMMIT 与 RAM 发布不是同一个 ACID 事务。COMMIT 后，Store 在协调门内调用
`SessionMemory.publish`；未知提交结果使用 `commit_outcome_unknown`，已提交但发布
未就绪使用 `committed_publication_pending`，暂停当前服务并按原稳定键对账／重建。
恢复通过 `SqliteStore.reconciliation` 与受支持的 `resume_session` 边界进行，
跨连接代次失效也须保留；不能直接把 paused 标志清掉当成恢复完成。

`world_states.game_tick` 是唯一游戏时钟；`world_time.py::WorldTimeService` 做读取转换，
SessionTime 协调模式、消费者和等待，`time_driver.py::TimeDriver` 只是宿主采样／唤醒。
活动／Effects 等完整时间消费、秒制 NPC 兼容、活跃战斗精确恢复及多人负载仍有独立限制。

## 6. 跨仓库接口与按任务找代码

| 任务／接口 | 从哪里读起 | 进一步核对 |
| --- | --- | --- |
| 宿主请求／授权 | `models.py::TrustedContext`、`models.py::CreateSessionRequest`、`commands.py::TypedCommand` | `service.py`、SessionAccess；认证由外部可信层完成 |
| 修改容器重量 | `world_containers.py`→WorldFacts→运行字段和修改监听 | MC §2.3a、[World](../../agentic-trpg/docs/WORLD_SUBSYSTEM_DESIGN.md)、容器 Progress；runtime-change |
| 检查事务／恢复 | `storage.py`→`session_memory.py`→SessionRuntime／resume | [Session Management](SESSION_MANAGEMENT.md)、原故障验收；atomicity-review |
| 新增规则采用 | `rule_execution.py`、`rules_adapter.py`、`combat_handler.py` | RE 对应 evaluator／DTO、SM pin、支持白名单和验证；不能只加一条调用 |
| 事件与可靠消费 | EventCapture→PackageEffects→EventPersistence→队列／游标 | `events.py`、`perception_queue.py`、NPC 输入与输出 |
| 空间／机关语义 | GridService／ObjectSpatialService／MechanismService 的真实公开调用链 | [实例归属证据](reviews/RUNTIME_INSTANCE_OWNERSHIP.md)、locality-review |
| 说明／结构导航维护 | [源码说明入口](SOURCE_DOCUMENTATION.md)、本页 | trpg-code-documentation；跨仓库契约问题读 sibling 的 trpg-contract-sync |

RE 输入输出定义在其 `evaluation_contracts.py`、`evaluation_state.py` 和 `evaluation_rng.py`；
本库用 `RulesAdapter.request` 组织请求，并将结果转成已允许的领域变化。
实际安装版本以 `pyproject.toml`／`uv.lock` 为准，不自动使用 sibling 最新源码；
DTO／Schema 数字和正式 ABI 批准状态应查当前契约，不能把目标示例当现行接口。

## 7. 维护与证据边界

模块协作、公开入口、状态归属或跨库接口变动时更新本页；函数细节留给源码说明，
领域规则留给权威 DOC。局部说明修正不要求机械改动整份总览。
本页依据当前源码和[既有运行证据](reviews/SESSION_MEMORY_RUNTIME_IMPLEMENTATION.md)整理，
不重跑或扩大旧验收；本批修正与静态检查见[本批记录](reviews/CODE_DOCUMENTATION_AND_SKILLS.md)。
