# W-01 场景生命周期切片进度

2026-10-18；基线 `9ce60cb`；仅交付 `feat/scene-lifecycle-service`，不合并 main。
原工作区有大批与核心文件重叠的未提交修改，本次以已提交 HEAD 在
`.test-tmp/scene-lifecycle-delivery` 隔离实施，不纳入原修改。
仓库及适用祖先未发现 AGENTS.md，当前 HEAD 无 check-fast／CI 检查入口；
采用已有 pytest 专项、Ruff check／format、git diff --check，不扩大到全库测试。

W-01 为本次用户指定的 World 提取简称；上游架构待办仍映射 A2／C-25／DS-01、DS-02，
不新增并行完整 Backlog，不修改其他仓库的历史完成状态。

| W-01 范围 | 本轮状态 |
|---|---|
| 场景幂等初始化、入口创建、PC／NPC 进入离开、重访与恢复复用 | 完成此受限实现切片 |
| PC 访问分离与重放防重；初值保留 | 完成此切片，无历史计数重算 |
| 场景 Flags 校验、修改、原事件生成 | 已迁入服务 |
| WorldHandler 其余业务 | 仍保留，W-01 整体未完成 |
| Grid／Terrain 综合服务、其他 World 协调重构、SA-01 | 未在本轮实施 |
| 环境独立服务、跨服务新时间／Effects／认知消费者 | 不在本轮提交范围 |

WorldHandler 仍负责命令入口分派、执行来源／活动／战斗 guard、移动及对象准入、
通路／门锁／站位／占用／可见性验证、拓扑／空间查询及一致性审计、物品转移及投影协调、
原对象能力和时间服务分派。没有据“服务已新增”宣布整个 WorldHandler 迁移完成。

## 验证记录

原 `9ce60cb` 的同一预初始化／NPC 访问回归稳定失败（期望 0，实际 visited_count=1）；
修复后进入核心专项通过。专项包含首次／重复 ensure、入口／重访、PC／NPC 导航、
同场景移动／命令重放、历史计数保留、Flags、环境／能力部分事实保护，以及目标实例、
对象、NPC、角色位置、Session PC 字段、访问、Evidence、Outbox、回执 9 个阶段故障回滚。
新服务加入原领域写入／依赖方向检查。

核心专项：`test_scene_lifecycle.py test_sessions.py test_spatial_world.py
test_domain_runtime.py test_gc14_review_fixes.py`：**129 passed**，32.16s。
命令／Package 条件效果／真实 Check 后果／GC1 门／导航活动、故障及权限边界／NPC
状态与新进程恢复／基础集成／world_station 完整示例／容器、机关、光源、耐久专项：
**254 passed**，137.53s；两组合计 **383 passed**。
新增服务纳入后的领域写入／依赖方向检查另行确认 **1 passed**（重复验证，不计入合计）。
Ruff check src tests、Ruff format --check src tests（162 文件）、git diff --check 全部通过。

交付沿用同名分支，分支检出位置转到隔离工作区；原目录保留在 `9ce60cb` detached HEAD，
原未提交文件逐字节保留，不重置、不 stash、不混入提交。提交前远端任务分支尚不存在，
main 为 `1fec17a28e15bed06739be0a3811de7b2b2da584`，本次不修改 main。

首次执行的临时目录设置错误已修复；沙箱内异步运行卡在 Windows asyncio socketpair，
faulthandler 确认后仅停止本任务进程，在限制外以原 lockfile 的独立 Python 3.12.14
环境重跑核心专项并通过。两条既有 Lark deprecation warnings，无依赖／锁文件修改。
日志保留在交付工作区 `.test-tmp/`，不提交运行产物。

协议／Schema 不变，今后预初始化不再误增 PC 访问是唯一计数行为修正；历史值不更正。
独立 EnvironmentService 来自原工作区未提交修改，本次未集成，也不宣称其验收通过。
