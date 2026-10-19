# W-01 GridService 切片进度

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
