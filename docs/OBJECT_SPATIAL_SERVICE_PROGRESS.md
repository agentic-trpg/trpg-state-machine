# ObjectSpatialService 实施与边界（2026-10-18）

基线 main：`a897ae5`；实施分支：`feat/object-spatial-service`，不合并 main。
新增 Package `state-machine-foundation/8`；当前 SQLite Schema **33**。
本文描述实际实现，World、Openable、Container、WorldTime 历史记录继续保留。

## 职责与权威来源

WorldObjectService（WOS）仍是物体查询与操作公共入口。StateMachine 的
`get_object` / `list_scene_objects` 委托给它；业务操作经过原 TypedCommand 分派。
ObjectSpatialService（OSS）由 WOS 组合，负责占地、候选检查、空间贡献与接触几何。
WorldFacts 的 `objects.cell`、`objects.orientation`、`objects.state` 与耐久表
分别保存权威锚点、朝向、开关与 HP。**不保存第二份占地或空间投影**。

Openable、Container、Mechanism、Light 保留自己的能力、权限与业务判断。
WorldHandler 保留角色移动、共享通道、场景和事务编排；普通物体接触范围转为 OSS 委托。
WorldTimeService 与 WOS 平行；本次没有新建 SceneService、EnvironmentService 或地形系统。

## 朝向与旋转

坐标为 `x,y`，x 向东增加，y 向南增加。基础朝向 `north` 使用定义原始相对坐标。
锚点是实例位置，也是旋转基准；不会自动平移或重新居中。

| 实例朝向 | 相对坐标变换 |
|---|---|
| north | `(dx,dy)` |
| east | `(-dy,dx)` |
| south | `(-dx,-dy)` |
| west | `(dy,-dx)` |

`footprint=[[0,0],[1,0]]`、锚点 `3,3` 的四种结果依次是
`[3,3;4,3]`、`[3,3;3,4]`、`[3,3;2,3]`、`[3,3;3,2]`。
对称形状可以产生相同占地，但不同朝向仍是不同事实，产生事件与 world version 变化。
位置、朝向均不变才是无事件的空操作。

Package `/8` 的场景物体实例可声明 `initial_orientation`，缺省 north；定义的 spatial
能力声明 `allowed_orientations`，缺省 `["north"]`，初始值必须被允许。
新定义要求有本地网格、含原点、最多 64 格、偏移绝对值不超过 32、正交连通的整格形状。
普通刚性形状只支持网格角点上的水平／垂直墙线；非整数或斜墙明确返回
`unsupported_object_geometry`。共享门继续使用已有端点／门槛模型。
不支持斜向朝向、任意多边形、分离形状、跨场景搬移或随开关变化的形状。

复用 `world.reposition_object` / `RepositionObject`：

```python
# 原地顺时针旋转；移动并旋转只需改变 to_cell。
op = RepositionObject(
    scene_id,
    object_id,
    expected_cell="3,3",
    to_cell="3,3",
    expected_orientation="north",
    to_orientation="east",
)
```

沿用 `TrustedContext.can_place_world_objects`、`gm_placement`、实际本地地面物体、
角色可用状态与非战斗范围。共享门及 fixed 物体不能借此移动或旋转。
改变方向必须给出 `expected_orientation`；失配返回 `orientation_conflict`。
锚点失配返回 `position_conflict`，全局旧版本仍返回 `version_conflict`。
省略新字段的旧命令保持当前朝向；力量、动作消耗与玩家推动没有实现。

## 统一空间检查

`check_candidate` 以不可变候选替换一个实例，调用 `_check_scene`；`audit` 调用同一规则。
检查完整占地、边界、静态阻碍、形状内部墙边、阻挡占格冲突及通道保留格。
普通 spatial 物体不得占用通道 standing cell 或 threshold cell；共享门走原专项检查。

占有空间与阻挡移动独立，沿用已有 audit 策略：移动阻挡物体与角色、其他阻挡物体不能
重叠；非阻挡物体可与角色／物体重叠。没有新增所有物体排斥所有角色的规则。
旧普通 PlaceObject 的移动、接触与角色冲突要求保留，并接入同一候选底层；
多格 spatial 物体仍只能使用获准 GM 布置入口。

**跨墙复现**：在原 main `a897ae5` 的真实运行时，两个占格分别有效，
`edge_distance("1,0","1,1") is None`，原 GM 摆放仍返回 committed。
现在逐一检查刚性形状内部相邻整格的边，候选及最终 audit 均拒绝该结果。
专项测试同时覆盖公开命令拒绝与故意绕过 admission 后的 audit 拒绝／事务回滚。

## 状态与空间贡献

原有三个 `*_blocks_movement` 字段继续表示移动阻挡，不推断视觉遮挡或掩护。
Package `/8` 可分别声明 `open_properties`、`closed_properties`、`destroyed_properties`：

```json
{
  "kind": "spatial",
  "footprint": [[0, 0], [1, 0]],
  "mobility_policy": "gm_placement",
  "allowed_orientations": ["north", "east", "south", "west"],
  "open_blocks_movement": false,
  "closed_blocks_movement": true,
  "destroyed_blocks_movement": false,
  "open_properties": {"difficult_terrain": true, "cover": "half"},
  "closed_properties": {"vision_occlusion": true, "cover": "three_quarters"},
  "destroyed_properties": {"difficult_terrain": true}
}
```

每个 properties 缺省 `difficult_terrain=false`、`vision_occlusion=false`、`cover="none"`。
掩护几何标签支持 none / half / three_quarters / total，作用于整份实际占地。
遮挡表示配置的完整不透明格贡献，困难地形表示进入格的地形贡献；不支持高度、半格、
材质透射、射线命中比例或随观察者变化的形体。空集合表示没有声明该贡献，不能用来
断言现实物体必然透明或没有其他规则影响。

HP 为零时，`disable_spatial_policy` 选择 destroyed 移动与性质配置；
`disable_preserve_obstruction` 保留当前开／关空间配置。两种策略均保留实例与占地。
不可开关的物体使用 closed 配置，不统一假设摧毁后消失或保留阻挡。
Durability 仍只接受原内部获准结算，**没有新增玩家扣 HP 命令**。

## 实际接口与调用链

以下 OSS 接口用于可信组件组合，不是无权限全局对象 API：

| 接口 | 实际结果／用途 |
|---|---|
| `state(sid, oid, package)` | 不可变 ObjectSpatialState：scene_id、anchor、orientation、open_state、destroyed；未初始化场景只读初始定义，现有实例绝不回退定义。 |
| `cells(package, oid, anchor, orientation)` | 纯占地变换；旧单格无 spatial 能力物体返回锚点格。 |
| `project(sid, oid, package, candidate=...)` | 不可变 ObjectSpatialProjection，独立包含 occupied_cells、movement_blocking_cells、difficult_terrain_cells、vision_occlusion_cells、cover_cells；后者为 `(cell, degree)` 元组。 |
| `scene_contributions(sid, scene_id, package)` | 对象 ID 到上述投影的映射；共享门用本场景端点，包括未初始化的另一侧。后续地形系统可按自己的合并策略消费。 |
| `check_candidate(...)` / `audit(...)` | 同一底层规则；前者返回合格候选投影，不写事实。 |
| `target_cells(...)` / `visible_cells(...)` | 完整普通物体几何／受支持视觉规则下可见部分；共享门使用本地端点。 |
| `contact_cell(...)` | 可见且现有移动边可接触的部分中选最近格，稳定按距离与格 ID 排序；排除目标自身阻挡，保留其他角色、物体、墙线与角落限制。 |
| `movement_cells(...)` | 原移动专用投影；共享门继续由 WorldHandler 门槛逻辑处理。 |

公开 WOS 查询返回 orientation、occupied_cells、movement_blocking_cells、
difficult_terrain_cells；原 vision_occlusion、cover_candidates 从 unsupported 标记
演进为实际整格贡献元组。它们是对象侧输出，不是已结算的综合视觉或 AC 结果。

普通地面物体任一部分可见即可被查询，单对象与场景列表共用同一判断。
继续使用原灯光投影与 GridTopology.can_see，不绕过条件、感官或未知光照保护。
携带物仍限于自己的实例；容器内物体沿祖先链检查内容访问，并用地面外壳完整几何检查
接触。共享门的可操作侧、standing cell、门槛占用与权限要求保持。
已有同格手动操作特例保留，例如角色所在格的灯具开关。

Openable 开关、ForceOpen 后果、Mechanism 顺序联动、内部 Durability 应用复用候选计算
及原最终 audit。各入口自己的资格、锁／闩／卡住、工具、访问性与结果采用检查保留。
失败回滚空间事实、状态、事件、版本及联动采用记录；原 command receipt 可记录拒绝，
不代表部分游戏事实已提交。

## 兼容与迁移

- `/1`–`/7` 继续加载；旧 spatial 定义方向只有 north，保持原 footprint，不自动赋予旋转许可。
  旧离散 footprint 仍按原偏移读取，`/8` 新定义要求正交连通。
- Schema33 仅增加带 CHECK 的 `objects.orientation TEXT NOT NULL DEFAULT 'north'`。
  旧行／旧列、HP、开关、位置、事件证据、RNG、版本与回执不重写；没有占地表或历史朝向事件。
- Package digest 仍基于原始数据，解析默认值不会修改原 Package。
  新字段只能在 `/8` 使用；旧 Schema 明确拒绝。恢复仍校验 Session Package 绑定。
- RepositionObject 新可选字段为 None 时不进入指纹，原四字段命令历史重放不变。
  朝向有值时进入指纹，沿用原幂等冲突检查。
- 原 main 的真实 Schema32 非空存档测试验证所有旧列保持及原回执重放；
  migration33 中断测试验证整次迁移回滚和可重试；旋转后 Session 在新解释器中恢复。

## 验证与后续接入

专项文件为 `tests/test_object_spatial_service.py`，保留原 World spatial、Openable、Container、
Mechanism、Durability、Light 与导航测试。运行仓库 Ruff check / format，迁移及共享运行时
接口变更扩大到全套测试。本次最终结果：

- 全套收集 **1,454** 项，按互不重复的测试文件分为四个原生执行分区：
  **307 / 394 / 368 / 385 passed**，全部 exit 0，包含 **34** 项新增 OSS 专项测试。
  使用 `.venv/Scripts/python.exe -m pytest -q <分区文件> --basetemp=.test-tmp/oss-suite-N`；
  Windows 异步 Engine 的本地 socketpair 在原生权限下执行。
- 朝向命令复用同一 command_id、不同方向的幂等冲突断言另行复验：1 passed。
- `ruff check .`、`ruff format --check .`、`git diff --check` 全部通过。
  Ruff 同时以 `uv run --locked --no-sync --offline` 复核，使用工作区可写缓存。
- `examples/world_station.py` 通过，包含新进程恢复和回执重放；最终
  `scene.workshop` / `1,0` / world version 32 / event seq 49。
  示例的 world version 与存储 Schema33 是不同概念。
- 实际安装的 Engine / SRD 均为 0.6.0，Git SHA 与 main 的固定依赖
  `d596161060555630019163ab301e1d25fd816549` 一致；未修改依赖或 lockfile。
- 测试仅有既有 lark `sre_parse` / `sre_constants` 弃用警告。

仍 OPEN / NOT IMPLEMENTED：

- 地形系统对多对象及静态地形的合并、困难地形移动消耗、对象高度模型。
- 综合动态 LoS、照明遮蔽、感官／条件、Perception / Evidence 的完整物体几何接入。
  当前 WOS 部分可见修复只在原受支持的静态视觉与灯光投影内成立。
- Combat / RE 动态几何接线；动态障碍／光源保护保留，新增非阻挡困难地形、视觉与掩护
  贡献也会阻止未接入的 Combat 初始化。
- 掩护 AC／豁免、攻击优势劣势、玩家物体攻击／修复、力量、推动搬运与旋转动作成本。
- NPC Evaluation 新命令意图转换；场景、环境与时间服务的后续独立演进。
