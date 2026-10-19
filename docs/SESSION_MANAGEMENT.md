# Session Management

2026-10-19；SM 本地实现说明。职责依据上游 MODULE_CONTRACTS §0.3、§2.4、§4.3、
§10；不修改上游规范，不把目标完整实例化宣布为已完成。
本批次基线 `3fe36e2`，验证与缺口见 [进度](SESSION_MANAGEMENT_PROGRESS.md)。

2026-10-19 地图后续（基线 a25c35e）：SessionRuntime 通过 GridMaps 领域接口组织地图
运行表示的准备和释放；SceneLifecycle 初始化后准备，原 CombatHandler 机械场景补建后
准备，save／resume 在授权读取事务内准备当前场景。其他场景可首次读取时准备。
WorldFacts 保存权威 JSON，Grid 负责只读解析表示、基础拓扑与具体查询算法；同一 Store
有界复用，每次先确认事务可见的实际编码，不靠 world_version 判断配置。
回滚／旧读取事务／另一连接更新按各自可见内容选择；准备不产生进入事件或 NPC 模拟。
释放仅丢弃可重建表示，Store.close 清理，旧 View 保留原快照。详见
[Grid 生命周期与算法](GRID_SERVICE.md#地图表示与生命周期2026-10-19)。
本轮未扩展 WorldObject／Character／Item 持久化，也未接入 RE 固定素材接口。

## 三部分职责与现有执行路径

| 逻辑责任 | 实际组件和调用路径 |
|---|---|
| Session Creation | `StateMachine.create_session → SessionCreation.create`；校验身份、请求、PC、Package 与实际 RE，组织领域初始化及创建幂等键 |
| Session Access Control | `SessionAccess`；Session owner、Actor 控制、可信 GM／NPC 授权、可靠交接来源及 Package pin 核对 |
| Session Runtime | `SessionRuntime`；固定 Session 元数据写入、命令指纹／重放、版本检查与更新、RNG 上下文／转换保存、事件发布协调、回执保存／读取 |
| 世界／活动流程 | 仍是 `CommandRuntime → WorldHandler / ActivityHandler → 领域服务` |
| 真实规则流程 | 仍是 `CombatHandler → RuleExecution → RulesAdapter → RE`，读准备、事务外求值、写事务重新核对 |
| 合成规则流程 | 仍是 `MechanicalRuntime`；显式合成协议，不冒充真实 RE |

SessionRuntime 的 `publish` 协调世界版本与 EventPersistence，`finish` 将回执加入同一事务。
三条路径共用这两处；各自的领域准备、候选求值、快照／Delta 验证、savepoint 和拒绝策略
保留。`pending_choice` 和合成路径的 `pending_choice_unsupported` 继续按原契约处理。
资源、HP、物品、位置及战斗事实仍由 Facts／Records 修改；EventPersistence 继续独占
`event_seq` 推进及 Evidence／Outbox／感知队列持久化。没有新 CommandRuntime、事务系统、
数据库、状态缓存或位置权威。

`SessionAccess._world_operation_capability`、`require_legacy_npc_time` 与 NPC 战斗目标资格
仍为过渡归属：它们属于领域能力／时间消费者／感知资格，暂未迁出；行为保留。
本批次不实施 SA-01，不扩大战斗世界操作支持。

## 创建、绑定与规则身份

创建前无 Session，创建协调器以已验证 Package 的 ruleset_id、data_revision、
evaluator_version 调用实际 RE 校验；synthetic.atomic 也核对配置的 evaluator。
foundation 的 `none/1` 仍是无规则求值的占位契约。创建重放继续返回既存结果，
不因后续部署变化重新执行初始化。

一个既有 `BEGIN IMMEDIATE` 原子保存 Package ID／版本／内容摘要、三项规则绑定、
principal／player／PC 控制关系、世界版本 0、独立 RNG 记录及引用，并调用：

1. WorldFacts 创建全局 Flags、事实；WorldTimeService 保存选择的整体 TimeBinding。
2. SceneLifecycleService 创建入口和原先预初始化范围内的空间场景；CharacterFacts 创建
   PC／NPC；NPCBootstrap 仅安装首次 NPC 数据。
3. CharacterFacts／CombatHandler 安装现有显式机械组件、初始位置及 RE RNG；
   WorldFacts 安装物品和锁／凭证；做已支持实例结构和空间一致性检查。
4. 现有 PackageEffects 扩展创建事件，EventPersistence 保存事件／Outbox，保存创建回执。

任一步骤或最终提交失败全部回滚，创建不留下半个 Session。SessionCreation 不解析
Container、Light、角色或环境领域配置；省略值仍由现有 Package／领域 Schema 的明确默认
处理，非法显式值、缺失 Required、无效引用仍拒绝。本轮未新增默认规则。

运行时 `SessionAccess` 先核对调用者及 Package ID／版本／摘要／三项规则绑定。
Package 只证明调用者交来的定义与 pin 相符，不能定义或覆盖 Session 规则身份：

| 请求／校验 | 持久化身份来源 |
|---|---|
| verify_ruleset，战斗准备，非战斗／战斗快照和 RNG 读取 | 已授权读取的 Session row → RulesAdapter.session_binding |
| RuleEvaluationRequest，Openable／Mechanism 检定辅助请求 | Session row；兼容旧 request 参数时内部重新读取并核对 pin |
| ActionAvailabilityRequest | 与查询快照同一读取事务取得的 Session row |
| mechanism-effect/1 端口请求 | Session row；生产仍为 unsupported，不宣称真实 RE 已支持 |
| 合成 RuleEvaluationRequest 与 evaluator 核对 | 从 Session row 构造的 detached SessionSnapshot |
| 命令指纹 | Session 的摘要和绑定；规范编码与旧回执逐字段兼容 |
| Event Evidence、感知入队证明和历史证据绑定校验 | 读取 Session 固定绑定；不重新从 Package 定义身份 |

SM 读取绑定并显式交给 RE；RE 不读取 SM 数据库。结果仍匹配本次请求的绑定、Session、
命令、版本和 RNG；Availability 也校验返回身份／绑定／快照版本。
配置不匹配发生在真实求值、资源支付及权威 RNG 推进前。拒绝和 OCC 冲突不会推进 RNG；
已提交重试经授权后返回原回执。旧 Package 参数和公开 API 保留。

### 固定素材集实际状态

`pyproject.toml`／`uv.lock` 仍固定 RE/SRD `d596161060555630019163ab301e1d25fd816549`，
distribution 0.6.0、evaluator `dnd5e-evaluation/36`。该实际安装接口没有 FrozenRuleset，
`verify_ruleset` 每次重新计算实际可变 loader 的全部生效素材摘要，无摘要缓存。

只读核对上游 `3718f7c3d35424cc7910bcc19673083a357afdb7`：已有
`freeze_ruleset`／`get_default_ruleset`／`FrozenRuleset`，但上游分支基于 evaluator `/37`，
与本 pin 分叉，而且不含 SM 当前依赖的 `evaluation_openable.py`。直接换 pin 不是兼容的
素材接口接入，会引入其他协议变化并失去现有 Openable 入口。因此本轮**未接入快速校验**，
也没有将已绑定 Session 改为 `/37`。后续需要包含 Openable 的兼容固定候选、相应协议／
旧 Session 迁移审阅，再按现有 Git SHA 依赖管理方式接入。没有修改 RE 仓库、复制 RE
实现、按 Session 复制全量素材，或对可变 loader 保存旧摘要绕过检测。

## 实例化、激活与进入

`SceneLifecycleService.ensure_scene` 是实际使用的内部协调入口：要求调用方已有写事务
和操作授权，再核对 Session 固定 Package。只初始化所需场景及现有领域创建边界确定的
对象、NPC 和机械场景依赖。已存在实例复用，检查当前支持的必需行，缺失时明确
`incomplete_session`，不从 Package 补造或重置。首次装配前已有的领域事实继续按原
INSERT-if-absent／ON-CONFLICT-DO-NOTHING 规则保留。

调用者包括创建流程、PC 移动和 NPC 移动。抽象场景的合法 NPC MoveActor 现在也可在
原命令 savepoint 内确保目标存在，无需 PC 先进入；通路、活动、控制权和战斗 guard
继续先通过。查询只验证可达性，不创建；入场事件和访问计数仍只有实际 PC 进入时产生。
实例创建、NPC 位置改变、版本、事件和回执同事务，失败或冲突回滚创建部分。

实例化安装权威记录；NPCBootstrap 安装初始 NPC 数据不等于执行模拟／认知。
激活／自动机关行为仍由已有领域流程在对应实际事件时触发，`ensure_scene` 不制造
玩家进入事件或自动提交激活。当前策略仍是入口及空间场景提前创建、其他场景按需创建。

没有新建通用依赖框架。跨场景机关目标若没有现有领域创建／准入支持，仍明确拒绝；
并未将所有操作改为“找不到目标就创建”。Mechanism 组件／效果的任意依赖图、单个对象
创建、后台 Activity 目标创建、动态 Actor／Item 生成仍需各领域提供真实创建能力。
静态只读导航模板和既有未实例化门初始态预测仍存在，详见下表。

## 持久化、恢复与具体后续缺口

数据库仍为 Schema36；没有搬表、新迁移、字段改名或历史数据重算。旧命令指纹编码和
回执序列兼容。旧机械版本、legacy audit、活跃战斗跨进程 `combat_unrestorable` 及
显式 Package 源转换机制保留，不自动重绑已有 Session。

save／resume 验证 Session／Package pin、必需全局／PC／RNG／当前场景记录，以及已实例化
场景的对象、NPC、网格和已支持对象能力状态行。当前 D&D Session 还校验安装 RE；
已选择机械版本时验证已保存 Actor／组件和 RNG；不要求场景中的普通 NPC 获得新增机械
档案。规则请求仍要求完整局部快照。缺失或不支持继续明确拒绝，FK 损坏仍由
Store 拒绝打开。正常恢复只读取当前事实，不重新应用模板或初始化访问次数。
这些是当前支持数据的结构约束，**不代表完整有效配置已持久化**。

| 后续领域批次 | 目前已保存 | 仍依赖 Package 的具体配置／缺口 |
|---|---|---|
| WorldObject / WorldObjectService.view | 对象身份、位置／容器／carrier、朝向、开合状态 | definition_ref、能力声明、名称／可见性、重量和完整有效配置仍从 world_objects／scene objects 组合 |
| Container | 父子／carrier 关系、实际物品 JSON | 容量、重量规则、容纳限制、contents-access 等由 ContainerService.config 查模板；未持久化完整配置 |
| Spatial / Grid | 当前机械网格、对象位置／朝向、环境版本 | 占地、移动阻挡／困难地形／遮挡／掩护及防护配置仍查模板；未实例化场景仍有有界静态网格／门初始态预测；已实例化网格不回填 |
| Openable / Lock | 开合、barred／jammed、锁状态和凭证关联 | interaction／bar／jam 策略、方法、DC、tool／leverage、锁定义仍查模板 |
| Light | enabled／mode | 模式半径／亮度、遮挡策略、开关资格、燃料／持续消费仍未完整实例化 |
| Mechanism | 七字段状态、次数／激活、重置期限、激活去重 | 操作／触发器／组件／目标／动作／频率／重置策略、规则效果定义仍查模板；缺少一般跨场景依赖创建，真实效果 RE 端口仍 unsupported |
| Durability | current_hp | max_hp、破坏阈值／后果／防护配置仍查模板；不能用模板默认修复旧实例 |
| Character / NPC | 当前 PC 核心和显式机械 actors／components、NPC 已支持运行数据、唯一位置 | 场景／通路资格和 NPC senses／部分身份配置仍查 Package；完整角色有效配置和动态创建／升级不是本批次成果 |
| Item | 可携带实例 item_json、quantity／charges、当前位置和关系、机械库存投影 | portable_items 成员资格／transfer、装备与规则素材引用、锁凭证／容器规则仍有 Package 依赖；缺少一般生成与完整有效属性升级 |
| Session 外的静态行为 | Package pin；世界／场景 Flags 和当前世界时钟 | PackageEffects 事件定义、passages／exits／public_interactions、world_facts 可见性、NPC perception 定义仍使用固定 Package |

后续应由领域 Schema／创建器保存最终有效配置与初态，并给出版本化记录、实际读取入口和
显式旧数据迁移；本轮没有添加无调用方的空初始化接口来宣称这些缺口已完成。
