# GridService

## 2026-10-22 按实例版本复用地图输入

GridMaps.scene 接入 Scene.spatial.record；prepared_scene/current_grid 共享不可变地图，
MechanicalGrid 完整类型模型由 spatial.sensory 持有。局部动态阻挡采用共享基础集合的
只读覆盖，不复制、合并排序或编码整图。base_topology、公开 Light 和阶段采集使用同范围
已准备拓扑；未知版本的显式固定地图仍按自身内容处理。地图事实变更推进原投影版本，
移动不失效基础地图；旧输入、候选、回滚、预算和恢复保持隔离。
实际入口、计数及完整输出边界见 [本次验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md#2026-10-22-follow-up-managed-cache-inputs)。

## 2026-10-22 场景空间字段

GridReader 的生产读取使用 Scene.spatial 的 map／mechanical_map、occupants、
object_positions 和有界 cells 字段。机械地图与精细空间授权仍分开；局部 topology
读取请求格及边缘邻格的角色／Schema40 占格索引，不枚举场景目录。地图、关系和格子
投影列依赖由同一候选／提交范围维护。Light／防护只读物体位置时不附带无关角色位置。
单对象／门／指定区域的可见性用目标格照明覆盖，共享不可变几何，不逐对象解析或投影全图。
完整 GridView 仍按明确场景范围构建，保留旧 View。证据见
[实例归属报告](reviews/RUNTIME_INSTANCE_OWNERSHIP.md)。

## 2026-10-22 当前运行层

GridMaps／局部空间输入现从统一 RAM 或候选读取，已加载地图不再为内容键重复 SELECT。
原只读拓扑、动态几何算法及明确范围保留；固定旧 View 不被后续提交或卸载修改。
本轮范围与成本见 [实施报告](reviews/SESSION_MEMORY_RUNTIME_IMPLEMENTATION.md)。
下述 2026-10-21 目标段落保留历史状态。

## 2026-10-21 Session 运行层接入目标

Architecture Decision Status **[DECIDED]**；完整 Session 内存运行层 Implementation Status **[NOT IMPLEMENTED]**；Acceptance Status **本次未执行运行验收**。 统一事实读取、候选／持久提交／内存发布与故障规则见 [Session Management](SESSION_MANAGEMENT.md#session-内存运行生命周期与事实访问2026-10-21) 及其 MC 引用；下文带日期接口与测试仍描述各自已有能力，不改变当前 ABI。

保留已有只读 GridMaps／基础拓扑、Schema40 定向占格与 local_topology，目标将地图配置、角色／对象姿态及动态贡献从统一 RAM 一致范围读取，再构建必要 GridView／Snapshot。候选几何检查读候选位置和候选索引；旧 View 不随发布／回滚／释放改变。当前 GridMaps.scene 仍读 state_json 取内容键，操作 SQL／memo 不是完整 RAM 运行层；接入后不应为有效内存地图反复读库验证。

## 稳定职责与契约

GridService 负责 Scene 网格内容的读取、坐标／边界校验、当前空间事实组合及几何查询。
Scene 生命周期由 SceneService 协调；角色位置由 Character 管理；物体姿态与空间贡献由
ObjectSpatial 等所属组件管理。查询不建立第二份位置权威，不初始化场景、不执行移动、
不开门、不推进时间、不触发认知，也不判定攻击、施法或感知是否成功。

实体占用、移动阻挡、困难地形、视觉遮挡和掩护分别表达。内部完整空间输入和 Actor
授权视图有不同信任边界；完整输入不能直接传给 Host／玩家／NPC。Navigation 负责路线与
步骤，Grid 提供空间输入。战斗移动资格与额度由 RE／Combat 管理；探索不引入每回合
Speed 或剩余额度，探索耗时由 Activity／探索／时间流程负责。距离、路径长度和移动费用
具有不同语义。

上述责任沿用顶层 `MODULE_CONTRACTS.md` §0.3a／§0.5–0.6、
`STATE_MACHINE_ARCHITECTURE.md` §2.0–2.2 与 `WORLD_SUBSYSTEM_DESIGN.md`。
这些目标架构中的日期化实现快照不能作为当前代码状态；本文以下内容是 SM 本地实现说明，
不冻结跨仓库 ABI、不改写其他仓库的历史快照。

## 当前实现与调用

WorldHandler 组合 `grid`。它的 `reader` 是只依赖 Store／WorldFacts 的底层只读组件；
ObjectSpatial 和 Light 只调用这个读取层，不能回调 `read_scene`、`base_topology` 或
`exploration_topology`。Grid 的上层组合再消费 OSS 贡献和既有 Light 投影，不形成递归。
GridMaps 保存有界的只读地图运行表示，SQLite 仍是权威配置；没有持久化格表或数据库迁移。

### 地图表示与生命周期（2026-10-19）

`WorldFacts.maps` 引用当前 Store 唯一的 GridMaps，GridReader 和地图消费者共享它。
`mechanical_scenes.state_json` 继续保存 Session 的有效配置，spatial_enabled 保留精细
空间资格。PreparedScene／PreparedGrid 保存递归只读数据、grid_json 和基础拓扑。
WorldFacts.spatial_scene／mechanical_scene、GridReader.scene_grid 继续返回独立字典／
列表副本。GridView 原五个 dataclass 字段、asdict、grid_json 和构造参数保持兼容；
运行表示不进入序列化数据，旧 View 不被后续变化原地修改。

SessionCreation 经 SceneLifecycleService 在地图写入后调用 SessionRuntime.prepare_scene_maps；
延迟实例化和已有场景投入运行走同一入口。CombatHandler 补建原先允许的机械场景后
也准备地图。save／resume 在同一授权读取事务校验事实并准备当前场景，其他场景首次
读取时按需准备。准备不创建权威 Scene、不计访问、不产生进入事件、不启用 NPC 模拟。
原场景提前创建范围保持。

每次 live lookup 先 SELECT 当前事务可见的 state_json／spatial_enabled，再以原始编码
查询池，不为了取得身份先解析地图。记录键包含 Store 隔离、Session、Scene、live 来源和
完整编码内容，不依赖 world_version、loader 地址或仅 scene_id。当前生产地图写入口仍是
WorldFacts.initialize_rule_scene／initialize_mechanical_scene 的 INSERT，没有新增地图
编辑命令。有效配置变化后，下一次读取按实际内容准备对应表示。

本事务候选、savepoint／整个事务回滚、另一连接提交和旧 WAL 读取事务，各自选择其可见
内容。池中没有“当前地图”指针或提交状态：回滚候选可以留在有界池中，但数据库不再
返回其内容时就不能选作当前事实；失败创建的 Session 不能从池中复活。已有实例缺地图
仍拒绝，不能通过 Package 默认修复。未实例化场景的受控静态读取走独立 template 来源。

记录池和几何池各最多 64 项、各最多 8 MiB 编码键预算；LRU 淘汰，超预算单项仅临时
使用。该预算不是 Python 堆内存精确值，解析数据和拓扑数量也受项数限制。
SessionRuntime.release_scene_maps 可释放指定 Session 的引用，Store.close 清空两池。
调用方持有的旧 View 独立保存快照，淘汰／关闭不破坏它，释放 View 后可回收。
不按请求无限累积完整地图，不向 SQLite 序列化 Python 拓扑。

ReadOnlyTopology 继承固定 RE GridTopology 原算法，只将集合变为 frozenset／只读映射、
墙段变为不可变四坐标记录，并禁止属性／字典修改。没有复制距离、移动、掩护或视线算法。
该只读适配核对了当前固定 RE 的 backing fields，升级 RE 时应重新审阅其内部可变字段。
GridScene 校验默认值仍不能代替完整 MechanicalGrid 感官证据校验。

角色和 OSS 贡献仍在每个读取范围重新取值。Light／探索组合先取当前动态输入，再用完整
投影网格内容复用拓扑；灯光／阻挡改变即选另一表示。开合、朝向、门槛、占格和掩护仍按
原通道消费，不把探索移动阻挡混入机械／视觉基础地图。View 的动态 can_step 和独立对象
掩护可建立临时组合拓扑，不把它们当成基础拓扑。历史感知证据的几何构建保留历史输入，
不拿当前 Session 地图替换事件时地图。

导航授权后改用 prepared_package_topology，在 template 命名空间复用静态 Package 地图，
不读远端当前秘密或覆盖 live 配置。旧静态 package_topology 保留脱离 Store 的单次构建
兼容路径；实际 Activity 规划已经使用 Store 级准备接口。

### 三个查询算法与调用计数

固定 RE 当前只支持矩形方格、Chebyshev 八向距离，没有额外斜向距离选项。
函数保留原 y 从小到大、再 x 从小到大的顺序和几何含义，不增加通行或视觉过滤。

- cells_within_range：先校验范围和中心，将 ft 换算为候选半径，裁剪至地图边界，仅枚举
  候选矩形，再调用 RE within_range 精确筛选。浮点按精确整数比计算半径，先按地图
  跨度限幅，合法极大有限值不会产生无穷大或边界转换溢出。
- entities_within_range：校验同样参数后，按原 occupants 顺序直接判断实际占格，任一
  占格命中即返回实体一次；支持多格物体和空场景，不调用范围扫描或 occupied。
- neighbors：直接生成边界内周围最多八格，用 RE within_range 筛选，不扫描全图。

行为和计数测试用旧式全图枚举对照。100×100、5 ft 地图中心 5 ft 查询：范围精确检查
由 10,000 次降为 9 次；邻格由 10,000 次降为最多 8 次。三个实体、实际五个占格的
示例，实体查询由 10,000 次范围扫描再求交集变为 3 次占格判断，多格首次命中即短路。
创建已准备地图后，两次公共 read_scene 和反复属性／坐标／距离／范围、基础 Light 与
环境校验，新增地图 json.loads 和基础拓扑构建均为 0 次。冷配置解析一次、构建一次，
LRU 淘汰后可重建。这些计数不代表全部 JSON、完整 Light 投影、RE Snapshot 校验或
历史证据几何都变为常数工作。

以下另外实际载入基线 a25c35e 的 GridView，对同一 100×100 地图、中心 5 ft 和上述
三实体夹具逐项计数；新实现已经准备好表示。计数依次为 json.loads／拓扑构建／
within_range，不是耗时比较，冷启动准备开销单独发生一次。

| 单次调用 | a25c35e | 本轮已准备表示 |
|---|---|---|
| width + height + cell_size_ft | 3 / 0 / 0 | 0 / 0 / 0 |
| distance_ft | 3 / 3 / 0 | 0 / 0 / 0 |
| cells_within_range | 103 / 2 / 10,000 | 0 / 0 / 9 |
| entities_within_range | 112 / 11 / 10,000 | 0 / 0 / 3 |
| neighbors | 105 / 3 / 10,000 | 0 / 0 / 8 |

| SM 本地接口 | 语义与范围 |
|---|---|
| `grid.reader.scene_grid` | 当前持久网格；只有尚未初始化的场景才可读取 Package 初值 |
| `grid.reader.scene_occupants` | 当前角色／物体锚点目录；未初始化场景只补未实例化实体，不恢复已移动／携带实体的模板位置 |
| `grid.read_scene` | 一个读取范围中的不可变 GridView：网格、world_version、角色锚点及 OSS 各类完整贡献 |
| `GridView.normalize_cell` | 规范化查询坐标并检查边界；已有事实必须已规范化，缺失／非规范／越界事实拒绝 |
| `occupied` / `entity_cells` | 格子集合与实体占格查询，实体保留 actor／object 类型；未知本地实体不冒充空占格 |
| `distance_ft` / `is_adjacent` / `neighbors` | RE 的距离／相对邻接；墙和占用不改变几何距离或相对邻接 |
| `cells_within_range` / `entities_within_range` | 几何范围内候选，包含阻挡／占用格；候选不等于可达、可见或获准目标 |
| `movement_blocking_cells` / `difficult_terrain_cells` | 静态与对象贡献分别按对应通道组合；角色不加入物体阻挡通道 |
| `can_step` | 越界检查、静态／物体阻挡、墙和切角；不包含经过角色的规则资格、不返回或支付移动费用 |
| `has_line_of_sight` / `cover_between` | 受支持的几何遮挡／掩护；不包含灯光、感官、感知成功或授权 |
| `cells_in_template` | 当前 RE 的二维模板候选格；没有效果传播、目标筛选或命中裁定 |
| `grid.exploration_topology` | 原探索／接触专用输入，保留保守的角色占格阻挡、对象阻挡、门槛与目标排除；不能用作战斗／视觉 Grid |
| `grid.base_topology` | 原静态／Light 可见性消费者的兼容输入；不是完整动态视觉能力 |
| `GridService.package_topology` | Navigation 检查授权后使用的静态 Package 网格，不读取远端动态秘密 |

所有服务读取自动建立单个只读 SQLite 事务，或加入调用方当前事务。组合中的网格、位置、
门状态、贡献和版本不跨读取范围。事务内能看到本事务已应用的候选事实，查询不提交调用方
事务；此时 world_version 可以仍是外层操作提交前的版本。不能把这样的内部候选视图提前
发布为已提交世界。提交后重新查询取得新布局；回滚后重新查询取得原布局。已经取得的
GridView 保持冻结，不自动刷新，也不能据其旧版本执行操作。

读取当前布局的内部示例（Session／Package 绑定和权限已由入口核验）：

```python
view = world.grid.read_scene(session_id, scene_id, package)
nearby = view.entities_within_range("1,1", 10)
geometry_clear = view.can_step("1,1", "2,1")
# nearby / geometry_clear 是内部几何候选，不是 Actor 授权或执行许可。
```

## 支持范围与能力限制

- 坐标为 `x,y`，x 向东、y 向南；矩形整格边界为 `[0,width) × [0,height)`，
  尺度读取当前网格的 cell_size_ft。几何与模板调用固定 RE 的公开接口，没有复制距离、
  墙、切角或寻路算法，也没有新增 RE 私有方法依赖。
- 物体占格、朝向及五类贡献完全复用 OSS；共享门在当前场景端点贡献，不复制另一侧锚点。
  支持范围继承 OSS 的整格／四向形状与门槛限制，不能由物体多格支持推断角色多格支持。
- **角色仅表示当前唯一锚点格。** 未计算 Size 对应的完整 footprint，不提供大体型／多格
  角色的完整占用、接触或通行保证。后续 Character／RE 协作见支持矩阵的待办。
- 静态 blocked_cells 保留固定 RE 的既有全格障碍语义。动态物体移动阻挡不会进入视觉／
  掩护输入；角色占用不等于墙。基础 `can_step` 不把角色阻挡；探索专用投影继续保留
  既有“其他角色阻挡”的限定策略，战斗经过角色的规则仍由 RE 判定。
- 对象掩护使用 RE 公共 cover 查询逐来源计算，与静态掩护取最强等级；不会根据移动阻挡
  生成 total cover。墙线／静态实心障碍阻断几何视线时报告 total。调用方明确请求
  `creature_cover=True` 才将其他角色锚点传给 RE 半掩护查询，仍无 AC／豁免结算。
- **存在动态 vision_occlusion 贡献时，完整视线查询返回 `unsupported_grid_vision`。**
  当前 RE 没有独立的动态视觉阻挡输入，不能用移动 blocked_cells 假装实现，也不能把
  缺口解释为无遮挡。掩护和移动通道仍可独立查询。静态／Light 原消费者保留原支持范围。
- 模板为 RE 的 sphere／cylinder／cone／line／cube／square 二维离散候选，保留其原点
  包含约定；有方向要求而缺方向、未知形状等返回 `unsupported_grid_template`。三维、
  高度、效果传播与实际 AoE 目标裁定不在此接口承诺内。
- 缺网格返回 `spatial_unconfigured`；缺失／非规范／越界位置返回 `invalid_spatial_state`；
  查询坐标非法或越界返回 `invalid_cell`。已初始化但缺失网格时不回退模板、不修复写库。
- `read_scene` 是可信组件接口，未增加 StateMachine／TypedCommand 的无权限空间目录。
  面向 Actor 的对象／场景／空间查询继续复用现有控制、可见性及 Package 检查；Navigation
  只规划授权静态地图，执行时重新检查当前布局。不会把内部实体目录塞进公开导航结果。

现有 Combat 动态障碍／灯光／对象贡献 guard 保留；本批未把探索投影接入 RulesAdapter
机械 Snapshot。Effects 仍归所属组件，未增加未经确认的贡献协议。Turn Episode／Round
Episode 设计不变，没有新增逐机械 Event 的感知信号或阶段取证要求。

当前 Schema 36、Package 支持版本、命令协议和 RE／SRD 固定 SHA
`d596161060555630019163ab301e1d25fd816549` 均不变。
验证、W-01 进度和跨系统依赖见 [实施记录](GRID_SERVICE_PROGRESS.md)。


## 2026-10-20 跨仓库对齐与证据适用范围

本文件带日期批次／分支／Schema／“未发现 AGENTS”属于原时点证据；最新 main 为 Schema39，实例配置与冻结素材生产装配见 [Instance Progress](INSTANCE_CONFIGURATION_PROGRESS.md)，时间协调见 [Time Progress](SESSION_TIME_COORDINATION_PROGRESS.md)。旧缺口表不能覆盖上述已完成切片。当前开发入口为仓库 AGENTS.md；共同规范只维护于顶层 MC §2.3、§6.5–§6.6。全局审查、局部准入、实际 SQL／投影范围及剩余重构详见 [校验范围审查](reviews/RUNTIME_VALIDATION_SCOPE_REVIEW.md)。本次只更新文档，没有运行新增业务／规模测试。


## 2026-10-20 Scoped runtime validation refactor

本批实现采用 Schema40 的事务内派生占格索引和结构验收记录，普通命令由领域
局部准入保护，操作内复用读取，提交不再默认完整空间审查或库存重建。
逐项能力、读取范围、存档兼容、实际计数及本批验证结果见
[系统性重构验收](reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md)。
上文历史测试数字与旧 Schema/待办保持原批次语境，不作为本轮验证结果。
