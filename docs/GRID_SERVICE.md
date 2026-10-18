# GridService

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
没有派生缓存、持久化格表或数据库迁移。

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

当前 Schema 35、Package 支持版本、命令协议和 RE／SRD 固定 SHA
`d596161060555630019163ab301e1d25fd816549` 均不变。
验证、W-01 进度和跨系统依赖见 [实施记录](GRID_SERVICE_PROGRESS.md)。
