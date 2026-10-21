# ObjectSpatialService 实施与边界（2026-10-18）

## 2026-10-22 事实读取与共同发布

空间／开合／耐久及地图输入已接入一致 RAM／候选；Schema40 占格与验收触发器不变，其实际
行变化也参与提交后内存索引维护。原局部准入、回滚、旧 View 和授权边界保持；完整地图消费者
的算法成本另计。统一验证见 [实施报告](reviews/SESSION_MEMORY_RUNTIME_IMPLEMENTATION.md)。

2026-10-20：运行几何／政策和环境依赖身份已改取 Session 实例配置；当前位置和朝向仍由 objects 独占。
未实例化场景的预测取保存的 Session 计划，完成创建后清除；不写读查询。
详见 [实例配置与验证](INSTANCE_CONFIGURATION_PROGRESS.md)。

## 环境权威版本管理接入（2026-10-19）

本分支在已推送 `382a4ae` 上继续实现 Schema36：Environment 按场景防护输入版本缓存
OSS 中间投影，来源／防护两版命中时不扫描对象输入。正式当前属性提供方通过
VersionedProtectionProvider 登记对象／组件及额外 actor／容器／时间依赖；同步 SQL
触发器在实际修改事务中推进版本。雨强不使防护版变化，Wind 输入使两版同步变化。
未登记第三方路径不跨查询复用。几何算法、地面模型、WindBoundary、公开权限与
未知完整性保持。完整依赖、迁移、回滚／旧事务发布、计数和验证见
[本批 Environment 记录](ENVIRONMENT_SERVICE_PROGRESS.md)。

## Environment Snapshot 消费与降水扩展（2026-10-19）

`feat/environment-derived-snapshot` 基于最新 `origin/main@d0fda62`；本轮未合并 main。
新增显式 `PrecipitationProtection`，复用既有位置／旋转／覆盖／边界裁剪算法；旧 rain
字段只声明雨。OSS 增加本次查询不可变输入准备，由 Environment 缓存已经计算的场景
防护结果（32 项 LRU，按风向），没有新增防护事实镜像或持久表。
开关／损毁等政策仍由当前属性提供方负责；WindBoundary 的自身格排除及高度规则保持。
缓存依赖、事务发布、最终环境层、区域可见性复用、完整验证与限制见
[本轮 Environment 实施记录](ENVIRONMENT_SERVICE_PROGRESS.md)。Light 跨查询 Snapshot
保留后续 Light 重构范围；Environment Snapshot 本轮已实现，不新增为延期待办。

以下保留此前 OSS 批次的历史范围和证据。

基线 main：`a897ae5`；实施分支：`feat/object-spatial-service`，不合并 main。
新增 Package `state-machine-foundation/8`；当前 SQLite Schema **33**。
本文描述实际实现，World、Openable、Container、WorldTime 历史记录继续保留。

> 上述基线与分支是此前占地专项的历史记录，该专项已合并为 main@97c5af1。
> 当前总体说明见 [OBJECT_SPATIAL_SERVICE.md](OBJECT_SPATIAL_SERVICE.md)；
> 本轮挡雨／挡风扩展另记于文末，不将原验证数字作为本轮测试结果。

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

### 后续 Environment 消费接入（2026-10-18）

`feat/scene-environment-service` 基于已合并 `main@9ce60cb` 实现 EnvironmentService，
实际消费上述 OSS 防护几何，组合场景静态防护、当前风来向和显式高度区间。
前述矩阵“Environment 联调未完成”是 OSS 批次当时状态；现已完成这一服务消费链。
真实 WO／Package 属性、角色实际高度及机械消费者仍按原职责接入，测试 provider 不代表
生产接通。详见 [EnvironmentService](ENVIRONMENT_SERVICE.md) 和
[本次验证记录](ENVIRONMENT_SERVICE_PROGRESS.md)。W-02、MECH-01 状态不变。

仍 OPEN / NOT IMPLEMENTED：

- 地形系统对多对象及静态地形的合并、困难地形移动消耗、对象高度模型。
- 综合动态 LoS、照明遮蔽、感官／条件、Perception / Evidence 的完整物体几何接入。
  当前 WOS 部分可见修复只在原受支持的静态视觉与灯光投影内成立。
- Combat / RE 动态几何接线；动态障碍／光源保护保留，新增非阻挡困难地形、视觉与掩护
  贡献也会阻止未接入的 Combat 初始化。
- 掩护 AC／豁免、攻击优势劣势、玩家物体攻击／修复、力量、推动搬运与旋转动作成本。
- NPC Evaluation 新命令意图转换；场景、环境与时间服务的后续独立演进。

## 挡雨／挡风扩展（2026-10-18）

本轮分支 `feat/oss-environment-shelter`，基于执行时 fetch 的最新
`origin/main@97c5af102bd8ca2c397b84bd4d36664983db54a7`；初始工作区干净。
仓库及上级目录未找到适用 AGENTS.md。读取顶层最新架构／API 契约及本仓库现有
World / OSS 支持说明，保留上述占地专项的历史身份。本轮提交／推送当前分支，不合并 main。

- 保留既有占地、旋转、候选检查、接触、移动／困难地形／视觉／掩护贡献。
  占地和防护使用同一个局部旋转变换；旧投影结构不变，新增配套不可变防护接口。
- 新增强类型 `CurrentProtectionProperties` / `CurrentProtectionProvider` 接口。
  显式局部挡雨 Cell 独立于实体 footprint；挡风采用显式局部背风面、局部适用风向、
  有界 N 格和高度区间，变换后按场景边界裁剪。多来源／分离高度段保持，不填补空隙。
- 支持单物体、场景贡献和显式候选；查询不写库、不产事件、不改变版本／RNG。
  缺高度不默认零；未配置、明确无贡献、缺失输入和不支持分别报告。
- 不推测开门、HP=0、视觉、移动或战斗掩护的环境含义。
  共享门防护缺少支持的双侧几何时明确不支持；携带／容器内对象不自动产生地面防护。
- 新建总体文档并同步 README；没有改动 Schema、Package Loader、固定 RE 依赖或命令语义。

交付分别为：

| 项目 | 本轮实际状态 |
|---|---|
| 几何计算 | 完成所述地面垂直雨、四来向整格等高挡风模型 |
| 当前属性读取接口 | 强类型只读输入／provider 接口就绪，显式输入和测试 provider 已验证 |
| 真实 WO／Package 接入 | 未接入；WO-01、PKG-01、SESSION-01 按原职责提供／装配当前属性 |
| Environment 联调 | 未完成；输出已提供来源和场景高度段，消费者需结合地面／单位高度 |

W-02、MECH-01 保持独立跟踪；未将本轮 OSS／Environment 工作新增为延期待办。
原 Combat、权限、Package 绑定、事务和 Turn／Round 认知聚合边界保持。

本轮实际验证命令（Windows / pwsh，仓库 cwd）：

```powershell
# 专项及原空间回归
.venv/Scripts/python.exe -m pytest -q tests/test_object_protection.py tests/test_object_spatial_service.py tests/test_world_spatial.py --basetemp=.test-tmp/oss-protection-regression

# 全套：四个进程分别将 $ossPartition 设为 0、1、2、3，临时目录独立。
$ossPartition = 0
$ossFiles = @(Get-ChildItem tests -Filter 'test_*.py' | Sort-Object Name)
$ossSubset = for ($ossIndex = 0; $ossIndex -lt $ossFiles.Count; $ossIndex++) {
    if ($ossIndex % 4 -eq $ossPartition) { $ossFiles[$ossIndex].FullName }
}
.venv/Scripts/python.exe -m pytest -q @ossSubset -o faulthandler_timeout=60 --basetemp=.test-tmp/oss-protection-native-$ossPartition

.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
git diff --check
.venv/Scripts/python.exe examples/world_station.py
```

全套 collection 的文件集合与上述分区集合已比对，69 个文件无重复／遗漏，共 1,504 项。
受限单进程运行在早期异步测试处停滞；已核对进程命令行归属后停止，不计为通过。
原生单进程运行后为减少耗时改用上述互斥文件分区，未完成的单进程运行也不计为通过。
全套分区使用原生权限提供 Engine 异步事件循环所需本地 socketpair，未调用外部服务。

最终实际结果（均为本轮执行，不沿用上批数字）：

- 专项及原空间回归：**87 passed**，exit 0；其中本轮防护专项 50 项。
- 原生全套分区 0／1／2／3：**409 / 394 / 410 / 291 passed**，全部 exit 0，
  合计 **1,504 passed**；各分区耗时 168.27 / 188.93 / 242.61 / 223.01 秒。
  对 PowerShell 实际排序后的文件集合再次核对，各分区收集数与通过数一致。
- `ruff check .`、`ruff format --check .`、`git diff --check` 均通过；
  README／总体说明／专项记录的本地 Markdown 链接目标检查通过。
- World 综合示例 exit 0，实际新进程恢复及回执重放通过；最终
  `scene.workshop` / `1,0` / world version 32 / event seq 49。
- 实际安装的 Engine / SRD 都为 0.6.0，direct_url 的 commit_id 均为
  `d596161060555630019163ab301e1d25fd816549`；pyproject.toml 与 uv.lock 无改动。
- 测试仅有既有 lark `sre_parse` / `sre_constants` 弃用警告。


## Mechanism follow-up — 2026-10-18

The historical statements above retain their original scope. MECH-01 is now
verified complete on `feat/world-mechanism-service`: local typed candidate maps
advance in action order, Openable retains each qualification check, and OSS checks
the given complete candidate scene. Both legal free-then-occupy and real-conflict
cases are tested through Session query/submission without query writes. The bounded
repeatable/lifecycle/automatic extension and its OPEN dependencies are documented
in [MechanismService contract](MECHANISM_SERVICE.md) and
[progress](MECHANISM_SERVICE_PROGRESS.md). W-02, SA-01 and other component work
are not closed. No main merge is implied.

## GridService 消费与后续接入（2026-10-18）

`feat/world-grid-service` 在 main@0554add 上消费现有 OSS 的占地、移动阻挡、困难地形、
视觉与掩护贡献，未重算物体 footprint／朝向。对象／Light 仅依赖 Grid 底层只读输入，
不会回调组合查询。当前空间组合、通行几何和来源分离已在 SM 本地完成；对象属性到正式
机械后果仍 OPEN。准确范围见 [GridService](GRID_SERVICE.md)。

| 既有未完成项 | 依赖／当前限制 | 后续修改位置、触发条件与验收 |
|---|---|---|
| 综合动态 LoS | 固定 RE 无独立动态视觉阻挡输入；Grid 发现贡献时返回 unsupported_grid_vision，原静态／Light 支持范围不扩大 | RE 公布稳定通道契约并固定版本后，改 Grid 几何组合及 Light／授权视觉消费者；用真实门开合、非阻挡遮挡物、移动专用障碍和回滚证明视线、掩护、移动各自正确且隐藏实体不泄露 |
| 困难地形与掩护机械消费 | Grid 可读真实贡献及几何掩护，但未结算费用、AC 或豁免；探索不引入战斗额度 | 稳定 RE／Combat 空间契约后，改 RulesAdapter 输入、Combat guard 与所属领域采用路径；真实战斗验证费用／AC／豁免及 OCC／幂等／整体回滚，不能用 Grid 查询或 mock 替代 |
| Effect 空间贡献 | Effects 当前来源／生命周期契约尚未完成；Grid 没有新增占位协议，也不复制效果范围／期限 | Effects／RE 新契约固定后，按所属模块接入 Grid 组合、RulesAdapter 和 Light；真实效果创建、移动／锚定、到期及失败回滚后查询与机械输入一致，无第二份权威 |

这些是已有 OSS 集成缺口的细化，不关闭 W-02 或 SA-01；未执行的新契约端到端测试
没有通过记录。现有可运行集成继续回归。


## 2026-10-20 Scoped runtime validation refactor

本批实现采用 Schema40 的事务内派生占格索引和结构验收记录，普通命令由领域
局部准入保护，操作内复用读取，提交不再默认完整空间审查或库存重建。
逐项能力、读取范围、存档兼容、实际计数及本批验证结果见
[系统性重构验收](reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md)。
上文历史测试数字与旧 Schema/待办保持原批次语境，不作为本轮验证结果。
