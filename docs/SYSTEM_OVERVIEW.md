# 系统与接口总览：State Machine

当前提交、发布及恢复路径按 SM `4d62c6e` 静态核对。此页是实现导航，
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
角色资源池按拥有者／资源 ID 保存独立余额行，SessionMemory 沿原提交边界发布；
角色 UI 和规则 Snapshot 读取同一持久事实。角色装备 /19 与 /20 的护甲、盾牌及另一只手物品按 World 物品实例 ID 绑定，快照机械
镜像由角色与当前物品事实派生；World 转移按实例保护实际装备。
探索命令 `world.equip_weapon` 和 `world.unequip_weapon` 可按确切实例 ID
拿起、替换或放回直接携带的武器。CharacterFacts 保存手持 ID 与握法，
武器类型从 World 物品事实派生；普通攻击继续使用该实例绑定。
探索命令 `world.equip_armor`／`world.unequip_armor` 和
`world.equip_shield`／`world.unequip_shield` 在同一提交内改动确切装备栏 ID、
冲突手持关系及角色基础 AC。可单双手武器按另一只手占用自动切换握法；
普通护甲 AC 使用冻结 RE 的规则函数，受支持的职业与物品范围见
[ITEM-01 装备批次](ITEM_01_EXPLORATION_ARMOR.md)。
`StateMachine.read_character_ac` 在授权窄读中叠加已启用的标量 AC 效果，
战斗规则输入继续读取角色基础 AC，由 RE 在攻击时叠加适用效果和掩护。
角色属性、AC、等级、机械感官与熟练项各由按角色定位的字段／有序项行持有，规则 Snapshot 与事件机械感官从同一事实投影。
角色显式施法来源、已知／准备法术和材料对象绑定以带键记录归 CharacterFacts；
战斗与探索共用施法投影，World 转移按实际对象 ID 的材料反向索引保护绑定。
SM `4d62c6e` 已包含 SPELL-01 的受限实现：材料绑定与法术同一行，角色已知与已准备列表引用同一记录，
`engine-snapshot/20` 以每个来源的一份法术列表表达这些事实。
持续效果以目标效果 ID 行及单条专注组记录归 Effects；角色条件链接、
生命周期时钟和战斗快照投影使用同一记录；独立角色条件归 CharacterFacts 的
带身份顺序行，战斗与探索从独立行和效果链接投影原有 RE 列表。
RE 负责受支持机械规则，SM 负责输入准备、结果校验和采用，不能另算一套规则。
`combat.item` 已通过既有 RuleExecution 接入所选 PC 自饮普通治疗药水的受限路径；
WorldFacts 按实例删除并记录消费，CharacterFacts 写入 HP，详见
[ITEM-01 Batch B](ITEM_01_COMBAT_HEALING_POTION.md)。
`world.use_item` 沿同一路径以 `engine-evaluation/28` 和探索快照 `/20` 支持
按实例自饮普通治疗药水。满 HP 时保留药水。成功时药水、HP、RNG、事件和
回执一起提交。探索喝药不消耗战斗动作，也不推进时间。已开始的时间推进
须先结算，之后可以重试喝药命令。
`world.use_item` 的 `engine-evaluation/32` 路径也可从确切的保留型物品实例施放
已审核的即时触及治疗。World 核对持有与可取用性，RE 校验物品定义中的施法活动及充能支付。
充能、治疗、RNG、事件和回执共同提交，充能耗尽时保留原物品。
治疗法杖的 `engine-evaluation/41` 路径验证已持有的确切同调实例，允许选择一至四环疗伤术或群体疗伤术。
World 组装区域中心视野和局部路径事实，RE 验证选区和各目标并一次结算充能。TimeDriver 采用该实例
在黎明的随机回充，充能上限、最后一格导致的装备移除以及 RNG 都由各自的共享事务提交。
SPELL-01 沿同一 RuleExecution 接入战斗内一环 Cure Wounds，角色余额与本场
支出分别归 CharacterFacts 和 CombatRecords，治疗及支付一起采用。
SPELL-02 候选把即时治疗与单目标豁免伤害接到同一普通法术采用路径，
机制支持由冻结 RE 分类；已识别的生命／临时生命等未接线后果拒绝。
EFFECT-01 隔离候选在内部受控 `RuleExecution` 调用中采用 RE 的二环单目标
Hold Person 效果图、目标 TurnEnd 重复豁免、施法者 TurnEnd 专注时钟，以及
正 HP 受伤后的专注维持／移除。效果、链接条件、时钟、施法位、生命值和回执
沿既有共享提交与发布边界持久化；公开施法、Pass 与带效果攻击准入尚未开放。
RE 世界时间期限接口与 SM 的期限消费不属于此批已验收能力。
EFFECT-02 在 RE `/42` 下将已验证的二环单目标 Hold Person 接到公开
`combat.intent`。正 HP 的受支持遭遇可用普通 Pass 推进回合，有效 Hold 图
结束后继续使用同一入口。对专注施法者的受支持攻击将伤害与专注变化共同采用。
公开可用性查询仍由 RE 提供攻击预检。当前范围及边界见
[Session Management 进度](SESSION_MANAGEMENT_PROGRESS.md#2026-11-18-effect-02-公开-hold-person-命令)。
RE `/47` 已接通三环 Hold Person 在同一专注组内选择两个目标。
各目标单独豁免，成功时只移除自己的效果。法术位和专注沿原有共享提交路径采用。
参见 [Hold Person 双目标](SESSION_MANAGEMENT_PROGRESS.md#2026-11-19-战斗-hold-person-双目标)。

探索施法可通过 `world.cast_spell` 采用 RE 的法术位扣减和定时效果。
次级复原术的施法者可通过 `StateMachine.query_spell_condition_choices` 查询本次
合法触及目标的独立条件选项。选项按角色条件记录顺序只返回名称与不透明 ID，
不含来源；命令用 `selected_condition_id` 和查询的世界版本精确解除一条记录。
RE 按固定输入和绑定法术核对解除，SM 按目标身份核对并采用对应记录、法术位和回执。

固定点 Silence 复用空间法术的持续时间和专注记录。SM 从施法者实际位置及区域几何
冻结当前言语成分可用性，RE 沿原有成分规则判定。效果覆盖应用时已在区域内的施法者，
也覆盖移动后开始的施法。这里的 Silence 入口使用普通动作和法术位。本入口的
区域后果限于言语成分，声学、耳聋、雷鸣伤害免疫与 Silence 仪式施放仍待独立实现。
受信任的环境管理者可以通过 `world.apply_strong_wind` 指定当前探索场景中已受到强风的
格子。该命令只终止与覆盖相交且规则允许被强风吹散的空间法术效果，并记录风源 ID。
Effects 和 SessionTime 共同保存及消费到期时间。`drop_concentration`
通过已有战斗命令入口解除受支持的专注图。详见
[Session Management 进度](SESSION_MANAGEMENT_PROGRESS.md#2026-11-18-effect-03-定时施法和主动结束专注)。
探索期间，获准的新专注施法与旧专注组释放共同提交。Effects 按原身份结束旧角色效果或
空间效果，再安装新组。角色定时专注沿 SessionTime 到期，也可以通过 `world.drop_concentration` 解除。
SessionTime 处理同一角色在同一个探索时点的自然到期时，Effects 按该角色效果列表的保存顺序逐条结束，
并在同一次时间推进中采用终止事件。不同消费者之间有依赖时沿已确认的顺序或等待处理。

探索隐形术的首个公开切片采用二环单目标应用。Effects 按真实条件来源与原施法登记，
成功的光亮术在同一次 `world.cast_spell` 中终止该应用。法术结果、条件关联、专注、
事件和回执共同提交。非专注大步奔行的原施法者可以通过 `world.dismiss_spell` 提交
目标、效果 ID、origin 和原施法命令 ID，精确结束所选应用且无需支付。
攻击和伤害触发需各自接线。

探索的普通长施法和仪式由 `activity.start_spell_cast` 建立可重开的 Activity。Effects 持有施法期间的专注，SessionTime 到达期限时保存完成等待。授权 `activity.finish_spell_cast` 沿同一规则采用与回执事务支付、施放并释放等待。获准开始时结束原专注，主动放弃施法专注会中断正在进行的活动。首批验收为完成后无需专注的自身定时效果。

同一活动入口的 `engine-evaluation/40` 可鉴定接触中的魔法飞弹魔杖。开始后丢失接触或所选珍珠会中断，
完成时重验两件实例。普通施法扣一个法术位，仪式免法术位，值至少 100 GP 的珍珠均保留。
物品属性、用法、当前充能和同调信息保存到施法者获准读取的活动记录。

探索的治疗真言使用当前施法者视野、60 尺射程和规则通路。二环疗伤术按实际环位结算。
RE 的共用治疗规则使用施法者侧生命门徒和目标侧希望信标；SM 提供相关角色与持续效果事实，
在同一事务采用治疗、支付和 RNG。树肤术的 AC 下限与治疗共存。

RUO-31 候选的单目标探索施法可由 RE 返回需要目标同意。`RuleExecution` 保存待答复及原命令
回执。受控目标通过 `choice.respond` 作答，接受后按当前施法条件重新求值。答复、法术位、
效果、事件与回执通过同一提交边界采用。等待窗口使用 60 秒游戏时间。
多目标探索施法按目标保存授权答复。施法者确认原名单中的自愿子集后，RE 按当前事实
重验目标、付费和效果。目标数沿绑定法术的上限，整次施法只支付一次。

受支持的 NPC 武器攻击会在命中后、伤害前向 PC 提供护盾术答复。当前进程复用固定攻击输入
继续结算，反应、法术位、攻击后果与回执共同提交。护盾持续到防守者下一回合开始。

固定 RE `/48` 将单个定时步行或技能加值纳入普通攻击的依赖判断。
攻击跨越到期轮界时仍由 SessionTime 在同一提交内清理效果。详见
[普通攻击读取定时效果](SESSION_MANAGEMENT_PROGRESS.md#2026-11-19-普通攻击读取定时效果)。
普通战斗攻击与步行共用已审核的探索定时效果投影。效果可同时贡献不同属性，
树肤术的 AC 下限与大步奔行的速度加值分别到期，移动账本保留已花费的尺数。
探索移动接入固定环境来源的进出区域效果。World 提供格子覆盖，RE 从来源的效果定义
算出要添加或解除的效果，Effects 和位置共同提交。普通探索属性或技能检定把角色已有的
效果交给 RE，由现有检定规则计算适用的数值加成和优势或劣势。详见
[固定区域环境来源](ENVIRONMENT_SERVICE.md#固定区域环境来源)。
战斗中的酸液飞溅从选定格计算球形范围，按 RE 确定的顺序采用各目标的豁免和伤害。
启用战斗触发的固定中毒区域在进入或回合开始时采用同一套区域效果，离开时按来源移除。
战斗 `combat.intent` 接入受支持的单目标 Haste 施法、自然到期和主动结束专注，使用
RE `dnd5e-evaluation/44` 的 `engine-evaluation/30` 结果。Effects 在同一提交内依序
解除旧效果并采用返回的 Lethargy。疲乏持续到目标下一自然回合结束。
单条 Haste 效果下，受益者可用额外动作执行一次已支持的普通武器攻击。受支持的正 HP
伤害若使专注失败，会在同一提交中解除 Haste 并施加疲乏。混合效果与其它额外动作有独立准入。

固定 RE `/52` 的候选通过原战斗施法入口采用命中产生的战斗效果，Effects 将初始
回合时钟和减速随伤害共同提交。普通步行读取效果，回合推进采用 RE 的到期移除。
参见[命中速度效果](SESSION_MANAGEMENT_PROGRESS.md#2026-11-20-法术命中与战斗减速候选)。

受支持的 Hold Person 施法者被普通武器攻击打到 0 HP 时，Combat 与 Effects 共同
采用生命值、专注结束和效果清理。对应的死亡豁免和合法关闭沿用类型化 RE 结果。
参见 [0 HP 专注与战斗关闭](SESSION_MANAGEMENT_PROGRESS.md#2026-11-20-专注者降到-0-hp-与战斗关闭候选)。

`activity.long_rest` 将可信主持确认的单次无中断长休接入 SessionTime 和
RuleExecution。非战斗秒制 Session 在同一提交中推进 8 小时，采用固定 RE
的角色 HP 和资源恢复，并保存 Activity 的最近完成时间及回执。
参见[单次无中断长休](SESSION_TIME_COORDINATION.md#单次无中断长休)。

`activity.short_rest` 复用这条时间与提交路径。可信主持确认一次无中断的短休后，
SM 推进一小时，并与 RE 返回的所选生命骰支付、治疗和适用的资源恢复共同提交。
参见[单次无中断短休](SESSION_TIME_COORDINATION.md#单次无中断短休)。

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
`get_spatial_view`、`get_illumination_area` 及 `environment` 的授权查询。
对象详情、场景对象列表、空间视图和环境区域查询共用观察者的视线判断。Grid 先用当前
地图排除被墙、实心地形或全掩护挡住的目标，再为剩余目标及必要视线格准备光照。
角色的有效感官范围来自持久基础感官和该角色当前生效的 Effects。RE 结合这些范围、
地图遮蔽、普通光照和魔法黑暗完成最终判断。光照和环境的区域入口也接受只含一个格子的请求。

探索的细网格场景中，`get_scene_view` 让玩家、其旁白视角和获授权 NPC 从各自
的观察位置读取当前角色与对象。每个角色按依赖版本保留已筛选的场景成员，授权、
当前世界版本及玩家自身状态仍按此次请求读取。管理员视图、历史事件取证和现有
战斗视图继续使用各自路径。动态物体视觉遮挡以及动态雾烟的视线规则仍有独立接入边界。

探索视野的目标状态由 `visual_target_state.py` 按当前 Effects 和 World 实例事实计算。
观察者的有效识破隐形和目盲由 `visual_observer_state.py` 投影，RE 结合真视、盲视、
普通光照、魔法黑暗和遮挡裁定。物体实例可显式配置其基础隐形，详情、列表和空间／场景
视图都采用有效隐形。妖火施放时，World 确定区域占用，RE 声明求值所需的角色和请求版本，
SM 按这些角色组装快照。选中目标后由同一确切效果提供随目标移动的 10 尺微光和隐形压制，
原隐形来源仍按自己的期限结算。

`get_spatial_view` 的 `tremor_cells` 在授权读取中提供地点线索。`world_surfaces.py`
读取当前角色接触、地图连续表面与可由物体位置切断的连接，通过局部范围查询收集候选；
RE 判定能否定位。目睹目标仍按视野路径判断。已在效的 Stonecunning 使用石面限制，
主动启动与支付以及运动物体线索需要各自的已验证事实。

探索的侦测魔法沿用施法活动与 Effects 专注期限。`get_scene_view` 的玩家视图和
`get_spatial_view` 的获授权角色视图在 `detection_clues` 返回 `magic_present`。
这个提示仅表示附近有可感应的魔法。范围查询从观察者当前格子定位相关角色、物件和
效果，并沿实际容器关系检查内容。World 物件可声明
`detection_barrier: {material: wood, thickness_mils: 500}`。`thickness_mils` 的单位是
千分之一英寸，岩石、泥土或木材达到 12000、金属达到 1000 就阻断侦测，铅的任意已知
正厚度阻断侦测。障碍作用于闭合容器的外壳或穿过物件占格的路径。旧地图墙段、全掩护和
缺少材质厚度的闭壳仍可使某条路径无法确认。共享门在两侧阈口读取同一门的当前开合、
破坏状态及材质厚度。静态墙和实心地形有显式材质、厚度及局部空间关联后再验证穿透和
阻断（`LF1123-D49-01`）。固定位置法术使用当前原点，
效果覆盖体积需要有界查询，并以原点在范围外、覆盖进入范围的用例验收（`LF1123-D49-02`）。
主动读取灵光有独立接入边界。
[D&D 2024 Detect Magic](https://www.dndbeyond.com/spells/2619097-detect-magic)

光照区域查询只为缓存缺失的格子筛选有效光源。Grid 按光源到目标格之间的范围查找墙、
物体占地和共享门。物体可在空间属性中声明 `light_opaque`，PC 模板和 NPC 也可显式声明
其已有占格遮光。灯具和物品上的 Light 法术共用连续投影，格子等级和受照多边形来自
同一份结果。遮挡事实变化沿既有场景光照版本失效。

定向灯具沿物体朝向照射，燃料由世界时间到期处理。场景配置和世界时钟确定室外基础光。
Effects 持有探索施法产生的发光和魔法黑暗。施法可达性、魔法黑暗遮罩和效果驱散继续沿用
RE 的规则范围。区域查询、整图规则输入和事件投影共用有效光源，并携带阳光和魔法黑暗标记。
当前战斗准入拒绝依赖动态昼夜、物品发光或探索光照效果
的场景。接口职责见 Module Contracts C09.1。
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

/19 与 /20 装备由 CharacterFacts 按角色保存到 `character_equipment`，角色运行时字段
共享 World 的物品实例引用。RulesAdapter 将绑定及其派生规则值随固定战斗快照交给 RE。
可用性与提交共享相同的装备事实，提交前重验世界版本及快照。

| 表示 | 持有者／修改责任 | 读取与隔离边界 |
| --- | --- | --- |
| 持久事实／配置／版本／RNG | 各领域通过 SqliteStore 共同事务写 SQLite | 创建、冷读、恢复、回执及可靠查询的持久依据 |
| 物品事实与库存视图 | `objects` 的带类型列持有物品 slug／充能／基础可访问性与携带／容纳关系 | WorldFacts 按实例和实际容器关系读取；当前容器成员沿 ObjectRuntime 持有受控内部物品引用。CharacterFacts 按需生成隔离的 RE `InventoryState` 和角色携带类型 |
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
`effect_time.py::EffectTimeConsumer` 将受控 RE Longstrider 效果期限接入同一有界消费者路径；
其余 Effects 时间消费、活动、秒制 NPC 兼容、活跃战斗精确恢复及多人负载仍有独立限制。

## 6. 跨仓库接口与按任务找代码

开发时请从逻辑工作区的 sibling checkout 阅读同名契约；表中的 GitHub 链接供远端浏览。

| 任务／接口 | 从哪里读起 | 进一步核对 |
| --- | --- | --- |
| 宿主请求／授权 | `models.py::TrustedContext`、`models.py::CreateSessionRequest`、`commands.py::TypedCommand` | `service.py`、SessionAccess；认证由外部可信层完成 |
| 修改容器重量 | `world_containers.py`→WorldFacts→运行字段和修改监听 | MC §2.3a、[World](../../agentic-trpg/docs/WORLD_SUBSYSTEM_DESIGN.md)、容器 Progress；runtime-change |
| RE 返回结果后谁采用、谁提交 | [RE 求值与采用](#re-求值与采用)、[MC §6.1](https://github.com/agentic-trpg/agentic-trpg/blob/main/docs/MODULE_CONTRACTS.md#61-执行时序-decided-partially-implemented) | [RuleExecution](../src/trpg_state_machine/rule_execution.py)、[真实检定回归](../tests/test_session_memory_rng_failures.py) |
| SQLite 已提交但 RAM 发布失败 | [事务、发布和恢复边界](#5-事务发布和恢复边界)、[MC §6.4](https://github.com/agentic-trpg/agentic-trpg/blob/main/docs/MODULE_CONTRACTS.md#64-原子性与故障-decided) | [Store](../src/trpg_state_machine/storage.py)、[故障回归](../tests/test_session_memory_runtime.py) |
| 活跃战斗进程退出后能否继续 | [Session Management 当前边界](SESSION_MANAGEMENT.md#当前会话保存与恢复)、[MC §8.2](https://github.com/agentic-trpg/agentic-trpg/blob/main/docs/MODULE_CONTRACTS.md#82-必须明确不支持的场景-open)、[MVP §6.3](https://github.com/agentic-trpg/agentic-trpg/blob/main/docs/MVP_SCOPE.md#63-state--memory--knowledge) | [恢复入口](../src/trpg_state_machine/service.py)、[独立进程回归](../tests/test_dnd_hardening.py) |
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
