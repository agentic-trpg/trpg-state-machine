# Session Management

## 2026-10-22 Light 字段与固定查询准备

Scene.lighting 的 sources 和 candidates/geometry 分别采用同一 SessionMemory 中的维护版；
cells/projection/topology 依赖二者的常数大小组合。读取命中不展开光源叶子，外层 parsed
消费者也只继承字段版。即使没有开启光源，结果仍登记地图／几何子版本，防止基础照明
改变后误复用；版本清理保留被组合 stamp 使用的子版本。字段值只标记计算开始时对应版，
后续变化不能将旧计算冒充新版本。

源成员／开关／模式、移动子树及携带者、容器透光／开合／摧毁与相关配置按旧／新实际
Scene 维护源版；实际阻挡形状、地图和共享门端点维护几何版。无关空袋归属和无关非阻挡
物体位置不扩大失效。必要维护沿原父子／能力桶；一次同步 TEMP change callback 内共享
不可变对象点读取，callback 结束即丢弃，嵌套触发器另有临时读取 memo。这不是跨语句候选
缓存，也不是新的事务、实例目录或事实来源。

这些字段随原 UnitOfWork 候选版本根处理回退、COMMIT 后发布和故障恢复；旧固定读取输入
不被更新，跨连接恢复仍使用已有 generation 栅栏，卸载／淘汰后从持久实例准备。GridView
的索引／覆盖层和地图资格随各自固定输入保留，不依赖后来当前 Scene 指针。
范围、真实计数和验证见[本批验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md#2026-10-22-local-query-reuse)。

## 2026-10-22 SQL 失败协议与局部元数据

RuntimeConnection 的 execute／executemany（含参数源中途失败）采用失败范围协议：
写范围任一语句抛错即设置 UnitOfWork.statement_failed，清空读取 memo；RAM guard、
写入要求和提交边界均拒绝 transaction_failed。即使调用者捕获 SQLite 异常，也不得
继续读取候选、写入或发布。成功 ROLLBACK TO 失败前保存点恢复原候选字段、版本根、
change log 长度及失败标记，清除 memo／派生候选；此后可以继续，保留保存点前合法修改。
现有保存点上下文也在退出前检查失败标记，自动回滚并抛错。SQLite ROLLBACK 已取消整个
事务时不再 RELEASE 不存在的保存点，外层丢弃候选。没有保存点的失败必须整体回滚。
既有参与者 finally 中的 RELEASE 允许用于清理，保留原 storage_error；它不清除失败
标记，不属于恢复，不能放行之后的候选读写或外层提交。
Store 首次升级到 BEGIN IMMEDIATE、固定 SQL 快照及重建保存点时若失败，也设置同一失败
标记；升级可能已经结束原读取事务，不能捕获错误后继续使用旧候选或空提交发布。
COMMIT 结果未知及提交后发布失败仍按原暂停／恢复协议，不能误当作未提交失败。

SessionMemory.versions 与 scope.memory_versions 现在是原机制中的私有 VersionMap 根句柄。
事务启动、保存点、回退、accept_read／publish 共享不可变 AVL 节点；单键查询／推进为
O(log N)，根复制 O(1)，不逐次复制／遍历全表，也无事务覆盖链。保存点仍复制本事务的
memory_pending，成本属于当前候选而非所有已加载实例。旧根只由实际旧范围／保存点持有，
不被新根链式引用；结束后正常回收。原预算高水位为 capacity×32：显式裁剪保留活缓存
依赖及库存组合版本；若保留量超过 capacity×16，释放可重建缓存，留下至少半预算间隔，
避免接近高水位时每操作反复全量裁剪。卸载／恢复允许按生命周期释放必要范围。

库存模型字段依赖清单（当前固定 InventoryState 的六个条目字段）：

| 来源 | 失效依据及保留的一致性要求 |
|---|---|
| mechanical_components | 库存／资源／casting／effects 的原完整闭包；已知库存补丁按实际拥有者推进，未知聚合写入保守推进共享版本 |
| characters | 身份、inventory_json（carried slugs）、resources_json／effects_json（拒绝非空旧来源）；HP／max HP／XP 不使库存失效 |
| actors／mechanical_actors | 拥有者存在／身份、成员版本；坐标／HP／行动及装备元数据不属于 InventoryState 字段，mechanical JSON 从对象变为其他形状仍失效并拒绝 |
| objects | 身份、归属、item_json（slug／数量／充能／可访问标记等）、state；只改落地坐标不失效库存 |
| 祖先及能力状态 | 沿旧／新父子链到实际 carrier；开合、barred、locked、零 HP 损毁边界；非零 HP 变化及 jammed 不改变现有库存结果 |
| instance_configs | 对象及祖先配置影响实际拥有者，actor 配置影响该角色，Session layout 使用共享后备版本 |

跨角色转移维护两端，嵌套祖先访问变化维护实际拥有者。组合标记仍保留 aggregate、成员、
layout 依赖，裁剪不得丢失它们。CharacterFacts._validated_rule_components 是一次完整
严格校验的缓存结果；rule_components 由其投影字典，inventory_model 直接复用类型值。
只对已验证条目的 owner 子集 model_copy，不绕过标量／重复 ID／投影校验，不重复验证
同一 InventoryState。完整公开列表仍复制／序列化实际输出，权限和披露仍运行。
当前实现、实测与限制见 [四项验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md#2026-10-22-four-fixes-transaction-and-local-costs)。

## 2026-10-22 受管理输入与角色库存模型

地图、完整感知模型、Light 及角色 InventoryState 使用原 SessionMemory 实例字段，
身份／版本来自调用者范围，不为查缓存编码整图或库存。CharacterRuntime.inventory 的
版本由受影响拥有者及保守聚合后备版本组成；受控库存补丁从已有旧／新拥有者集维护，
其他聚合 SQL 保守失效。保存点与发布继续处理同一份候选字段和版本；版本裁剪保留复合
标记的依赖。冷加载、有界淘汰、卸载和各连接恢复代次不变。类型校验、授权／披露和
完整固定证据仍执行。具体维护入口、计数与独立未修复问题见
[后续验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md#2026-10-22-follow-up-managed-cache-inputs)。

## 2026-10-22 后续：修改驱动的字段有效性

重量、场景机关索引及能力成员关联沿现有 SessionMemory.changed 捕获实际修改；事实、
关联补丁和失效版本属于同一 UnitOfWork 候选／保存点／发布边界。有效重量只检查自身
字段版本，修改时沿旧／新容纳祖先失效；场景机关提供只读条目查询。场景能力成员按
移动子树／携带关系和实际能力维护，同场景位移／遮光由所选贡献依赖处理，不重新发现
全部成员。照明地面几何的负向能力探测单独由修改版本维护，避免上层缓存展开无关依赖。
原提交后发布故障暂停、跨连接恢复代次、旧读取发布栅栏、卸载及持久实例冷恢复不变。
不可变字段创建时计入保守载荷大小；owner 更新复用该计费及分组依赖引用，不重新格式化
其他已缓存值或展开全部依赖叶，仍执行原预算和生命周期管理。
本批实际计数、维护入口与限制见 [后续验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md#2026-10-22-follow-up-mutation-maintained-validity)。

## 2026-10-22 实例字段归属与跨连接恢复

当前分支沿原 SessionMemory／UnitOfWork 实施 ObjectRuntime、SceneRuntime 和 WorldRuntime
字段；它们是现有事实的不可变运行表示与派生关联，不是第二套 Session 或事务管理器。
消费者、维护规则和验收见 [实例归属报告](reviews/RUNTIME_INSTANCE_OWNERSHIP.md)。
普通查询／准入沿目标及必要索引；候选祖先隔离计算，未改子树只在原 scope／stage 复用。
每次修改仍增量 SQLite COMMIT 后发布 RAM。共享恢复代次使各上下文在自己的线程清除旧
当前表示及旧 Driver；旧读取范围不能在恢复后重新发布，即使 `data_version` 没有变化。
卸载／LRU／冷补入与固定旧 DTO 保留。下面带日期的历史证据不覆盖此后续结果。

## 2026-10-22 当前生产实现

当前已支持入口统一通过 Store 的 RAM／候选读取与增量提交后发布，不提供业务可选的旧 SQL 模式。
SessionRuntime 的原事务内 `publish` 已改名 `stage_outcome`；实际 RAM 发布属于 Store COMMIT 之后。
完整读写入口、稀疏回滚、提交未知／部分发布失败、重建、卸载及现存成本见
[本轮实施与验收](reviews/SESSION_MEMORY_RUNTIME_IMPLEMENTATION.md)。下文 2026-10-21 的
NOT IMPLEMENTED 和更早测试数字保留当时证据，不作为本轮结论。

## Session 内存运行生命周期与事实访问（2026-10-21）

Architecture Decision Status **[DECIDED]**；完整 Session 内存运行层 Implementation Status **[NOT IMPLEMENTED]**；Acceptance Status **本次未执行运行验收**。 单一领域所有权、统一 RAM 读取、SQLite 增量提交后发布与故障恢复由 [上游 MC §0.3／§2.3／§6](https://github.com/agentic-trpg/agentic-trpg/blob/docs/session-memory-runtime/docs/MODULE_CONTRACTS.md) 维护；本节细化本仓库责任，不复制另一套协议。链接指向本次配套文档分支，**不表示已合并 main**。逻辑并列工作区为 `session-memory-runtime/agentic-trpg`、`trpg-state-machine`、`trpg-rules-engine`；本地权威文档对应 `../agentic-trpg/docs/`。

当前静态证据固定于 SM **`d6be5a99b12fefcf8cfc57fdebb81493c1ac4d34`**（fetch 后 origin/main，包含用户指定提交）；架构基线 `8cdfc18390d105ff1d4c19ba70712276afb3eee8`，RE main `34ce8e764c1a8b1d5be2c4465a013e322fdbbc5a`。SM 固定 Engine／SRD 仍为 `a011eba3ea2226e22d8081294ee38d3ec193b325`，Schema40；本次无代码、依赖、Schema 或公开 DTO 变更。下文带日期的批次保留原始实现、测试和版本，不作为最新状态。

### 目标责任

- Session 统一管理活跃角色／物品／场景实例配置／关系／机关／环境来源／效果等的内存生命周期、一致读取和最终修改顺序；WorldFacts、CharacterFacts、CombatRecords、Activity、Effects、NPC Cognition 各拥有其事实和不变量，不集中进巨型 SessionState。
- RAM 是已提交当前事实的运行表示，SQLite 是同一事实的持久提交表示及对账／恢复依据；二者不独立接受业务写入。Facts／Records 演进为统一访问与受控修改边界，业务调用方不自行选 SQL／RAM，不直接修改共享对象或绕过协调写库。
- 已加载且有效的事实，查询、操作检查、RE 请求、角色视图、Snapshot 和环境／光照／重量派生输入统一从同一版本 RAM 读取；已有有效派生结果直接复用。不再为同批事实重复 SQL／JSON／装配，也不通过每读查库扫描证明 RAM 有效。提交必要数据库约束、权限、版本／原值和 OCC 校验保留。
- 固定输入不可被后续发布改变；候选中间阶段从隔离候选读取，不提前改已发布对象，不读旧 SQL 漏掉候选变化。不要求每次全 Session 拷贝或每次查询完整 Snapshot；具体组织、读范围接口、细粒度版本、锁及容量预算 OPEN。
- 首次／冷加载与恢复只用持久实例事实并保证读取版本一致；无法补齐同一时点则重试／重建读取范围，不能混接数据或从 Package 默认值补字段。按 Session／活跃场景及真实依赖加载，明确卸载／冷访问／引用／容量和关闭释放，不让全部世界及历史常驻 RAM。
- 每个 Session／世界唯一协调最终修改；推理和网络／只读工作可并行，最终提交有序。异步返回重验权限、版本、任务资格和前提；不在写事务或世界提交临界区等待玩家、LLM、外部 I/O 或长时间 RE。进程内协调＋SQLite 路线不变；未来 5–6 玩家不扩大当前单人 MVP，提交先后不替代争抢／先攻规则。

### 现有组件—目标责任—迁移缺口（固定源码静态核对）

源码路径均为 `src/trpg_state_machine/`；本次阅读测试源码，未运行这些业务测试。

| 现有组件／路径 | 已有能力 | 目标责任与缺口 |
|---|---|---|
| `session_creation.py`／`scene_lifecycle.py` | 创建幂等、领域初始化及延迟实例化；既有实例验证，Schema40 结构验收 | 创建 COMMIT 后发布，加载／卸载与首次实例化／实际进入分离；完整活跃 RAM 生命周期未实现 |
| `session_access.py`／`session_runtime.py` | 授权、固定绑定、指纹／回执、版本、RNG、地图准备／释放和时间协调 | 统一 RAM 元数据／版本与修改协调；当前 `publish` 在 SQL 事务内写版本／事件，不能当作提交后运行事实发布 |
| `storage.py`／UnitOfWork | BEGIN／BEGIN IMMEDIATE、外层 COMMIT／ROLLBACK、savepoint；提交后发布 `derived_pending` | 保留短事务；增加完整事实发布门槛及 SQL 已提交而发布失败的暂停／对账／重建；目前只是派生缓存发布 |
| `domain_facts.py`（CharacterFacts／WorldFacts／CombatRecords）、`instance_data.py`、`activity_records.py` | 领域 SQL 访问／受控写入与配置闭包，唯一事实／库存投影 | 业务统一从已加载 RAM／候选读取，再增量持久化；不再让调用方依赖具体 SQL；逐域盘点尚待实施 |
| `service.py` 的 `_snapshot`／read/save/resume，`rules_adapter.py` 的机械 Snapshot | 读取 SQLite、解析 JSON／装配 DTO；save 只读校验已持久事实，resume 委托 save | Snapshot／角色投影／RE 组装统一读固定 RAM 版本；当前仍有重复装配，非全内存路径 |
| `commands.py`／`rule_execution.py`／`mechanical_runtime.py` | 普通／真实 RE／合成执行，事务外 RE，写事务重验、savepoint、RNG／回执 | 保留合法失败／拒绝／冲突／pending choice；增加隔离候选与提交后发布，不无版本混用旧 SQL／新 RAM |
| `event_capture.py`／`event_persistence.py`／感知队列 | 生产阶段冻结依据，同事务事件／Evidence／Outbox／可靠投影进度 | 阶段读取转候选，已提交来源及历史输入不可变；当前投影须等内存版本就绪，投递失败不回滚事实 |
| `npc_evaluation.py`／`npc_results.py`／`npc_modes.py`／`npc_outputs.py` | 独立 Task、Meta、Result、模式、交接事务及稳定键／资格检查；不都经过 CommandRuntime | 这些真实写入口也统一提交／发布，不能只接玩家命令；细粒度认知版本策略 OPEN，防旧结果继续保留 |
| `time_coordination.py`／`time_driver.py`／Activity／机关／PackageEffects | 有界时间工作、真实 Round、现有领域联动、独立可靠步骤 | 系统和后台修改统一协调并逐步发布；不创造第二时钟，不误称完整 Effects／时间消费者已接通 |
| `scoped_reads.py`／`grid_maps.py`／`environment.py`／`world_containers.py`／`world_mechanisms.py` | 操作阶段 memo、内容键地图复用、环境版本缓存、ContainmentState、批内机关索引；Schema40 SQL 派生索引 | 保留有效资产；跨命令事实／索引／重量复用与统一版本维护需独立实施，不能由 memo 名称推定完成 |

### 提交、发布和恢复的本地接入责任

请求从一致 RAM 准备并固定必要输入／独立 RNG，领域在隔离候选上计算及检查中间变化；唯一协调者重验后，Facts／Records 在同一 SQLite 短事务增量写入全部实际变化、版本、RNG、必要事件／证据、Receipt、Outbox 和可靠工作。COMMIT 后运行层发布对应 RAM，维护相关索引／依赖并失效缓存；该版本就绪后才允许后续当前态操作并正常返回成功及对应投影。更新范围按变化决定，不每条命令保存／重载整个世界。

完整故障矩阵只维护于 MC §6.4：提交前失败不发布候选、不推进权威 RNG／发送事件；结果未知按原 command_id／工作键对账；提交后发布前崩溃从库恢复；发布异常进程仍活着时暂停该 Session 并按库重建，不能读落后 RAM 或伪报未提交失败；响应丢失返原回执；Outbox 失败可靠重试。SQLite 与 RAM 不是跨介质 ACID，现有 Store 的缓存发布循环不证明完整故障协议已实现。

恢复校验 Schema、Package／Ruleset、持久事实／RNG／回执及可靠工作后，重建活跃 RAM 与必要索引，昂贵派生按需计算。手动／定期存档是命名检查点、一致备份与受控回档，不是唯一持久化入口；复制活跃主文件不等同完整 SQLite 备份。迁移、导入、修复与维护先停用／排空相关 Session，受控修改后重载校验再开放；脚本不能绕过运行层直改活跃库。事实恢复仍不保证活跃战斗、PendingChoice／Continuation 任意阶段可续玩，当前 combat_unrestorable／绑定拒绝保持。

### 后续验收边界

迁移沿 [现有 C-25／C-27／A2／DS 轨道](https://github.com/agentic-trpg/agentic-trpg/blob/docs/session-memory-runtime/docs/GAME_CORE_EVOLUTION_PLAN.md#81a-session-内存运行层的依赖阶段2026-10-21)。先盘点全部读写／提交／恢复入口，再定义边界；以已支持物品转移真实切片验证加载、查询、候选、增量提交、失败、重试及重启，逐领域推广后接索引／派生，最后在正确性证据上测 5–6 玩家及适量 NPC。聚合 JSON 拆分、RE 按需机械闭包与 RAM 接入分别验收；新旧路径不独立写同一事实，同操作不无版本混读。

验收同时检查 SQL、RAM 当前态、旧读取引用／固定 Snapshot、索引、缓存及恢复。至少覆盖提交失败／未知、持久成功后发布失败、丢响应、Outbox 重试、重复命令、旧 NPC 结果、并发取物及真实竞争语义；场景加载／卸载、版本一致冷补入、进程重启和派生重建；热路径不重复解析装配同批事实、局部操作不随无关实体扫描整个 Session。性能以真实调用链 SQL 行数／解析／构建／扫描、提交／发布及内存测量为准，不凭人数给吞吐承诺。

## 2026-10-20 当前更正

Schema39 将已存在的实例配置读取依赖迁移至 WorldFacts.instances；配置与当前事实分离。
ContainerService 使用一次操作的 ContainmentState；旧存档须显式升级配置，不在恢复 getter 中修复。
冻结 RE／Openable 整合已接入 a011eba；Runtime 一次装配并共享默认或自定义固定素材。
旧 /36 绑定保留，save／resume／新规则执行不匹配时明确拒绝，不静默迁移。
[当前字段与调用路径、迁移和接口证据](INSTANCE_CONFIGURATION_PROGRESS.md) 优先于下文历史缺口表。

2026-10-19；SM 本地实现说明。职责依据上游 MODULE_CONTRACTS §0.3、§2.4、§4.3、
§10；不修改上游规范，不把目标完整实例化宣布为已完成。
本批次基线 `3fe36e2`，验证与缺口见 [进度](SESSION_MANAGEMENT_PROGRESS.md)。

2026-10-19 时间后续：当前 Schema37，SessionRuntime 已装配有界时间协调和 TimeDriver，
创建保存暂停／未配置倍率状态，恢复读取配置并显式重建单调基准，不补离线时长。
世界事实、共同命令和 RE 执行器继续复用。具体同步／回执／版本和业务接线边界见
[Session 时间协调](SESSION_TIME_COORDINATION.md)。下文的 Schema36 是此前批次记录。

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

### 固定素材集生命周期

Engine／SRD 同时固定 `a011eba3ea2226e22d8081294ee38d3ec193b325`。StateMachine 装配时，
默认选择 RE `get_default_ruleset()`；自定义最终组合完成后 `freeze_ruleset(source)`，然后同一
引用注入 WorldHandler 和 RulesAdapter，供创建、普通／Openable 求值、availability 与校验使用。
RE 的 binding 已验证并保存在 FrozenRuleset；Session 持久三项绑定只与该绑定比较，不重算摘要。

来源或配置改变时重建 Runtime／显式传入新 FrozenRuleset，不热改正在运行的规则；重启可重新
冻结一次，再验证已有 pin。RE getter 返回防御性深拷贝，内部固定素材不会随调用方修改。
不另建 SM 素材缓存或持久复制规则集。详见 [路径、计数与限制](INSTANCE_CONFIGURATION_PROGRESS.md)。

`/36` 完整 Package 仍可用于读取原 pin 和显式 upgrade_instance_configs，但新 RE `/37`
拒绝 save／resume／新规则求值，不推进 RNG、不改世界或旧绑定。没有活跃存档自动规则迁移；
继续原部署，或用显式新 Package／新 Session 创建。原 `/4`、`/8` audit-only 及不完整事实限制保持。
`migrate_package_16` 仅转换可证明完整的源内容为新版本，复用一次冻结，不迁移既有 Session。

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

## 历史批次：持久化、恢复与当时后续缺口

数据库仍为 Schema36；没有搬表、新迁移、字段改名或历史数据重算。旧命令指纹编码和
回执序列兼容。旧机械版本、legacy audit、活跃战斗跨进程 `combat_unrestorable` 及
显式 Package 源转换机制保留，不自动重绑已有 Session。

save／resume 验证 Session／Package pin、必需全局／PC／RNG／当前场景记录，以及已实例化
场景的对象、NPC、网格和已支持对象能力状态行。当前 D&D Session 还校验安装 RE；
已选择机械版本时验证已保存 Actor／组件和 RNG；不要求场景中的普通 NPC 获得新增机械
档案。规则请求仍要求完整局部快照。缺失或不支持继续明确拒绝，FK 损坏仍由
Store 拒绝打开。正常恢复只读取当前事实，不重新应用模板或初始化访问次数。
这些是当前支持数据的结构约束，**不代表完整有效配置已持久化**。

| 历史后续领域批次 | 当时已保存 | 当时依赖 Package 的配置／缺口（当前更正见文首） |
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


## 2026-10-20 跨仓库对齐与证据适用范围

本文件带日期批次／分支／Schema／“未发现 AGENTS”属于原时点证据；最新 main 为 Schema39，实例配置与冻结素材生产装配见 [Instance Progress](INSTANCE_CONFIGURATION_PROGRESS.md)，时间协调见 [Time Progress](SESSION_TIME_COORDINATION_PROGRESS.md)。旧缺口表不能覆盖上述已完成切片。当前开发入口为仓库 AGENTS.md；共同规范只维护于顶层 MC §2.3、§6.5–§6.6。全局审查、局部准入、实际 SQL／投影范围及剩余重构详见 [校验范围审查](reviews/RUNTIME_VALIDATION_SCOPE_REVIEW.md)。本次只更新文档，没有运行新增业务／规模测试。


## 2026-10-20 Scoped runtime validation refactor

本批实现采用 Schema40 的事务内派生占格索引和结构验收记录，普通命令由领域
局部准入保护，操作内复用读取，提交不再默认完整空间审查或库存重建。
逐项能力、读取范围、存档兼容、实际计数及本批验证结果见
[系统性重构验收](reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md)。
上文历史测试数字与旧 Schema/待办保持原批次语境，不作为本轮验证结果。
