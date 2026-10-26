# Agentic TRPG — State Machine Foundation

## Source explanations and development workflows — 2026-10-27

[Source documentation maintenance](docs/SOURCE_DOCUMENTATION.md) is mandatory for
new/changed code. Repository workflows are installed under `.agents/skills/`;
[coverage and validation evidence](docs/reviews/CODE_DOCUMENTATION_AND_SKILLS.md)
separate explanation coverage from runtime acceptance.


## Session RAM runtime — 2026-10-22

现有生产入口已接入统一 RAM／候选读取、SQLite 增量提交后发布与暂停恢复；普通命令不再
为初始准入构造完整 SessionSnapshot。容器重量和机关索引跨命令复用；授权、原协议及恢复
限制保持。入口／故障／容量／实际测试和成本见
[实施报告](docs/reviews/SESSION_MEMORY_RUNTIME_IMPLEMENTATION.md)。下述 10 月 21 日目标段落保留原历史状态。

## Session memory runtime target — 2026-10-21

Architecture Decision Status **[DECIDED]**；完整 Session 内存运行层 Implementation Status **[NOT IMPLEMENTED]**；Acceptance Status **本次未执行运行验收**。 活跃已加载事实的查询、检查、RE 输入、角色视图、Snapshot 与环境／光照／重量输入统一读取同版本 Session RAM；候选与旧固定输入隔离。每次修改通过 SQLite 短事务增量持久化，COMMIT 后发布 RAM，版本就绪后正常返回成功及投影。统一协议与现有组件差距见 [Session Management](docs/SESSION_MANAGEMENT.md)。当前 main `d6be5a9` 为 Schema40，已有操作 memo／GridMaps／环境缓存与空间索引，完整内存运行层及跨命令重量／机关索引仍待实施。未来 5–6 玩家负载不扩大单人 MVP；活跃战斗精确恢复仍不支持。下述批次版本、数字和兼容边界保留原时点。

MechanismService 的 `mechanism/3` 配置和独立 `mechanism-effect/1` 端口支持强类型
规则效果定义、单体／区域目标筛选、原命令关联及有序事务采用验证。固定 RE /24
尚无真实机关来源的 `rules.effect`，生产明确返回 unsupported；接受结果仅通过契约
fixtures 验证。持续状态、零 HP／死亡、完整 Effects 生命周期和战斗采用仍 OPEN。
详见 [机关效果契约](docs/MECHANISM_EFFECTS_CONTRACT.md)。

Batch 1 实现了一个可运行的本地闭环：**验证原创合成 Adventure Package → 创建隔离 Session → 初始化/重访 Scene → 修改实际状态 → SQLite 持久化 → 新进程恢复**。

StateMachine 是统一 API 与组件装配入口；各领域 Facts / Records 管理所属权威事实，通过共享事务共同提交。Package 是不可变模板；返回的 Snapshot 是独立副本，修改副本不会写入数据库。已接入固定候选 SHA 的真实 D&D 基础攻击及受限胜利/TPK 关闭，同时保留隔离的合成测试路径；没有第二份运行态权威。正常关闭后可以保存并在新进程恢复；活跃战斗跨进程精确续玩仍不支持。

Session Management 已分离为 SessionCreation 创建协调、SessionAccess 授权和 SessionRuntime
执行元数据／公共提交协调。公开 create_session 等签名保留；当前存储为 Schema40（实例配置批次为 Schema39）；运行时 RE 校验、
求值和查询以持久化 Session 的三项绑定为准。已授权 NPC 移动也可延迟创建目标场景，
不制造 PC 进入事件。对象能力、Scene 配置、NPC senses 和辅助读取现已取持久实例数据；
旧库须显式调用 upgrade_instance_configs，恢复 getter 不补字段、不重置状态。
ContainerService 在一次读取／候选状态中共用邻接索引和子树重量，位置变更使旧重量失效。
RE 冻结素材已按 a011eba 整合提交接入：Runtime 一次选定，初始化／求值／查询／校验共用。
旧 /36 存档保留审计与显式实例升级，新 RE 不静默重绑；三项状态与计数证据见
[实例配置／容器／RE 核对记录](docs/INSTANCE_CONFIGURATION_PROGRESS.md)。
实际调用链、旧 Session 策略和字段缺口见 [Session 管理](docs/SESSION_MANAGEMENT.md)，
检查与交付见 [进度](docs/SESSION_MANAGEMENT_PROGRESS.md)。

RF-1 将现有世界交互和战斗编排改为显式组件组合，角色、世界、战斗和 Session
事实经受控写入口共享同一 SQLite 事务。RF-1 保留了公开 API、Schema 21、GC-1 门与效果来源
语义及固定 RE/SRD 依赖保持兼容；NPC/感知仍采用受控过渡，完整战斗日志解耦尚未
实现。字段责任与提交链见 [RF-1 设计](docs/RF1_DOMAIN_RUNTIME.md)，实际验证见
[RF-1 Review](docs/reviews/RF1_DOMAIN_RUNTIME_REVIEW.md)。

## 架构与运行契约核对（2026-10-20）

当前 main 源码、Full Audit／局部准入／查询分类和待重构调用链见
[Runtime validation scope review](docs/reviews/RUNTIME_VALIDATION_SCOPE_REVIEW.md)。
共同规范以 sibling `agentic-trpg/docs/MODULE_CONTRACTS.md` §2.3、§6.5–§6.6 为准，
现有接口／目标差异索引在 §1.6；开发入口为 [AGENTS.md](AGENTS.md)。
本次对齐只修改文档，静态证据不是本次运行测试或性能修复。

## SceneLifecycleService：场景生命周期提取

`sm.scene_lifecycle` 与 `WorldHandler.scenes` 引用同一组件，协调幂等初始化、
PC／NPC 进入与离开、PC 访问和声明式布尔 Flags。初始化不记录访问，只有 PC
实际进入才递增已有 `visited_count`；恢复与重访不重套 Package 默认值。
位置仍由 CharacterFacts 写入，访问／场景事实由 WorldFacts 写入，全部参与既有命令事务。
WorldHandler 保留移动合法性与其他世界业务。仅 W-01 的生命周期切片完成，W-01 整体仍未完成。
接口／调用链与兼容边界见 [生命周期设计](docs/SCENE_LIFECYCLE_SERVICE.md)，
验收与剩余职责见 [生命周期进度](docs/SCENE_LIFECYCLE_SERVICE_PROGRESS.md)。

## GridService：当前空间组合与几何查询

WorldHandler 的 GridService 统一网格读取、当前实体占格与 OSS 空间贡献、距离／邻接／范围
候选及基础通行几何，复用 RE 公开接口。占用、移动阻挡、困难地形、视觉遮挡和掩护保持
区分；查询使用一个读取事务或当前操作事务，没有位置副本。静态地图／基础拓扑已有 Store-local 有界复用；动态占用仍按当前事实组合，不能据此宣称局部 SQL 已实现。原探索、交互、导航、
Light／Environment 调用点已迁移，原地图授权与 Combat guard 保留。

角色目前只表示单锚点；动态视觉阻挡组合显式 unsupported，不能宣称完整动态 LoS 或战斗
几何已接通。探索不结算战斗移动额度。接口与限制见 [GridService](docs/GRID_SERVICE.md)，
验证和跨系统项见 [Grid 进度](docs/GRID_SERVICE_PROGRESS.md)；W-01 整体仍未完成。

## EnvironmentService：Scene 环境事实与有效查询

`sm.environment` 独立管理温度、四向来风／风速、降水及自然雾烟，SQLite Schema 36
保存 Session 隔离的基础／局部来源和静态防护。受控初始化与批量修改经 TypedCommand、
明确 `can_manage_environment` 权限、版本／幂等回执及共享事务执行；首次访问前的变化
不会被模板覆盖，查询不会初始化场景或写库。

场景持久化来源版／防护输入版，相关修改在权威事务内同步推进；两版命中时不扫描来源、
物体或环境地图。正式提供方登记数据库及时间依赖，未接入版本的第三方不跨查询复用。
共享防护投影与按需完整结果仍分两层有界缓存；未命中输入构建的 Runtime 长期复用留后续。

有效查询组合静态防护与真实 OSS 几何，区分场景下雨和地面 Cell 实际淋雨，按来向与
显式高度保留挡风覆盖及空隙。区域逐格返回；公开结果复用访问／可见性授权并隐藏来源。
Effects 只读贡献、WO 属性及角色实际高度依赖注入就绪，真实法术／WO 动态属性、
Package 默认加载和 Session 自动接线仍待对应组件接入；其他消费者的规则后果未联调。
完整模型、来源组合、接口、实例和支持边界见 [EnvironmentService](docs/ENVIRONMENT_SERVICE.md)，
实际验证见 [实施记录](docs/ENVIRONMENT_SERVICE_PROGRESS.md)。

## WorldTimeService：Session 固定时间协议

SQLite Schema 32 提供 `sm.world_time`（WorldHandler 实际装配的同一 Service）。
显式 `TimeBinding()` 使用 `world-time/1`：**1 tick = 1 游戏秒**；默认日长 86400 秒，
起始第 1 日 00:00，06:00 开始白天、18:00 开始夜晚。Package 可提供完整的可选
`time_binding` 配置；`CreateSessionRequest(time_binding=...)` 可整体覆盖并固定保存。
未指定配置的旧／兼容 Session 继续使用 `game-clock/1` 抽象 tick，绝不换算历史时间。

```python
from dataclasses import replace
from trpg_state_machine import TimeBinding

# request / package / context 是宿主已有的可信 Session 初始化输入。
result = sm.create_session(replace(request, time_binding=TimeBinding()), package, context=context)
time = sm.world_time.read(result.session_id, package, context=context)
# 推进仍使用 TypedCommand + world.advance_time + AdvanceGameTime(expected_tick, next_tick)。
```

支持秒／分钟／小时／自定义日长的精确换算、当前日与日内时刻、昼夜阶段和
`(start_tick, end_tick]` 有界分页边界查询。`game.time_advanced` 提供实际起止 tick；
授权、版本、幂等、事件证据、Outbox、回滚和战斗期间拒绝手动推进均沿用真实命令链。
旧 NPC schedule、OB 100 tick、Gate 20／60 tick 策略保留抽象语义；秒制 Session
当前拒绝配置型 NPC Runtime 和 NPC 观察访问，等待专门的秒制消费策略。
完整 Combat Round 通过真实 RE Delta 增加 6 tick，Turn 不独占六秒。
Session Runtime 已支持显式倍率、按业务边界推进的 TimeDriver、活动目标跳时和只读显示同步。
**没有默认实时倍率**；未配置倍率仍允许手动／活动／Combat 推进，但不能启动实时流逝。
唯一已提交时间、显示契约、版本／幂等、预算继续、重启与尚未接入项见
[Session 时间协调](docs/SESSION_TIME_COORDINATION.md)，测试证据与范围见
[实施记录](docs/SESSION_TIME_COORDINATION_PROGRESS.md)。
已实现、迁移策略、接口和后续 TIME 依赖见 [WorldTimeService 进度](docs/WORLD_TIME_SERVICE_PROGRESS.md)。

## World W1–W8：可组合非战斗世界能力

Package `/6` / SQLite Schema 31（当前存储版本 40）增加显式对象定义、内部耐久结果应用、独立卡住/门闩、
多锁、嵌套容器、受权 GM 多格布置、普通场景光源与一次性手动联动机关。
普通操作仍通过 `submit_command` / `submit_command_async` 的既有命令、版本与回执入口；
对象、角色位置、库存投影与事件证据在共享事务中提交。

运行原创贯穿示例（包含真正的新进程恢复与继续执行）：

```powershell
uv run --locked python examples/world_station.py
```

玩家取放/开锁/开合/光源/拉杆与 GM `RepositionObject` 分别报告。
当前 RE pin 没有可用的物体 HP 结算链，因此**玩家攻击物体与修复仍 unsupported**；
内部合成结果测试只验证耐久应用。战斗世界互动、撬锁、有限燃料、动态视觉遮挡/Cover、
自动称重压力板与新操作的 NPC Evaluation 意图转换均未宣称支持。
`get_object`、`list_scene_objects`、`get_illumination` 沿用受权范围；封闭容器内容被过滤。
容器取放仅校验移动子树及原／目标祖先，统一最多两个容器层级，普通物品不计层数；
内容访问复用 Openable 的只读接口。实际修复、必要数据与未完成事项见
[ContainerService 专项记录](docs/CONTAINER_SERVICE_PROGRESS.md)。
完整字段/Facts/Handler/存储映射、迁移、验证与限制见 [实施记录](docs/WORLD_W1_W8.md)，
最终审阅见 [World 审阅](docs/reviews/WORLD_W1_W8_REVIEW.md)。以下 GC 章节保留各批历史范围。

ObjectSpatialService 现支持 Package `/8` 的四向实例朝向、原地旋转及移动并旋转，复用获准
GM `RepositionObject` 命令。WorldFacts 保存锚点和方向，OSS 即时推导完整占地；候选检查
与最终 audit 共用墙线、静态障碍、占格与通道约束。普通物体查询使用任一可见部分，
交互与容器外壳使用可达接触格。对象侧分别输出移动阻挡、困难地形、视觉遮挡和掩护
整格贡献；综合动态视觉、地形成本及 Combat / Perception 接线仍未实现。
Schema33 保留旧行、旧 Package digest 和旧摆放回执指纹；详见
[ObjectSpatialService 实施与支持边界](docs/OBJECT_SPATIAL_SERVICE_PROGRESS.md)。

OSS 新增只读挡雨／挡风贡献：显式局部挡雨格、四来向背风 N 格等高延伸、逐来源高度区间，
与实体占地／移动阻挡分别表达。强类型当前属性输入和 provider 接口就绪；真实 WO／Package
接入尚未完成；Environment 已消费真实 OSS 几何接口，测试 provider 不代表生产动态防护已完成。
总体职责、接口、示例与支持范围见 [ObjectSpatialService 总体说明](docs/OBJECT_SPATIAL_SERVICE.md)，
本轮验证继续记入上述专项历史文档。

## GC-4：共享通路锁与实际持有凭据

Schema 25 / Package `/5` 为显式批准的共享门增加独立锁状态；
`world.unlock_door` / `world.lock_door` 要求实际 Actor 当前携带配置接受的
GC-3 物品实例。两侧使用同一份门与锁事实，类型 slug 相同不代表凭据相同。
解锁不开门、关门不上锁；普通开门、通行、导航与自动后果都尊重锁状态。
导航遇锁进入 blocked，取得凭据后需明确解锁与恢复，或取消后重新导航。

本批仅支持公开、非战斗、确定性原创机关。示例使用实际 `amulet` 实例，
不调用其魔法或施法效果。未实现撬锁、破坏、修锁、容器或完整 D&D 锁具规则。
见 [设计与支持矩阵](docs/GC4_LOCKABLE_PASSAGES.md)、
[Review](docs/reviews/GC4_LOCKABLE_PASSAGES_REVIEW.md) 和
[含新进程恢复的公开示例](examples/lockable_passages.py)。

## GC-3：普通物品实例取放

GC-3 的 SQLite Schema 24 与 Package `/4` 增加显式批准的便携实例。
`world.pickup_item` / `world.drop_item` 复用 TypedCommand、共享事务、OCC、
幂等回执与可靠 NPC handoff；`read_carried_items` 只允许读取自己或获准 Actor。
世界对象 ID 在地面、携带、移动和重启后保持不变。RF-4 真实库存实例和携带
slug 投影同事务更新；拾取不改变持握、装备或机械加成。

首版仅支持非战斗、公开、无争抢、可信配置的单件取放；导航不会自动拾取。
旧 Package 不获得新能力，旧库存数量/充能不被重建或清空。容器、完整锁具规则、
装备切换、堆叠、负重裁决、消耗效果和战斗取放仍不支持。
见 [GC-3 设计与支持矩阵](docs/GC3_ITEM_TRANSFERS.md)、
[Review](docs/reviews/GC3_ITEM_TRANSFERS_REVIEW.md) 和
[可运行示例](examples/item_transfers.py)。

## GC-2：持久非战斗导航活动

GC-2 的 Schema 22 仅追加导航活动记录。Package /3 显式授予公共区域的静态布局和通路；
PC 或获准 NPC 提交一次 `StartNavigation` 后，通用有界驱动器自动选择单格移动、
普通共享门和通路操作。活动与真实世界、事件证据、Outbox、版本和回执共同提交，
正常关闭后可在新进程连续推进。动态障碍只在实际执行时检查；受阻需明确恢复决定。

```powershell
uv run --offline --locked python examples/navigation_activity.py --directory .test-tmp/navigation-new
```

使用尚不存在的目录；默认演示真实 NPC Task / 合成 Result / 可靠 handoff。
`--actor pc --destination platform` 使用同一执行器验证另一角色和目的地。
示例自动关闭并启动新进程恢复，输出保存进度与回执的 `trace.json`。不调用 LLM、
不自动推进游戏时间，不实现锁具、取装备、自动绕路或活跃战斗恢复。
API、责任边界与限制见 [GC-2 设计](docs/GC2_NAVIGATION_ACTIVITIES.md)，
本轮验证见 [GC-2 Review](docs/reviews/GC2_NAVIGATION_ACTIVITIES_REVIEW.md)。

## GC-1：坐标化世界交互

Package /2 与 Schema 21 支持每个 Scene 一张局部 Grid，角色和物体在原有实体行中
保存当前 Cell。新增 `StepActor`、`TraversePassage`、`PlaceObject` 共用 TypedCommand
事务；旧移动入口和 NPC handoff 也执行端点、占位、门及障碍验证。单格物体只阻挡
普通移动，不自动遮挡视觉。探索坐标进入真实 RE 战斗投影，关闭时冻结历史站位。

```powershell
uv run --offline --locked python examples/spatial_world.py --directory .test-tmp/spatial-new
```

使用新目录；增加 `--object stool` 可验证第二种物体。示例通过公开 API 完成移动、
放置、PC/NPC 被阻、解除和恢复，并启动新进程校验。没有 LLM、战斗移动或历史存档
坐标补全；RE/SRD 依赖未升级，动态物体战斗等缺口明确拒绝。模型与支持矩阵见
[GC-1 文档](docs/GC1_SPATIAL_WORLD_INTERACTIONS.md)。

## NPC Observation Lifecycle（OB-1～OB-3）

Schema 16 在既有 Observation Work 上保存本地候选 `ob-retention/1`：首次持久接纳后
100 个权威游戏 tick 内为 Recent；旧数据时间未知，不补造历史。Pending/Completed
与 Recent/Expired 独立，Completed 在期限内仍可读取。`get_recent_npc_observations`、
`get_pending_npc_observation_work`、`get_npc_observation_retention` 复用 NPC 授权；
原 `read_observations` 保持返回类型并只提供 Recent。过期只影响投影，不删除证据、
多来源/历史 Meta、任务、回执或 Cursor。详细候选策略及验收见
[OB-1 Review](docs/reviews/OB1_OBSERVATION_RETENTION_REVIEW.md)。

OB 批次交付时保留了 `PER-19-22-01` P1；该阶段的原始审计记录仍保留。
后续 SM-PER-1 的修复与存档限制见下节，不实现模型或 Context 管理。

OB-2 / Schema 17 增加本地 `npc-memory-gate/2`：
`get_npc_memory_gate_input` 固定授权批次，原 `route_npc_observations(..., judgments=...)`
采用四类结构化候选；错误或不确定不完成 Work。`get_npc_evaluation_eligibility`
提供持久领域期限：周期 20、最长等待 60、余量 10 tick（TTL 100），合批不重置。
旧 Gate 输入路径保持显式兼容。没有模型、轮询或执行队列；详见
[OB-2 Review](docs/reviews/OB2_MEMORY_GATE_REVIEW.md)。

OB-3 / Schema 18 通过原 `route_npc_observations(..., classification="eligible")`
按当前模式授权单一 EVA，固定 Task 的 Recent 来源范围，保持 Pending 完成范围独立。
`NPCEvaluationInput.recent_observations` 包括仍有效的 Completed 背景；Meta 引用需同时
符合 Task 授权与提交时有效性。过期 Pending 只可通过显式
`get_npc_evaluation_recovery_input` 获取；`get_npc_observation_expiry` 提供下一次
Invocation 的元数据同步。均不操作模型、Context/KV 或 ACK；详见
[OB-3 Review](docs/reviews/OB3_EVA_LIFECYCLE_REVIEW.md)。

## SM-PER-1：事件时感知条件树

新 Signal 在四个既有字段之外支持受限 `origin`（Scene、可选实体/格子）和
`stimulus`（普通视觉活动、自身发光现象、一般声音）。旧 Package 仍可加载，Digest
算法不变；只在新事件提交时绑定明确默认值，不回填历史 Signal。
SM-PER-1 的 `event-evidence/3` 与事件/Outbox 同事务保存感官、Conditions、Grid、可用空间证据
和规则绑定。SQLite 表结构仍为 Schema 18。Scope Discovery 与独立的 Visual /
Auditory 三态条件树分开，内部 reason code 随判断持久化。

条件树复用安装版 Engine 的 `GridTopology.can_see()`、LoS / Cover 和
`conditions_auto_fail_check()`；`clear` 与机械可见都不直接等于实际察觉。
确定性阻断返回 Negative，信息或规则不足保持 Unresolved，Positive 仅来自既有
受限 Judgment 结果采用，不宣称 Engine 已提供自动察觉能力。没有真实 JEV 或新
模型接口、感知检定、距离公式、声学传播、半径 Scope 或其他 Channel。

`PER-19-22-01` 的新事件错误 Positive 已转为真实 SQLite 黑暗场景回归。
旧版未完成事件缺少机械证据时保持 Unresolved，不用当前环境重建过去；此前已经
提交的 Observation / Meta / Receipt 不静默改写，旧存档可能仍需独立审计及受控恢复。
测试证据、具体条件与剩余限制见 [SM-PER-1 Review](docs/reviews/SM_PER1_REVIEW.md)。

## Batch B：Event-local / Signal-scoped Evidence

新事件使用本地版本 `event-evidence/4`。Producer 先构造实际 Signals，再按各 Signal
的 Scene Scope 完整发现候选 NPC，冻结该阶段的必要证据；最终写入函数只分配事件
身份和持久化，不再读取 Command 结束态作为历史。Scene 退出在移动前、进入在到达后、
对象/NPC 修改在自身修改后、Package 事件在对应效果后采集。Clock、Schedule 和
Combat 初始化目前没有 Signals，保存明确的空范围。Dialogue 在实际发布点采集。

Evidence 保存逐 Signal 候选关系、完整性标记、候选 NPC 并集及独立的必要来源实体和
Scene 环境；不复制无关 NPC、Scene 或对象。来源 Actor 可为 PC；来源 NPC 同时为候选
时复用同一记录。Package 的不同 Signal 可引用不同的已声明 Scene，仍只支持 Scene
Scope，Origin 的实体引用须属于其 Signal Scene。未实例化的 Scope Scene 保持
`missing_scope_scene`，不能视为空候选或事后补证据。Signal Schema / Digest 算法不变。

固定 Engine 的 Result 只有最终 Delta 和有序事件，没有逐事件阶段状态；具有 Signals
的 Engine 事件明确记录 `engine.intermediate_phase_unavailable` 并保持 Unresolved，
不交 Judgment 猜测机械历史。合成 Evaluator 的无阶段 ProposedEvents 同样保守处理。
世界效果自身仍在实际执行点取证。没有实现 Action Started 或新的 Engine 阶段协议。

Batch B 使用 Schema 18，无 DDL 迁移。历史 `/1`～`/3` 已完成的 OB、Meta、Receipt
不改写；未完成的非空 Signal 事件无法证明阶段时保持 Unresolved，已知旧版本的空
Signal 可完成。现有历史边界和连续 Cursor 规则不变，因此这些缺口仍可能阻塞后续
Observation 交付；Batch C 的 NPC 独立放行见下节。黑暗场景修复与 Visual/Auditory
判断继续有效，机械可见不自动生成 OB。
实现、真实 SQLite 测试及未覆盖阶段见 [Batch B Review](docs/reviews/SM_PER2_REVIEW.md)。

## Batch C：NPC 独立感知队列与水位

SQLite **Schema 19** 增加 `npc_perception_queue`、`npc_perception_cursors` 和
`perception_event_barriers`。队列只保存 Session/NPC/Event 引用、该 NPC 的候选
Signal IDs、完成状态及来源，不复制 WorldEvent / Evidence。新事件提交时从 `/4`
冻结的 Discovery 同事务建立队列，不在异步阶段查询当前位置恢复候选。

`project_npc_event(session_id, event_seq, package, context=NPCContext, judgment=...)`
只处理获授权的该 NPC；`get_npc_perception_queue(...)` 和
`npc_perception_cursor(...)` 提供同一授权边界下的 Worker 元数据。它们不是供 LLM
直接读取的事件正文接口。原 `project_event(...)` 保留可信 Owner 的事件整体处理接口，
`perception_cursor(...)` / Outbox Done 只反映全局整体完成审计，不是 NPC 完成证明。

Recent OB、Pending Work、Memory Gate、模式路由、EVA 来源及 Recent Scope 都使用
对应 NPC 的水位。同一 NPC 较晚事件可以先判断并保存 OB，但较早候选未完成前不可
读取或送入 EVA；无关 NPC 的缺口不再拦截。NPC Completion Cursor 表示认知工作完成，
受自身感知水位和 Pending Work 限制；它与感知完成、读取和 TTL 继续独立。

完整 Discovery 的零 Signal / 零候选不建队列。无法证明阶段或范围的事件保存一份
共享隔离记录，保守阻塞没有自身完成证明的 NPC，包括后来实例化的 NPC，不把未知
当作不在 Scope。迁移仅使用冻结 `/4` 或已有逐 NPC 完成记录；历史全局 Done 不作为
NPC 证明，旧 OB / Inbox / Meta / Task / Receipt / 完成记录保留。可判断的 `/4` 工作
可重试恢复；不可恢复历史与 Engine 阶段缺口仍需未来可信证据或受控迁移，不提供
任意跳过/清除 API，也不补造历史。详见 [Batch C Review](docs/reviews/SM_PER3_REVIEW.md)。

## PERF-01 / PERF-02：按需水位与 Pending 分页

当前 SQLite **Schema 20** 为 `complete=0` 的 Queue 增加部分索引
`npc_perception_pending(session_id,npc_id,event_seq)`，删除派生缓存表
`npc_perception_cursors`。事件提交和感知判断完成不再遍历整个 Session 的 NPC
刷新感知或认知 Cursor。`npc_perception_cursor(...)` 在当前 SQLite 事务快照中，取
全局末尾、该 NPC 首个 Pending Event 之前、首个没有该 NPC 自身完成证明的 Barrier
之前三者的最小值。所有 Recent OB、Pending Work、Memory Gate、模式路由与 EVA
来源查询共用此水位；无关 Event 可直接越过，较晚 OB 不可越过本 NPC 的缺口。
`npc_completed_cursor(...)` 同样按需计算，并受 Pending 认知 Work 限制。
旧 `npc_completion_cursors` 仅保留该 NPC 在 Gate/EVA 完成事务中的检查点，读取
不以它作为当前水位。Unknown Discovery、历史逐 NPC 证明和后来实例化 NPC 的
Barrier 规则沿用 Batch C。

Worker 使用以下授权元数据接口，按 `event_seq` 严格升序，只从 SQLite 取本页
未完成引用，不加载已完成历史，也不依赖交付 Cursor 找工作：

```python
page = sm.get_pending_npc_perception_queue(
    session_id,
    package,
    context=npc_context,
    limit=20,
    after_event_seq=None,
)
```

`limit` 默认 20，允许整数 1～1000；`after_event_seq` 是可选的排他扫描位置，允许
0～`2**63-1` 的整数，拒绝 Boolean。继续本轮扫描可传上一页末尾序号；每轮重试
扫描须重新从 `None` 开始，保留较早 Unresolved 工作。该位置不代表 ACK。空页也
不代表未知 Discovery 的共享 Barrier 已解决。并发 Worker 可重复读到同一引用，
完成仍通过原有幂等 `project_npc_event(...)` 提交。
`get_npc_perception_queue(...)` 保留原签名、返回类型和完整历史行为，供显式审计；
不会静默把原接口改为 Pending-only。

Schema 19 及更早存档通过原有迁移写锁原子升级；Queue、Evidence、Event、OB、
Outbox、Receipt、RNG 与旧认知检查点保留，失败回滚和重复/多进程打开均有真实
SQLite 测试。Schema 20 不提供降级迁移，旧运行时不能直接打开新版本存档。
本地合成验证中，200 NPC 的无关事件提交 SQL 从 815 降至 12，Cursor 计算 VM
指令从 25,511 降至 70；包含 5,001 条完成历史和 40 条 Pending 的 Worker 查询
返回从 5,041 行降至 20 行，并使用部分索引。这些是合成 SQLite 证据，不代表生产
延迟或吞吐量；可用 `uv run --locked python examples/benchmark_perception_efficiency.py`
复现。测试结果、修改文件、迁移审阅及剩余成本见
[PERF-01 / PERF-02 Review](docs/reviews/SM_PERF01_PERF02_REVIEW.md)。

## 安装与验证

当前固定 Engine/SRD 完整 Git SHA（包含 Openable 与冻结素材整合）
`a011eba3ea2226e22d8081294ee38d3ec193b325`，Evaluator `/37`、Evaluation `/24`、
Snapshot `/17`、Availability `/13`、Stage Facts `/2`。冻结生命周期与旧 `/36` 存档策略见
[当前接入记录](docs/INSTANCE_CONFIGURATION_PROGRESS.md)。RF-4 引入 SQLite Schema 23（GC-3 升至 24，GC-4 升至 25）。当前 Snapshot/OCC 从受控事实
组件读取，不加载完整战斗事件历史；历史事件继续通过授权审计入口读取。
[RF-RE4 契约、归属、兼容与验证记录](docs/RF_RE4_HISTORY_FREE_RULES.md) 说明了当前边界。

SM-10–SM-13 的旧版本文档保留当时实现与证据；其 `/8` 依赖及 Snapshot V2 说明已由
RF-RE4 更新。实际旧 `/4`、`/8` Package/存档不能证明新版所需的完整组件事实，默认拒绝；
显式 `load_adventure_package(..., allow_legacy_audit=True)` 可保留原 Digest 和绑定供授权审计，
不授权新游戏命令或待发布对话。完整 `/16` 源 Package 可用显式转换辅助函数生成新的受审
Package；不会重绑旧 Session。

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。运行时依赖 PyYAML、固定 Git SHA 的 dnd5e-engine / dnd5e-srd-data 及其必要依赖；SQLite 使用 Python 标准库，pytest/Ruff 是开发依赖。`uv.lock` 固定完整解析结果。真实异步 Engine 测试在 Windows 需要事件循环使用的本地 socketpair；受限环境须允许该 OS 功能，测试不调用外部服务。

```powershell
uv sync --locked --group dev
uv run --locked python -m pytest -q
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
```

如果受限 Windows 环境无法使用系统临时目录，可把测试临时目录放在仓库内：

```powershell
New-Item -ItemType Directory -Force .test-tmp | Out-Null
uv run --locked python -m pytest -q --basetemp=.test-tmp/run
```

数据库、WAL/SHM、虚拟环境、缓存和测试临时目录均由 `.gitignore` 排除。数据库应放在支持可靠锁和同步的本地文件系统上。不要只复制正在使用的 `.db` 文件作为备份；当前未提供备份工具。

## 已实现的接口

以下是由可信适配层提供上下文的 Python 进程内接口，不是冻结的跨模块网络协议；NPC 读取入口按当前授权过滤。

| 接口 | 行为 |
|---|---|
| `load_adventure_package(local_yaml_path)` | 安全解析并验证单文件逻辑 Package；返回 `PackageLoadResult`，仅 `valid` 含不可变 Package；`invalid` 返回 `code/path/message`，不暴露半验证包 |
| `StateMachine(database_path)` | 打开/初始化 SQLite 文件，支持上下文管理器与显式 `close()`；每实例一个连接，只在创建它的线程中使用 |
| `create_session(request, package, *, context)` | 可信 `(principal_id, command_id)` 持久幂等；返回原始 `CreationResult` 有限元数据；同键不同 Fingerprint 抛出 `idempotency_conflict` |
| `get_npc_view(session_id, npc_id, package, *, context)` | 复用 NPCContext / npc_access，读取完整授权 NPC Meta 与历史；不消费 Observation、不授予战斗控制权；详见 [SM-14](docs/SM14_NPC_STATE.md) |
| `create_npc_evaluation_task / get_npc_evaluation_task / list_npc_evaluation_tasks` | 持久授权 Task、完整授权输入及重启发现；Background 须经过 Inactive 路由 |
| `submit_npc_evaluation_result / get_npc_evaluation_receipt` | 七字段本地 Typed Result、五类 Meta 整批原子提交、唯一 Result/回执/可靠输出 |
| `request_npc_interaction / set_npc_inactive / route_npc_observations` | 原子模式切换、取消旧资格、Lightweight Gate 与未完成 Work 保留 |
| `get_npc_mode / report_npc_evaluation_failure` | 持久模式读取与有界、带版本前置条件的失败报告；不执行模型重试 |
| `dispatch_npc_output / get_npc_output_status / npc_completed_cursor` | 独立场景移动 Command、受众校验的 Dialogue 发布、原回执对账与连续完成前缀 |
| `read_session(session_id, package, *, context)` | 在一致的读事务中验证主体、玩家控制关联和 Package/Ruleset 固定值，返回独立运行态 Snapshot |
| `save_session(...)` | 所有修改已同步提交；校验并读取非活跃战斗存档，不接收整份状态覆盖，不额外推进版本；活跃 Combat 明确拒绝 |
| `resume_session(...)` | 恢复非活跃战斗 Session；不重放初始模板，不迁移 Package；身份/Digest/Ruleset 不一致或活跃 Combat 时明确拒绝 |
| `combat.initialize / combat.intent` | 同一个 TypedCommand / Receipt 入口；只初始化受审 Package Encounter，再调用真实 Engine 基础攻击 |
| `combat.close` | 提交真实 Engine 的受限终局、XP 和 CombatEnded；完整原子性与持久幂等，详见 [SM-11](docs/SM11_COMBAT_CLOSURE.md) |
| `rules.check` | 上游 CheckPayload，可信 GM 裁决授权，真实非战斗 Ability/Skill 检定；空 Delta、RNG、CheckRolled 与回执原子提交，详见 [SM-12](docs/SM12_REAL_CHECKS.md) |
| `submit_command_async(...)` | 真实 combat.intent 的异步入口；复用相同幂等键与提交逻辑 |
| `read_combat_snapshot(...) / read_rng_context(...)` | 从 SQLite 重建实际上游类型，供受信审计；不等于活跃战斗续玩许可 |
| `query_action_availability(...)` | 授权、过滤的真实只读可用性查询；无 RNG、Receipt 或写入 |

运行时拒绝使用 `StateMachineError`，通过 `.code` / `.issue` 获取结构化原因；底层 SQLite 故障保留在异常 cause 中。Package 加载采用首错返回策略，当前没有非阻塞 warnings。

最小创建/恢复示例（从仓库根目录执行）：

```python
from trpg_state_machine import (
    CreateSessionRequest,
    PlayerSelection,
    StateMachine,
    TrustedContext,
    load_adventure_package,
)

loaded = load_adventure_package("tests/fixtures/lantern_station.yaml")
if loaded.status != "valid":
    raise ValueError(loaded.errors)
package = loaded.package
assert package is not None

# 只能由已经认证、已核验玩家控制关系的适配层构造，不能从请求 JSON 接收。
context = TrustedContext("authenticated:demo", "player.demo")
request = CreateSessionRequest(
    command_id="cmd.demo.create",
    adventure_id=package.adventure_id,
    package_version=package.package_version,
    package_digest=package.package_digest,
    players=(PlayerSelection("player.demo", "pc_template.visitor"),),
)
with StateMachine("demo.sqlite3") as runtime:
    created = runtime.create_session(request, package, context=context)
    snapshot = runtime.read_session(created.session_id, package, context=context)

with StateMachine("demo.sqlite3") as restarted:
    restored = restarted.resume_session(created.session_id, package, context=context)
    assert restored == snapshot
```

`TrustedContext` 只表示集成信任边界：本库不实现登录、认证服务或主体/玩家目录。受信适配层必须先核验控制关系；本库再核验请求的 `player_id` 与可信上下文一致，并限制后续访问为原主体/玩家。内部完整 Snapshot 仅供可信调用者；NPC 通过 NPC View / Scene View / Inbox 取得各自权限过滤后的信息。

## 基础合成 Package 子集

`tests/fixtures/lantern_station.yaml` 是完全原创、按 CC0-1.0 发布的合成数据。没有 First Blush 原文、地图或派生故事内容。

格式为 UTF-8 单文件 YAML，包含 `manifest/world/scenes/actors/pc_templates/events/rule_bindings` 逻辑数据；本地格式版本是 **`state-machine-foundation/1`**，不宣称冻结了总控仓库的完整 Adventure Package Schema。

- 校验必填/额外字段、命名空间化 ID（小写点分 ID、最长 128 字符）、作用域唯一性、Scene/Location/Actor 引用、入口和地点对应关系、Scene 可达性及多个内置 PC Templates。创建严格选择一个玩家、一个 PC Template；未知模板拒绝。
- 基础绑定是 `synthetic.foundation / synthetic-data/1 / none/1`，仅包含 HP 初值及空 resources/inventory。SM-4 的 synthetic.atomic 和 SM-6 的真实 D&D 绑定是明确隔离的扩展，不能互相冒充。
- World/Scene flags 仅允许命名空间为 `flag.` 的布尔值。对象状态仅支持合成 `closed/open`。出口只能是无条件的已定义 Scene 连接。
- NPC 复用现有稳定身份；`controller.kind` 支持 `sub_agent`、`deterministic_policy`、`scripted_event` 的元数据，不执行控制器。按初始 Scene 首次需要时创建一次；另一 Scene 的身份引用不会克隆或重置 NPC。PC 的实际 Actor ID 由 State Machine 分配。
- 旧基础 Fixture 的 `world_facts` 仍为空；SM-14 可选择声明有显式 NPC 授权的静态事实和私有资料，格式见 [SM-14](docs/SM14_NPC_STATE.md)。基础 `rule_bindings/resources` 为空，inventory 为空；其他绑定的受限扩展见 SM-4 / SM-6。D&D 只接受显式完整的 `rule_bindings.combat_foundation`，不提供通用 Encounter、脚本或任意机械更新接口。Compatibility 未解决项/适配说明仍须为空。
- 必需来源/许可/审阅元数据只做结构检查，不证明版权合法、剧情可通关、规则正确或战斗平衡。
- SafeLoader 禁用类实例化、重复键、别名/合并；只接受普通字符串、整数、布尔、null、列表/映射。时间戳、浮点数、二进制等拒绝；大小上限 1 MiB、嵌套上限 32 层、节点上限 20,000。

摘要算法为本地 **`foundation-json/1`**：对完整已解析/验证数据，以排序键、紧凑分隔符、`ensure_ascii=False`、`allow_nan=False` 生成 JSON，再对 UTF-8 字节求 SHA-256，表示为 `sha256:<hex>`。包含内容/来源元数据，排除文件路径、键顺序、注释和排版；不进行 Unicode 归一化。创建 Fingerprint 另含 `fingerprint_schema=foundation-create/1`、Package 身份/版本/摘要、生效 RulesetBinding 和 players；命令键与可信主体在独立主键中。这些编码仅为本仓库 Batch 1 实施选择。

## 状态修改与持久化边界

SM-2 introduced `submit_command(TypedCommand, package, *, context)` and authorized `get_command_receipt(session_id, command_id, package, *, context)`. Local schema `sm-command/1` retains enter_scene, set_scene_flag, set_object_state and move_actor; later batches add rule_operation, combat.initialize and combat.intent with exact typed payloads. NPC movement/combat requires the adapter-issued `TrustedContext.controlled_npc_ids` grant. The compatibility `_apply` routes through this same pipeline. Unsupported active-combat traversal/relocation/object operations are refused.

Receipts use durable `(session_id, command_id)` keys and a canonical fingerprint binding command, principal/player, package digest and ruleset. Replays reauthorize, same key/different content raises idempotency_conflict. Business rejections/version conflicts persist rejected/conflict receipts; commits persist committed receipts. Schema/auth/storage errors remain structured exceptions. State/version/events/receipt and event-time position evidence commit atomically. No-op commands save receipts without advancing world_version or event_seq.

- SQLite 使用 **WAL、foreign_keys=ON、synchronous=FULL、busy_timeout=5000ms**。创建在一个 `BEGIN IMMEDIATE` 短事务内写入 Session、World、入口 Scene、必要 Actor、选中 Character、独立 RNG、内部审计记录和创建键/原结果；任何阶段乃至延迟外键提交失败均全部回滚。
- Package 读取/验证、指纹计算、UUID 准备在写事务外进行；写事务内没有网络/模型/规则求值。持久唯一键和事务内再查创建键处理并发重入；失败不保留占位 Session 或幂等 claim。
- Scene 主键是 `(session_id, scene_id)`，首次初始化对象与 flags；重访保留实例。Actor 的 Scene 归属在唯一 actors 行；战斗格点位置在唯一 CombatState.actor_zone 中，当前拒绝无法完整表达机械副作用的战斗中场景转移。全部运行态表按 Session 隔离。
- 初始 `world_version=0`。每次实际状态修改成功提交推进一次；无变化操作不推进。`event_seq` 是独立的内部审计序号：创建产生两条记录，Scene 转移产生 exit/enter 两条记录，因此不等于 world_version。
- 事件保存稳定 ID、来源命令及受限 Signals，事件时位置证据与持久 Outbox 同事务提交；当前提供受限 Perception 投影，完整规则/传播协议未实现。真实检定提交 RNG 后推进一次 world_version；通用 event-only/Meta-only 策略仍为 OPEN。
- RNG 单独持久保存，Session 仅保存引用。Foundation 使用占位编码且不掷骰，synthetic.atomic 使用独立合成计数 RNG，真实攻击使用候选 Engine 的 `stdlib-mt19937/1` 状态及独立流版本；不把候选实现宣称为已冻结的通用跨版本规则 RNG 协议。
- 所有修改立即提交；`save_session` 不创建第二份权威快照/检查点。Storage schema 14 automatically migrates schemas 1–13 while preserving package pins/digests and state. Migration locks cover version recheck, all steps, FK validation and commit; interrupted upgrades roll back together. Legacy events/trigger sources have no fabricated historical evidence. Unknown schemas are refused. No online backup tool.

## 自动化验收证据

测试涵盖有效/无效/危险 YAML、所有必要引用与重复 ID、摘要稳定性、Session/快照隔离、可信身份、幂等重试及 Fingerprint 冲突、相同/不同请求的并发创建、四个写入阶段故障与提交失败回滚、Scene 重访/初始化失败、状态与事件原子回滚、版本冲突、Package/Ruleset 固定值和连接关闭恢复。

`tests/test_integration.py` 的完整合成测试执行：

```text
create → enter workshop → mutate door/flag/NPC → leave → revisit → save
       → process exit → fresh process → resume
```

正常关闭及 `os._exit(0)` 提交后异常终止两种路径均由真实子进程执行。恢复前独立 SQLite 连接核验真实行、连续事件序号、外键与完整性；恢复后比较全部 Snapshot/创建结果，确认 `world_version=6`、`event_seq=11`、门仍打开、标志和 NPC 位置保留，且只存在一个 Session/创建映射。没有 Mock 代替持久化或恢复。这证明提交后进程丢失恢复；不等于完成了硬件断电/磁盘故障验证。

## 架构依据与未完成范围

2026-10-10 核验 Meta 最新 main 为 [`fc41d3c584816198ba4f23bb4d326655ea3c774d`](https://github.com/agentic-trpg/agentic-trpg/commit/fc41d3c584816198ba4f23bb4d326655ea3c774d)。当前以 [Module Contracts 的 DECIDED](https://github.com/agentic-trpg/agentic-trpg/blob/fc41d3c584816198ba4f23bb4d326655ea3c774d/docs/MODULE_CONTRACTS.md) 为最高架构依据；旧 ADR 已删除，不覆盖最新契约。

SM-15～SM-18 核对 Module Contracts §3/§7/§9/§10、State Machine Architecture §3.4/§3.7–3.9/§6/§8、Agent Architecture §4–§6/§8–§10、Adventure Package Schema §5/§9 和 MVP Scope。本地及远程 Meta 文档仅作为只读依据，未修改其他仓库。PROPOSED / OPEN 字段不会因本地实现自动冻结。

当前支持受限真实基础攻击、胜利/TPK 关闭、通用 Ability/Skill Check、[NPC 持久领域状态与授权读取](docs/SM14_NPC_STATE.md)，以及下述 SM-15～SM-18 的 Task、原子 Meta Result、Mode/Gate、可靠输出和处理完成闭环。测试使用手工 Typed Result，不代表 NPC 已具备真实模型决策能力。**未实现** 通用 PC 构建验证、完整 Combat 启动/终局协议、完整回合/法术/Reaction、Saving Throw、完整声明式条件/奖励链、Quest/Inventory 操作、NPC 战斗 Action Intent 映射、完整感知传播、LLM Host/Prompt/Context、视觉系统、活跃战斗安全恢复、通用脚本或插件框架。

仍为 OPEN 或后续事项：正式跨模块 ABI/跨版本 RNG 回放、事件/Meta-only/未来无随机操作的版本策略、完整规则能力闭包、通用系统来源权限、条件 AST、Pending Choice/Continuation、完整感知传播、备份策略及外部 PC 导入。受限完整 Snapshot/Typed Delta 已实际实现和验证，不能等同于所有规则机制均已支持。

SM-2: 112 tests passed, including durable retry after lost response, revoked authorization, concurrent writers, injected transaction rollback and Batch 1 migration. See `docs/batches/SM2.md`.

## SM-3 event pipeline

`read_events(..., after_seq=0)` is an owner-only ordered committed-event query. Each event has a stable SM-owned event_id, event_seq, source_command_id, type, payload and typed Signals. `deliver_event(..., event_seq, sink=...)` executes the sink outside the write transaction and records durable completion; delivery is at least once, so sinks must deduplicate event_id. `delivery_cursor(...)` cannot cross an older incomplete work item. A delivered event is not completed NPC cognition.

Package events support only authoritative event-type/scene triggers, a typed flag_equals condition, set_scene_flag or set_object_state effects, per_session once and explicit visual/auditory scene-scoped Signals (clear/blocked/undetermined evidence). Scene event_definition_ids must match declared definitions. Conditions and effects run synchronously within the command transaction, including first-entry creation. Cycles are bounded by once history and a 32-consequence limit; exceeding the bound rejects and rolls back. No rule effects, downstream rule commands, arbitrary scripts or asynchronous cross-transaction atomicity are claimed. Signal visibility is authored synthetic evidence, not D&D perception rules or physical propagation.

123 tests passed for SM-3. Tests cover restart, signal/event/outbox atomicity, initial-entry triggers, once/cycle/cascade protection, failed delivery after sink acceptance, stable retry IDs, session isolation and cursor gaps. Schema migration preserves old events with legacy.unavailable source and no invented Signals. See docs/batches/SM3.md.

## SM-4 synthetic evaluation boundary

`rule_operation` carries a `RuleOperation` with operation_kind, real actor/target, declared resource and current-scene flag, difficulty and domain. Only `synthetic.atomic / synthetic-data/1 / synthetic-evaluator/1` admits the synthetic evaluator. Its PC Templates may declare namespaced integer resources and unique synthetic item IDs. This is not D&D data or an Engine integration. Foundation packages keep their original digest and placeholder RNG.

The local synthetic `RuleEvaluationRequest/Result`, `NonCombatSnapshot`, `StateDelta`, `RNGTransition` and `SyntheticEvaluator` live in mechanics.py; they are not the real Engine ABI. Detached synthetic input includes all supported characters/resources/inventory/effects, actor positions and world/scenes; RNG is separate. Direct synthetic results cover four statuses. The SM now refuses unsupported choice before evaluation with pending_choice_unsupported; unexpected post-admission needs_choice is an independent protocol error. The real D&D path imports actual versioned upstream types and never uses this synthetic snapshot.

Evaluation uses a copied request outside write transactions. Commit rechecks authorization, world version, full read-set digest, pins, RNG and every Delta target/precondition/invariant under BEGIN IMMEDIATE. HP, resource spend, world flag, RNG, consequences, events, outbox and receipt commit together. Accepted mechanical failure still spends its token. Invalid result or storage failure writes no state or RNG. A committed retry returns the stored receipt before evaluator invocation. Evaluation-response loss retries the same explicit RNG state, giving the same synthetic result; no real remote Engine retry protocol is claimed.

The synthetic stream uses SHA-256 of state_id and counter to generate a 1..6 value. Counter, position and rng_version persist atomically. This deliberately local test encoding is not a frozen RNG contract or statistically reviewed dice protocol. Event-only/Meta-only/no-draw global version policies remain OPEN; mechanical commits currently require an actual world change.

Engine main 64dd920 was inspected read-only. Public resolve_check and rest resolvers are reusable pure fragments, but evaluate, full Snapshot/Delta/RNGTransition and four-status envelopes are absent. The old registry-backed combat API cannot satisfy ADR-001. See docs/batches/SM4.md for exact gaps and actual smoke evidence.

## SM-5 NPC perception foundation

Package NPCs may declare explicit synthetic `senses: {visual: true, auditory: true}`. Missing senses remain unknown; they do not default to true/false. Every new event durably captures actor positions and these senses together with authored Signal scope/occlusion and perception work. Projection never reads a later position to reconstruct history. Known pre-perception history is explicitly archived at the durable compatibility boundary described below; genuine new missing evidence remains unresolved.

`project_event(session_id, event_seq, package, *, context, judgment=None)` processes durable work outside the write transaction. Scene scope, declared senses and clear/blocked evidence give deterministic answers. Only undetermined, adequately evidenced Signals are sent as a same-observer/event JudgmentRequest batch to the optional restricted callback. JudgmentResult must contain exactly one typed Boolean for every requested signal_id; omissions, duplicates, unknown IDs, bad types and exceptions leave work unresolved. No real JEV/LLM Host is implemented.

One EventObservation aggregates all perceived Signal content per observer/event/projection-version. All false completes without an Observation. Partial/unresolved decisions do not complete the event or advance the contiguous perception cursor. Observations and inactive-NPC inbox references commit atomically and deduplicate on retry. `perception_cursor(...)` tracks projection completion, not cognition completion. There is no ACK, Agent activation, EVA, belief or goal update.

Owner-only `grant_npc_access(...)` assigns an authenticated principal to an existing NPC. NPCContext is session/observer bound and checked against this durable grant on every `read_observations(...)` and `get_scene_view(...)`. NPCs cannot use owner snapshots or read_events. NPC observations expose opaque observation_id, observer_id and perceived channel/content only; event type/source command/true actor/secret target/internal trace remain in storage. Inbox reads retain records and are not cognition ACKs. A Session-wide cursor conservatively withholds later observations across any earlier unresolved gap.

`get_scene_view` returns CurrentPerceptualView for NPCs: own current scene and currently visible actors/objects in the synthetic clear-scene model, with unresolved status for missing senses. It never infers a past causal actor from current presence. Owner access returns the ordinary current SceneInstance. Physical LOS, distance, walls, D&D senses/checks and secret-content authoring validation remain unsupported.

168 tests pass, including 22 perception tests: two observers/multiple Signals, event-time movement, hidden activity, malformed/timeout JEV, all-false/known occlusion, durable unresolved gaps, inbox rollback, concurrent deduplication, authorization revocation/isolation and fresh-process reading of real persisted inbox rows. See docs/batches/SM5.md.

## Stabilization of SM-2–SM-5

Evaluator configuration and Result RulesetBinding mismatches raise structured
`StateMachineError(code="ruleset_binding_mismatch")`, without a receipt, state/RNG
change or event. Correcting configuration allows the original command to retry.
Unsupported operations still return their durable business rejection. Mechanical
`event_cascade_limit` rolls back the candidate HP/resources/world/RNG/events/trigger
history inside a savepoint, then atomically saves the rejected receipt. Replays,
including after restart, return that receipt without evaluating again. Protocol,
evaluation and storage failures are not cached as business rejections.

Schema 6 adds `legacy_perception_boundaries(session_id, through_event_seq, reason)`.
The locked source schema 1–4 proves that all existing events predate perception.
For already upgraded schema 5, migration identifies only a contiguous prefix with
old `event-evidence/1`, or missing evidence plus the migration-generated source,
empty Signals and no matching real command/creation key. Classification stops at
the first other event. The stored reason is `legacy_event_evidence_unavailable`;
original events, evidence and Work remain, with no fabricated Signals, false
decisions or historical Observations. `project_event` returns `legacy_unavailable`
for this archived prefix. The perception cursor orders work **after** this explicit
boundary; it does not assert historical projection or cognition completion.
Delivery work and its cursor are unaffected. Genuine new pending work, unknown
senses and missing event-time evidence still hold later Inbox delivery.

This is a local migration compatibility policy, not a frozen cross-module epoch
or ACK protocol. It cannot recover lost evidence or invent senses for an old pinned
Package. Ambiguous schema-5 provenance stays unresolved conservatively. Package
digest mismatches still refuse resume; no silent Package rewrite is provided.

191 tests pass (168 existing + 23 stabilization cases), including real historical
runtime subprocesses, schema 1–4 upgrades, already-upgraded schema-5 mixed history,
nonzero RNG/receipts, concurrent initialization/upgrades, migration process exit,
new Observation delivery and new unresolved cursor gaps. Historical tests require
Git and the five fixed commits in `tests/test_migrations.py` to be available to
`git archive`; shallow clones/source archives need that repository history first.
The original old Loader/Fixture tests are separate from forward-authored senses
fixtures passed through the old trusted typed-package service boundary; they do
not claim old YAML loaders supported senses.

See [Stabilization Review](docs/reviews/SM_STABILIZATION_REVIEW.md) for commands,
evidence, compatibility and the merge recommendation. F5 (exact triggering-event
reference) and F6 (full versioned Snapshot/read-set and PendingChoice protocol)
were known follow-up work at stabilization. SM-9 links new trigger sources and
records F6's scoped acceptance matrix; old unavailable sources remain audited.
The stabilization itself added no real D&D Adapter or NPC EVA.

## Real Engine integration history (SM-6–SM-9)

以下记录原批次边界；当前固定版本与 B1 进展以上文及 [支持矩阵](docs/SM13_SUPPORT_MATRIX.md) 为准。

The candidate D&D Engine and SRD data dependencies are pinned to
`a7225ebaa4914f6db4ae72f3f6454c355ea7baac`. Schema 7 persists complete restricted
mechanical components and separate RNG. `combat.initialize` accepts only the
reviewed encounter embedded in a D&D-bound Package; trusted audit reads rebuild
actual upstream `CombatSnapshot` / `RNGContext` from SQLite. Active combat
save/resume is explicitly refused under SM-02. See
[the mapping and compatibility notes](docs/SM6_COMBAT_FOUNDATION.md).

SM-7 adds real PC `combat.intent` evaluation and closed Typed Delta/RNG/Event/
Receipt/Outbox atomic commits. Sync callers use `submit_command`; async callers
use `submit_command_async`. Supported tests cover ordinary explicit basic attacks,
misses, Temp HP and damage resistance/immunity/vulnerability. Choice, complex
mechanics and the unimplemented upstream `combat.end` remain restricted. Read
[the execution and error boundaries](docs/SM7_REAL_ATTACK.md); active combat
audit recovery must not be described as safe continuation.

SM-8 adds trusted NPC `combat.intent` through that same pipeline and read-only
`query_action_availability`. Explicit ordinary weapons and bundled Bandit's basic
`scimitar` / `light-crossbow` actions are tested; unsupported/unknown actions fail
closed. Availability is conditional and does not grant execution rights or reserve
resources. See [NPC authorization and query boundaries](docs/SM8_NPC_AVAILABILITY.md).

SM-9 upgrades storage to Schema 8 and links new Trigger History entries to exact
committed events. Unrecoverable old trigger sources are explicitly audited.
Committed basic attacks feed only a generic visible Signal through the existing
Perception/Inbox pipeline. Integration tests verify real state, RNG, receipts and
observations in a fresh process. They explicitly refuse active-combat continuation;
the candidate has no stateless combat.end, so completed-combat safe save/resume
was **Blocked** at SM-9. SM-11 now resolves this for bounded victory/TPK closure. See [historical hardening evidence and F6 scope](docs/SM9_INTEGRATION_HARDENING.md).

## 可执行跨阶段示例（SM-13）

```powershell
uv run --locked python examples/real_cross_phase.py --directory .test-tmp/demo-new
```

使用尚不存在的目录。示例保留原创 Package 与实际 SQLite 数据，并启动新 Python
进程完成关闭后的恢复和下一次真实检定。完整流程覆盖探索、检定、世界命令、NPC/PC
攻击、战斗关闭、XP 和 RNG 连续性。示例中的 TrustedContext 是本地受信 Host 演示，
生产身份认证和 GM 授权必须由集成层提供。

## SM-14 NPC persistent state

SQLite Schema 10 在现有 actors 身份下保存唯一 NPCState、静态 Profile、Known Fact 引用、版本化 Memory / Belief History / Relationship / Goal / Plan 和 Runtime Schedule / Current Activity。初始 Belief 可为错误判断且不改 World Fact；无 Profile 的旧 Package 明确为 unconfigured。旧 Schema 1–9 迁移只给已存在 NPC 建立 legacy/unconfigured 行，不从 Package 补造认知。

`get_npc_view(session_id, npc_id, package, *, context=NPCContext(...))` 返回该 NPC 全部授权 Meta，复用 npc_access，严格按 Session/Observer/Principal 检查；读取不消费观察或改变权限。初次 Actor/Scene 初始化与既有事务同成同败，重访、移动和重启不重置私有记录。控制策略只是保存的元数据，没有自主决策、模型调用或任意 Meta 写入接口。

实现、大小/引用限制、迁移与下一阶段边界见 [SM-14](docs/SM14_NPC_STATE.md)；Batch 验收证据和审阅结论见 [SM-14 Review](docs/reviews/SM14_NPC_STATE_REVIEW.md)。

SM-14 最终全量回归：**462 passed（396 既有 + 66 新增）**；Ruff 检查、格式检查和 `git diff --check` 通过。测试包含真实 Schema 1–9 建库/升级、并发创建、迁移异常退出、私有来源约束和新进程恢复。

## SM-15 durable evaluation authorization

Configured NPCs can receive persistent, controller-bound `npc-evaluation/1` tasks via `create_npc_evaluation_task`, read complete authorized inputs via `get_npc_evaluation_task`, and discover tasks after restart via `list_npc_evaluation_tasks`. Migration 11 preserves existing Inbox observations and adds Work/provenance references. Reads do not complete cognition. This batch does not run a model or submit Meta/results. See [SM-15](docs/SM15_NPC_EVALUATION_TASKS.md).

## SM-16 atomic NPC cognition result

`submit_npc_evaluation_result` validates a local seven-field typed Result and atomically appends all five private Meta histories with one selected Result, EvaluationReceipt and reliable output records. `get_npc_evaluation_receipt` recovers lost feedback. Schema 12 retains prior data and adds scoped Observation provenance. No model or action execution is provided in this batch. See [SM-16](docs/SM16_NPC_META_COMMIT.md).

## SM-17 modes and Gate

Persistent Interactive/Inactive scheduling now serializes task authorization, nonblocking mode cancellation and Result commit. `request_npc_interaction`, `set_npc_inactive`, `route_npc_observations`, `get_npc_mode` and bounded `report_npc_evaluation_failure` reuse the existing NPC authority and Work. Schema 13 records safe retention/no-op without inventing Meta; Interactive mode excludes Background even while idle. See [SM-17](docs/SM17_NPC_MODE_GATE.md).

## SM-18 output and processing recovery

`dispatch_npc_output`, `get_npc_output_status` and `npc_completed_cursor` complete the local NPC domain transaction loop. Reliable action handoffs use the existing `move_actor` TypedCommand subset and original CommandReceipt; authorized local speech publishes one observable event. Schema 14 persists a per-NPC continuous processing cursor. Completed EVA does not wait for action success, and real-process crash recovery avoids duplicate cognition/actions. This remains hand-built Result integration, not a real Agent Host/LLM MVP. See [SM-18](docs/SM18_NPC_OUTPUT_COMPLETION.md).

## SM-19: NPC combat output handoff

`NPCCombatIntent(payload: CombatIntentPayload, expected_world_version)` in `npc_results`
uses the pinned Engine payload inside the existing seven-field Result. `NPCActionIntent`
(move_actor) remains compatible. `dispatch_npc_output` restores the original TypedCommand,
uses its durable command ID and CommandReceipt, and leaves completed Meta/EVA intact on
command refusal or failure. Both command preparation and atomic commit recheck the
persisted handoff/controller grant. Target admission is deliberately limited to a
current, shared, clear bright scene with explicit visual senses and no unsupported
conditions/obstructions. Other perception situations fail closed; no D&D visibility,
attack, damage or action-economy formula is implemented here.

Real attacks still run outside SQLite write transactions. A fresh process may reconcile
an already committed command/handoff; it cannot resume an interrupted active combat.
No new table, ACK, executor, continuation or second receipt store was added.

## SM-20: authorized scene reads

`get_scene_view(..., context=GameViewContext(principal_id, session_id, player_id, role))`
accepts adapter-authenticated `gm`, `player`, or `narrator` roles. SM checks the actual
Session owner/player, scope and Package pins without casting these callers to the
internal TrustedContext. The adapter must authenticate role grants; request payloads
are never credentials. These read-only contexts cannot call commands or full snapshot/event
reads. GM gets Session world facts and current scene flags; Player gets visible current
objects/actors and its own CharacterState; Narrator gets current visible committed state.
No static scene prose, secret flags, NPC cognition or raw event payload is exposed to the
player-facing roles. Public fact discovery/history is not yet a separate capability.

NPC reads keep existing NPCContext/npc_access, NPC Meta and Inbox. Current scene views
now carry view_world_version; unknown mechanical visibility fails closed, including
unsupported lighting, concealment, obstacles and conditions. Event-time observations
remain independent historical evidence. observation_signal_sources retains only actual
positive Signal provenance internally; source event semantics stay out of NPC input.
No reads ACK, consume observations, advance completion, or change state/RNG.
### SM-21 conditional world effects

An authenticated GM may attach local `CheckWorldEffects` to a `rules.check`
TypedCommand: bounded source reference, exact flag/object preconditions, and fixed
success/failure operations. The pinned Engine's structured `CheckRolled.succeeded`
selects the branch; accepted does not mean success. Existing world operations,
RNG, events/outbox and receipt commit atomically. No HP/resource/inventory writes,
new trigger engine, tool checks or continuation support are introduced. See
`docs/reviews/SM21_CONDITIONAL_WORLD_EFFECTS_REVIEW.md` for evidence and limits.

### SM-22 game time and NPC schedule

SM-22 is independently delivered under the user's batch-only authorization.
Existing Event-time Perception P1 `PER-19-22-01` remains unresolved and is a
**Global Merge Blocker**. This delivery does not include its repair or a formal
four-batch Global Review; see the SM-22 review for the preserved evidence.

Schema 15 adds the single `WorldState.game_tick` (local abstract `game-clock/1`)
and `npc_schedule_runs` lifecycle. Trusted adapters grant `can_advance_time` for
`world.advance_time` / `AdvanceGameTime(expected_tick, next_tick)`. Existing command
idempotency and transactions cover clock, NPC runtime/version, stale Task
cancellation, bounded events/outbox and receipt. Authorized NPC Views include
their own runs. Declared windows and actual location govern start/continue/end or
interruption; missed windows never pretend to have run. No teleport, cognition
update, automatic EVA or D&D time calculation occurs. Active combat advancement,
overlap priority, recurrence and arbitrary schedule-condition AST are unsupported.
See `docs/reviews/SM22_GAME_TIME_SCHEDULE_REVIEW.md` for migration and test evidence.


## OpenableService (partial scope, 2026-10-17)

Open/close, keyed locks, multiple locks, bars and their availability now share
OpenableService; WorldHandler retains command orchestration and WorldFacts
retains the single authoritative state. Mechanism, existing NPC handoff,
Activity, Package and check consequences use the same transition validation.

Package state-machine-foundation/7 adds explicit picking and force policies.
`world.pick_lock` / `PickLock(scene_id, object_id, lock_id, tool_instance_id,
expected_cell)` and `world.force_open` / `ForceOpen(scene_id, object_id, method_id,
tool_instance_id, expected_cell)` resolve through the real RE shared check
pipeline. `world.clear_jam` / `ClearJam` uses a configured method that clears jam.
No client-provided DC, success, trusted flag or arbitrary state patch is accepted.
Use query_world_action for read-only admission and the existing TypedCommand
submit/replay protocol for execution. Failed valid attempts produce actual check
results; illegal/stale requests do not consume RNG. Repeated completed operations
produce no new roll/event.

For an accessible, explicitly selected unlocked lock, the PickLock query returns
`status="available", reason="already_unlocked"` (该锁已解开，无须撬锁), regardless
of the supplied tool's availability. Execution commits a replayable no-op receipt
without a rule request, roll, event or world/RNG version change. Identity, access,
authorization and version checks still apply. Locked targets retain tool checks.
Openable checks retain the generic check data and verified original operation in
`rules.check_rolled.payload.adjudication.openable_operation`, associated through
the existing `source_command_id`; this audit data does not create sensory Signals.

This is **not completion of the full requested World scope**: combat object costs,
Arcane Lock/Knock and their Effects/TimeBinding lifecycle, real object attacks,
and new NPC EVA picking/forcing DTOs remain unfinished. Existing guards remain.
The acceptance checklist, pins, validation and missing dependencies are in
[Openable progress](docs/OPENABLE_SERVICE_PROGRESS.md).


## MechanismService (bounded extension, 2026-10-18)

Package /10 `mechanism/2` adds named repeatable operations, finite/unlimited uses,
no-op consumption policy, block/disarm/prepare/reset methods, real ordinary RE
checks and explicit failure activation. Ordered read-only candidates fix MECH-01
without removing lock/jam/bar/occupancy checks. Actual region entry, object
operations and adopted object checks feed the existing bounded PackageEffects
queue; NPC Signals/OB are not trigger authority. Portable instances retain their
original identity/location and Container/Item paths. Second deadlines use
WorldTimeService; per-Turn and public combat operations remain unsupported.

Schema33→34 preserves old armed/spent rows and receipts, and adds activation
adoptions. Schema35 adds Environment storage. Historical ENV34 saves are recognized
by their actual table structure, retain all environment data and atomically receive
the missing main34 mechanism upgrade; existing main34 mechanism data is retained.
Unknown/partial structures are refused without writes. See
[Environment compatibility](docs/ENVIRONMENT_SERVICE.md).
Damage/save/condition/Effects and material/tool-proficiency/timed-Activity work
remain OPEN by their respective owners. See the [exact contract](docs/MECHANISM_SERVICE.md)
and [progress/validation record](docs/MECHANISM_SERVICE_PROGRESS.md).
