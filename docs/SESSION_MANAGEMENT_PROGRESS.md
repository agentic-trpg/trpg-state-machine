# Session Management 重构进度

## 2026-11-19 单目标远程法术攻击

公开 `combat.intent` 的普通施法路径接入 RE `/49` 分类的 Fire Bolt 和 Poison Spray。
当前范围为使用智力施法的一级 Wizard 对单个活着的敌方生物施放已知戏法，支付普通动作，
不消耗法术位。SM 核对攻击事件与回合交接顺序。命中采用与普通伤害相同的类型化伤害，
未命中采用攻击掷骰、施法和回合变化。掷骰计数、伤害、回合、RNG、事件和回执在同一
事务写入，提交后发布相应的内存状态。

候选公开回归核对两种戏法的命中、未命中、暴击和射程，覆盖击杀后回合交接，
以及回执写入故障后的回滚与幂等重试。物件、多目标和额外效果选择超出本批范围。

## 2026-11-19 普通伤害结果采用

普通武器攻击与受支持的单目标伤害法术共用类型化伤害采用入口。固定 RE `/48`
在普通敌方 NPC 受伤时先消耗临时 HP。耗尽临时 HP 后的伤害可将其降至 0 HP，
SM 将死亡事件、生命状态与死亡账本作为同一结果核对，按更新后的死亡名单交接回合。
公开回归覆盖 Sacred Flame、Inflict Wounds、临时 HP 吸收、回合交接与写入回滚。
角色的零 HP 施法后果与将专注施法者打到 0 HP 仍受各自的采用范围限制。

## 2026-11-19 战斗 Hold Person 双目标

公开 `combat.intent` 使用 `target_id` 指定首个目标，可通过有序 `target_ids` 列出全部目标。在固定 RE `/47` 下，
二环可选一个活着的人形目标，三环可选一个或两个。双目标仍只花费一个三环法术位，
并记入一次战斗支出。SM 将每个目标的豁免证据与同一专注组核对，只采用豁免失败的
效果成员以及相应的条件链接和时钟。全部豁免成功时保留施法者的专注锚点。

通过公开 Pass 进入目标回合末。成功的重复豁免移除该目标的效果，剩余成员继续受影响。
施法者回合末推进专注计数，持续期结束或施法者主动解除时清理剩余成员。现有单施法者、
单组准入仍适用，将施法者打到 0 HP 的结果继续返回 `effect.capability`。

## 2026-11-19 普通攻击读取定时效果

固定 RE `/48` 的攻击可用性查询与结算从同一战斗快照读取单个定时被动效果。
步行速度加值可参与闪避资格，具名技能检定加值由相应检定使用。
采纳攻击结果仍沿既有事务路径，效果在原世界时钟截止点到期。
公开回归覆盖 Longstrider 和「感知检定 +2」测试法术，包括攻击完成整轮时
在同一提交内结算到期。战斗移动预算投影和混合效果组仍需接入。

RE 独立求值也验证了普通攻击对 Hold Person 定身目标的优势与近距离暴击。
当前单人公开战斗的施法与装备限制使该对照覆盖到 RE 求值层。

## 2026-11-19 角色效果视图

`read_session` 和玩家的 `get_scene_view` 从 Effects 读取当前角色的效果。
可信快照返回角色的效果记录，玩家视图只展示自己角色已启用效果的标识和名称。
真实施法、到期及主动解除专注的回归覆盖效果列表随提交更新。

## 2026-11-18 EFFECT-03 定时施法和主动结束专注

探索 `world.cast_spell` 使用 RE `/43` 的 evaluation `/29` 和探索
Snapshot `/20`。请求只组装实际施法者和目标的规则输入。RE 校验施法与支付，
返回法术位扣减和以秒为单位的持续效果。SM 在同一提交内扣法术位、保存效果和
到期时间并记录回执。Longstrider 作为一环、单目标的真实用例，效果到期沿用
已有 SessionTime 路径。

施法者可在其他角色的回合使用普通命令入口主动结束自己维持的 Hold Person。
SM 采用准确的专注及效果成员清理，保留独立来源的状态。命令不扣行动、不推进
回合或游戏时钟、不掷骰。另行结算普通战斗动作使用这些效果的规则。

## 2026-11-18 EFFECT-02 公开 Hold Person 命令

使用同一 `combat.intent` 入口提交 RE `/42` 的二环单目标 Hold Person。
按固定输入的命令和当前效果图采用结果，校验专注、效果身份、法术位支付及
回合变更。已验证的施法者配置为具有合法施法来源和确切材料包的 Wizard 5 Evoker，
目标为当前遭遇的单个活着的人形生物。RE 执行实际规则检查，领域事实、RNG、事件和
回执仍沿现有 UnitOfWork 共同发布。

受支持的 `/20` 遭遇在所有参战者仍有 HP 时可通过公开 Pass 推进回合，包含重复豁免、
施法者期限及效果结束后的普通 Pass。攻击命中后施法者仍有 HP 时，伤害与已有 Hold 图的
专注维持或解除一起提交或回滚。
会把专注施法者打到 0 HP 的已验证结果返回 `effect.capability`，权威 HP、RNG
和效果保持原值。其退出条件是死亡后果可采用，并能继续结算死亡豁免与遭遇关闭。
公开可用性查询仅使用攻击预检。施法与 Pass 的可用性为 `unknown`。
多目标、升环、混合效果及 Wizard 的 `combat.close` 依赖仍为后续范围。

## 2026-11-16 EFFECT-01 Hold Person 受控候选

在内部显式 `hold_candidate` 准入下，SM 将固定 RE 的单目标二环 Hold Person
图创建、目标回合末重复豁免、施法者回合末专注计数及正 HP 受伤后的专注结果
转为 Effects 按身份持有的效果／条件链接／时钟和单条专注组更新。资源、战斗
支出、生命值、RNG、事件与回执沿现有 UnitOfWork 提交。当前公开命令仍保持
原能力准入；本批不宣布公开 Hold Person／Pass／带效果攻击能力。

初始战斗 Foundation 的 /19、/20 职业与装备资料改由固定 RE
`operation_dependencies` 一次验证，旧版本沿用原基础准入；战斗法术位支出
保留实际类型、等级 1–9、原值与逐次增加校验，入口仍由已验证的 Delta 控制。
真实受控测试覆盖初始豁免两支、目标回合末保留／解除、施法者九次回合末到期、
受伤后的专注维持／解除、旧快照与重开，以及不一致 Wizard AC 的初始拒绝。
完整独立验收与故障矩阵另记本批证据；世界时间期限交接由后续独立批次处理。

## 2026-11-16 SPELL-02 即时法术候选

SM 候选移除按法术 ID 的前置准入与结果分流，改用 RE 机制分类和单一
法术结果采用。真实 RE 的公开命令回归覆盖治疗术、圣火术豁免两支、
另一招同机制法术 Inflict Wounds、重复归因、致命结果拒绝、晚期 SQL
故障回滚与重开回执；候选定向检查结果见本批阶段证据。正式锁定 RE
提交、完整回归及独立审阅另行记录。

## 2026-11-16 ITEM-01 探索喝药候选

`world.use_item` 在探索中把所选玩家角色、确切药瓶和活动 ID 交给
`engine-evaluation/28`，使用固定的 `engine-snapshot/20` 非战斗快照。规则拒绝
满血、无权或无法使用的自饮。成功时沿用同一份 WorldFacts 物品消费记录和
CharacterFacts 的 HP，RNG、事件和回执进入同一提交。当前 Schema 及战斗喝药
协议沿用原版本。已有时间推进未结算时，喝药返回可重试的
`time_advance_pending`，此时没有喝药回执。定向回归和独立审阅以本批固定候选报告为准。

## 2026-11-16 ITEM-01 / SPELL-01 隔离集成候选

当前候选保留 ITEM-01 的 Schema52 装备表及精确物品实例绑定，独立的
Schema53 整合法术材料行、来源与准备顺序。`CharacterRuntime` 同时保留库存、
装备和有序法术字段。定向测试以真实 RE 规则资产验证装备链衫与硬头锤的牧师
施放治疗术：提交后装备实例、法术引用、生命值与法术位保持一致，关闭重开后
机械快照相同。该记录只描述隔离候选，尚未合入 main。

## 2026-11-16 SPELL-01 治疗术候选

候选将角色法术材料绑定并入角色法术行，准备名单从同一批内存记录派生。
RE `/20` 角色法术数据按来源各传一份名单，SM 采用治疗术的一环法术位和治疗结果。
端到端、回归和审阅结果以本批固定候选的阶段证据为准。

## 2026-11-15 ITEM-01 探索换武器候选

探索中的 PC 可通过 `world.equip_weapon` 指定直接携带的武器实例 ID 与
单手／双手握法，原子替换手持绑定；`world.unequip_weapon` 带预期手持 ID，
清除绑定而不移动物品。仅支持 /19 显式实例绑定，护甲、盾牌和有效 AC
未增加修改入口。CharacterFacts 持久化 ID 与握法，WorldFacts 提供当前
归属与可访问性，武器类型在读取规则快照时由物品事实派生。
现有命令事务负责世界版本、回执、事件、回滚和提交后 RAM 发布。
定向测试与独立评审结果以本批交付报告为准。

## 2026-11-15 STORAGE-02 核心属性候选

Schema50 候选把机械角色属性、AC、等级、熟练覆盖、机械感官及武器熟练的显式列表标志存为每角色一行，四类熟练按有序项保存。战斗／探索规则投影重新组合原 DTO，事件证据的机械感官直接读取同一窄行。新库、初始化故障回滚、普通攻击、旧固定快照及关闭重开已由定向测试覆盖；完整回归和独立评审另行记录。本批未新增属性编辑或熟练编辑命令。

## 2026-11-15 STORAGE-02 独立角色条件候选

Schema48 候选保存按角色定位的独立条件行，保留同名、重复、内容、顺序和稳定
内部 ID；效果链接由既有 Effects 行投影。点读取、存在检查、ID 定向删除和
RE 结果采用共用现有 Store 与发布边界。定向测试覆盖两种同名移除次序、
重排 ID、重开和非法链接写入；完整回归及独立评审另行记录。
现有战斗 Foundation 准入拒绝非空初始条件，未扩大其规则支持范围。
非战斗 Foundation 的孤立效果链接在角色写入前拒绝，实测创建事务全量回滚。

## 2026-11-15 ITEM-01 物品实例充能候选

Schema49 的充能写入复用原 UnitOfWork 候选、保存点、持久提交及 SessionMemory
发布。容器直接成员字段持有稳定内部物品引用；只有成功提交才更新其不可变余额载荷，
旧固定 Snapshot 不变。引用不能跨卸载、清空或恢复当作当前值；字段淘汰后其他
内部持有者仍须通过有效当前读作用域访问。详见
[容器进度](CONTAINER_SERVICE_PROGRESS.md#2026-11-15-item-01-batch-a-候选实例充能)。

## 2026-11-15 STORAGE-02 生命与伤害事实候选

Schema47 候选把临时生命值、存活状态、可空且有界的死亡豁免与最近伤害来源
放入每位机械角色的 `mechanical_life` 带键行；HP／最大 HP／XP 保留在
`characters`。新库建表、公开攻击采用、生命写入失败回滚、旧固定快照、
回执重放和冷重开通过 `tests/test_storage_life_state.py`（3 项通过）。
Schema1–46 预发布存档在可写打开前拒绝。完整候选回归及独立评审另行记录。

## 2026-11-14 STORAGE-01 持续效果行候选

Schema46 候选将效果、专注组、条件链接和生命周期时钟放入带键持久行；
现有 SessionMemory/UnitOfWork 候选和提交后发布维护读取。已覆盖多目标、
单成员移除、同 ID 异来源与独立条件保留、组移除、关闭重开及保存点回滚。
未接入施法或轮次命令，也未宣称 RE 持续效果采用完成。

## 2026-11-14 STORAGE-01 角色施法来源候选

Schema45 候选把显式施法角色、职业来源、已知／准备法术、材料对象绑定及角色
`spells_known` 顺序存入带键记录。原 SessionMemory／UnitOfWork 负责读取、候选、
回滚、发布和重开；战斗与探索读取同一施法投影。World 转移沿移动子树的实际
对象 ID 查材料反向索引，不为检查一个绑定枚举角色全部材料。独立评审与完整
回归尚待完成；施法规则、资源支付、效果及物品施法不在本批。

定向测试覆盖双来源的已知／准备顺序、显式与独立 `spells_known`、战斗／探索
快照与重开、保存点回滚和旧快照隔离、同一角色 97 个材料绑定下固定对象的
反向索引与热读取、不可用材料的精确物品转移保护，以及旧 Schema44 只读拒绝。
这不等于完整回归或独立评审结论。

## 2026-11-14 STORAGE-01 角色资源行候选

新建 Schema44 中，CharacterFacts 把合成规则余额、显式 D&D ResourceState 池和
无显式组件时的独立法术位保存为有主键的角色资源行。显式资源的角色法术位和
feature-use 镜像从同一行按需派生；无显式组件时保留开放的角色 custom_counters。
Session UI／玩家感知与 RE Snapshot 共读此事实，原 SessionMemory／UnitOfWork
管理候选、保存点、提交后发布和恢复。角色／组件重复投影已沿拥有者资源范围复用；
装备绑定只读不构造未使用的资源镜像。旧 Schema1–43 开发库写连接打开前拒绝。

本候选定向验证包括合成公开扣减与重开、显式池扣减后的 UI／RE 同步与旧快照隔离、
保存点回滚、无显式组件的独立法术位、冷公开快照重复解码计数及资源写后失效。
完整回归和独立验收由后续评审单独记录；D&D 资源消耗命令、物品充能、施法和效果
存储未在本批实现。

## 2026-11-14 装备实例绑定候选

Schema43 与快照 /19 的角色装备实例绑定已接入初始验证、当前快照派生和
Drop/Put/Take 局部保护；现有 SessionMemory、World 物品事实及 UnitOfWork 继续拥有
有效性和发布边界。同类型备用装备及另一只手物品的公开路径、拒绝回滚、保存重开和
固定目标的无关实例扩容已作定向检查；独立审阅和更广验收仍单独记录。
详见[装备实例记录](reviews/EQUIPMENT_INSTANCE.md)。

## 2026-11-14 预发布存档策略

旧开发库升级和库存回填退出运行入口；Schema1–40 在打开时只读识别并拒绝。
当前 Schema41 的创建、保存、恢复与回滚验证见
[清理记录](reviews/PRE_RELEASE_SAVE_CLEANUP.md)。下文旧库升级描述保留当时范围。

## 2026-10-27 source explanations and workflows

Added production source explanations and repository workflows without changing runtime
logic, interfaces, dependencies or persistence. See [the documentation batch evidence](reviews/CODE_DOCUMENTATION_AND_SKILLS.md).
Existing runtime measurements below retain their original dates and scope.


## 2026-10-22 通路反向关联修复

基线 `9627eb0` 中 SetObjectState → changed → _light_candidate_changes →
_light_door_scenes 对普通箱子也展开全 Session 通路。现由 WorldRuntime.passages 保存
统一只读关联，InstanceData 与光照维护共用；配置修改维护单字段版本，原候选／回滚／
发布／恢复及预算继续管理。无关通路 +0/+10/+100 时 helper VM 步数 39/79/439→0/0/0。
冷准备的 2/12/102 条记录成本另行计数；292 个定向测试的最终结果均通过，初次两条
旧冷／热边界断言的调整与复跑也如实保留。见[实施与验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md#2026-10-22-shared-passage-reverse-association)。
下述历史批次数字及状态保留原口径，不作为本次的新证据。

## 2026-10-22 四项修复：失败范围、库存、NPC 目标与版本根

基线 5137742 的捕获 SQL 语句错误后提交 RAM／SQLite 分歧已经真实复现并修复：当前写入
范围失败后拒绝读写／提交，有效保存点回滚后允许继续；没有保存点则整体回滚。库存移动／
HP／行动变化按真实字段判定，完整类型结果只校验一次；NPC 两轮目标 guard 按两身份查询；
事务、保存点及发布复用不可变版本根，单键推进保留对数路径成本。没有新 Session、Schema
或事务体系。维护字段、明确协议和证据见 [Session 实施说明](SESSION_MANAGEMENT.md#2026-10-22-sql-失败协议与局部元数据)
及 [四项验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md#2026-10-22-four-fixes-transaction-and-local-costs)。
下面“独立未修复”是 5137742 当时状态，由本节新证据在其具体范围内取代。

## 2026-10-22 运行缓存身份与版本复用

地图、公开单格照明、库存及完整感知地图的受管理生产路径已消除整输入缓存键编码。
库存 11／101 条完整输出的编码记录从 11／101 降至 0／0，版本检查 41／311→4／4；
无关角色受控库存转移后，目标查询 RAM 调用 18／108→2／2。多阶段、保存点、整体回滚、
完整感知字段、旧地图、卸载与跨连接恢复验证见
[本次验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md#2026-10-22-follow-up-managed-cache-inputs)。
NPC 全角色查询、全版本字典复制和捕获 SQL 语句错误后继续提交的问题保持独立未修复状态。

## 2026-10-22 二次性能修复：字段版本与能力关联

修复 `c64f9d7` 命中仍展开依赖／复制机关全表、无关归属修改失效场景能力关联的问题。
修改驱动重量祖先失效、场景成员补丁与只读机关条目查询共享原候选／发布／恢复机制；
补齐照明上层负向依赖的修改版本。新旧来源、独立 SQL／RAM／版本／复制／计算计数、
实际测试与限制见 [后续验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md#2026-10-22-follow-up-mutation-maintained-validity)。
下述历史数字与状态保留原时间和口径，不作为这批新证据。

## 2026-10-22 后续：实例字段与全上下文恢复失效

沿既有 RAM／候选／提交后发布增加领域实例字段，真实消费者与维护责任见
[实例归属验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md)。恢复代次覆盖发布前／部分／完成后
抛错与结果未知；B 恢复不能放行 A 的旧当前表示，旧读取也不能重新发布。各上下文在自己
线程失效，旧 Driver 同步释放后须重新 attach；不跨线程操作 SQLite。原单人 MVP、
公共 DTO、Schema40、RE pin、授权／RNG／幂等／可靠工作及受限恢复边界保持。

## 2026-10-22 Session RAM 生产接入

统一记录范围、解析复用、候选保存点、延迟取得 SQLite 写锁、共同提交后发布、失败暂停与
持久实例重建已接入现有 World／RE／NPC／Activity／Time／Perception 写入口。补充授权卸载、
Driver 和派生释放；没有升级 RE pin、Schema 版本／DTO，Schema40 兼容补齐私有空间查找索引，
也未实现原来不支持的战斗任意阶段恢复。
实际调用链、入口清单、缓存维护、测量和本轮最终检查见
[本轮实施记录](reviews/SESSION_MEMORY_RUNTIME_IMPLEMENTATION.md)。以下历史批次原文保持。

## 2026-10-21 文档决定：Session 内存运行层待实施

Architecture Decision Status **[DECIDED]**；完整 Session 内存运行层 Implementation Status **[NOT IMPLEMENTED]**；Acceptance Status **本次未执行运行验收**。 静态源码基线 `d6be5a99b12fefcf8cfc57fdebb81493c1ac4d34` 已为 fetch 后 main；原 Scoped runtime refactor 不再是未合并前提。现有 Schema40、操作 memo、GridMaps／Environment 缓存与旧验收数字保留原范围，不是完整内存事实层。

本轮只更新 [Session 责任／入口／目标差距与验收](SESSION_MANAGEMENT.md) 及相关领域文档，未修改实现、测试、Schema、依赖或协议。后续按上游 C-25／C-27／A2／DS 阶段完成全部入口盘点、统一事实访问／候选／发布、真实操作切片、逐域接入、索引／缓存和正确性后的合成并发负载。热查询、Snapshot 和派生不再重复 SQL 装配同批已加载事实；故障验收同时检查持久状态、RAM 当前态、旧引用、索引、缓存和恢复。详细执行检查以本次交付记录为准，以下测试数字都是历史运行证据。

## 2026-10-20 实例配置与容器索引后续

当前 Schema39；有效对象能力、Scene 配置、NPC senses 和辅助路径已保存并改读 Session；
旧库通过显式 upgrade_instance_configs 升级，普通读取不补字段。容器操作内复用邻接索引及重量 memo。
RE Openable 外部阻塞已解除：Engine／SRD 同 pin a011eba，默认／自定义素材在 Runtime 装配时冻结。
初始化、实际求值、availability、持久绑定校验与恢复共享固定对象，连续运行无全量摘要。
/36 旧 Session 保留绑定与实例升级，RE /37 下恢复或新求值不匹配时拒绝，不自动重绑。
字段权威、创建／恢复、Package 审计、候选计数和最终检查见
[本轮完整记录](INSTANCE_CONFIGURATION_PROGRESS.md)。下文未迁移清单与数字是历史批次快照。

## 地图运行表示后续批次（2026-10-19）

在当前 `feat/session-management-refactor@a25c35e` 的干净工作区继续，未切 main。
创建协调仍经原 SceneLifecycle 初始化领域地图，新增 SessionRuntime.prepare_scene_maps
协调 Grid 准备；原机械场景补建、延迟实例化和 save／resume 当前场景也准备。释放只移除
可重建内存引用，Store.close 清池，保留调用方持有的旧 View。

SQLite state_json 仍为有效地图权威。GridMaps 内容键先读取事务可见编码，覆盖候选、
回滚、相同世界版本、旧读取事务和连接隔离；Scene 预初始化范围、绑定、授权、幂等、
RNG、事件／Outbox／回执语义不变，无新 Schema 或 RE 依赖变更。
查询算法、调用次数与本轮验证见 [Grid progress](GRID_SERVICE_PROGRESS.md)。
本轮验证覆盖 718 个不同用例：Focused 248、Integration 470；最终构造／序列化兼容复核
302 passed（与上述重叠，不另加合计），均 exit 0。Schema36 和固定 RE／SRD 依赖不变。
本轮仅完成地图运行表示和 Grid 算法；下文列出的 WorldObject／Character／Item、
复杂创建依赖与固定素材接口缺口继续保留。

下文为 a25c35e 的原 Session 重构实施和验证记录，915 项属于该历史批次。

2026-10-19；工作区 `D:\Projects\agentic-trpg\trpg-state-machine`；
分支 `feat/session-management-refactor`，干净基线 `3fe36e2`。
仅修改 SM，不修改 RE／架构仓库、不合并 main。
当前职责、兼容影响和逐领域缺口见 [设计说明](SESSION_MANAGEMENT.md)。

## 实施状态

- 创建流程提取至 SessionCreation，StateMachine.create_session 保留兼容签名；原有领域
  初始化、创建幂等、TimeBinding、PackageEffects、事件／Outbox／回执在同一事务。
- SessionAccess 保留授权和 pin 检查；SessionRuntime 接管指纹、重放、版本、RNG 和回执，
  世界／真实规则／合成规则共用 publish／finish，不重写领域 Handler。
- 普通求值、战斗准备、辅助检定、机制效果端口、可用操作查询和相关恢复校验从持久化
  Session 取三项绑定；结果仍严格匹配请求。Package pin 校验继续强制。
- ensure_scene 核对固定 Package，复用并验证既存实例；授权 NPC 抽象移动可在操作事务中
  创建必要目标且不生成 PC 进入事件，读查询不写库。保留原空间场景提前初始化策略。
- save／resume 增加当前受支持实例结构、当前 RE／机械 Actor／组件和 RNG 检查；缺失事实拒绝，
  不重套模板。Schema36、公开 API、旧回执、legacy audit／机械版本边界保持。
- WorldObject 能力配置、完整 Character／Item 等仍未迁移；逐字段和调用点清单在设计说明。
- 固定 pin `d596161` 无固定素材接口；上游 `3718f7c` 有 FrozenRuleset，但 `/37` 分支缺少
  当前 Openable 模块。未做不兼容升级，快速校验**未接入**，保留实际 loader 全量摘要检查。

## 检查等级与证据

仓库、源码／测试子目录及适用祖先未发现 AGENTS.md；无 check-fast 或 CI 等级脚本。
依据公共授权、幂等、RNG、跨领域事务和恢复影响，选 Focused + Integration，未运行无关全量。

初次沙箱内基线／专项在 Windows asyncio `_fallback_socketpair → accept` 阻塞，
faulthandler 定位后仅停止本任务测试进程；改用原 `.venv`／固定依赖在限制外运行。
未改 Python、RE 依赖或锁文件。中途失败均保留日志；最终退出码另行确认。

- Focused：`test_session_management.py test_sessions.py test_commands.py test_mechanics.py
  test_domain_runtime.py test_scene_lifecycle.py test_perception.py test_perception_conditions.py
  test_npc_perception_queue.py test_perception_efficiency.py test_event_local_evidence.py
  test_event_traceability.py`：**287 passed**，166.54s，exit 0；
  日志 `.test-tmp/session-focused-verified.log`。
- 随后确认原六参数 SceneLifecycleService 构造器仍兼容：新增且未包含于上述收集的用例
  **1 passed**，0.73s，exit 0；`.test-tmp/session-constructor-final.log`。
  Focused 合计 **288 个不同用例**，其中 Session 新增回归 19 项。
- Integration：真实 RE／战斗／查询／关闭、Check 后果、NPC 交接、旧库迁移／跨进程恢复、
  导航／物品／锁／Openable／Mechanism／Environment／WorldTime 及示例，最终 **627 passed**，
  357.71s，exit 0；日志 `.test-tmp/session-integration-final.log`。
- 两组合计 **915 个不同用例**，只有两条既有 Lark 弃用警告；未运行全库测试。
- Ruff check src tests、Ruff format --check src tests（183 文件）、git diff --check、
  本轮文档的相对链接／代码 fence 检查通过。最终 diff 已按生产路径、测试和文档审阅。
  pyproject.toml／uv.lock／storage／SQL Schema 与基线一致；安装元数据也确认 Engine 和
  SRD 均为 0.6.0、Git SHA `d596161060555630019163ab301e1d25fd816549`。

Integration 文件清单（均位于 tests/）：`test_integration.py test_combat_foundation.py
test_dnd_runtime.py test_dnd_hardening.py test_dnd_npc_availability.py test_combat_closure.py
test_real_checks.py test_conditional_world_effects.py test_history_free_rules_adapter.py
test_npc_combat_handoff.py test_npc_outputs.py test_versioned_state.py test_migrations.py
test_navigation_activities.py test_navigation_faults.py test_navigation_migration.py
test_grid_service.py test_item_transfers.py test_lockable_passages.py test_openable_checks.py
test_mechanism_effects.py test_mechanism_service.py test_environment_service.py
test_environment_snapshot.py test_environment_versions.py test_world_time_service.py
test_world_example.py`。运行方式为现有 `.venv/Scripts/python.exe -m pytest -q` 加上述文件，
独立 `--basetemp=.test-tmp/session-integration-final`，无依赖升级。

Integration 首轮 **624 passed / 3 failed**。修正过严的恢复检查：不能将当前场景普通 NPC
没有 RE 档案误判为旧存档损坏；验证已保存的 Actor／组件，后续求值仍要求完整局部快照。
另外更新运行时先授权／Session pin 拒绝后的异常码断言。原失败及其参数化用例专项
**4 passed**，11.12s（已包含于 Integration 范围，最终合计不重复计数）。
Focused 中新增 PickLock 测试曾误写参数顺序，修正后上述最终分组全部通过。

新增测试通过隔离 `_check_package_pin`，故意令 Package 与 Session 绑定不同，并断言
verify_ruleset／求值请求／查询请求／指纹仍由 Session 驱动；真实 pin 拒绝测试另行保留。
真实错误绑定结果和查询结果拒绝且不写事实／RNG／回执；SQLite late fault 验证新建场景、
NPC 位置、版本、事件／Outbox 和回执一起回滚。既有权限、重放、指纹冲突、OCC、
pending_choice 以及历史兼容测试复用现有真实公开路径。


## 2026-10-20 Scoped runtime validation refactor

本批实现采用 Schema40 的事务内派生占格索引和结构验收记录，普通命令由领域
局部准入保护，操作内复用读取，提交不再默认完整空间审查或库存重建。
逐项能力、读取范围、存档兼容、实际计数及本批验证结果见
[系统性重构验收](reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md)。
上文历史测试数字与旧 Schema/待办保持原批次语境，不作为本轮验证结果。
