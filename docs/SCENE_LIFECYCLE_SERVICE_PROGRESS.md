# W-01 场景生命周期切片进度

2026-10-20：Schema39 保存实例配置；ensure_scene 新建后完成配置装配并清除未实例化预测计划，
既有实例只读验证；旧库通过显式源校验升级。详见 [当前记录](INSTANCE_CONFIGURATION_PROGRESS.md)。

2026-10-19 后续：[Session Management 批次](SESSION_MANAGEMENT_PROGRESS.md) 整理了创建／
授权／Runtime 职责，ensure_scene 加入固定 Package 和必要实例结构核对；抽象 NPC 的
合法移动现在也能在同一操作事务内延迟创建目标，不要求玩家先进入，不增加 PC 访问。
以下旧准入和验收记录是历史切片；当前兼容影响及仍未迁移的完整能力配置见
[Session 设计说明](SESSION_MANAGEMENT.md)。

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
| main 的独立环境服务、机关服务及其迁移 | 后续授权集成已纳入，见下节 |
| 新时间／Effects 生命周期／认知消费者 | 不在本轮范围 |

WorldHandler 仍负责命令入口分派、执行来源／活动／战斗 guard、移动及对象准入、
通路／门锁／站位／占用／可见性验证、拓扑／空间查询及一致性审计、物品转移及投影协调、
原对象能力和时间服务分派。没有据“服务已新增”宣布整个 WorldHandler 迁移完成。

## 初次生命周期提取验证记录（7e45277）

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
初次提取时未集成独立 EnvironmentService；这条历史范围说明由下节的已提交 main 集成记录更新。

## 授权合入 origin/main（2026-10-18）

用户指定在本交付工作区执行 fetch／合入 main／正常推送任务分支。
开始时 `feat/scene-lifecycle-service@7e4527759b2949ac68b1087cc62abb99ce72d0de`
工作区干净。fetch 后 main 为 `1fec17a28e15bed06739be0a3811de7b2b2da584`，
采用真实 merge，不 rebase、不强制推送、不更新 main 工作区。

4 个文本冲突逐段整合，未整文件选择 ours／theirs：

| 文件 | 整合结果 |
|---|---|
| README.md | 同时保留场景、环境与机关效果说明及领域事实所有权描述 |
| service.py | SceneLifecycle 与 Environment 同时装配，保留 provider／命令适配器接线 |
| spatial.py | 保留 Environment 分发／装配；移动协调继续由 SceneLifecycle 执行；main 私有 ActorMotion 生成迁入服务 |
| domain_facts.py | 保留 main 七字段机关状态、初始 broken 判断和 max_uses；同时保留实例／能力冲突保留写入、PC 访问分离 |

自动合并审查：迁移34／35、storage／历史谱系协调、Environment、Mechanism／效果契约、
CommandRuntime、SessionAccess、PackageEffects 和 rule_execution 与 origin/main 内容一致；
导出同时保留 SceneLifecycle、环境和机关命令类型。pyproject.toml／uv.lock 与 main 一致，
RE 固定 SHA 仍为 `d596161060555630019163ab301e1d25fd816549`。

复用环境首次访问／独立进程恢复用例，增加生命周期幂等及访问断言；复用跨场景机关触发
用例，增加 no-op／重复 ensure 不重新激活和访问断言。新增两项组件初始损坏／七字段机关
状态保留回归。定向衔接 smoke **23 passed**，9.33s。

最终相关专项（没有扩大到全库）：

| 覆盖 | 结果／日志 |
|---|---|
| SceneLifecycle、Environment／环境审查、Mechanism／效果契约、环境与机关迁移、通用迁移、版本化存档 | **192 passed**，134.53s；`.test-tmp/main-merge-services-final.log` |
| Session、空间移动、GC14、领域装配／写入、命令／Package 后果、导航活动／故障／迁移、机关／光源／耐久／容器及独立进程 world_station | **214 passed**，82.15s；`.test-tmp/main-merge-paths-final.log` |

两组合计 **406 passed**，均 exit 0。随后强化跨场景机关故障用例：真实激活采用写入失败，
原位置／PC 场景／访问／机关／事件／回执全部回滚，同命令重试及回放不重复访问或激活；
更新后的用例另行 **1 passed**，0.43s（重复用例不加入 406 合计）。
全部只有既有两条 Lark 弃用警告。Ruff check src tests、Ruff format --check src tests
（173 文件）、staged／unstaged diff check、场景文档链接与 fence 检查通过。

采用 main 的 Schema35，不另建迁移；已有环境事实与机关状态和历史访问计数不重算。
W-01 仍仅场景生命周期切片完成，其他 WorldHandler／SA-01 工作未宣布完成。

## GridService 后续切片（2026-10-18）

基于已合并 `main@0554add`，`feat/world-grid-service` 进一步提取网格／占格读取、
探索拓扑和静态／Light 几何输入，新增当前空间组合及几何查询。上文“Grid 未在本轮实施”
保留为生命周期批次的历史范围，当前状态见 [Grid 进度](GRID_SERVICE_PROGRESS.md)。
WorldHandler 仍保留移动／通路／业务准入和事务协调；W-01 整体与 SA-01 仍未完成。
初始化不计 PC 访问、角色位置唯一、预先环境／能力事实保留等生命周期语义不变。
原目录的 38 个修改／未跟踪文件、原 HEAD、索引与状态在本次集成前保存校验快照，
提交前再次核实保持原样；不从原目录复制任何功能修改。合并提交仅推送本任务分支，
远端 main 保持 `1fec17a28e15bed06739be0a3811de7b2b2da584`；最终 SHA 见交付报告。


## 2026-10-20 Scoped runtime validation refactor

本批实现采用 Schema40 的事务内派生占格索引和结构验收记录，普通命令由领域
局部准入保护，操作内复用读取，提交不再默认完整空间审查或库存重建。
逐项能力、读取范围、存档兼容、实际计数及本批验证结果见
[系统性重构验收](reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md)。
上文历史测试数字与旧 Schema/待办保持原批次语境，不作为本轮验证结果。
