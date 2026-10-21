# SceneLifecycleService

## 2026-10-21 Session 运行层接入目标

Architecture Decision Status **[DECIDED]**；完整 Session 内存运行层 Implementation Status **[NOT IMPLEMENTED]**；Acceptance Status **本次未执行运行验收**。 统一事实读取、候选／持久提交／内存发布与故障规则见 [Session Management](SESSION_MANAGEMENT.md#session-内存运行生命周期与事实访问2026-10-21) 及其 MC 引用；下文带日期接口与测试仍描述各自已有能力，不改变当前 ABI。

区分首次实例化、加载已有持久实例到 RAM、实际 Actor 入场；加载不重套 Package、不增加 visited_count 或制造入场／奖励。SceneLifecycle 与 Session 协调按真实依赖加载／卸载，冷补入保持同一读取版本，候选新建 COMMIT 后才发布当前 RAM。结构验收与 Schema40 既有成果保留，不为建索引初始化全部世界；活跃战斗恢复限制保持。

2026-10-19 Session Management 后续：SessionCreation 组织首次创建，SessionRuntime 管理
PC 场景字段与公共执行元数据；ensure_scene 增加固定 Package 和已支持实例结构检查。
已授权抽象 NPC MoveActor 现在可在原事务中确保目标，无需玩家先进入。
具体兼容影响、恢复拒绝策略和未完成的领域配置持久化见
[Session Management](SESSION_MANAGEMENT.md)；本文件的历史验收范围保留。

本地实现切片，初始基线 `9ce60cb`，分支 `feat/scene-lifecycle-service`。
2026-10-18 已将 `origin/main@1fec17a28e15bed06739be0a3811de7b2b2da584`
合入本分支，整合 EnvironmentService、MechanismService／效果契约及 Schema35；
没有合并回 main。验收结果见进度文档的本次集成记录。
设计依据为相邻架构仓库当前 MODULE_CONTRACTS §0.3／§0.3a、World Subsystem
Design §2／§4／§5 及 2026-10-17 v0.4 评审记录；MODULE_CONTRACTS 是最终权威，
本文件描述 SM 的实际内部接口，不冻结新的跨模块 ABI。

## 所有权与装配

StateMachine.scene_lifecycle 与 WorldHandler.scenes 为同一个 SceneLifecycleService。
服务显式引用 SqliteStore、SessionAccess、SessionRuntime、CharacterFacts、WorldFacts、EventCapture、
NPCBootstrap，不引用 StateMachine，不持有状态缓存或角色位置副本。
EnvironmentService 仍由 WorldHandler 装配，StateMachine.environment 引用同一实例，
保留 command_submitter、Effects／角色高度 provider 与 OSS 防护 provider 接线。
MechanismService 仍在 WorldObjectService 内装配，原能力分发／自动触发和效果采用路径保留。

| 事实／业务 | 唯一写入／协调责任 |
|---|---|
| Scene 实例、Flags、visited_count、对象／机械场景事实 | WorldFacts |
| Actor 唯一 scene／cell，初始角色身份 | CharacterFacts |
| 权限、固定 Package 核对 | SessionAccess |
| PC 当前场景字段、固定绑定写入与提交元数据 | SessionRuntime |
| NPC 初始认知与运行数据 | 原有 NPCBootstrap；仅角色首次创建时调用 |
| 初始化／进入离开／PC 访问／Flags 的业务协调 | SceneLifecycleService |
| 移动准入、通路／占用／门锁、命令分发、其他世界业务 | WorldHandler 与原领域服务 |
| 权限／领域执行／公共发布和回执／事务／事件扩展与持久化 | SessionAccess／CommandRuntime／SessionRuntime／SqliteStore／PackageEffects 与 EventPersistence |

## 实际内部入口与调用链

这些是可信进程内事务参与者，不是绕过 submit_command 的玩家／Host 写 API。
全部写入口 require_write；服务不自行开启事务、不 commit、不递归提交命令。

* `ensure_scene(sid, scene_id, package)`：WorldFacts.initialize_scene 首次写入场景
  默认 Flags 和对象；随后委托原对象能力、角色／NPCBootstrap、机械场景初始化。
  返回是否首次创建。先核对 Session 固定 Package；已存在实例复用并检查已支持的必要行，
  缺失则 incomplete_session，不补造旧存档缺失组件，不套用 Package 初值。
* `initialize_session(sid, pc_actor_id, pc, package)`：在既有创建事务中确保入口及原有
  预初始化范围内的空间场景，委托 CharacterFacts.initialize_pc，同步 Session PC 场景，
  记录一次入口访问。它仅用于首次创建；重复创建由原 creation_commands 回放处理。
* `move_admitted(row, package, actor_id, destination)`：接收 WorldHandler 已验证的目标，
  捕获离场阶段、确保目标实例、委托角色移动、按 PC 身份同步 Session／访问、捕获到达阶段。
  同场景真实步进保留 actor.stepped；完全相同的位置无事件、无访问、无状态修改。
* `set_flag(row, package, operation, actor_id=..., effect_origin=...)`：迁入当前 PC 场景范围、
  已声明 Flag、严格 bool、no-op 与 scene.flag_changed 事件业务。内部来源仍经原执行来源验证。

`create_session → initialize_session`；`EnterScene／MoveActor／StepActor／TraversePassage
→ WorldHandler._operate → _world_destination（原准入）→ move_admitted`。
导航的 AdvanceNavigation 仍按持久化资格执行同一 StepActor／TraversePassage 原语；
NPC 可靠交接仍进入原命令协议，受相同控制权／目标规则约束。
SetSceneFlag 的普通命令、PackageEffects、合成 Delta／CheckWorldEffects 仍通过原
WorldHandler._operate 分派至 set_flag。对象开合等继续分派原能力服务。

原 `_initialize_scene` 私有兼容入口委托 ensure_scene，已无重复业务。
resume_session 继续读取已存状态，不调用初始化、不补算访问。

## 初始化与访问分离

新实例 `visited_count=0`，预初始化不记访问；创建时入口 PC 实际进入记 1。
PC 跨场景每次进入目标递增一次，包括首次进入已预初始化的目标；同场景移动不增。
NPC 进入不会改 Session PC 场景或访问计数。原命令重放直接返回原回执，不再进入服务。

不新增访问表、初始化历史或统计。本切片自身不增加 Schema，整合采用 main 的 Schema35；
保留原 Schema34 机关迁移与 main34／ENV34 分支历史协调逻辑。历史 visited_count 按原值保留，
即使旧值包含历史误计也不从事件重算。唯一行为修正是今后的 ensure 不再制造 PC 访问，
删除了旧的“先计一次、预初始化再清零”流程。

初始化对象／能力用已有身份的冲突保留写入，机械场景沿用 INSERT-if-absent。
在首次生命周期协调前已存在的对象状态、能力状态和环境／网格值不会被覆盖；
已初始化实例完全跳过默认装配，保留门锁／开合／耐久／机关、物品／容器关系、NPC
位置／机械／认知、Flags 和环境修改。各领域仍保存和初始化自己的具体数据。
整合后独立环境事实由 WorldFacts 经 scene_environments 表读写，不依赖延迟创建的 scene_instances；
EnvironmentService 在首次访问前已写入的事实不会被 ensure_scene、进入、重访或恢复覆盖。
生命周期不重新初始化环境，也不擅自增加 Package 默认加载／Session 自动环境接线。
机关初值保留 main 的剩余次数、激活次数、重置期限／恢复状态及组件初始损坏判断；
已有机关行采用 ON CONFLICT DO NOTHING，后续 ensure 不恢复次数或重置状态。

## 事件与事务兼容

| 路径 | 保留的事件及顺序 | 保留的阶段 |
|---|---|---|
| 创建 | session.created → scene.entered | session.after_initialization |
| PC 跨场景 | scene.exited → scene.entered | scene.before_departure → scene.after_arrival |
| NPC 跨场景 | actor.departed → actor.moved | world.before_actor_departure → world.after_actor_move |
| 同场景步进 | actor.stepped | world.after_actor_move |
| Flag 真实改变 | scene.flag_changed，含原 execution 来源 | world.after_flag_update |

载荷、信号与阶段顺序保持兼容。离场在原位置取证，到达在目标状态取证；仍由原路径
扩展 Package 事件和保存 Evidence／Outbox。原活动内 savepoint 与外层 CommandRuntime
事务负责整体回滚，目标实例／对象／NPC 创建、位置、PC 字段、访问、事件和回执同成同败。
服务不另建幂等键；无变化遵循原 no-op/world_version 语义。

到达／步进的事务内候选事件继续附带 main 的私有 `_ActorMotion`，由生命周期服务在
实际移动后生成，供 MechanismService 的区域进入等自动触发读取。该元数据不改变公开
事件载荷，不增加认知信号或逐动作感知证据。机关后果及原移动／访问仍在原命令事务
同成同败；同场景 no-op 不创建 motion、不触发机关、不增加访问。

Flags 只表达已声明的普通场景布尔值，不创建门锁、机关、战斗、关系或角色私有知识
的替代写入口。所有原移动合法性、活动／战斗 guard、公开性及目标验证仍在原消费者。

## 范围边界

离场不销毁、重置、休眠、冻结，不推进时间，不终止战斗／持续效果，不调度 NPC 认知。
无动态世界生成、通用脚本、战斗跨场景／追击／逃跑或 SA-01 全面迁移。
保留 v0.4 战斗认知边界：本场战斗内按 Turn Episode、战斗外按 Round Episode 聚合；
本次不增加逐机械 Event 的认知信号或逐动作感知证据，不声称完成尚未实现的 Episode 协议。

W-01 的整体状态和验证证据见 [进度](SCENE_LIFECYCLE_SERVICE_PROGRESS.md)。


## 2026-10-20 跨仓库对齐与证据适用范围

本文件带日期批次／分支／Schema／“未发现 AGENTS”属于原时点证据；最新 main 为 Schema39，实例配置与冻结素材生产装配见 [Instance Progress](INSTANCE_CONFIGURATION_PROGRESS.md)，时间协调见 [Time Progress](SESSION_TIME_COORDINATION_PROGRESS.md)。旧缺口表不能覆盖上述已完成切片。当前开发入口为仓库 AGENTS.md；共同规范只维护于顶层 MC §2.3、§6.5–§6.6。全局审查、局部准入、实际 SQL／投影范围及剩余重构详见 [校验范围审查](reviews/RUNTIME_VALIDATION_SCOPE_REVIEW.md)。本次只更新文档，没有运行新增业务／规模测试。


## 2026-10-20 Scoped runtime validation refactor

本批实现采用 Schema40 的事务内派生占格索引和结构验收记录，普通命令由领域
局部准入保护，操作内复用读取，提交不再默认完整空间审查或库存重建。
逐项能力、读取范围、存档兼容、实际计数及本批验证结果见
[系统性重构验收](reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md)。
上文历史测试数字与旧 Schema/待办保持原批次语境，不作为本轮验证结果。
