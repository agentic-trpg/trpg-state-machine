# W-01 GridService 切片进度

## 2026-10-22 GridView 与 PlaceObject 定向读取

本批基线 `a2a2fe0`，分支 `fix/runtime-local-query-reuse`。公开 PlaceObject 的目标占用
检查改用既有格子索引；固定 View 持有自己的双向占用索引、移动／掩体准备，旧 View
不随发布改变。8×8／64×64 固定地图的热 can_step、cover_between 不再复制 24／3776 条
静态阻挡；相关墙段由地图版本绑定的索引过滤，再调用原 RE 精确几何。首次建 View、
显式完整输出及索引准备单列，未重做 StepActor 的 local_topology。
实测口径、正确性、回滚／恢复及限制见[本批验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md#2026-10-22-local-query-reuse)。

## 2026-10-22 运行缓存身份与版本复用

Scene.spatial.record/map/mechanical_map/sensory 已接入地图、局部移动与完整阶段模型。
10／100 个远处静态阻挡下，局部输入编码记录由 10／100 降为 0／0，完整地图复制 1→0，
排序仅余固定的 12 个局部候选格。公开 Light、真实 StepActor 及感知阶段分别计数，
完整必要证据保留编码。旧固定图／候选／保存点／恢复与剩余边界见
[后续验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md#2026-10-22-follow-up-managed-cache-inputs)；历史数字不改写。

## 2026-10-22 后续：场景空间字段与格子读取

已接入解析地图／机械地图、物体位置、占用格和动态阻挡字段；局部拓扑沿已有格子索引
读取必要邻域，角色／物体位置仍由原 Facts 持有，旧 GridView 不修改。新增公开准入规模
用例在增加 100 对象／角色及 20 场景后复用原格子字段；详细计数和验证范围见
[实例归属验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md)。历史测试数字不计入本批。

## 2026-10-22 一致 RAM 输入

GridMaps 的内容键和不可变地图继续复用；编码记录现从统一 RAM／候选范围取得，热命中不再
重读同批 SQL。保存点／旧读／跨连接发布保护保持，卸载和 close 释放自有引用，旧 View 仍固定。
没有声称所有空间／场景算法都已局部增量化。证据见 [实施报告](reviews/SESSION_MEMORY_RUNTIME_IMPLEMENTATION.md)。

## 2026-10-21 文档目标补充（未实施）

Architecture Decision Status **[DECIDED]**；完整 Session 内存运行层 Implementation Status **[NOT IMPLEMENTED]**；Acceptance Status **本次未执行运行验收**。 既有地图／局部空间成果保持；读取输入从同版本 RAM 接入、候选空间及旧 View 隔离、版本一致冷加载仍须后续实现与验收。 统一责任与验收引用 [Session Management](SESSION_MANAGEMENT.md#session-内存运行生命周期与事实访问2026-10-21)，沿既有 C-25／C-27／A2／DS 跟踪，不另造任务体系。本轮仅修改文档，未执行本领域业务或性能测试；下述历史日期／SHA／测试数字保留原证据范围。

## 地图运行表示与查询优化（2026-10-19）

在 `feat/session-management-refactor@a25c35e` 上继续，开始时工作区干净，没有重建分支、
切换 main 或修改其他进行中工作。仓库及适用祖先仍无 AGENTS.md。
本次影响创建／恢复、事务可见性以及所有地图消费者，选择 Focused + Integration，
不依据历史全量记录宣称本次全量通过。

- SessionCreation 原流程经 SceneLifecycle／SessionRuntime，在领域地图保存后准备运行
  表示；延迟场景、原机械场景补建、恢复当前场景均接线，Store 关闭／Session 释放清理。
- GridMaps 每次先读当前事务的实际 state_json，按完整内容和 Store／Session／Scene／
  live 或 template 来源复用；不新增数据库版本字段，不依赖世界版本或 loader 地址。
  两个 LRU 各 64 项及 8 MiB 编码键预算，超预算单项不驻留；候选没有“当前已提交”指针。
- GridView 原字段／grid_json／asdict 和几何语义保留，复用只读解析数据与基础拓扑。
  嵌套集合、墙段和拓扑属性不能修改；旧 View 在更新、回滚和释放后保持原快照。
- 范围查询裁剪候选区域后精确筛选；实体直接检查实际占格；邻格最多生成八个候选。
  120 组参数与旧式全图枚举对照，覆盖不同尺寸／尺度、中心／边界、小数和极大有限范围。
- 公共路径计数：100×100／5 ft 地图中心 5 ft，范围 10,000 → 9 次，邻格
  10,000 → 最多 8 次；五个实际占格的三实体示例只需 3 次判断，不调用范围扫描。
  地图准备后，两次公共 read_scene 加 View 查询及基础 Light／环境消费，额外地图
  json.loads／基础拓扑构建均为 0。不是全部业务 JSON 或规则／Light 工作为零。
- 已盘点地图实际写入口：WorldFacts 两个初始化方法的 INSERT；无生产地图编辑命令。
  SQL 配置候选／回滚测试验证未来受控更新后按可见内容重建，不新增无调用方编辑接口。
- 迁移 WorldFacts、GridReader、Light、Environment 区域校验、Navigation 实际规划、
  RulesAdapter 和实时 EventCapture 的重复读取／基础构建路径；历史证据仍消费历史地图。

当前 Schema36 和 RE／SRD pin 保持。Package 静态兼容 helper 仍可脱离 Store 单次构建，
实际导航使用独立 template 运行表示；缺 live 地图仍拒绝，不回填 Package 默认。
WorldObject 全能力配置、角色／物品完整实例化、RE 固定素材接口和独立动态视觉仍未解决。
具体范围、释放和预算说明见 [GridService](GRID_SERVICE.md)。

### 本轮检查

新增 maps／算法 Focused 首轮：174 passed／1 failed；失败为测试在降低池容量后错误地
要求另一尚未发生淘汰的分区立即缩容，修正为清空后验证，未改变生产淘汰策略。
随后 `test_grid_maps.py test_grid_service.py test_session_management.py test_sessions.py
test_scene_lifecycle.py test_domain_runtime.py`：248 passed（32.49s），退出码 0。
最终检查结果：

| 分组 | 实际结果 | 日志 |
|---|---|---|
| Focused 六文件（地图、Grid、Session 管理／创建、Scene、领域装配） | 248 passed，35.42s，exit 0 | maps-focused-verified.log |
| Integration 22 文件 | 470 passed，181.46s，exit 0 | maps-integration.log |
| 最终 GridView 构造／asdict 兼容保护后，Focused 加空间／导航／Light 复核 | 302 passed，50.67s，exit 0 | maps-final-compat.log |

三组合计覆盖 **718 个不同用例**，后组重叠不重复计数；新增地图测试 **135 项**（含
120 组算法对照），只有两条既有 Lark 弃用警告。所有测试使用原固定依赖，Windows
真实异步集成沿用限制外执行，日志在 `.test-tmp/maps-*.log`，未运行无关全量。

Integration 文件：test_spatial_world、test_object_spatial_service、test_navigation_activities、
test_navigation_boundaries、test_navigation_faults、test_navigation_migration、test_world_lights、
test_environment_service、test_environment_review_regressions、test_environment_snapshot、
test_environment_versions、test_combat_foundation、test_dnd_runtime、test_combat_closure、
test_real_checks、test_history_free_rules_adapter、test_cross_phase、test_rf4_position_evidence、
test_event_local_evidence、test_perception_conditions、test_world_example、test_integration，
均为 tests/ 下 .py 文件。命令使用 `.venv/Scripts/python.exe -m pytest -q` 加对应文件，
独立 basetemp 和 faulthandler_timeout=60。

实际载入 a25c35e 源码计数与当前准备后的 View 对比：范围 JSON／拓扑／候选次数
103／2／10000 → 0／0／9；实体 112／11／10000 → 0／0／3；邻格
105／3／10000 → 0／0／8。完整条件和属性／距离计数见设计说明，原始记录为
`.test-tmp/maps-call-counts.json`。这不等于没有 SQL 读取或没有动态投影成本。

Ruff check src tests、Ruff format --check src tests（185 文件）、git diff --check 和
本轮文档链接／代码围栏检查通过；最终 diff 按生产路径、测试和文档审阅。
pyproject.toml、uv.lock、SQL Schema／迁移不变；新增 Store 内存字段不改变数据库结构。

下文为原 GridService 提取批次的历史记录，非本轮测试／Schema 状态。

2026-10-18；分支 `feat/world-grid-service`，基线
`0554add35c5dbe3c5405fe9a31e2ce3aad81c7c7`。执行时 fetch 确认 origin/main 与 HEAD
相同，初始工作区干净；本批不合并 main，不修改 RE 或顶层架构仓库。

已检查仓库、src／tests／docs 以及适用祖先路径，没有适用 AGENTS.md。验证入口沿用
README 的 pytest 全套、Ruff check／format、git diff --check。架构依据读取当前工作区的
顶层 MODULE_CONTRACTS、STATE_MACHINE_ARCHITECTURE、WORLD_SUBSYSTEM_DESIGN，
并核对本仓库 World／OSS／Light／Environment／Navigation／SceneLifecycle 说明与待办。

## 本批已完成

| 范围 | 当前状态 |
|---|---|
| 网格／锚点读取、探索拓扑、原静态／Light 输入 | 从 WorldHandler 提取，调用点已迁移，旧重复方法已删除 |
| 当前空间组合／占用／实体占格／距离／邻接／范围候选 | 不可变 GridView，单读取事务，当前 OSS 贡献与 Character 锚点 |
| 通行几何／静态遮挡／独立掩护／二维模板 | 复用固定 RE 公开纯计算接口；动态视觉缺口显式 unsupported |
| 门／对象移动与旋转／事务内变化／回滚／并发读取 | 真实事实与公开命令测试，不增加缓存或第二份位置权威 |
| 原探索、导航授权、场景初始化、交互与机械输入 | 保留原行为；没有探索战斗额度结算或自动耗时 |
| WorldHandler | 保留操作、能力／授权、移动／通路及事务协调；W-01 整体仍未完成 |

模块职责与准确支持范围见 [GridService](GRID_SERVICE.md)。Scene 生命周期切片的历史
记录继续保留在 [原进度](SCENE_LIFECYCLE_SERVICE_PROGRESS.md)。W-01 仍映射顶层
A2／C-25／DS-01、DS-02；本批不关闭 SA-01、Terrain、Effects 或其他 World 工作。

## 现有能力限制与跨系统工作

以下只链接对应模块的既有待办登记，本页不另建平行完整 Backlog：

| 未完成项 | 主要登记及后续触发条件 |
|---|---|
| 独立动态视觉、物体贡献的正式规则消费 | [OSS 后续集成](OBJECT_SPATIAL_SERVICE_PROGRESS.md#gridservice-消费与后续接入2026-10-18)；RE 有可准确表达各通道的稳定契约并固定版本后接入 |
| 角色多格／Size、战斗空间与移动规则 | [支持矩阵](SM13_SUPPORT_MATRIX.md#gridservice-后的空间跨系统待办)；Character／RE 共同确定当前角色占格与经过角色的规则输入后接入 |
| Activity／Effect 新契约适配和真实端到端 | [Navigation 后续](GC2_NAVIGATION_ACTIVITIES.md#gridservice-后的-activityeffect-依赖) 和支持矩阵；新契约实际发布并固定依赖后执行真实验证 |

动态 vision_occlusion 已能作为对象贡献读取，但当前 RE 完整视线组合不受支持；角色
仍为单锚点。这些不是“等待后就算通过”的测试，也未添加 mock 或占位协议掩盖缺口。
当前可运行的既有 RE 检查、攻击、空间和导航集成继续参加全量回归。

## 验证记录

新增 `tests/test_grid_service.py`：**23 passed**（6.27s，exit 0）。覆盖坐标规范化、非法
范围／模板、缺网格／位置、墙／切角、物体多格和旋转、共享门双侧状态、通道区分、掩护
来源、探索无机械资源／时间扣除、隐藏对象和未授权地图，以及真实并发命令期间的快照。
SQLite query_only 与全部表快照验证查询无写入；事务内更新／回滚验证没有陈旧缓存。

首轮新增夹具的时钟方法、门槛切角与未初始化 Session 外键设置错误已修正，不计为通过。
受限环境的旧真实 RE 测试停在 Windows asyncio socketpair；faulthandler 定位后停止
任务自身测试进程，使用相同环境与固定依赖在限制外重跑。没有跳过该集成或修改生产逻辑
绕过它。

最终全量四个分区全部 exit 0：

| 分区 | 结果 | 耗时 |
|---|---|---|
| 0 | 391 passed | 324.41s |
| 1 | 448 passed | 360.35s |
| 2 | 467 passed | 328.50s |
| 3 | 386 passed | 205.59s |
| 合计 | **1692 passed，无失败／跳过** | 各分区并行运行，不将耗时相加作为墙钟时间 |

包含新增 GridService 23 项及既有真实 RE 攻击／检定／跨阶段、场景、导航／故障／恢复、
门锁／物体／容器／机关、Light／Environment、授权感知、NPC 与历史存储迁移测试。
每个分区只有两条既有 Lark 弃用警告。没有把这些固定契约集成推定为未来 Activity／Effect
契约已经打通。

最终 Ruff check .、Ruff format --check .（252 文件）、git diff --check 通过；8 个变更
Markdown 的代码围栏及 79 个本地文件链接目标检查通过。依赖／锁文件／SQL migration
无变更。仅本批文件选择性提交并正常推送当前分支，不更新或合并 main；交付 SHA 见最终报告。

全量命令按文件名排序作四个互不重叠的分区（每次设置 gridPartition 为 0、1、2、3；
临时目录与日志各自独立）。只清除当前测试进程的 PYTHONPATH，避免历史迁移子进程误导入
当前 SM；pytest 的 pythonpath 配置仍使用当前 src。未修改用户持久环境或依赖。

```powershell
$env:PYTHONUTF8 = '1'
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
$gridPartition = 0
$gridFiles = @(Get-ChildItem tests -Filter 'test_*.py' | Sort-Object Name)
$gridSubset = for ($gridIndex = 0; $gridIndex -lt $gridFiles.Count; $gridIndex++) {
    if ($gridIndex % 4 -eq $gridPartition) { $gridFiles[$gridIndex].FullName }
}
.venv/Scripts/python.exe -m pytest -q @gridSubset --basetemp=.test-tmp/grid-suite-$gridPartition -o faulthandler_timeout=60
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
git diff --check
```


## 2026-10-20 Scoped runtime validation refactor

本批实现采用 Schema40 的事务内派生占格索引和结构验收记录，普通命令由领域
局部准入保护，操作内复用读取，提交不再默认完整空间审查或库存重建。
逐项能力、读取范围、存档兼容、实际计数及本批验证结果见
[系统性重构验收](reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md)。
上文历史测试数字与旧 Schema/待办保持原批次语境，不作为本轮验证结果。
