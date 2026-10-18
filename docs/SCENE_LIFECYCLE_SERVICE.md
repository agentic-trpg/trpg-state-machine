# SceneLifecycleService

本地实现切片，基线 `9ce60cb`，分支 `feat/scene-lifecycle-service`。
设计依据为相邻架构仓库当前 MODULE_CONTRACTS §0.3／§0.3a、World Subsystem
Design §2／§4／§5 及 2026-10-17 v0.4 评审记录；MODULE_CONTRACTS 是最终权威，
本文件描述 SM 的实际内部接口，不冻结新的跨模块 ABI。

## 所有权与装配

StateMachine.scene_lifecycle 与 WorldHandler.scenes 为同一个 SceneLifecycleService。
服务显式引用 SqliteStore、SessionAccess、CharacterFacts、WorldFacts、EventCapture、
NPCBootstrap，不引用 StateMachine，不持有状态缓存或角色位置副本。

| 事实／业务 | 唯一写入／协调责任 |
|---|---|
| Scene 实例、Flags、visited_count、对象／机械场景事实 | WorldFacts |
| Actor 唯一 scene／cell，初始角色身份 | CharacterFacts |
| PC 当前场景字段、权限、固定绑定与提交元数据 | SessionAccess |
| NPC 初始认知与运行数据 | 原有 NPCBootstrap；仅角色首次创建时调用 |
| 初始化／进入离开／PC 访问／Flags 的业务协调 | SceneLifecycleService |
| 移动准入、通路／占用／门锁、命令分发、其他世界业务 | WorldHandler 与原领域服务 |
| 权限、版本、命令重放／回执、事务、事件扩展及持久化 | 原 CommandRuntime／共享提交路径 |

## 实际内部入口与调用链

这些是可信进程内事务参与者，不是绕过 submit_command 的玩家／Host 写 API。
全部写入口 require_write；服务不自行开启事务、不 commit、不递归提交命令。

* `ensure_scene(sid, scene_id, package)`：WorldFacts.initialize_scene 首次写入场景
  默认 Flags 和对象；随后委托原对象能力、角色／NPCBootstrap、机械场景初始化。
  返回是否首次创建。已存在实例直接复用，不补造旧存档缺失组件，不套用 Package 初值。
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

不新增访问表、初始化历史或统计。Schema／存档格式不变，历史 visited_count 按原值保留，
即使旧值包含历史误计也不从事件重算。唯一行为修正是今后的 ensure 不再制造 PC 访问，
删除了旧的“先计一次、预初始化再清零”流程。

初始化对象／能力用已有身份的冲突保留写入，机械场景沿用 INSERT-if-absent。
在首次生命周期协调前已存在的对象状态、能力状态和环境／网格值不会被覆盖；
已初始化实例完全跳过默认装配，保留门锁／开合／耐久／机关、物品／容器关系、NPC
位置／机械／认知、Flags 和环境修改。各领域仍保存和初始化自己的具体数据。
本基线的环境事实为原 mechanical_scenes；未提交工作区中的独立 EnvironmentService
未纳入本交付，未声明其集成验收。

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

Flags 只表达已声明的普通场景布尔值，不创建门锁、机关、战斗、关系或角色私有知识
的替代写入口。所有原移动合法性、活动／战斗 guard、公开性及目标验证仍在原消费者。

## 范围边界

离场不销毁、重置、休眠、冻结，不推进时间，不终止战斗／持续效果，不调度 NPC 认知。
无动态世界生成、通用脚本、战斗跨场景／追击／逃跑或 SA-01 全面迁移。
保留 v0.4 战斗认知边界：本场战斗内按 Turn Episode、战斗外按 Round Episode 聚合；
本次不增加逐机械 Event 的认知信号或逐动作感知证据，不声称完成尚未实现的 Episode 协议。

W-01 的整体状态和验证证据见 [进度](SCENE_LIFECYCLE_SERVICE_PROGRESS.md)。
