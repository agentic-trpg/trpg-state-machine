# ObjectSpatialService 总体说明

本页统一描述 OSS 当前实现。2026-10-18 挡雨／挡风扩展基于最新
`origin/main@97c5af102bd8ca2c397b84bd4d36664983db54a7`，位于
`feat/oss-environment-shelter`，不合并 main。此前专项已经合并；
[实施与验证历史](OBJECT_SPATIAL_SERVICE_PROGRESS.md) 保留原批次身份与测试记录。
架构依据是顶层仓库 `docs/MODULE_CONTRACTS.md` 的 World / Scene 责任与环境边界，
以及 `docs/STATE_MACHINE_ARCHITECTURE.md`；本文说明代码支持，不能反向将目标设计标为已实现。

后续集成更新（2026-10-18）：`main@9ce60cb` 的防护接口已由
[EnvironmentService](ENVIRONMENT_SERVICE.md) 实际消费，组合静态防护、当前天气来向和
Cell／高度条件。WO／Package 生产当前属性及实际角色高度仍需所属组件接入；
Environment 的契约 fixture 不代表生产 WO 动态防护已完成。

## 职责与数据来源

WorldObjectService（WOS）组合 OSS、Openable、Container、Mechanism、Light，继续承接
物体公共查询和 TypedCommand 业务分派。OSS 负责占地、朝向变换、候选检查、接触几何与
对象侧空间贡献。WorldHandler 仍组织场景、移动、共享通路和事务。

| 数据 | 权威／读取方式 |
|---|---|
| 物体位置和朝向 | WorldFacts 的 `objects.scene_id/cell/orientation`；未初始化场景只读 Package 初始事实，已有实例不回退模板 |
| 原空间能力 | Package spatial 配置；开合／耐久策略沿既有明确的属性选择规则读取 |
| 挡雨／挡风当前属性 | WO 侧提供 `CurrentProtectionProperties`，通过显式参数或 `CurrentProtectionProvider` 只读取得 |
| 物体基准高度 | 当前属性的 `base_height_ft`，相对场景统一高度基准，不能从 Cell、地面或 HP 猜测 |
| 天气／风向 | 调用方传入 `wind_from`；OSS 不保存或生成天气 |
| 地面高度、单位实际高度与有效环境 | Environment 已实现 Cell／显式高度组合，实际单位高度提供方尚未生产接通；OSS 不裁决暴露、部分防护后果或环境规则 |

WO-01 负责开合、展开、损坏等变化如何变成当前防护属性。本轮建立可供真实 WO 接入的
强类型只读契约和计算接口，**未接入生产 WO 动态防护**。PKG-01 负责 Package 配置解析，
SESSION-01 负责创建／恢复装配，本轮未全面实现这些配套任务。
默认 WOS 仍构造无 provider 的 OSS，旧物体不会自动获得防护；测试 provider 只是接口验证。

OSS 不保存第二份物体状态、占地或防护格表；查询不写库、不发事件、不推进版本／RNG。
风、雨、实体占地、移动阻挡、视觉遮挡与战斗掩护分别表达，不能互相推导。

## 已有占地、旋转和业务支持

坐标 `x,y`：x 向东、y 向南。north 为定义原始局部坐标；以物体锚点顺时针旋转，
不自动重新居中。占地与防护复用同一 `transform_local_cells` 变换。

| 朝向 | 局部 `(dx,dy)` 变换 |
|---|---|
| north | `(dx,dy)` |
| east | `(-dy,dx)` |
| south | `(-dx,-dy)` |
| west | `(dy,-dx)` |

Package `/8` 实例支持 `initial_orientation`（默认 north），spatial 能力通过
`allowed_orientations` 授予旋转许可。新 footprint 为包含原点、正交连通的整格形状，
最多 64 格、局部坐标绝对值最多 32。旧 `/1`–`/7` 保持原有加载和占地语义。
不支持任意角度、多边形、部分格或随开关变化的实体形状。

`RepositionObject` 继续使用 GM 授权、位置／方向前置条件、world version、Package
绑定和命令幂等检查；支持原地旋转和移动并旋转。不变的实际姿态为空操作，
对称形状旋转仍改变方向事实。跨场景摆放和共享门摆放仍不支持。

候选与最终 audit 共用检查：场景边界、静态不可走格、刚性形状内部墙线、通道保留格、
角色和其他移动阻挡物。普通刚性物体仅支持网格角点上的轴对齐墙线；斜墙／非整数墙线
显式不支持。非阻挡物可重叠，不将所有占地一律当作障碍。

普通物体任一部分在原静态视觉／灯光支持内可见即可查询；接触选择最近的可见、可达
合法部分，稳定按距离和 Cell ID 排序。只排除目标自身阻挡，仍检查其他物体、角色、
墙线和角落。容器访问／开锁工具／门的 standing cell 等资格由原业务组件检查，
OSS 接触几何不授予权限。已有同格手动交互特例保留。

既有 `ObjectSpatialProjection` 分别提供 occupied、movement_blocking、difficult_terrain、
vision_occlusion 和 `(cell, cover_degree)`。空间性质按 open／closed／destroyed 的显式
配置选择；`disable_spatial_policy` 与 `disable_preserve_obstruction` 保留原耐久语义。
这些旧策略**不用于环境防护**。

## 挡雨：垂直降雨的地面整格覆盖

`RainProtection(local_cells=...)` 是显式局部挡雨格，可不同于 footprint。以当前物体锚点和
朝向转成场景 Cell；重复格去重，超出场景边界的格丢弃。每条 `RainContribution` 带
`object_id` 和 `cell`，表示对象侧垂直雨防护贡献。

不根据风向偏移雨线，不根据挡风高度生成屋檐，不根据视觉／移动／掩护推测遮雨。
当前只承诺地面 Cell 级垂直降雨；不承诺多层空间、飞行单位、斜雨或屋顶穿透模型。

## 挡风：显式背风边界与等高延伸

`wind_from` 统一表示风的**来向**，在场景坐标下解释：

| 来向 | 下风方向 |
|---|---|
| north | `+y` |
| east | `-x` |
| south | `-y` |
| west | `+x` |

`WindProtection` 包含若干 `WindBoundary`。每条边界声明：

- `local_cell`：该条横风向整格带、该高度区间内，物体厚度的**最后一个实体整格**。
  边界是这个格的下风侧网格边。此值由 WO 明确提供，不从 footprint 自动找边。
- `wind_from`：在物体 north 原始局部坐标下适用的来向。随物体方向一起旋转为场景来向；
  仅选择与查询来向相符的边界。需要四面挡风时由提供方分别列出四向边界。
- `downwind_cells=N`：显式整数距离，没有全局默认值。
- `lower_height_ft` / `upper_height_ft`：相对物体基准的局部高度区间。

对于变换后的边界格 `(x,y)`，下风单位向量为 `(vx,vy)`，防护格恰好为
`(x+k*vx,y+k*vy)`，`k=1..N`（两端均包含）。边界格本身不在背风贡献中；
不会把物体厚度算进 N，也不会对部分覆盖模糊取整。例如两格厚的墙，局部实体为
`(0,0),(0,1)`，north 风的边界必须明确为 `(0,1)`；N=2 时只输出 `(0,2),(0,3)`。
多条边界应分别给出每个横风整格带和高度段的实际背风面，不能用上风格代替厚度末端。

超出场景矩形的延伸停止；边界格自身不在场景内时不从场景外投射进来。
裁剪依据是场景范围 `0 <= x < width, 0 <= y < height`，不是可走性；
静态不可走格、墙线、其他物体不自动截断／生成防护。这是独立贡献计算，不是空气传播模拟。
每条边界覆盖横风方向恰好一格宽，不做对角扩散、绕流或距离衰减。

每条 `WindContribution` 都携带 `object_id`、`cell`、场景基准下的
`lower_height_ft` 与 `upper_height_ft`：

```text
scene_lower = base_height_ft + local_lower_height_ft
scene_upper = base_height_ft + local_upper_height_ft
```

高度统一为英尺，允许负的场景高度，必须有限且下界小于上界。区间采用 **[lower, upper)**。
所有背风 N 格保留同一高度范围，**等高延伸是项目简化规则**，不代表物理气流模型。
适用边界缺少基准高度或任一局部高度时，整份该对象挡风输出为 `missing_input`，
不返回看似完整的部分结果，不默认零；挡雨仍独立可用。

同一对象的完全相同 `(cell, lower, upper)` 去重；同格不同高度段、不同物体来源分别保留。
不取最高上界拼成一个连续区间。例如同格 `[0,2)` 与 `[4,6)`，高度 3 无覆盖。
`wind_at(cell,height_ft)` 返回覆盖该**场景高度点**的原始贡献，帮助消费者检索几何；
它不是单位全身防护判断。Environment 已提供显式高度点和保留空隙的高度区间查询；
实际单位高度须由角色／位置提供方供给，缺失时明确报告。本轮不规定部分防护的规则后果。

计算限制：雨最多 64 个局部格；风最多 64 条边界；局部坐标为整数且绝对值不超过 32；
N 为 1–256 的整数（bool／浮点数不接受）；单物体全部边界的 N 之和最多 4096，
按去重／裁剪**之前**计预算。输入使用 frozen dataclass 和嵌套 tuple，拒绝可变或未类型化输入。

## 接口、状态与示例

原 `project` / `scene_contributions` 的结构和消费者保持兼容；新增配套接口：

| 接口 | 用途 |
|---|---|
| `state` / `cells` | 当前权威姿态读取／完整局部占地变换 |
| `project` / `scene_contributions` | 原不可变对象几何投影／场景对象映射，包含既有五类独立贡献 |
| `check_candidate` / `audit` | 摆放候选准入与最终空间一致性检查 |
| `target_cells` / `visible_cells` / `contact_cell` | 原定位、可见部分、合法接触几何 |
| `movement_cells` | 原移动专用贡献；门仍走 WorldHandler 门槛逻辑 |
| `project_protection(..., wind_from, candidate=None, properties=None)` | 单对象当前或显式候选姿态的只读防护计算；不会执行候选摆放准入或提交 |
| `scene_protection_contributions(..., wind_from, properties=None)` | 不可变 SceneProtectionProjection；objects 为按 object_id 排序的不可变对象投影元组；properties 可按 ID 显式提供当前属性 |
| `ObjectProtectionProjection.wind_at` / `SceneProtectionProjection.wind_at` | 指定高度点的贡献检索，保留来源与原高度段 |
| `ObjectSpatialService(handler, protection_provider=...)` | 显式只读提供方接入；无提供方且无显式属性时为未配置 |

两类防护各有独立 status，不能仅按空 tuple 判断原因：

| status | 含义 |
|---|---|
| `ready` | 当前有场景内贡献 |
| `none` | 属性已配置，但显式空几何、当前风向不适用或边界裁剪后无贡献 |
| `unconfigured` | 没有 provider／返回 None，或该类属性字段为 None；不能断言物理上不挡雨／风 |
| `missing_input` | 适用的挡风边界缺少必要高度，reason 为 `wind_height_required` |
| `unsupported` | 共享门、携带／容器内姿态、缺少地面姿态或网格；reason 说明具体边界 |

无效参数（例如非法 N、非有限高度、可变输入、非四向风）明确报错；不静默修正配置。
共享门的旧占地／阻挡仍使用各场景端点，但其防护返回
`shared_door_protection_geometry_unsupported`，即使显式给出单锚点属性也不复制到两侧。
携带物与容器内物体缺少姿态／防护策略时返回 `nonground_protection_pose_unsupported`，
且不会自动出现在地面场景防护来源中。

```python
from trpg_state_machine.object_protection import (
    CurrentProtectionProperties,
    RainProtection,
    WindBoundary,
    WindProtection,
)

# 显式输入示例，不是 Package Loader 已支持的新字段。
current = CurrentProtectionProperties(
    rain=RainProtection(((0, 0), (1, 0))),
    wind=WindProtection((WindBoundary((0, 0), "north", 3, 0, 6),)),
    base_height_ft=10,
)
projection = oss.project_protection(
    sid,
    oid,
    package,
    wind_from="north",
    properties=current,
)
# 锚点 3,3 / north 时：挡雨 3,3 和 4,3；挡风 3,4 / 3,5 / 3,6，高度 [10,16)。
point_sources = projection.wind_at("3,4", 12)
```

这些是可信组件组合接口，与原 OSS 接口一样，不是公开的无权限对象目录。
调用方必须先验证 Session／Package 绑定和访问权限，并在一致的读事务中组合物体姿态、
当前属性及网格；玩家视图继续走已有 WOS / StateMachine 查询。
显式 properties 优先于 provider。provider 的 `current_protection(sid, oid, package,
*, candidate)` 在实际 OSS 投影调用时读取；candidate 是完整待计算姿态。
OSS 直接接口自身不缓存，Environment 的版本命中会跳过整个 OSS 调用。
提供方必须只读、无外部 I/O，能返回该候选的明确当前属性；不能支持时返回 None，
OSS 不为它推测开合／损坏后果。构造依赖的生产装配与恢复由 SESSION-01 对接。

## 兼容与支持边界

Schema 仍为 33，Package 仍为 `/8`，RE／SRD 固定 SHA
`d596161060555630019163ab301e1d25fd816549` 不变。本轮没有新持久化、数据库迁移、
Package 字段或命令。原 Package digest、旧摆放命令指纹、授权、版本、幂等、事务、回执、
恢复与 NPC handoff 语义保持。原 Schema33 朝向迁移历史见专项记录。

几何计算与当前属性接口已实现；Environment 已接入真实几何接口，真实 WO／Package
动态属性读取尚未接通，测试 provider 不代表生产动态防护已交付。本页记录这些配套接入点，不新增
OSS／Environment 延期待办。W-02 与 MECH-01 保持独立跟踪，不因本扩展标记完成。

原 Combat 支持边界不变：动态移动障碍、灯光以及未接入的困难地形／视觉／掩护贡献
继续拒绝不受支持的 Combat 初始化；本轮防护不是 RE Combat 几何接线或机械环境结算。
不改变 Turn／Round Episode、认知聚合、Recent OB／Gate／EVA。
综合动态 LoS、地形移动成本、Perception／Evidence、掩护 AC／豁免、物体攻击／修复及
摆放动作成本仍按既有边界处理，不能由对象侧贡献宣称完成。

## 本轮验证

### Environment Snapshot／显式降水保护接入（2026-10-19）

当前实现新增 `PrecipitationProtection(local_cells, kinds)` 和
`CurrentProtectionProperties.precipitation`，kinds 必须显式选择 rain/snow/sleet/hail。
旧 `RainProtection` 及其几何只声明 rain；没有把旧屋顶默认扩展为所有降水。
新几何使用同一个 `transform_local_cells`、权威锚点、朝向和场景裁剪，产生
`PrecipitationContribution(object_id,cell,kind)`；旧 rain_cells 是 rain 类型的兼容投影。
`precipitation_status(kind)` 保留 ready/none/unconfigured/unsupported 的完整性区别，
不能根据空 cells 推断没有保护。纯当前属性未配置时不补默认材质策略。

OSS 新增 `prepare_scene_protection`，读取当前对象集合（含共享门）、确切姿态、地面资格、
provider 的完整不可变当前属性与 Grid 配置；`SceneProtectionInputs` 仅是本次读取输入。
Environment 可以省略已经读取并证明降水／风几何均为空的来源；未配置或不支持来源保留，
防止未知完整性被解释为明确无保护。
`scene_protection_contributions(...,prepared=...)` 使用这份同事务输入，避免不同风向或
同一区域的 Cell 再次读出不同 provider 状态。prepared 与 properties 不能同时覆盖。
它仍是可信内部组合接口，不改变公开对象目录、Session pin 或授权责任。

Environment 持有容量 32 的场景防护 LRU；Schema36 版本管理批次改为按 Session／Package
固定身份／Scene／防护版本／请求风向复用全部 Cell 和高度段。Wind 输入变化在来源写事务
同步推进来源版和防护版；雨强独立变化不推进防护版。命中完整结果不先读取 Wind，也不扫描
OSS 当前输入；中间缓存未命中才调用 prepare_scene_protection。正式
VersionedProtectionProvider 声明物体及额外 owner 输入，登记的同步触发器覆盖实际姿态、
组件、归属和相关地图写入；actor／容器的额外依赖与 tick 边界必须明确声明。
OSS 本身不存权威版本、可变防护事实或第二个缓存。旧事务结果提交后已过时则不发布，写
候选不进入共享 LRU；旧版条目按当前身份剔除并受容量上限约束。无版本／未登记的第三方
路径一律不跨查询复用，不以内容扫描维持共享缓存。原只读 provider 接口继续可用。
完整依赖矩阵、最终环境层和故障处理见 [EnvironmentService](ENVIRONMENT_SERVICE.md)。

WindBoundary 的最后实体格语义、k=1..N、旋转风向、距离、边界及 `[lower,upper)` 均保持；
墙所在 Cell 不获得自身背风贡献，但可以接受其他显式保护。二维 Grid 不新增多层高度表；
共享高度段供原有显式高度／单位参数判断，不代表所有角色得到相同最终影响。
未实现积雪、融雪、结冰、冰雹伤害、WO／Effects 的新生产适配器或 Light 跨查询 Snapshot。

测试位于 `tests/test_object_protection.py`，使用真实 Session 事实与公开移动／开门／
携带／容器命令，并在只读查询前后对比全部数据库表、版本／事件／回执及 total_changes。
候选四向测试同时使用 SQLite `query_only=ON`。覆盖来源、高度空隙、提供方变化、
显式输入优先、方向和规模校验、旧消费者以及未初始化场景无初始化副作用。
实际命令和最终结果见 [本轮实施记录](OBJECT_SPATIAL_SERVICE_PROGRESS.md#挡雨挡风扩展2026-10-18)。


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
