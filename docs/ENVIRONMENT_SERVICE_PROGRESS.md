# EnvironmentService 实施记录

## 2026-10-22 后续：实例来源／防护／结果字段

现有 area／cell／unit／height 消费者接入 Scene.environment；区域校验用目标场景存在性
及已解析边界。原来源／防护／提供方版本协议、opt-out、可见性和高度语义不变，
公开单格照明与完整投影另由 Scene.lighting 持有。回滚／旧引用／恢复／有界淘汰及
局部失效的最终证据见 [实例归属验收](reviews/RUNTIME_INSTANCE_OWNERSHIP.md)。

## 2026-10-22 RAM 输入与生命周期

基础事实／依赖版本由统一 RAM／候选读取，双版本与提供方登记协议不变；旧读取事务不能
在外部提交后发布旧的当前派生结果。卸载／重建／close 释放对应环境派生值，公开可见性与
授权检查继续逐次执行。具体生产链、故障和测量见 [实施报告](reviews/SESSION_MEMORY_RUNTIME_IMPLEMENTATION.md)。

## 2026-10-21 文档目标补充（未实施）

Architecture Decision Status **[DECIDED]**；完整 Session 内存运行层 Implementation Status **[NOT IMPLEMENTED]**；Acceptance Status **本次未执行运行验收**。 环境双版本与按需缓存保持；统一 RAM 输入、候选依赖、提交后发布和生产提供方各自验收，不能由现有缓存宣称完整内存层。 统一责任与验收引用 [Session Management](SESSION_MANAGEMENT.md#session-内存运行生命周期与事实访问2026-10-21)，沿既有 C-25／C-27／A2／DS 跟踪，不另造任务体系。本轮仅修改文档，未执行本领域业务或性能测试；下述历史日期／SHA／测试数字保留原证据范围。

## 环境缓存权威版本管理（2026-10-19，Schema 36）

基线为本分支已推送的 `382a4aeb17698a27c0966cd2a72821c039f0ecbb`；当前分支仍为
`feat/environment-derived-snapshot`，开始时工作区干净。复查仓库及适用祖先，没有 AGENTS.md
或 check-fast 配置，采用实际 pytest、Ruff check／format、diff 检查。不合并 main。
以下是本批当前机制；下一节“派生 Snapshot 与显式降水保护”是上一批的历史记录。

审计实际 Environment 模型／组合／公开披露、WorldFacts 写入、OSS 当前属性／资格、Grid
当前配置、WorldTime.set_tick、角色与物品位置／容器、开关／Durability／Mechanism 采用、
CommandRuntime savepoint／OCC／重放、SQLite migration 与 provider seam。基础来源没有 tick
期限；当前 Effects 环境来源及 WO 当前属性是注入接口，不能把 PackageEffects 或角色
EffectState 直接当作已经接通的环境提供方。

本批实现：

- Schema36 的 `scene_environment_versions` 只存两个权威输入版本：source_version、
  protection_version；不新增最终缓存版／风向版，不用 world_version。迁移已知场景补零，
  新 Session 为 Package 全部 Scene 建零值；历史未实体化 Scene 缺行按默认零读取，写时建立。
- 环境事实、静态防护和 Grid 的同步表触发器覆盖受控命令、导入、初始化及内部 SQL。
  环境来源变化推进来源版；静态防护、Grid 推进防护版。Wind 输入（包括跨提供方同优先级
  冲突所需的完整值）、范围、priority、observable 改变时两版都推进。只改雨强不改防护版。
- 新正式适配器 VersionedEnvironmentEffectsProvider／VersionedProtectionProvider 声明
  SQLite owner 的完整行／字段依赖；bind_providers 在可信写事务登记同步触发器及清单，
  不复制 owner 输入。对象姿态／地面归属／当前组件依赖自动登记；额外 actor／容器／属性
  依赖须明示。跨 Scene 物体写入覆盖原／目标 Scene。reader 仍拥有属性政策与生命周期。
- Effects JSON 来源变化自动比较 Wind 子输入；计算 Wind 的其他 owner 输入标 wind_source。
  明确边界／连续变化通过 EnvironmentTimeWatch 接入同一个权威 tick 写入，不轮询或按所有
  tick 无条件失效所有场景。环境不新增天气时钟／期限镜像。
- 无版本第三方、未登记／不匹配或显式禁用的路径禁止跨查询复用；没有退回逐项比较来
  继续启用共享缓存。正式登记路径确实通过版本命中，未把全部正式输入都归入例外。
- 两层仍为 32 项防护中间结果＋512 项按需完整结果（Cell 或显式高度区间）。缓存先查两版
  和绑定，命中不读来源／物体／风向／环境 Grid；雨强变化只重合成，不扫描防护。
- 公开、内部 project、区域、区间、unit 走统一版本入口。内部 project 自动加入／建立读取
  事务；公开入口继续重查授权和可见性。区域复用同次授权 topology 的范围，环境校验不再
  重复准备地图；授权所需当前 Light／Grid 仍按原语义每批准备一次。
- 写事务候选隔离；只读暂存仅成功 COMMIT 后校验当前版本再发布。旧 WAL 结果过时不发布，
  不改贴新版本；回滚版 N 不进入共享缓存，随后另一输入提交为 N 也不会误命中。
  发布当前版时剔除同身份旧版，其他 Session／绑定仍受容量限制。清空／重启不改权威版本。
- 迁移及依赖登记均原子，登记自身还用 savepoint；同登记重试为空操作。持久触发器清单在
  重开时校验，丢失／部分注册拒绝打开。原环境、事件、回执、Package pin、RNG、tick 和
  历史 Schema33／两类 Schema34／Schema35 存档事实不重写。既有测试只更新当前 Schema
  期望；真实历史版本断言及数据保持断言保留。
- 默认空提供方未发生政策变化时，登记也保持完全空操作；正式提供方开合／损毁依赖和
  回滚通过真实命令及受控 HP 采用 seam 验证，不能以第三方不缓存替代正式路径。

确定性计数（`test_version_hit_rain_and_wind_call_counts`，正式 SQLite owner fixture）：

| 路径 | 来源扫描 | 防护物体／属性准备 | OSS 投影 | 环境合成 |
|---|---:|---:|---:|---:|
| A、A、B、B 累计 | 2 | 1 | 1 | 2 |
| 已有 Cell 命中 | 0 | 0 | 0 | 0 |
| 仅雨强变化后查询 A | 1 | 0 | 0 | 1 |
| Wind 输入变化后查询 | 1 | 1 | 1 | 1 |

命中同时断言无环境 Grid 准备；将来源／物体／环境地图读取直接替换为抛错函数，
公开／内部点、内部／公开高度区间、整场景区域命中仍成功。权限错误继续拒绝。
新增专项还覆盖多版跳跃、批量／重放／拒绝／冲突、同数值回滚版、COMMIT 故障、旧读取、
Effects 时间边界与连续输入、跨 owner 风速冲突、真实携带／容器／落地、actor 依赖、
跨 Scene、静态防护／Grid、迁移故障／恢复、登记失败、触发器丢失与容量清理。

新增专项 `tests/test_environment_versions.py`：**25 passed**，9.83 秒，exit 0。
环境／披露／Snapshot／OSS／Grid 中途综合 focused 为 172 passed；另有历史环境／机关
迁移 13 passed（86.73 秒）、旧机关迁移 2 passed，均 exit 0。最终完整收集为 1,747 项，
79 个文件按 20／20／20／19 分区。早期全套发现的失败均来自旧迁移测试结构期望：
Schema33 夹具保留了 ENV35／36 新增表，已先移除后再构造真实目标结构；历史恢复测试的
新增表清单补上两张元数据表，并新增版本默认零与绑定表为空的断言。没有弱化原数据、
故障回滚、完整性、回执重放断言。修正后的前轮四区 484／440／397／424 全通过；
其后默认空提供方空操作修正与两项新增专项通过。最终冻结代码／测试后完整复验：
四区分别 **486 passed（314.65 秒）、440 passed（192.57 秒）、397 passed（214.73 秒）、
424 passed（280.62 秒）**，合计 **1,747 passed**，四个进程均正常通过。复验期间只修改
文档，完成后对照启动前 SHA256 清单确认全部源码、SQL 和测试没有变化。

实际命令（仓库 cwd，最终四个进程分别设 `$versionPart = 0,1,2,3`）：

```powershell
.venv/Scripts/python.exe -m pytest -q tests/test_environment_versions.py --basetemp=.test-tmp/env-version-final-25 -o faulthandler_timeout=60

$versionFiles = @(Get-ChildItem tests -Filter 'test_*.py' | Sort-Object Name)
$versionSubset = for ($versionIndex = 0; $versionIndex -lt $versionFiles.Count; $versionIndex++) {
    if ($versionIndex % 4 -eq $versionPart) { $versionFiles[$versionIndex].FullName }
}
.venv/Scripts/python.exe -m pytest -q @versionSubset -o faulthandler_timeout=60 --basetemp=.test-tmp/env-version-delivery-$versionPart

.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
git diff --check
```

Ruff check／format（257 files）、diff、五份文档本地链接与 code fence 检查通过。
全套使用原生本地 socket 权限执行 Engine 异步测试，没有调用外部服务。
固定 Engine／SRD 与 pyproject／uv.lock 不变；只有既有 lark 弃用警告。

剩余成本和边界：命中保留两个索引读取（两版、提供方绑定）及公开授权／Light 准备；
冷结果仍读取／解析来源、物体属性和 Grid。完整 Wind、相关对象组件及 Grid 使用保守
失效，可能重算未实际改变的几何。正式 reader 依赖声明是可信 owner 契约，不能读取未声明
的可变内存／I/O；生产 WO／Effects 法术适配仍是原有接线边界。携带／容器对象按现有 OSS
不自动生成地面防护；仅其额外来源依赖明确读取这些状态时登记并失效。
**命中通过版本检查省掉输入扫描已在本批解决；未命中输入构建的 Runtime 长期复用留后续。**

## Environment 派生 Snapshot 与显式降水保护（2026-10-19）

本轮分支 `feat/environment-derived-snapshot`，基线为本轮 fetch 验证的最新
`origin/main@d0fda62eba8f975232f47abe64be95660a791110`，初始 HEAD 与 main 相同、工作区干净。
仓库、适用祖先及子目录没有 AGENTS.md，没有 check-fast 脚本／配置；使用实际 pytest、
Ruff check／format、diff 检查。只交付本分支，不合并 main，不修改固定依赖或 Schema。

先审计 Environment 模型／命令、WorldFacts JSON、CommandRuntime savepoint 与提交、
OSS 当前状态／属性与投影、GridReader 当前配置／来源、Effects／UnitHeight provider、
SessionAccess pin 与公开披露、WorldHandler 可见性及 Light 的真实准备路径；
再在 [服务说明](https://github.com/agentic-trpg/trpg-state-machine/blob/382a4aeb17698a27c0966cd2a72821c039f0ecbb/docs/ENVIRONMENT_SERVICE.md#snapshot-依赖设计2026-10-19实施前审计)
形成依赖矩阵，随后实施。顶层 MODULE_CONTRACTS／World 设计保持所有权约束；
其中早期 NOT IMPLEMENTED 表述不是本仓库实际代码现状或本轮完成证据。

本轮实现已完成：

- 两层独立有界 LRU：32 项 OSS 场景／风向防护，512 项已完成 Cell／height 结果。
  只缓存实际算出的结果，不展开整张环境地图，不把屋顶覆盖等同完整环境相同。
- 当前不可变输入验证，Session／Package／Scene／Grid／姿态／当前属性／来源与必要
  参数隔离；不依赖 TTL、变化通知或 world_version。天气与 Effects 不使不变 OSS
  几何重算；普通移动、明确无贡献物品拾取、时间与无关锁字段输入不变可继续命中。
- OSS 输入准备与计算分离，provider 每次读取；多方向／区域使用同一次读取结果。
  外部路径可 `environment_cacheable=False` 绕过跨查询复用。
- 只读事务有界暂存，成功 COMMIT 后发布；写事务／无管理 scope 绕过共享缓存。
  失败、回滚、提交失败、旧读取事务及 clear 期间的暂存不会污染新查询。
- 显式 rain/snow/sleet/hail 的对象与静态防护，共用位置／旋转／裁剪算法。
  旧 RainProtection／rain_cells 仍只声明 rain；新可选字段缺省不改变旧命令指纹，
  无新增持久表／迁移。保留 local／outdoor、未知完整性和大气条件／地面暴露区别。
- WindBoundary 自身格排除、旋转来向、距离、边界与高度空隙保持。
- 区域查询一次观察者／Grid／Light 准备，每格继续授权可见性判断，任何失败整批拒绝。
  缓存命中仍检查当前授权与披露，隐藏来源不变成 ID／状态／高度分段侧漏。

确定性调用次数证据（`tests/test_environment_snapshot.py`，实际执行断言）：

| 路径 | 合成次数 | 场景 OSS／屋顶投影次数 | 其他证据 |
|---|---:|---:|---|
| A、A、B、B，屋顶一次覆盖 16 格 | 2 | 1／1 | 两格温度／雾烟不同；最终仅缓存这 2 格；4 次公开查询仍各自准备并验证可见性 |
| 同格天气雨强改变后再查 | 累计 2 | 仍 1／1 | 环境即刻更新，几何复用 |
| 5 格区域查询，再查同区域 | 累计 5 | 共享按风向投影 | 每批 visibility／Light 准备各 1 次，逐目标检查各 5 次 |
| 禁用跨查询复用后，同格再查两次 | 增加 2 | 场景投影增加 2 | 不使用不确定外部路径的缓存 |

30 项新增测试还覆盖来源增加／删除／替换及范围、priority、exposure、observable，
Effects 移动／修改／结束／不可用，当前防护、真实移动旋转／开关、内部 HP 采用与回滚，
四类降水显式子集和旧 JSON，Session／Scene／Package／高度／Grid，故障／提交失败、
旧 WAL 读取、公开授权与 Dark 光照、隐藏范围、有界淘汰、清空与重启一致性。
最终环境／披露／OSS／Snapshot focused checks 为 **130 passed**（41.72 秒），
包括 30 项新增专项，exit 0。全套原生四区为 **432／399／446／444 passed**，
合计 **1,721**，全部 exit 0，耗时 208.69／175.00／246.24／213.39 秒。
全套覆盖无关物品拾取优化后的实现；随后保留重构前“共享门也先验证显式属性类型”的
兼容校验，并新增 1 项回归，最终 130 项 focused 全部再验证。当前 collection 为 1,722；
不将后加的 1 项计入此前全套数字。78 个测试文件按 20／20／19／19 分区，无重复或遗漏。

实际命令（cwd 为本仓库，Python／Ruff 为本地 `.venv/Scripts`，basetemp 各自独立）：

```powershell
.venv/Scripts/python.exe -m pytest -q tests/test_environment_snapshot.py tests/test_environment_service.py tests/test_environment_review_regressions.py tests/test_object_protection.py --basetemp=.test-tmp/env-snapshot-delivery-focused -o faulthandler_timeout=60

# 四进程分别使用 $snapshotPart = 0、1、2、3；完整文件集合已核对。
$snapshotFiles = @(Get-ChildItem tests -Filter 'test_*.py' | Sort-Object Name)
$snapshotSubset = for ($snapshotIndex = 0; $snapshotIndex -lt $snapshotFiles.Count; $snapshotIndex++) {
    if ($snapshotIndex % 4 -eq $snapshotPart) { $snapshotFiles[$snapshotIndex].FullName }
}
.venv/Scripts/python.exe -m pytest -q @snapshotSubset -o faulthandler_timeout=60 --basetemp=.test-tmp/env-snapshot-final-$snapshotPart

.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
git diff --check
```

Ruff check／format（254 files）及 diff 检查通过；四份文档本地链接与 fenced code
完整性检查通过。全套使用原生本地 socket 权限执行 Engine 异步测试，无外部服务调用。
只有既有 lark 的 sre_parse／sre_constants 弃用警告。实际安装 Engine／SRD 均为 0.6.0，
direct_url commit_id 均为 `d596161060555630019163ab301e1d25fd816549`，与固定依赖相同，
pyproject.toml／uv.lock／存储迁移无改动。

已知限制：当前无专用依赖版本，命中仍需读取／校验来源、对象与 provider，比较成本随
输入大小增长，Grid JSON 读取／规范化当前还可能逐对象重复；保守 Scene 级身份可能使
无实际几何变化的开关／Grid 变化失效。
缓存容量按项数限制，单项仍受现有来源／几何预算约束，未宣称生产内存或延迟指标。
WO／Effects／Package 生产适配及角色实际高度仍是既有外部接入边界。
不实现积雪、融雪、结冰、冰雹伤害、三维地图、角色交互或认知重构；
Environment Snapshot 本轮完成，Light 跨查询 Snapshot 保留后续 Light 重构范围。

以下是此前批次的历史记录，保留其各自基线、范围及验证结论。

日期：2026-10-18。仓库：`agentic-trpg/trpg-state-machine`。
分支：`feat/scene-environment-service`，基线为 fetch 后确认的最新
`origin/main@9ce60cbe4fdbf91dce6ffef811969482369fb08a`，工作区开始时干净。
不合并 main。仓库及适用祖先路径未发现 AGENTS.md，没有额外 CI/check-fast 入口；
遵循 README 的 pytest、Ruff check/format 和 diff 检查。

读取当前总控 `MODULE_CONTRACTS.md` v0.17 的领域／Scene／环境责任、
`STATE_MACHINE_ARCHITECTURE.md`、`WORLD_SUBSYSTEM_DESIGN.md`，并检查本仓库 World W1–W8、
OSS 总体／实施记录和 WorldTimeService 记录。目标契约不是已实现 API；独立实现 Scene
Environment 不要求 Scene 门面或 Grid/Terrain 重构。检查了真实 CommandRuntime、
SessionAccess、WorldHandler、WorldFacts、SQLite migration、EventCapture／Outbox、
PackageEffects、CharacterFacts 的当前数据能力，未升级 RE/SRD pin。

## 实现与行为

新增 environment_models / environment_commands / environment，Schema 34 追加空环境表，
WorldFacts 读写，StateMachine／WorldHandler 实际装配。强类型受控初始化、基础条件、
局部来源添加／替换／移除、静态防护和显式批量场景修改经统一命令事务执行。
批量先验证全部候选；一次最终变化一个版本和 environment.changed；净空操作无版本／事件，
仍记录回执。未访问／未初始化场景修改不被后续初值覆盖。

组合规则、只读查询、未知／不支持状态、授权与披露、接口和完整生产接入边界见
[服务说明](ENVIRONMENT_SERVICE.md)。测试使用真实服务／Session／SQLite／命令路径；
WO、Effects、角色高度提供方明确为契约 fixture，不代表这些外部组件已生产接通。

查询测试对比全部数据库表及 total_changes，并执行 `PRAGMA query_only=ON`。
真实 OSS 测试覆盖四向来风、静态／动态挡雨、[0,2) 与 [4,6) 中间空隙及上界不含。
Effects fixture 验证自然薄雾与法术浓雾同时存在、移动和移除后自然来源保留。
新解释器读取真实存档、resume、旧命令重放；历史 `main@9ce60cb` 解释器建立 Schema33
存档后升级，验证每个旧表／列／行不变、迁移故障原子回滚及旧 WorldTime 绑定／tick 保留。
旧回归测试将“当前存储版本”期望从 33 更新为 34；历史迁移的新增表白名单加入
scene_environments，并明确断言旧存档中该表为空。历史版本、故障注入目标和旧行保留校验不变。

## 验证记录

`uv run --locked` 最初因沙箱 uv 缓存权限和 hatchling 下载 DNS 不可用未完成，
随后使用仓库已有 `.venv/Scripts/python.exe`（相同锁定依赖环境）直接运行。
实际命令与最终结果：

- `.venv/Scripts/python.exe -m pytest tests/test_environment_service.py -q
  --basetemp=.test-tmp/environment-expanded-final`：**40 passed**，exit 0。
- 原生完整套件按 PowerShell 实际排序后的 `tests/test_*.py` 文件分四区，
  分区 0／1／2／3 分别 **364 / 420 / 362 / 398 passed**，全部 exit 0，
  合计 **1,544 passed**；耗时 224.14 / 148.26 / 139.07 / 237.73 秒。
  核对 70 个文件的分区并集、无重复、各区收集数与通过数完全一致。
  初次失败均为历史迁移的当前版本／新增表断言未同步，修正后重跑受影响分区；
  已通过分区 2 不包含随后修改的测试文件，未重复计入。
- `python -m ruff check .`、`python -m ruff format --check .`、`git diff --check`：
  全部通过，234 个文件格式一致。
- 五份更新 Markdown 的本地链接／fence 校验通过；服务文档 Python 示例在真实 Session
  中执行初始化及查询成功。
- `.venv/Scripts/python.exe examples/world_station.py`：exit 0，真实新进程恢复及回执重放；
  终点 scene.workshop / 1,0 / world_version 32 / event_seq 49。
- 实际 Engine / SRD 均为 0.6.0，direct_url commit_id 均为
  `d596161060555630019163ab301e1d25fd816549`；pyproject.toml / uv.lock 未改。
- 仅有既有 lark `sre_parse` / `sre_constants` 弃用警告。

完整分区的实际选择与调用（各区使用独立 basetemp，原生并行运行）：

```powershell
$testFiles = @(Get-ChildItem tests/test_*.py | Sort-Object Name)
$members = @(for ($index=$part; $index -lt $testFiles.Count; $index+=4) {
    $testFiles[$index].FullName
})
.venv/Scripts/python.exe -m pytest -q -x @members `
    --basetemp=.test-tmp/environment-shard-$part `
    -o faulthandler_timeout=45 --durations=10
```

## 交付与共享工作区保护

提交前检测到外部操作把共享目录切到 `feat/world-mechanism-service`，HEAD 仍为原基线。
保护检查在暂存前中止，没有在该分支暂存／提交。把本次 38 个任务文件按 SHA256 保存，
复制到 `.test-tmp/environment-delivery` 的独立 Git 工作树，继续交付原定
`feat/scene-environment-service`；共享目录分支及所有文件保持不动。
完整回归结束后核对共享文件与交付快照无差异；隔离工作树明确使用自己的 src 导入，
再次运行 **40 passed**（20.69 秒），Ruff／格式／diff 检查通过。
仅暂存上述任务文件，提交推送原定分支，不合并 main。具体提交及远端身份随最终交付报告提供。

## 631d957 审查修复：批量指纹与隐藏范围披露（2026-10-18）

工作位置为上述原分支工作树，开始时干净；fetch 后本地／远端原分支均为
631d95752101f9faeb286af1b75f4b812fa43ca6。重新检查适用 AGENTS.md（未发现）、
现有注册表、wire 协议、指纹／回执、环境事实和公开／内部查询。不改共享目录的
feat/world-mechanism-service 或其改动，不合并 main。

先新增并实际运行 `tests/test_environment_review_regressions.py`：修复前
**7 failed / 2 passed**。失败明确覆盖 Add/Replace 成功和拒绝回执被冒用、指纹类型碰撞、
无类型历史回退和公开点／范围探测；保留这些失败断言后修复生产代码。

环境编码由 environment_operation_to_data 统一生成，复用已有 ENVIRONMENT_COMMANDS
和带标签 wire 联合，保留操作类型／内容／顺序；环境单操作和其他已合并命令历史编码不变。
旧批量回执的类型歧义不可恢复，明确拒绝无类型哈希回退，保留原记录供受控核对。

四个公开查询统一使用整个 Scene／字段的披露规则；受隐藏来源影响的字段及依赖结果
全场景 unknown，贡献为空，关联防护状态同样屏蔽。公开分段不使用隐藏范围；内部投影
继续真实合成。更新原范围测试为“保留查询、返回场景统一 unknown”，未删除查询接口。
新增 SQLite 全表／total_changes／query_only 断言验证查询只读，以及隐藏来源移动、
数值／身份变化、公开来源混合时的点／区域／范围／单位输出一致性。

实际验证（使用共享仓库现有 `.venv/Scripts/python.exe`，工作目录为本分支工作树）：

- `python -m pytest tests/test_environment_review_regressions.py -q
  --basetemp=.test-tmp/environment-review-red`：修复前 **7 failed / 2 passed**。
- `python -m pytest tests/test_environment_review_regressions.py tests/test_environment_service.py
  -q --basetemp=.test-tmp/environment-review-confirmed`：**50 passed**（12.40 秒）。
  随后补强恢复及分别只改变隐藏身份／数值／范围的断言，新回归单独执行
  `--basetemp=.test-tmp/environment-review-final-focused`：**10 passed**（2.50 秒）。
  原有 40 项和最终增强的 10 项均包含在下述最终全套中。
- 全套沿用上述 PowerShell 按文件排序、索引 mod 4 的分区算法，实际调用
  `python -m pytest -q -x @members --basetemp=.test-tmp/environment-review-shard-$part
  -o faulthandler_timeout=45 --durations=10`。最终分区 0／1／2／3 为
  **441 / 375 / 388 / 350 passed**，全部 exit 0，合计 **1,554 passed**；
  耗时 309.46 / 315.20 / 132.52 / 243.23 秒。核对 71 个测试文件分区并集无遗漏／重复，
  通过数与全套收集数完全一致，覆盖命令／历史指纹、OSS、权限、持久化和恢复。
- 分区 2 首轮受到外层 PYTHONPATH 指向当前 src 的影响，Schema8 解释器误读新源码；
  只移除测试进程外层 PYTHONPATH，保留 pytest 项目自身的 `pythonpath=src`，
  确认旧解释器实际导入 `.test-tmp/legacy-sm8/src`、存储版本为 8，再以
  `--basetemp=.test-tmp/environment-review-shard-2-clean-env` 完整重跑该分区通过。
  没有改历史测试或生产代码来接受该环境错误，失败轮次不计入最终合计。
- `python -m ruff check .`、`python -m ruff format --check .`、`git diff --check`：
  全部通过，235 个文件格式一致；两份 Environment Markdown 链接和 fence 检查通过。
- 最终回归期间核对 3 个生产文件及 2 个测试文件 SHA256 未变。仅有原有 lark
  `sre_parse` / `sre_constants` 弃用警告。未改 Schema、Package、依赖 pin 或其他消费者。

本次仅提交上述 3 个生产文件、2 个测试文件、2 份 Environment 文档，并推送
feat/scene-environment-service；不合并 main，具体提交号随最终报告提供。

## 与最新 origin/main 整合及 Schema35（2026-10-18）

用户随后明确授权把最新 main 整合进原 ENV 分支，不授权把 ENV 合并到 main。
fetch 后 main 为 `afe6871f550fef395ee48b3af5c7cb7f712071f7`，原 ENV 分支为
`06c7af8fedc7a7db89f4b8f78a60b61bf9d37b4d`。适用 AGENTS.md 未发现。
原交付 worktree 起始干净，将其在原 HEAD 分离后，在新的干净
`.test-tmp/environment-main-integration` worktree 检出原 ENV 分支；
`git merge --no-ff --no-commit origin/main` 仅产生 migration34 的 add/add 冲突。
原目录分支、HEAD 和全部 38 个既有未提交文件保留，用执行前 SHA256 快照核对。

解决方案：逐字保留 main 的 `_migration_34.sql`；ENV 移至 `_migration_35.sql`，
user_version=35。存储加载器按 1～35 顺序执行；在既有 SQLite 迁移锁／事务内部，
按环境表、机关表、键和激活索引识别两种真实 Schema34。旧 ENV34 保留环境 JSON，
补齐原样机关 migration34，随后 ENV35 复用已验证的环境表。main34 不重复映射机关；
未知／部分结构无写入拒绝。失败回滚包含兼容修复、后续 DDL、所有数据和版本。
当前版本测试期望同步为35，历史版本断言和旧行／旧列保留断言保持原语义。

逐项核对自动合并的 CommandRuntime／注册表／历史指纹、WorldFacts、WorldHandler、
Package、OSS 候选校验和防护投影、WorldTime：保留 ENV 批量类型编码和场景／字段
公开披露修复，同时保留 main 的机关操作／生命周期、事件触发、顺序候选与定时重置。
Package `/9` ENV 预留与 main `/10` 机关模型均保留；外部 Effects／角色高度 provider、
PKG-01／SESSION-01 自动环境导入等边界没有新增生产接线。

新增 `tests/test_environment_mechanism_migration.py`，通过 `git archive` 分别运行
`9ce60cb`、`afe6871`、`06c7af8` 的真实旧解释器，创建两个 Session、已激活／未激活机关、
命令回执和已推进时间；ENV34 另含已访问与未访问场景基础条件／局部来源。
验证所有旧表／列／行、环境数据和完整机关状态保留、重启无变化、Session 隔离、
新环境命令可用、三线程旧 ENV34 同时打开只升级一次；分别在34复制后和35末尾
注入失败，确认 SQLite schema／全表数据／版本完全回滚并可重试。
新数据库升级／失败空库、未知／部分结构拒绝也有实际测试。

实际验证命令（cwd 为新 worktree；专项使用共享 Python，全套使用下述独立环境）：

- `python -m pytest -q -x tests/test_environment_mechanism_migration.py
  tests/test_mechanism_migration.py tests/test_mechanism_service.py
  tests/test_environment_service.py tests/test_environment_review_regressions.py
  --basetemp=.test-tmp/integration-focused -o faulthandler_timeout=45 --durations=10`：
  **90 passed**，74.74 秒，exit 0。

首轮全套使用共享虚拟环境时，四个分区各在新进程恢复测试失败：共享环境的
editable `.pth` 指向原目录旧 src，子进程因此拒绝 Schema35。没有更改断言或原目录；
执行共享 Python 的 `-m venv --without-pip .venv`，在新 worktree 的 site-packages
写入 `integration-dependencies.pth`，依次引用本 worktree 的 src 和共享已安装依赖目录。
明确验证独立 Python 的 `trpg_state_machine.__file__` 位于新 worktree。
移除外层 PYTHONPATH，真实历史解释器仍使用各自明确的旧源码路径；重新完整执行四区。
初次失败轮次不计入最终通过数。

全套实际调用（各分区原生并行；独立 basetemp，日志和文件清单均留在本 worktree）：

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONUNBUFFERED='1'
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
$env:UV_CACHE_DIR='D:\Projects\agentic-trpg\trpg-state-machine\.uv-cache'
$testFiles=@(Get-ChildItem tests/test_*.py | Sort-Object Name)
$members=@(for($index=$part;$index -lt $testFiles.Count;$index+=4){
    $testFiles[$index].FullName
})
.venv/Scripts/python.exe -m pytest -q -x @members `
    --basetemp=.test-tmp/integration-isolated-$part `
    -o faulthandler_timeout=45 --durations=10
```

核对74个测试文件的分区并集无遗漏／重复，全套 `--collect-only -q` 为1597项。
提交前 fetch 的一次 GitHub 连接失败，HTTP/1.1 重试成功；origin/main 仍为 afe6871，
远端 ENV 分支仍为06c7af8，没有覆盖新的远端提交。

最终四区0／1／2／3分别为 **377 / 439 / 387 / 394 passed**，合计
**1597 passed**，退出码全部为0；耗时366.00 / 206.87 / 277.07 / 338.93秒。
通过数与1597项完整收集数一致，覆盖环境、机关、命令指纹、OSS、权限和恢复回归。
只有既有 lark `sre_parse`／`sre_constants` 弃用警告。
`python -m ruff check .`、`python -m ruff format --check .`、
`git diff --check`、`git diff --cached --check` 均通过，241个文件格式一致；
README及环境／机关两组文档的本地链接与代码块 fence 检查通过。
整套回归后207个源码／测试文件的 SHA256 与运行中快照一致；
main migration34 的 Git blob 为 `b800bd23018bf70cf1ca30cbe1df6e41d7c0e8d5`，
与 origin/main 完全相同。原目录38个既有修改文件字节不变。

选择性暂存本次整合文件和已合并的 main 内容，在原 ENV 分支生成合并提交，
正常推送该分支，不强制推送、不修改 main。最终提交号及远端核对结果随交付报告提供。


## 2026-10-20 Scoped runtime validation refactor

本批实现采用 Schema40 的事务内派生占格索引和结构验收记录，普通命令由领域
局部准入保护，操作内复用读取，提交不再默认完整空间审查或库存重建。
逐项能力、读取范围、存档兼容、实际计数及本批验证结果见
[系统性重构验收](reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md)。
上文历史测试数字与旧 Schema/待办保持原批次语境，不作为本轮验证结果。
