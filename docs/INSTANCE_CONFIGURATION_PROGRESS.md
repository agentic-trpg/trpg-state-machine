# 实例配置、容器读取及冻结规则素材核对

2026-10-20；基线 `f826cb8`；分支 `feat/frozen-ruleset-complete-instances`。
仅修改 SM；不合并 main。当前分支已包含远端 Session Management 和 main，未重复建立协调器。
仓库和适用祖先未发现 AGENTS.md 或 check-fast 脚本；选择 Fast 静态检查及相关 Focused／Integration。

## 三类问题的实际状态

1. **冻结规则素材接入：已完成生产装配。** RE Openable 外部阻塞已由指定整合提交解除。
   默认与自定义有效素材在 Runtime 装配时冻结，共享给初始化、规则求值、availability 和绑定校验。
2. **实例配置闭包：本轮实际存在的读取依赖已迁移。** 对象能力、普通物体属性、portable 准入、
   Scene 元数据／地图、NPC senses 及辅助路径读取 Session 配置与当前事实。
   创建、延迟创建及旧库显式升级仍可读取固定 Package，普通 getter 不写库。
3. **容器索引／遍历／重量：已实现。** 操作内邻接索引和重量 memo 复用；候选变更使重量失效；
   SQLite 有序直接内容物查询扩展既有索引。最终运行证据记于本文验证节。

## 冻结素材接入后续（2026-10-20，基线 ec9c714）

Engine 与 SRD 同时精确固定 `a011eba3ea2226e22d8081294ee38d3ec193b325`；已阅读该提交
`docs/dev/openable-support.md`、evaluation_ruleset、SRD frozen、请求／结果与阶段验证类型。
实际 diff 中 Snapshot 仍为 `/17` 且类型未变；请求／结果 `/24`，evaluator `/37`，
Openable 外层 `/1`、availability `/13`、stage facts `/2`。此前 3718f7c 缺失工具 ABI 的阻塞已解除。

### 装配、生命周期与实际调用路径

- `StateMachine.__init__` 在打开 Store 前选择最终素材：默认 `get_default_ruleset()`；传入自定义
  source 则 `freeze_ruleset(dnd_loader)`。调用方先完成组合和覆盖，构建时须保证来源稳定。
  RE 在冻结阶段验证所有最终素材、身份冲突及 typed accessors，再计算一次正式 binding。
  同一引用注入 `WorldHandler.loader` 和 `RulesAdapter.loader`，后续 Service 不再冻结。
- 默认对象由 RE 自身管理进程生命周期；自定义对象由 Runtime 持有，多个 Runtime 可显式注入
  同一个已冻结对象（RE 返回原对象）。SM 没有地址缓存、第二套摘要或规则身份。
- 创建中的角色／怪物能力检查、物品初始化、战斗准备和运行能力检查使用该冻结对象。
  普通求值、Openable 求值、Openable availability、普通 action availability 与 verify_ruleset
  均传同一选定对象；没有将原始 BundledAssetLoader 交回 RE 兼容路径。
- 修改入口为关闭并重新构造 StateMachine，传入修改后的最终 source 或新构造的 FrozenRuleset；
  不支持运行中热替换。相同 source 对象再次构建也重新采集，来源文件变更不会误命中旧结果。
  不同版本并存时彼此隔离；冻结失败发生在 Store 打开前，不发布半成品。
- 重启后自定义规则重新冻结一次，再比较持久绑定。默认每个新进程也重新构建一次；
  同进程恢复默认规则复用 RE 单例，不假设内存对象跨进程保留。
- 源 Package `/16` 的显式转换 `migrate_package_16` 也先冻结一次，供多个 foundation 和最终
  binding 复用；这是原有源内容转换，要求新 Package 版本，不是存档规则迁移。

RE 的公开 getter 返回内部已解析模型的**防御性深拷贝**；修改返回值不会污染固定素材。
因此重复 dagger 读取复用内部固定素材，不读文件／解析／model_validate，但返回值不保证
Python `is` 身份，且仍有深拷贝分配。未在 SM 包装第二套素材缓存或绕开 RE 私有模型。
这是精确依赖的正式限制，不能宣称公开素材返回对象完全零分配或同一只读对象。

### 绑定、阶段与旧存档

创建先将 Package 的三项 binding 与实际冻结对象的正式 RulesetBinding 校验为完全相等，
然后同事务保存该已验证绑定；运行／save／resume 从 Session row 取三字段与固定对象比较。
ruleset_id、data_revision、evaluator_version 均保留，Git SHA 只用于依赖锁定。
每次比较不会重新枚举素材或计算摘要；不匹配在求值与权威 RNG／世界变更前拒绝。

`validate_dnd_result` 继续先严格重新验证真实 RE DTO，再调用 `verify_request`：后者按 `/2`
验证阶段结构及与请求／事件／Delta 的对应关系。Openable 与普通检定保留 RE 的
`uncovered(operation_not_in_stage_profile)`，不伪造 complete 或扩展感知 Evidence／战斗范围。
缺失阶段、伪造 complete、uncovered 携带执行记录均原子拒绝。
既有 accepted 检定失败、工具／技能熟练去重、优势、可靠才能、拒绝、冲突、重放及提交回滚保留。

`/36` 的完整 `/17` Package 仍可解析，用于原 pin 的只读审计与原有实例配置显式升级；
真实 ec9c714＋原 RE `/36` 生产者创建、执行并保存的数据库已做恢复回归。
新 RE `/37` 下 save／resume／新规则执行明确 `ruleset_binding_mismatch`，保持旧绑定、RNG、
世界、历史与回执；不会静默重绑。已提交请求重放仍遵守原幂等契约，不重新执行。
本次不提供活跃存档 `/36 → /37` 自动迁移：继续旧部署，或在新 Package／新 Session 显式创建。
原 `/4`、`/8` audit-only 以及不完整机械事实拒绝边界、`upgrade_instance_configs` 状态保护保持。
示例内容更新为新 authored 版本 gc3.2／gc4.2／world-station.2，不能替换旧 Session 的 Package pin。

### 冷构建与后续计数

`tests/test_frozen_ruleset_runtime.py` 旁观实际 Path.read_text、canonical model_validate、
source／frozen list_slugs 和 RE `_compute_binding`，不替换其实现，也不使用耗时断言。
计数中的“模型”指素材解析校验创建，不把 RE getter 的防御性 deepcopy 称为零对象创建。

| 实际路径 | 文件读取 | 素材模型校验 | 素材枚举 | 全量摘要 |
|---|---:|---:|---:|---:|
| 默认冷构建一次，1663 个素材 | 1663 | 1663 | source 11 + frozen 11 | 1 |
| 同 Runtime 10 次 dagger＋binding 校验、第二个默认 Runtime | 0 | 0 | 0 | 0 |
| 原始兼容 loader 连续 3 次 verify（优化前的重复路径，同版本同素材） | 8265 | 8265 | source 33 | 3 |
| SM 冻结后连续 3 次 verify | 0 | 0 | 0 | 0 |
| 默认／自定义各 3 次真实 Openable＋3 次普通检定、查询、创建与保存 | 0 | 0 | 0 | 0 |
| 普通战斗初始化＋3 次 availability＋真实攻击，冻结后 | 0 | 0 | 0 | 0 |
| 自定义 dagger 文件两版各构建一次 | 2 | 2 | source 22 + frozen 22 | 2 |

自定义恢复重新构建增加一次摘要，之后连续 resume／verify 为零；默认同进程恢复不增加摘要。
专项同时覆盖重启、冲突素材拒绝后重试、新来源不串用、真实旧存档拒绝和非法阶段无副作用。
历史 Schema 生产者原来通过当前 sys.executable 加载归档 SM，升级 RE 后实际混入 /37，
导致旧 /36 loader 在创建前拒绝。回归现通过固定 ec9c714 lock 环境运行对应归档源码，
由其实际 RE 在创建时生成 source binding，再把原 source 交给新 SM；不是修改已存规则绑定。
Schema、所有旧行、RNG、回执与故障回滚断言保留；恢复断言按正式旧 binding 拒绝策略加强。
### 本次最终验证（按文件取最终结果）

| 检查 | 实际结果与边界 |
|---|---|
| 历史生产者、实例升级、环境存档与冻结专项，8 文件 | **163 passed**，286.17s，exit 0；含新增冻结 17 项 |
| Schema32／Openable 兼容的完整 2 文件修复重跑 | **67 passed**，39.32s，exit 0；原两个固定摘要常量保留 |
| 其余 57 文件 | JUnit 逐项核对 **1184 passed**；其最终源码在该次运行期间未改动 |
| 旧环境 fixture 最终入口复核 | **1 passed**，4.10s，exit 0；与上列重叠，不加合计 |
| 3 个实际可执行示例 | item_transfers、lockable_passages、world_station 全部 exit 0，含新进程恢复 |
| Fast／安装 | Ruff check、format（203 文件）、diff check、Markdown 链接／fence、uv lock --check --offline 通过；两包 direct_url 精确 SHA 均为 a011eba |

去重共 **1414 个不同用例／67 文件**，不是仓库全量或远端 CI 完成声明。
其余 57 文件的证据来自 `.test-tmp/frozen-unaffected-final.xml`：该整组原为 1251 项，
**1248 passed／3 failed**、920.19s、exit 1；3 个失败全部属于上述 2 文件的旧生产者／
固定摘要输入，已修复并完整重跑为 67 passed。按 classname＋name 验证新旧覆盖集合完全相同，
替换这两文件的结果后才统计其余 1184；未把 exit 1 宣称整组通过，也未只统计原成功项而漏掉失败。
其他一次遗漏 sys 导入的中途重跑已修复，最终完整 67 项包含该真实重启用例。

最终日志为 `.test-tmp/frozen-historical-final.log`、`frozen-legacy-additional-v2.log`、
`frozen-unaffected-final.log`、`frozen-predecessor-final.log`；计数输出为 `frozen-runtime-final.log`。
覆盖核对为 `frozen-final-coverage.json`；未修复的首轮大组已停止并保留日志，不作通过证据。
只有两条既有 Lark 弃用警告。生产源码在上述最终分组期间保持相同；后续只修复对应历史测试输入。

剩余边界：RE getter 按正式接口进行防御性深拷贝；没有公开同一对象／零分配承诺。
没有活跃存档的 /36 → /37 自动重绑；未扩展战斗 Openable、世界来源 Effects、空间重复读取或机关索引优化。
下文计数与检查数字均属于 ec9c714 历史批次。

## 历史：先前 RE 候选与外部阻塞（现已解除）

远端 main 核对为 `3718f7c3d35424cc7910bcc19673083a357afdb7`，提供
`FrozenRuleset`、`freeze_ruleset`、`get_default_ruleset`；evaluator `/37`，request `/24`。
SM 原固定提交为 `d596161060555630019163ab301e1d25fd816549`，evaluator `/36`，request `/23`。

实际将 Engine／SRD 锁定、安装到 3718f7c 后，兼容实验发现：

- 缺少 `dnd5e_engine.evaluation_openable`。
- `evaluation_checks` 缺少 `check_support_failure`；`evaluate_check(request, loader)`
  不接受原 `tool_slug`／`tool_advantage` 参数。
- `activities.check_pipeline.resolve_check_request` 也不接受这两个工具参数；
  原工具／技能熟练不重复叠加、组合优势、crowbar 及 reliable talent 行为不能仅靠入口重命名保留。
- SM 入口包装实验实际失败为 `ImportError: cannot import name 'check_support_failure'`。
  不能把受工具约束的操作降为普通检定，也不能将这些规则计算移入 SM 来伪装兼容。
- 远端 Openable 最新 `74d9e0800c22a740614af72930dd77dd3c064561` 包含原工具接口，
  但无 FrozenRuleset，仍为 `/36`。本轮未发现同时具备两者的可用固定候选。

不修改 RE 仓库。实验性的依赖／协议／包装变化已撤回；pyproject、uv.lock、实例 Package 绑定、
Session 持久绑定均未改写。需要 RE 提供同时含冻结数据和上述工具检定行为的固定候选。
之后才能接入默认共享只读素材并通过实际装载／验证计数；本轮没有这项通过证据。
Session 三项绑定仍由现有 SessionRuntime／RulesAdapter 从持久 row 构造请求和验证结果。

## 唯一权威与实际装配

新增 Schema39 的 `instance_configs(session_id, domain, instance_id, config_json)`。
这是有效配置的权威记录，不是整个 Package 的副本，也不储存当前位置或余额。
WorldFacts.instances 提供领域辅助读取，WorldFacts.object_definition 改为按 Session＋实例 ID 读取。
序列化使用仓库内领域类型的带类型编码；普通读取缺失时报 `incomplete_instance_config`，不修复。

| 数据 | 配置权威 | 当前状态权威／运行读取 |
|---|---|---|
| 普通对象、Container、Spatial | object 配置：名称／来源、重量／体积／fit、容量、限制、占地、朝向许可、独立空间政策 | objects 的排他位置、container_id、朝向、开关；ContainerService／OSS／WOS |
| Openable、Lock | jam policy、操作侧、force methods、lock policy／DC | objects.state、object_openables、door_locks；凭证只由 door_credentials 保存 |
| Light | 模式、半径、形状、燃料政策 | object_lights.enabled／mode；LightService 及事件取证 |
| Mechanism | 操作、触发器、组件、目标、后果、频率、重置政策、typed effect 配置 | object_mechanisms 和 mechanism_activations；触发索引从实际实例配置构建 |
| Durability | max_hp、AC、阈值、损毁政策 | object_durability.current_hp；没有初始 HP 镜像 |
| Item | object 配置的 portable_policy、有效物理属性及身份 | objects.item_json 的 slug／quantity／charges 和唯一位置；库存仍为 CharacterFacts 同事务投影 |
| Scene | scene 配置的 location、exits、权限、声明依赖 ID | scene_instances 的 flags／visited_count；已创建地图只取 mechanical_scenes.state_json |
| NPC／Character | actor 配置保存 NPC senses；身份、controller、profile／runtime 继续取既有 npc_states | actors 唯一位置；characters、mechanical_actors／components、NPC 各领域记录继续持有当前状态 |
| Session 静态配置 | 固定 passages、navigation grants、机械初始化模式 | Session rules binding、world time 等继续由原表独占 |

对象配置不保存 `initial_*` 状态。为兼容已有不可变能力类型的构造器，读取投影将对应字段从
当前状态表组合，不再读取初始模板。bar 是否存在单独保存为配置标志；锁凭证投影同样取既有表。
玩家 View 继续只返回原许可字段，隐藏机关规则／触发器不因统一配置读取而直接公开。

`initialize_world` 保存 Session 静态配置和 Scene 配置；`initialize_scene` 保存实际对象／NPC 配置，
随后原领域入口安装能力状态、NPCBootstrap、地图。SceneLifecycle 在原事务内调用 finish_scene。
尚未创建场景的静态地图、对象／NPC 初始预测计划保存在 Scene 配置中，仅用于未实例化目标。
finish_scene 将其清除，实际实例此后只从所属事实表读取。保留原只读导航预测和提前／延迟创建策略，
不因读取或提前配置而产生进入事件、访问计数、NPC 模拟。已有实例缺记录时不能退回预测计划。

## 创建、恢复和 Package 读取审计

- SessionCreation → WorldFacts.initialize_world → SceneLifecycle.initialize_session／ensure_scene：
  从已验证的固定 Package 解析合法默认和引用，在原创建事务保存配置及初始事实。
- 已授权移动中的 ensure_scene 只创建尚不存在的目标，沿用原 savepoint；重复 ensure 验证既有记录。
- save／resume → SceneLifecycle.validate_initialized → Session 配置／事实读取；重启不重套模板。
- 旧 Schema 数据迁移只增加空配置表及扩展索引，保留所有旧行／列。没有正确来源时普通读取明确拒绝。
  调用 `StateMachine.upgrade_instance_configs(session_id, exact_package, context=...)` 执行一次性升级：
  授权及核对 Package ID、版本、摘要、三项绑定；只补配置，不调用初始 HP、开关、位置等写入口。
  无法证明对象来源、缺少必要既有事实、升级／COMMIT 失败，整个事务回滚。重复升级不重置事实。
  该 API 不迁移 evaluator、不重新绑定 Session。
- 已消除：能力 config、portable 成员资格、WOS View、OSS 属性／投影、光源识别、机关触发索引、
  环境依赖身份、事件取证和 NPC senses、实际 Scene 元数据／地图的 Package 补字段。
- 合理保留：创建／显式升级；Session pin 和来源审计；Package 固定事件程序；
  显式首次 Encounter／机械组件创建用的 combat foundation；独立静态几何／导航兼容 helper。
  生产实例几何调用始终传 Session ID；现有机械组件恢复／请求不从 foundation 补字段。
- 当前没有独立动态 spawn API；不新增无调用方的生成平台。机关真实 world-source RE ABI、SA-01、
  战斗世界操作、装备效果生命周期等原 OPEN／unsupported 状态保持。

## 容器读取上下文和事务生命周期

ContainerService._rows 一次读取 objects 并建立 `ContainmentState`。
它复制只读行，按原行顺序建立 `container_id → 直接子 ID` 邻接表；不持久化 contents。
subtree 仍返回含根的 set，total_weight 仍返回重量或 None；WOS contents 仍按 object_id 排序。

validate 在同一上下文收集移动子树、旧祖先，require_unbound 复用该数据；relocate 修改候选关系，
更新旧／新父邻接表并清空重量 memo，然后收集新祖先和审查候选。audit 在相关范围内沿索引取子节点，
多个祖先的重量计算复用已经算出的子树。未知重量也记录为 None，但仍遍历检查全部相关后代，
不因未知测量或非 Container 对象而隐藏循环／非法内容物关系。

上下文只在一次操作／列表调用的局部变量中存在，不在 Service、Store 或全局保留。
公开列表共用一个上下文；新的查询／事务重新读取当前可见数据。拒绝、savepoint 或 COMMIT 回滚
不会向后续请求发布索引或重量。只读行防止绕过 relocate 改变关系或数量而保留旧 memo。
没有额外持久化位置、容器内容列表或跨事务重量缓存。

原 Schema29 `objects_container(session_id,container_id)` 对
`WHERE session_id=? AND container_id=? ORDER BY object_id` 的实际计划选择 Session PK 扫描。
Schema39 将同名索引扩展为 `(session_id,container_id,object_id)`，保留非 NULL 条件，
执行计划回归验证使用 objects_container。这与操作内邻接索引分别解决 SQL 读取和内存重复扫描。

## 计数与验证

确定性计数回归：101 节点（箱子＋100 直接内容物）原逐节点扫描需 **10201** 次记录检查；
现在输入扫描／索引构建 **1 次、101 条**。箱子＋内袋＋100 内容物为 **10404 → 102**。
重复 subtree／total_weight 及内袋查询不增加输入扫描；每个节点的定义读取和重量计算均一次。
实际持久配置作为字段来源，未使用严格耗时断言。候选位置连续变更会清空旧重量；
新读取看到事务候选，回滚后的新读取恢复原关系／重量。

ec9c714 历史批次生产代码冻结后：

| 检查 | 实际结果 |
|---|---|
| Fast：Ruff check／format src tests examples | 通过，202 文件格式正确 |
| Focused：Session／实例／Container／Scene／Grid／领域能力／时间协调，共 15 文件 | **440 passed**，166.03s，exit 0 |
| Integration：真实 RE、规则结果准入、机械库存、世界能力、旧库及跨进程恢复／重放、事件取证 | **767 passed**，479.43s，exit 0 |
| 补充 NPC／感知与 Schema 断言，共 10 文件 | **217 passed**，195.69s，exit 0 |
| uv lock --check --offline；Engine／SRD 安装身份 | 通过；均为 0.6.0／d596161060555630019163ab301e1d25fd816549 |
| git diff --check；改动 Markdown 相对链接／代码 fence | 通过 |

三组合计去重为 **1361 个不同用例／64 文件**（三组存在 63 个重叠用例，不重复计数）。
仅有两条既有 Lark 弃用警告。新增实例／容器专项共 18 项，已包含于上述 Focused。

文件清单见本地 `.test-tmp/instances-focused-files.json`、`instances-integration-files.json`、
`instances-support-files.json`；去重收集清单 `instances-final-manifest.json`。
最终日志分别为 `instances-focused-final.log`、`instances-integration-final.log` 和
`instances-support-complete.log`。没有运行仓库全量或声明远端 CI 通过。

新增实例／容器回归覆盖实际 Schema38 生产者、源路径断言、升级 COMMIT 失败回滚、
100 件实际创建／持久化内容物的公共读取和列表一次装配，以及禁用 Package 定义后
Container、Openable 真实检定、Mechanism、Light、OSS、Durability、save／resume 的执行。
保持旧断言的迁移用例现显式调用配置升级；并确认自动 Schema 迁移的新表为空。
两项旧指纹回归将历史 JSON 形状明确排除后来新增且为空的 time_wait，生产指纹代码未改变。

中途失败与兼容实验日志保留于 `.test-tmp`，不作为最终通过证据。初次 Windows asyncio
沙箱阻塞和一次 faulthandler 原生异常运行均未计入；后续在原固定依赖上以仓库内 basetemp
完成重跑，没有跳过测试、取消安全断言或修改事件循环。此前 progress 的数字仍只属于历史批次。


## 2026-10-20 Scoped runtime validation refactor

本批实现采用 Schema40 的事务内派生占格索引和结构验收记录，普通命令由领域
局部准入保护，操作内复用读取，提交不再默认完整空间审查或库存重建。
逐项能力、读取范围、存档兼容、实际计数及本批验证结果见
[系统性重构验收](reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md)。
上文历史测试数字与旧 Schema/待办保持原批次语境，不作为本轮验证结果。
