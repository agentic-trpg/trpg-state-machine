# EnvironmentService

Environment 是 Scene 的独立逻辑组件。`sm.environment` 与
`WorldHandler.environment` 为同一个实际服务；不依赖统一 SceneService 门面、
SceneLifecycle、Grid 或 Terrain 重构。本文描述本仓库实际接口，非跨模块冻结 ABI。

## 所有权与真实提交路径

### Snapshot 依赖设计（2026-10-19，实施前审计）

基线 `origin/main@d0fda62`。现有模型没有环境／防护专用版本，不能用
`world_version` 或 `environment.changed` 作为完整失效依据。采用不可变完整输入的
内容身份；每次查询仍在同一 SQLite 读取事务中核对，原始事实不建立可变镜像。

| 依赖／所有者 | 改变操作与当前身份验证 | 影响的结果层／事务处理 |
|---|---|---|
| WorldFacts 环境事实 | initialize/base/source add/replace/remove/protection/batch；读取并校验完整 frozen facts，包括范围、高度、优先级、exposure、observable | 最终环境；静态防护直接参与合成，不要求重新投影对象；沿原命令 savepoint／COMMIT，回滚后重新读旧事实 |
| WO 当前防护提供方 | 开关、损毁、部署或注入输入变化；每次按确切 candidate 读取完整 frozen CurrentProtectionProperties | 防护投影及最终环境；不从 HP、视觉或 footprint 猜属性，提供方仍拥有政策 |
| OSS 姿态与来源集合 | 放置、移动、旋转、拾取、容器／场景迁移、开关、HP=0；读取当前对象集合、ObjectSpatialState、非地面／共享门资格及当前属性 | 两层；不纳入角色位置、无关物品字段或总 world_version；无关空间物体变化允许保守失效 |
| Grid 当前场景配置 | 当前 spatial Scene 行或未初始化 Package grid；规范化配置形成身份；包含边界尺寸 | 两层；不复制权威网格，不用 Light 投影作为环境依赖 |
| Effects 来源提供方 | 创建、移动、字段修改、结束；每次读取完整不可变来源 tuple 和可用状态 | 最终环境；保留 Effects 所有权，无期限镜像；无提供方与显式空 tuple 不混同 |
| Session／固定 Package／Scene | sid、package digest／版本／ruleset binding、scene_id 隔离；公开入口每次执行原 SessionAccess pin 校验 | 两层；缓存实例还隔离 Store；可信内部投影仍由调用方负责 binding 和权限 |
| 查询条件 | Cell、height_ft（None 与 0 不同）；高度区间使用各实际计算的点结果 | 最终环境；防护共享原高度段，不生成多层三维地图 |
| Actor／权限／Light 可见性 | 每次公开查询重新验证控制权限、当前场景、public scope、感官／conditions 与逐格 can_see | 不缓存授权；区域内只准备一次观察者／Grid／Light，整批任一失败就不返回部分结果 |

两层独立有界 LRU：OSS 场景防护投影上限 **32 项**（包含风向）；完整单格环境上限
**512 项**（包含依赖和 height_ft）。缓存归当前 EnvironmentService／Store 实例，
不跨数据库共享。事务暂存的每层也采用相同容量上限，发布后按 LRU 淘汰。
只缓存实际产生的完整结果；屋顶投影覆盖其他格不代表这些格环境相同。
天气／来源变化不进入 OSS 身份，风向选择相应投影；不在变更后重算全图。
旧读取事务按自身内容身份保存，不能替换新状态身份；不维护可被旧事务倒退的 latest 指针。
写事务及无可验证读取作用域绕过共享缓存；只读事务内暂存的结果仅成功 COMMIT 后发布，
拒绝、异常、回滚、提交失败均丢弃暂存。清空或重启后从事实重建。

外部只读提供方仍必须无写入／I/O，在调用方快照内返回完整 typed immutable 输入。
输入读取、校验、身份比较及公开授权每次仍需执行；省去的是几何展开与环境合成。
无法保证该契约的注入方须禁用跨查询复用（`environment_cacheable=False`），
仍不能用缓存掩盖未知／不支持状态。Light 跨查询 Snapshot、角色交互与认知重构不在本轮。

### 已实现的读取与重建路径

`_inputs` 每次读取完整环境事实与 Effects；OSS `prepare_scene_protection` 每次读取当前
来源集合、姿态、资格、属性和规范化 Grid 配置，生成本次查询的不可变输入。输入是身份的
组成部分，不另建可变对象状态。每个对象仍先读取 provider；只有降水与风均明确配置且
几何全空的来源才可从 Environment 消费输入中省略。因此拾取明确无贡献的物品不会更换
身份，未配置／不支持来源不能用同样方式忽略。`scene_protection_contributions(prepared=...)` 使用这份
输入计算完整场景投影，并共享到后续其他 Cell；不再次调用 provider 形成混合状态。
区域与高度范围内仍用同一个 inputs／按风向 cache。最终 `_cell` 只在身份／Cell／高度
未命中时调用 `_compose_cell`；不同高度范围复用的是实际已算出的点，分段边界每次按原
来源规则构建，未将缺高度当零。

缓存以内容相等验证当前身份，没有环境专用版本或散列碰撞假设。保守地把对象开关、
destroyed 和完整 Grid 配置纳入依赖；HP 数值本身只通过 destroyed 或当前 provider 输出
影响身份。普通移动、无贡献物品拾取、时间推进、锁细节等不变更这些实际输入时不会失效；
若提供方消费了它们并返回不同来源／防护，立即更换身份。无版本仍需来源扫描、每物体
当前属性读取／校验及完整输入比较；当前 Grid JSON 读取／规范化还可能逐对象重复。
这不是零 SQL／零 provider 调用优化，不宣称生产延迟收益。

`SqliteStore.derived_read_scope` 仅允许受管理的非写事务；两层未命中结果先进入该
UnitOfWork 的有界暂存，COMMIT 成功后发布。写 scope、无 scope、provider 显式禁用路径
均直接计算；查询内仍可复用方向投影。回滚或提交失败清除暂存，不发布候选结果。
`clear_snapshots()` 清空两层并取消清空前的暂存发布；无事件、无版本、无 RNG 或 SQL 写入。
重启重新装配提供方后得到同样结果。旧内容项可暂留至淘汰，但新查询必须先验证当前输入，
不会因为旧项存在就使用它。

当前环境事实由 WorldFacts 的 `environment` / `set_environment` 读取和受控写入，
SQLite Schema 35 的 `scene_environments(session_id, scene_id, facts_json)` 为唯一存储。
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

| 打开时的存档 | 原子升级到 Schema35 |
|---|---|
| 新数据库／Schema33 及更早版本 | 顺序执行全部缺失迁移：main34 机关升级，然后 ENV35 |
| 机关版 Schema34 | 保留完整机关状态／激活记录，只执行 ENV35，环境表初始为空 |
| ENV 版 Schema34 | 校验现有环境表和旧三列机关表，补执行原样 main34，再执行 ENV35；复用已校验环境表，保留所有环境 JSON |
| Schema35 | 校验已完成的结构，重启不重复升级或导入默认值 |

兼容识别校验列、类型、非空／主键、外键以及机关激活表和索引；未知或部分升级的
结构返回 `unsupported_storage_schema`，不猜测、不清库。ENV35 的幂等建表只复用
已通过校验的现有环境表。整个兼容修复和顺序迁移共用原有 `BEGIN IMMEDIATE` 写锁、
事务和最终外键检查；任一步失败均回滚 DDL、数据和 user_version，可修复故障后重试。
旧 armed／spent 机关仍按 main34 映射次数，已有新版机关状态不会再次映射。
原 WorldTime 仍是需要时间的消费者的唯一时间输入；本服务当前不使用期限或自动循环。

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
| `project_cell(..., height_ft=None)` / `project_height_range(...)` | 可信组件内部组合；调用方负责同一读事务、Package pin、权限和结果披露 |

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

公开查询每次在一个只读事务内读取事实／当前提供方／OSS；不建库表、不初始化场景、
不发事件、不推进版本或 RNG。数据库生命周期迁移只发生在 SqliteStore 打开阶段。
命中最终结果仍执行本次控制权限、Package pin、Actor 当前 Scene、public scope 及可见性。
`query_area` 的 `_prepare_visible_targets` 一次准备观察者、带 Light 的 base_topology；
每格 `_visible_target(prepared=...)` 仍执行位置边界与 can_see。任一失败不进入结果合成，
不返回区域子集。公开披露输入与可信完整输入分别参与内容身份；公开返回总是再执行
当前场景字段掩蔽及来源去除。新增降水保护来源一并移除；隐藏降水会掩蔽所有种类状态。

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
每次查询重新读取；范围移动／效果结束反映在下次查询，Environment 不保存效果镜像。
提供方必须只读、无外部 I/O，在调用方 SQLite 快照中读取，Effects 继续拥有施法来源、
范围、专注、持续时间及生命周期。测试 fixture 的移动／移除只证明该契约和真实服务合成。

不做气候／流体模拟、天气继承树、降雨涨水／结冰／地下室进水因果、火源熄灭、
燃烧结束、伤害／力竭／攻击骰修改；水域、水位、水深仍归 Terrain。
不决定累计暴露最终所有者；不改变 W-02 或其他独立事项。
MECH-01 沿用最新 main 的机关实施结论，本次环境整合不重新裁定该问题。
本轮 Environment 功能已实现，不另新增 Environment 延期待办；外围组件按既有归属接入。
实际验证命令与结果见 [实施记录](ENVIRONMENT_SERVICE_PROGRESS.md)。
