# EnvironmentService

## 2026-10-22 防护批准备的共享输入

OSS 防护输入改为同范围只读地图的固定尺寸；同一批按 Scene 懒准备一次，不再为每个
物体复制地图、canonical_json 编码，再在投影阶段 json.loads。明确空的降水及风贡献在
取地图前识别；None、部分未知及共享门／非地面 unsupported 仍分别输出。已有登记
提供方身份、双版本、时间、方向、高度及披露规则保持；未知提供方不能因此跨查询复用。

这修复的是首次建立／真实失效重建防护时的重复输入准备。有效环境热命中原本就不会
逐物体重建。冷批仍需读取实际 Scene 防护输入，完整来源输出和真实投影有相应成本。
首次、重建、不同方向消费固定输入及热命中分别计数，见
[本批验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md#2026-10-22-local-query-reuse)。

## 2026-10-22 场景环境字段与区域校验

现有 public cell／area／unit／height 与内部投影接入 Scene.environment 的 facts、
inputs、protection、cells、ranges 字段；原来源／防护双版本、正式提供方身份／时间依赖、
未版本化 opt-out 和披露规则保留。命中不把偶然的锁具解码依赖替代已登记的防护协议。
area 校验读取目标场景存在性和已解析边界，不枚举场景配置或计算完整环境。
指定区域的可见性只准备请求格光照，不因权限判断隐式计算全图；整场景请求仍按明确范围执行。
字段沿现有 COMMIT 后发布／savepoint 隔离，并受 SessionMemory 与每字段预算约束。
当前提供方支持边界不扩大；验收及冷成本见
[实例归属报告](reviews/RUNTIME_INSTANCE_OWNERSHIP.md)。

## 2026-10-22 当前运行层

来源／防护版及分层缓存继续复用，事实输入统一读取同版本 RAM／候选；旧读取范围在外部提交后
不能发布为当前缓存。卸载／故障重建释放该 Session 的派生值。光照通过 RAM 关系和光源状态
取输入，原地图算法、提供方和规则接线边界保持。见 [本轮实施报告](reviews/SESSION_MEMORY_RUNTIME_IMPLEMENTATION.md)。
下述 2026-10-21 目标段落保留历史状态。

## 2026-10-21 Session 运行层接入目标

Architecture Decision Status **[DECIDED]**；完整 Session 内存运行层 Implementation Status **[NOT IMPLEMENTED]**；Acceptance Status **本次未执行运行验收**。 统一事实读取、候选／持久提交／内存发布与故障规则见 [Session Management](SESSION_MANAGEMENT.md#session-内存运行生命周期与事实访问2026-10-21) 及其 MC 引用；下文带日期接口与测试仍描述各自已有能力，不改变当前 ABI。

保留来源版／防护输入版和分层环境缓存；目标让环境、光照及防护／Effects／高度／时间输入来自同版本 RAM，已有有效结果直接复用，不重复扫描 SQL 证明有效性。候选门／灯／来源变化从候选计算，提交后维护依赖并发布，旧读取结果不覆盖当前缓存。正式提供方接线和规则消费者的既有缺口独立保留。

Environment 是 Scene 的独立逻辑组件。`sm.environment` 与
`WorldHandler.environment` 为同一个实际服务；不依赖统一 SceneService 门面、
SceneLifecycle、Grid 或 Terrain 重构。本文描述本仓库实际接口，非跨模块冻结 ABI。

## 所有权与真实提交路径

### 版本化 Snapshot（Schema 36）

本批基于 `feat/environment-derived-snapshot@382a4ae`，替换上一批“查询时读取全部输入并
比较内容”的有效性机制。`scene_environment_versions` 每个 Session／Scene 维护两个
持久化、非负整数版本：`source_version`（环境来源输入）和 `protection_version`
（防护计算输入）。没有最终结果版本、独立风向版本或 world_version 替代。

| 实际依赖／所有者 | 权威推进入口 | source_version | protection_version |
|---|---|---:|---:|
| WorldFacts 基础条件、局部来源的增加／替换／删除、area／priority／exposure／observable | scene_environments 的 INSERT／UPDATE／DELETE 同步触发器；initialize、SetBase、Source 操作和 Batch 最终事实均走该表 | 条件／来源实际改变时推进 | Wind 值、Wind 来源范围／优先级／observable 改变时推进 |
| 静态降水／风防护 | 同一事实表触发器比较 protection 输入 | 不推进 | 推进 |
| 地图／空间配置 | mechanical_scenes 的 INSERT／DELETE，以及 grid 或 spatial_enabled 的实际 UPDATE | 不推进 | 推进；非 grid 场景机械字段不触发 |
| 正式 WO 当前防护 | VersionedProtectionProvider 声明 object_ids；注册对象姿态、位置、开合、carrier／container、durability、openable、light、mechanism 当前组件的同步写触发器 | 不推进 | 相关对象在旧／新 Scene 的输入改变时推进 |
| WO 额外输入、Effects 创建／移动／修改／结束 | 声明 EnvironmentInputWatch；在所属数据库表的实际写事务中触发 | Effects 的 source／effects_json／wind_source 推进 | 防护 owner 输入推进；effects_json 内的 Wind 投影输入变化或 wind_source 推进 |
| Actor／容器／祖先的间接依赖 | 提供方明确声明其读取的 actor／对象／容器字段、行及适用 Scene，沿这些行的同步写触发器 | 按来源角色 | 按防护／Wind 角色；没有依赖则普通移动／容器变化不推进 |
| tick 的启用／结束边界与连续输入 | EnvironmentTimeWatch 在 world_states.game_tick 写入时同步识别 crossed boundary 或 continuous | 仅 Effects 声明的场景／边界／连续输入 | 防护时间输入及标记 wind=True 的 Effects 时间输入 |
| Session／Package／Scene、Cell／height／区间／披露模式 | 两层缓存键隔离；公开入口每次 SessionAccess pin 和权限校验 | 不靠查询增加版本 | 不靠查询增加版本 |

风向选择本身是环境来源计算的一部分，所以相关 Wind 输入改变时同时推进两版，查询不
通过先读风向来补救版本遗漏。Wind 来源还可能跨 WorldFacts／Effects 同优先级冲突：
即使 wind_from 不变，风速改变也可能使可选方向变成 unsupported。因此写入端对完整
Wind 值保守失效；雨强、温度等非 Wind 条件独立变化不会推进防护版本。

命令的 savepoint、OCC、幂等与回执语义保持：批量只写最终候选，净空操作不写事实，
重放不再执行写入，拒绝／冲突／异常／COMMIT 失败连同版本一起回滚。表级触发器同时覆盖
当前 WorldFacts、场景初始化、物体组件、Grid 导入及内部 SQL 写入路径，不依赖异步事件。
环境事实、空间场景及物体的标识字段禁止原地更名；跨 Scene 物体位置仍通过 scene_id 更新，
相应订阅覆盖原／目标 Scene。迁移与重访不重放来源或重置已经保存的版本。

### 两层缓存与统一读取入口

防护缓存键为 `(Session/Package/Scene/提供方绑定, protection_version, wind_from)`；
方向只用于区分实际计算请求，在最终命中路径不读取风向。完整结果缓存键为
`(同一身份, (source_version, protection_version), Cell/height 或高度区间/公开模式)`。
None 高度与 0 分离。公开与可信内部结果不能互相替代。

- 命中完整 Cell／区间：仅索引读取两版及提供方绑定，不读来源、物体属性或环境 Grid。
- 只变来源版：读取必要当前来源，复用同防护版已有的投影，重合成需要的 Cell。
- 防护版变化：按需读取当前物体／属性／Grid，重新投影并合成；不逐版追算。
- 同版而 B 格尚无结果：合成 B，复用 A 已产生的场景投影，不把屋顶覆盖等同完整环境相同。

query_cell／query_area／query_height_range／query_unit 与 project_cell／project_height_range
统一使用此入口。内部 project 自动加入或建立同一读取事务，继续由可信调用方负责权限及
Package pin。read_facts 是 GM 明确读取权威事实的审计接口，按请求读取事实，不是派生缓存。
区域可见性每批只准备一次带当前 Light 的 topology，并复用其范围枚举整图；每格仍检查
can_see，任一失败整批拒绝。公开命中仍重查当前权限、Actor Scene 与可见性；这项授权所需
的 Grid／Light 准备仍保留，环境校验不额外再准备地图。没有新增 Light 跨查询 Snapshot。

### 正式提供方、时间与第三方限制

正式适配器为 `environment_versions.VersionedEnvironmentEffectsProvider` 和
`VersionedProtectionProvider`。它们包装现有只读 reader，不保存来源或属性的第二份事实。
reader 只能读取声明的 SQLite 输入及显式时间依赖，不能依赖可变 Python 内存或外部 I/O。
稳定 key 标识读取政策；政策或依赖变更必须更换 key／声明并重新登记。

宿主通过 `sm.environment.bind_providers(sid, package, context=有环境管理权限的上下文)`
原子登记。登记只保存不可变依赖描述摘要、触发器清单，不复制输入；每个登记自身也有
savepoint，失败不会留下半套触发器。同一登记重试为空操作。Effects 的 watch 必须声明
source（不产生 Wind）、wind_source（计算 Wind 的其他输入）或 effects_json（typed 来源 JSON，
自动识别 Wind 子输入）；防护 watch 必须为 protection。额外依赖支持明确行、字段、Scene。
所有 provider 输入修改仍走所属 owner 的权威事务，其同步触发器直接推进场景两版。
SQL JSON 分类函数由 SqliteStore 安装；绕开该运行层的写连接若没有函数，会拒绝相关写入，
不会提交输入却漏过版本。

现有 EnvironmentSource／静态防护没有 tick 期限；普通时间推进不改环境版。定时 reader
必须声明 EnvironmentTimeWatch(boundaries=(...), scene_ids=(...))；越过启用／结束边界才推进。
连续变化用 continuous=True，仅推进该提供方声明的 Scene；若生成 Wind，还必须 wind=True。
动态新增／修改时间计划须在同一 owner 写事务重新登记依赖；不能先改变计划、以后再补版本。
WO／Effects 法术生产适配没有因这些接口自动接通；这些是可运行的正式版本化接入入口。

旧的／第三方只读 provider、未登记或登记不匹配的适配器，以及显式禁用路径，不进行任何
跨查询复用。它们仍按原路径读取最新输入、查询内复用，但不会靠逐项比较重新启用缓存。
正式声明必须完整且真实；这是一项可信宿主契约，不能把未声明的外部可变输入包装成正式
适配器。恢复时重新提供同政策 reader 即可匹配持久登记；新政策须受控重绑。缺失 reader
保持原 unconfigured 语义，并禁止误用先前提供方的结果。

当前 OSS 仍只把地面物体投影为防护；携带／容器内物体不会自动随角色成为屋顶。
拾起地面防护会失效其原 Scene，放回地面会失效目标 Scene；物品已经在携带／容器中时，
普通携带者移动不改变该地面模型，不无条件增加版本。若提供方的额外来源／属性实际依赖
角色或容器，必须登记对应字段；本批没有擅自增加一套角色暴露或三维规则。

### 事务、迁移与淘汰

版本与输入共用原数据库事务。写 scope 不读写共享缓存；只读计算先进入 UnitOfWork 的
有界暂存，COMMIT 后再核对当前版本才发布。回滚产生的 N 版永不发布，因此随后另一种
输入提交成同一数值 N 也不会命中候选结果。旧 WAL 读取结果只服务本次事务；COMMIT 后
已过时则不发布，绝不把旧结果改贴新版本。即使核对后又发生并发提交，键仍携带原版本，
新查询也不能把它视为当前；没有可被旧事务倒退的 latest 指针。

防护／最终缓存容量分别 32／512 项，事务暂存也有同样上限。发布当前版本时删除同身份
旧版条目，其他 Session／绑定继续受 LRU 上限约束；不建立历史环境库。clear_snapshots
清空派生值并取消旧暂存的发布，不推进输入版本。重启按保存版本和当前权威输入重建。

Schema 36 在原迁移锁及同一事务中追加版本表与 provider 绑定元数据，给已有实例／环境／
机械场景补 `(0,0)`；新 Session 为 Package 的全部 Scene 建默认行。历史未实体化的 Scene
缺行等价于默认 `(0,0)`，第一次相关写入建立持久行，查询不写库。已有表／行／环境 JSON、
Package digest、tick、RNG、事件、回执不重写。打开时校验新表结构、必需触发器及持久
provider 触发器清单；未知／部分升级失败关闭，Schema 35／两种 Schema 34 的兼容升级保持。

**本批已解决命中时通过版本检查省掉输入扫描。** 未命中时的事实读取、JSON 解析、网格与
输入对象构建仍有成本；地图、Package、规则素材等 Runtime 长期复用留待后续批次。

当前环境事实由 WorldFacts 的 `environment` / `set_environment` 读取和受控写入，
当前 SQLite Schema 36（事实表始于 Schema 35）的 `scene_environments(session_id, scene_id, facts_json)` 为唯一存储。
该表没有对懒初始化 `scene_instances` 的外键，因此可以在首次访问／初始化之前修改
获准场景。没有每格合成结果表、第二份角色高度、效果状态或天气时钟。

写入路径：`submit_command → SessionAccess 授权／Package pin／重放／版本 → savepoint →
WorldHandler → EnvironmentService.operate → WorldFacts → environment.changed →
阶段证据／Outbox／回执 → 同一 SQLite COMMIT`。普通 Actor 的交互权限不能修改天气；
可信宿主必须提供 `TrustedContext(can_manage_environment=True)`，该权限不能从命令数据解析。
信封 actor_id 沿用所选 PC 身份；不是赋予该 PC 自主改天气的能力。
活跃战斗期间沿用 `unsupported_combat_world_operation`，本轮未新增战斗天气结算路径。

`InitializeEnvironment` 是可实际执行的受控导入，不是仅供测试的写表工具。
`sm.environment.initialize` 使用同一 CommandRuntime；首次调用仅在该 Scene 没有任何环境行时
写入完整初值。若此前已修改基础条件、来源或静态防护，整个初值导入为空操作，
不会补套模板覆盖变化。需要补充基础条件时显式提交 `SetEnvironmentBase`。
旧 Session 无环境行时为未配置，查询不自动加载初值。重访／恢复读取已保存事实。

Schema34 保留 main 的机关表升级；Schema35 添加环境表，不修改已有环境行、
Package digest、WorldTime 绑定、抽象 tick、事件、RNG 或回执。
两个历史 Schema34 分支按真实表结构识别，不能仅凭 user_version 判断：

| 打开时的存档 | 环境／机关历史升级步骤（之后追加 Schema36） |
|---|---|
| 新数据库／Schema33 及更早版本 | 顺序执行全部缺失迁移：main34 机关升级，然后 ENV35 |
| 机关版 Schema34 | 保留完整机关状态／激活记录，只执行 ENV35，环境表初始为空 |
| ENV 版 Schema34 | 校验现有环境表和旧三列机关表，补执行原样 main34，再执行 ENV35；复用已校验环境表，保留所有环境 JSON |
| Schema35 | 校验原环境／机关结构后追加 Schema36，不导入默认天气 |
| Schema36 | 校验版本表、必需触发器及 provider 清单，不重置版本或重放输入 |

兼容识别校验列、类型、非空／主键、外键以及机关激活表和索引；未知或部分升级的
结构返回 `unsupported_storage_schema`，不猜测、不清库。ENV35 的幂等建表只复用
已通过校验的现有环境表。整个兼容修复和顺序迁移共用原有 `BEGIN IMMEDIATE` 写锁、
事务和最终外键检查；任一步失败均回滚 DDL、数据和 user_version，可修复故障后重试。
旧 armed／spent 机关仍按 main34 映射次数，已有新版机关状态不会再次映射。
原 WorldTime 仍是需要时间的消费者的唯一时间输入；环境事实本身不使用期限或自动循环；已声明提供方时间依赖按上述同步入口处理。

## 强类型与单位

模型位于 `environment_models.py`，全部使用 frozen dataclass、不可变 tuple 和有界输入。

| 类型 | 表达 |
|---|---|
| `Temperature(celsius)` | 摄氏度；None 明确未知，0 是有效温度；不判断安全阈值 |
| `Wind(speed_mph, wind_from)` | 英里／小时；来向 north/east/south/west 与 OSS 相同；0 + None 为明确无风；缺速度或有效风的来向为未知 |
| `Precipitation(kind, intensity)` | unknown/none/rain/snow/sleet/hail；unknown/none/light/moderate/heavy；none 必须成对 |
| `Obscurant(kind, density)` | unknown/none/fog/smoke；unknown/none/thin/dense；自然雾烟与 Effects 用相同物理表达，所有权不同 |
| `EnvironmentConditions` | temperature/wind/precipitation/obscurant 四个可选字段；字段 None 不贡献条件，类型化 unknown 是明确未知贡献 |
| `EnvironmentArea(cells, heights)` | 复用共享 Grid 的 canonical Cell ID；显式区域为 Cell 元组；空 cells 表示整场景；可选场景基准高度区间 |
| `HeightInterval(lower_ft, upper_ft)` | 英尺、场景统一高度基准、有限数值、`[lower, upper)`；允许负高度，不猜地面标高 |
| `EnvironmentSource` | 稳定 source_id、conditions、area、priority、exposure、observable |
| `EnvironmentSceneFacts` | 可选 base、独立 sources、静态 protection |

没有新增区域目录或地形坐标。已拥有其他区域表达的调用方应先通过共享网格将其展开为
有界 Cell 元组；拒绝越界区域，不裁剪天气范围。场景级基础条件可在无 Grid 场景保存，
Cell 查询和非空 Cell 区域必须有 Grid。高度缺失不默认为零；有高度范围的来源在未指定
高度时贡献明确未知，避免忽略可能有效的条件。

限制：区域最多 256 格，场景事实最多 64 个来源，Effects 单次最多 64 个来源，
批量最多 16 个非嵌套操作；优先级整数 -1000..1000；ID 长度最多 128。
查询整场景最多 256 格，更大场景必须选择区域；单 Cell 防护最多 4096 个高度来源段，
高度范围最多 512 个边界。拒绝 bool 数值、非有限值、重复格／来源、非法单位表达。
Cell 集合、持久来源及静态防护按确定顺序规范化。

## 来源组合规则

基础条件的优先级为 -1001，视为可观察的室外天气。局部来源默认 priority=0、
exposure=`local`、observable=False；Effects 提供方同样必须明确声明这些属性。
持久来源引用为 `fact:<source_id>`，效果引用为 `effect:<source_id>`，基础引用为 `base`。
这三个命名空间互不覆盖。

1. 温度、风、降水分别在适用来源中选择最高优先级。较低优先级保留于事实，
   不进入该字段的当前结果；移除上层后自动恢复。
2. 同优先级数值完全相同保留全部贡献；同级不同值返回 `unsupported` 和可信内部候选，
   不按 ID 随意胜出，不相加温度、不算风向矢量、不叠加降水强度。
3. 显式 unknown 遵循同样优先级，不能从下层补成已知。缺少字段不参与覆盖。
4. 雾烟始终保留各适用层，不按优先级相互消除；任何层未知则合成状态 unknown，
   但内部仍保留已知层。全是 none 才为 none；至少一个已知有效层为 ready。
   自然薄雾与法术浓雾同时存在，法术结束后自然薄雾仍有效。
5. `none` 只表达该来源没有条件，不能删除其他雾烟层；要停止来源使用移除／替换命令。
   薄雾／浓雾密度不在此映射成视线、攻击或察觉规则。

结果 `ResolvedCondition` 的 status 为 `unconfigured`、`unknown`、`none`、`ready` 或
`unsupported`。必须读取 status，不能把空元组／缺速度／缺温度当安全或无风。

## 静态防护、OSS 与有效暴露

静态防护由受控初始化或 `SetEnvironmentProtection` 显式保存：
`SceneEnvironmentProtection(rain_cells=..., wind=..., precipitation=...)`。None 表示未配置，空 tuple 表示
明确没有该类静态贡献。`StaticWindProtection(cell, wind_from, heights)` 不从墙体或
视觉阻挡猜测；每个方向与高度段分别保存。

新增 `StaticPrecipitationProtection(cells, kinds)`：`kinds` 是 rain/snow/sleet/hail 中
1–4 个不重复的显式类型。`precipitation` 是最多 256 项的不可变元组，每项至多 256 格；
仅限列出的类型，非所列类型不产生贡献。旧 `rain_cells` 继续只贡献 rain，旧 JSON
缺少新字段时按 None 读取，不自动升级。设置新字段仍沿用原强类型初始化／防护命令、
授权、批量原子性及 SQLite facts_json，没有 Schema／Package 格式变更。
默认新字段为 None 时规范命令编码省略它，保留已有单项与带标签批量的回执指纹；
显式空 tuple 与非空配置进入指纹。历史无标签批量的拒绝政策保持。

动态物体防护实际调用现有 OSS `scene_protection_contributions`，其内部使用
`project_protection`；读取当前对象姿态和 `CurrentProtectionProvider`，不复制物体几何。
保留 static／object 来源和所有原始高度段，不填平多个来源间的空隙。
静态或任一 OSS 来源证明有覆盖即可证明该点有保护；没有覆盖且任一提供方未配置、
缺输入或不支持时，暴露为 unknown，不能据空投影证明毫无防护。
共享门等 OSS 原有不支持边界保持；不会从其 footprint 造防护。

`precipitation` 表示当地大气条件，`ground_precipitation` 单独表示地面 Cell 的垂直降水暴露。
屋檐下仍可有 atmospheric rain/snow/sleet/hail，地面对应暴露可为 none。
旧 `RainProtection(local_cells)` 只贡献 rain；新增 WO 当前属性
`precipitation=PrecipitationProtection(local_cells, kinds)` 明确声明类型，多个类型共用同一
锚点／朝向／裁剪算法。OSS 返回 `precipitation_cells` 和每类独立完整性状态，rain 旧字段
继续包含旧 rain 与新声明 rain 的并集。Environment `CellProtection.precipitation` 是
`(kind,status,source_refs)` 元组，可通过 `for_precipitation(kind)` 查询；旧 rain_status／
rain_sources 接口保留。已配置旧 rain 输入不证明支持其他类型；缺 provider、缺属性及
unsupported 来源仍保留未知完整性，只有明确覆盖才证明有防护。

只有 `exposure=outdoor` 的来源接受对应种类的保护，local 直接生成的降水仍有效；同优先级
来源产生不同有效暴露时仍为 unsupported。不推断斜向降水或飞行高度遮挡。
显式 height_ft 用于风和有高度的环境来源；ground_precipitation 始终是地面模型，
不是指定高度上的雨线判断。有垂直范围的降水源缺少明确地面标高时保持未知。

`wind` 保留实际来向和速度，`wind_exposure` 用该来向的 Cell 防护区间检查指定高度。
完全覆盖的高度点可报告无当地室外风暴露；高度范围返回每个覆盖／未覆盖区段，
不对“半身挡风”规定机械收益。风未知时不选择一个假定方向判挡风；仅为独立雨投影
使用 OSS 必需的占位方向，绝不作为实际风事实。

`exposure=outdoor` 才接受室外降水／风防护。`local` 来源直接在当地形成，保留有效条件，
屋顶下的法术雨／局部风不会被屋顶自动抹掉。若同级同值来源由于不同防护策略产生
不同有效值，暴露结果 unsupported，保留内部候选，不强行合并。

## 接口

| 入口 | 契约 |
|---|---|
| `initialize(sid, seeds, package, *, command_id, expected_world_version, context)` | seeds 是 InitializeEnvironment 元组；真实受控批量导入，返回 CommandReceipt |
| `read_facts(sid, scene_id, package, *, context)` | GM 管理权限读取完整事实；无行返回 None，不初始化 |
| `query_cell(sid, actor_id, scene_id, cell, package, *, context, height_ft=None)` | 授权本场景可见 Cell 点查询 |
| `query_area(..., area, package, *, context, height_ft=None)` | 每格独立结果元组；保留差异，整批授权失败就不返回部分区域 |
| `query_height_range(..., cell, heights, package, *, context)` | 分段结果，保持空隙和局部环境高度边界 |
| `query_unit(sid, actor_id, package, *, context)` | 通过角色实际位置和 UnitHeightProvider 读实际高度；缺输入为 missing_input |
| `project_cell(..., height_ft=None)` / `project_height_range(...)` | 可信组件内部组合，自动加入／建立读取事务；调用方负责 Package pin、权限和结果披露 |
| `bind_providers(sid, package, *, context)` | 受环境管理权限保护，在写事务中登记正式 provider 依赖；默认空 provider 与相同登记为空操作 |

公开查询复用 `_spatial_reader`、`_public_scope`、`_visible_target` 的 Actor 控制、场景
公共访问及可见性边界，只允许 Actor 当前场景。不会因 GM 可写天气就暴露全 Session。
公开结果不返回任何来源 ID、效果名称或防护几何来源。披露粒度为**整个 Scene／字段**：
只要该场景的任一持久或 Effects 来源在某字段有贡献且 observable=False，该字段在场景
全部 Cell、全部高度均返回 unknown、空贡献元组，不考虑来源范围、优先级、当前点是否生效
或是否被公开来源覆盖。hidden wind 同时屏蔽 wind_exposure 与防风状态，hidden precipitation
同时屏蔽 ground_precipitation 与挡雨状态；防护来源／几何元组始终不对外披露。
同一策略下仅改变隐藏来源的身份、数值、水平范围或垂直范围，不改变这些公开输出。

query_cell、query_area、query_height_range、query_unit 共用该策略，不因隐藏范围返回
不同的 ready／unknown／拒绝。公开高度分段只使用可披露来源及 OSS 的独立几何构造，
再屏蔽上述字段并合并公开结果相同的相邻段，隐藏范围不会成为端点或分段规模错误来源。
被屏蔽字段不返回基础天气冒充有效结果；不受隐藏贡献影响的字段仍按原授权提供。
内部 project_cell／project_height_range 不过滤，继续返回全部真实条件、来源和高度段。

这是有意保守的场景字段级披露策略，不是完整 Perception 实现，不保证隐藏贡献的存在、
移出整个 Scene、字段集合或 observable 权限变更本身不可见；这些改变披露策略适用范围。
仅在同一 Scene 和相同受屏蔽字段集合内提供范围／数值／身份变化的不泄露保证。
`observable=True` 必须由可信配置／Effects 明确证明，非 LLM 自报。
这些查询不产生感知事实、NPC 记忆或可见性机械收益。

公开查询每次在一个只读事务内核对两版，未命中才读取事实／当前提供方／OSS；不建库表、不初始化场景、
不发事件、不推进版本或 RNG。数据库生命周期迁移只发生在 SqliteStore 打开阶段。
命中最终结果仍执行本次控制权限、Package pin、Actor 当前 Scene、public scope 及可见性。
`query_area` 的 `_prepare_visible_targets` 一次准备观察者、带 Light 的 base_topology；
每格 `_visible_target(prepared=...)` 仍执行位置边界与 can_see。任一失败不进入结果合成，
不返回区域子集。公开与可信结果按请求模式隔离；公开命中复用已经掩蔽、去除来源的结果，
披露输入改变会推进来源版并重新过滤。当前授权仍每次检查。降水保护来源一并移除；
隐藏降水会掩蔽所有种类状态。

## 受控命令、空操作和变化信息

注册的命令：`world.initialize_environment`、`world.set_environment_base`、
`world.add_environment_source`、`world.replace_environment_source`、
`world.remove_environment_source`、`world.set_environment_protection`、
`world.batch_environment`。DTO 与命令名一一对应，类型导出在包根。

添加不存在的来源建立来源；已有相同来源为空操作，已有不同来源拒绝。
替换要求来源存在；移除缺失来源为空操作。设置相同基础／防护、已存在环境上的
初始化均为空操作。空操作仍保存回执，不生成事件、不推进 world_version。
批量先在内存构造最终候选，校验所有场景、来源和范围后才写任何行；同一 Scene 多操作
顺序作用于候选。批量最终与原事实相同则无写入／事件／版本变化。
普通失败回滚 savepoint，存储／提交失败回滚整个事务及回执。

一次有效批量只产生一个 `environment.changed`，含 `scene_ids` 和 `changes`：每项有
scene_id、操作类型、source_id（基础／静态时 None）、旧／新影响区域及变化字段。
批内变更轨迹只为最终改变的 Scene 提供失效／协调范围，消费者应重新只读查询当前值；
不是逐项不同 world_version。事件阶段为 `environment.after_batch`，沿用既有证据与 Outbox。
目前没有附加感官 Signal；技术变化信息不能冒充 NPC 已察觉天气变化。
没有新增消息系统、LLM 调用或逐机械 Event 认知路径。

数据适配器 `world_command_from_data` 支持这些命令。批量 wire payload 用带标签的联合：

```json
{"changes": [{"command_type": "world.set_environment_base", "payload": {
  "scene_id": "scene.platform", "conditions": {"temperature": {"celsius": 12}}
}}]}
```

Python 的 BatchEnvironment 直接包含强类型 DTO；不能用 `asdict(batch)` 代替带标签的编码。
`environment_commands.environment_operation_to_data` 是唯一规范化编码入口，与现有
ENVIRONMENT_COMMANDS 注册表、environment_operation_from_data 的 wire 联合对应。
每项保留 command_type 和完整 payload，批量列表保持原始操作顺序，只有对象字段在
canonical JSON 中排序。TypedCommand 指纹使用该编码的 payload；环境单命令 payload
仍为原 asdict 形状，其他已合并命令指纹算法不变。相同请求返回原成功／拒绝回执；
类型、内容、顺序不同则 idempotency_conflict，不执行、不改原回执或状态。
未知字段／嵌套批量／权限字段继续拒绝。

631d957 的旧批量哈希没有子操作类型，无法可靠还原 Add 与 Replace 等歧义。
新版本**不回退匹配无类型旧批量指纹**；同一旧 command_id 的重试也会得到
idempotency_conflict，保留原回执与已提交事实，可通过 get_command_receipt 审计。
宿主须人工／受控核对旧回执与实际事实，确需新操作时显式使用新 command_id；
不猜测旧类型、不自动重执行、不自动迁移或清空回执。新编码批量可正常跨重启重放。

## 使用实例

```python
from dataclasses import replace
from trpg_state_machine import (
    EnvironmentConditions,
    EnvironmentSceneFacts,
    InitializeEnvironment,
    Temperature,
    Wind,
    Precipitation,
)

# sm / sid / package / context 来自宿主已批准的 Session 流程。
gm = replace(context, can_manage_environment=True)
view = sm.read_session(sid, package, context=gm)
receipt = sm.environment.initialize(
    sid,
    (
        InitializeEnvironment(
            view.current_scene_id,
            EnvironmentSceneFacts(
                base=EnvironmentConditions(
                    Temperature(8), Wind(20, "north"), Precipitation("rain", "heavy")
                ),
            ),
        ),
    ),
    package,
    command_id="environment.import.1",
    expected_world_version=view.world_version,
    context=gm,
)
# 在有 Grid 的场景，查询实际角色位置或获准可见 Cell。
position = sm.read_actor_position(sid, view.pc_actor_id, package, context=context)
local = sm.environment.query_cell(
    sid,
    view.pc_actor_id,
    position.scene_id,
    position.cell,
    package,
    context=context,
    height_ft=3,
)
```

## 生产接入与边界

| 项目 | 本轮状态 |
|---|---|
| 环境事实／受控修改／导入 | 完成，真实 CommandRuntime / WorldHandler / WorldFacts / SQLite 运行 |
| 持久化／重访／新进程恢复／Session 隔离 | 完成，真实 Session、Schema33 及机关／ENV 两种 Schema34 历史存档验证 |
| 场景静态防护 | 完成，通过强类型受控导入与命令保存读取 |
| OSS 有效环境合成 | 完成，调用真实几何接口；测试的 WO 当前属性是明确 fixture |
| Package 默认环境解析与自动加载 | 接入契约就绪，PKG-01 尚未接通；不自动修改 Package schema/digest |
| Session 自动初始化与恢复提供方装配 | `StateMachine` 构造依赖入口就绪，SESSION-01 默认加载政策未接通；恢复宿主需重新提供依赖 |
| WO 生产动态防护属性 | OSS CurrentProtectionProvider 可经 `object_protection_provider` 注入；WO-01 实际属性策略仍需其实现 |
| Effects 生产环境贡献 | `EnvironmentEffectsProvider.environment_sources` 与 `environment_effects_provider` 注入就绪；现有 PackageEffects 和角色 EffectState 没有可直接消费的通用环境／区域提供方，未宣称法术接通 |
| 角色实际身高／地面标高 | UnitHeightProvider 与 `environment_unit_height_provider` 注入就绪；当前 CharacterFacts 有实际位置但无可读身高，默认明确 missing_input |
| Light／Effects／Runtime／Perception／RE 后果 | 只读查询与变化事件可供接入，尚未完成这些组件的生产联调 |

Effects provider 返回当前有效来源 tuple；空 tuple 明确没有效果，None 表示提供方不可用。
正式路径由登记的 owner 写入同步推进版本，未命中才读取；第三方无版本路径每次读取。
范围移动／效果结束反映在下次查询，Environment 不保存效果镜像。
提供方必须只读、无外部 I/O，在调用方 SQLite 快照中读取，Effects 继续拥有施法来源、
范围、专注、持续时间及生命周期。测试 fixture 的移动／移除只证明该契约和真实服务合成。

不做气候／流体模拟、天气继承树、降雨涨水／结冰／地下室进水因果、火源熄灭、
燃烧结束、伤害／力竭／攻击骰修改；水域、水位、水深仍归 Terrain。
不决定累计暴露最终所有者；不改变 W-02 或其他独立事项。
MECH-01 沿用最新 main 的机关实施结论，本次环境整合不重新裁定该问题。
本轮 Environment 功能已实现，不另新增 Environment 延期待办；外围组件按既有归属接入。
实际验证命令与结果见 [实施记录](ENVIRONMENT_SERVICE_PROGRESS.md)。


## 2026-10-20 Scoped runtime validation refactor

本批实现采用 Schema40 的事务内派生占格索引和结构验收记录，普通命令由领域
局部准入保护，操作内复用读取，提交不再默认完整空间审查或库存重建。
逐项能力、读取范围、存档兼容、实际计数及本批验证结果见
[系统性重构验收](reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md)。
上文历史测试数字与旧 Schema/待办保持原批次语境，不作为本轮验证结果。
