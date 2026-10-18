# EnvironmentService

Environment 是 Scene 的独立逻辑组件。`sm.environment` 与
`WorldHandler.environment` 为同一个实际服务；不依赖统一 SceneService 门面、
SceneLifecycle、Grid 或 Terrain 重构。本文描述本仓库实际接口，非跨模块冻结 ABI。

## 所有权与真实提交路径

当前环境事实由 WorldFacts 的 `environment` / `set_environment` 读取和受控写入，
SQLite Schema 34 的 `scene_environments(session_id, scene_id, facts_json)` 为唯一存储。
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

Schema 33→34 只添加空表，不修改旧行、Package digest、WorldTime 绑定或抽象 tick。
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
`SceneEnvironmentProtection(rain_cells=..., wind=...)`。None 表示未配置，空 tuple 表示
明确没有该类静态贡献。`StaticWindProtection(cell, wind_from, heights)` 不从墙体或
视觉阻挡猜测；每个方向与高度段分别保存。

动态物体防护实际调用现有 OSS `scene_protection_contributions`，其内部使用
`project_protection`；读取当前对象姿态和 `CurrentProtectionProvider`，不复制物体几何。
保留 static／object 来源和所有原始高度段，不填平多个来源间的空隙。
静态或任一 OSS 来源证明有覆盖即可证明该点有保护；没有覆盖且任一提供方未配置、
缺输入或不支持时，暴露为 unknown，不能据空投影证明毫无防护。
共享门等 OSS 原有不支持边界保持；不会从其 footprint 造防护。

`precipitation` 表示当地大气条件，`ground_precipitation` 单独表示地面 Cell 的垂直雨暴露。
已知雨在屋檐下仍是 atmospheric rain，地面实际雨暴露可为 none。
这个挡雨模型仅作用于 `kind=rain` 的室外来源，不将降雪／冰雹／斜雨／飞行高度推断为
已遮蔽。显式 height_ft 用于风和有高度的环境来源；ground_precipitation 始终是地面模型，
不是指定高度上的雨线判断。有垂直范围的降水源缺少明确地面标高时保持未知。

`wind` 保留实际来向和速度，`wind_exposure` 用该来向的 Cell 防护区间检查指定高度。
完全覆盖的高度点可报告无当地室外风暴露；高度范围返回每个覆盖／未覆盖区段，
不对“半身挡风”规定机械收益。风未知时不选择一个假定方向判挡风；仅为独立雨投影
使用 OSS 必需的占位方向，绝不作为实际风事实。

`exposure=outdoor` 才接受室外雨／风防护。`local` 来源直接在当地形成，保留有效条件，
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
公开结果不返回任何来源 ID、效果名称或防护几何来源。隐蔽来源影响的字段直接 unknown；
其余可观察字段保留数值。高度范围若涉及未获准披露的来源直接拒绝，避免从分段端点
泄漏隐蔽效果范围。`observable=True` 必须由可信配置／Effects 明确证明，非 LLM 自报。
这些查询不产生感知事实、NPC 记忆或可见性机械收益。

公开查询每次在一个只读事务内读取事实／当前提供方／OSS；不建库表、不初始化场景、
不发事件、不推进版本或 RNG。数据库生命周期迁移只发生在 SqliteStore 打开阶段。

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

Python 的 BatchEnvironment 直接包含强类型 DTO；wire 标签不能用 `asdict(batch)` 自动恢复。
未知字段／嵌套批量／权限字段拒绝。现有命令指纹算法与旧回执兼容，不修改旧操作编码。

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
| 持久化／重访／新进程恢复／Session 隔离 | 完成，真实 Session 和 Schema33 历史存档验证 |
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
不决定累计暴露最终所有者；不关闭 W-02、MECH-01 或其他独立事项。
本轮 Environment 功能已实现，不另新增 Environment 延期待办；外围组件按既有归属接入。
实际验证命令与结果见 [实施记录](ENVIRONMENT_SERVICE_PROGRESS.md)。
