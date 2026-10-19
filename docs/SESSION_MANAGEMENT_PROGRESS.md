# Session Management 重构进度

## 2026-10-20 实例配置与容器索引后续

当前 Schema39；有效对象能力、Scene 配置、NPC senses 和辅助路径已保存并改读 Session；
旧库通过显式 upgrade_instance_configs 升级，普通读取不补字段。容器操作内复用邻接索引及重量 memo。
RE Openable 外部阻塞已解除：Engine／SRD 同 pin a011eba，默认／自定义素材在 Runtime 装配时冻结。
初始化、实际求值、availability、持久绑定校验与恢复共享固定对象，连续运行无全量摘要。
/36 旧 Session 保留绑定与实例升级，RE /37 下恢复或新求值不匹配时拒绝，不自动重绑。
字段权威、创建／恢复、Package 审计、候选计数和最终检查见
[本轮完整记录](INSTANCE_CONFIGURATION_PROGRESS.md)。下文未迁移清单与数字是历史批次快照。

## 地图运行表示后续批次（2026-10-19）

在当前 `feat/session-management-refactor@a25c35e` 的干净工作区继续，未切 main。
创建协调仍经原 SceneLifecycle 初始化领域地图，新增 SessionRuntime.prepare_scene_maps
协调 Grid 准备；原机械场景补建、延迟实例化和 save／resume 当前场景也准备。释放只移除
可重建内存引用，Store.close 清池，保留调用方持有的旧 View。

SQLite state_json 仍为有效地图权威。GridMaps 内容键先读取事务可见编码，覆盖候选、
回滚、相同世界版本、旧读取事务和连接隔离；Scene 预初始化范围、绑定、授权、幂等、
RNG、事件／Outbox／回执语义不变，无新 Schema 或 RE 依赖变更。
查询算法、调用次数与本轮验证见 [Grid progress](GRID_SERVICE_PROGRESS.md)。
本轮验证覆盖 718 个不同用例：Focused 248、Integration 470；最终构造／序列化兼容复核
302 passed（与上述重叠，不另加合计），均 exit 0。Schema36 和固定 RE／SRD 依赖不变。
本轮仅完成地图运行表示和 Grid 算法；下文列出的 WorldObject／Character／Item、
复杂创建依赖与固定素材接口缺口继续保留。

下文为 a25c35e 的原 Session 重构实施和验证记录，915 项属于该历史批次。

2026-10-19；工作区 `D:\Projects\agentic-trpg\trpg-state-machine`；
分支 `feat/session-management-refactor`，干净基线 `3fe36e2`。
仅修改 SM，不修改 RE／架构仓库、不合并 main。
当前职责、兼容影响和逐领域缺口见 [设计说明](SESSION_MANAGEMENT.md)。

## 实施状态

- 创建流程提取至 SessionCreation，StateMachine.create_session 保留兼容签名；原有领域
  初始化、创建幂等、TimeBinding、PackageEffects、事件／Outbox／回执在同一事务。
- SessionAccess 保留授权和 pin 检查；SessionRuntime 接管指纹、重放、版本、RNG 和回执，
  世界／真实规则／合成规则共用 publish／finish，不重写领域 Handler。
- 普通求值、战斗准备、辅助检定、机制效果端口、可用操作查询和相关恢复校验从持久化
  Session 取三项绑定；结果仍严格匹配请求。Package pin 校验继续强制。
- ensure_scene 核对固定 Package，复用并验证既存实例；授权 NPC 抽象移动可在操作事务中
  创建必要目标且不生成 PC 进入事件，读查询不写库。保留原空间场景提前初始化策略。
- save／resume 增加当前受支持实例结构、当前 RE／机械 Actor／组件和 RNG 检查；缺失事实拒绝，
  不重套模板。Schema36、公开 API、旧回执、legacy audit／机械版本边界保持。
- WorldObject 能力配置、完整 Character／Item 等仍未迁移；逐字段和调用点清单在设计说明。
- 固定 pin `d596161` 无固定素材接口；上游 `3718f7c` 有 FrozenRuleset，但 `/37` 分支缺少
  当前 Openable 模块。未做不兼容升级，快速校验**未接入**，保留实际 loader 全量摘要检查。

## 检查等级与证据

仓库、源码／测试子目录及适用祖先未发现 AGENTS.md；无 check-fast 或 CI 等级脚本。
依据公共授权、幂等、RNG、跨领域事务和恢复影响，选 Focused + Integration，未运行无关全量。

初次沙箱内基线／专项在 Windows asyncio `_fallback_socketpair → accept` 阻塞，
faulthandler 定位后仅停止本任务测试进程；改用原 `.venv`／固定依赖在限制外运行。
未改 Python、RE 依赖或锁文件。中途失败均保留日志；最终退出码另行确认。

- Focused：`test_session_management.py test_sessions.py test_commands.py test_mechanics.py
  test_domain_runtime.py test_scene_lifecycle.py test_perception.py test_perception_conditions.py
  test_npc_perception_queue.py test_perception_efficiency.py test_event_local_evidence.py
  test_event_traceability.py`：**287 passed**，166.54s，exit 0；
  日志 `.test-tmp/session-focused-verified.log`。
- 随后确认原六参数 SceneLifecycleService 构造器仍兼容：新增且未包含于上述收集的用例
  **1 passed**，0.73s，exit 0；`.test-tmp/session-constructor-final.log`。
  Focused 合计 **288 个不同用例**，其中 Session 新增回归 19 项。
- Integration：真实 RE／战斗／查询／关闭、Check 后果、NPC 交接、旧库迁移／跨进程恢复、
  导航／物品／锁／Openable／Mechanism／Environment／WorldTime 及示例，最终 **627 passed**，
  357.71s，exit 0；日志 `.test-tmp/session-integration-final.log`。
- 两组合计 **915 个不同用例**，只有两条既有 Lark 弃用警告；未运行全库测试。
- Ruff check src tests、Ruff format --check src tests（183 文件）、git diff --check、
  本轮文档的相对链接／代码 fence 检查通过。最终 diff 已按生产路径、测试和文档审阅。
  pyproject.toml／uv.lock／storage／SQL Schema 与基线一致；安装元数据也确认 Engine 和
  SRD 均为 0.6.0、Git SHA `d596161060555630019163ab301e1d25fd816549`。

Integration 文件清单（均位于 tests/）：`test_integration.py test_combat_foundation.py
test_dnd_runtime.py test_dnd_hardening.py test_dnd_npc_availability.py test_combat_closure.py
test_real_checks.py test_conditional_world_effects.py test_history_free_rules_adapter.py
test_npc_combat_handoff.py test_npc_outputs.py test_versioned_state.py test_migrations.py
test_navigation_activities.py test_navigation_faults.py test_navigation_migration.py
test_grid_service.py test_item_transfers.py test_lockable_passages.py test_openable_checks.py
test_mechanism_effects.py test_mechanism_service.py test_environment_service.py
test_environment_snapshot.py test_environment_versions.py test_world_time_service.py
test_world_example.py`。运行方式为现有 `.venv/Scripts/python.exe -m pytest -q` 加上述文件，
独立 `--basetemp=.test-tmp/session-integration-final`，无依赖升级。

Integration 首轮 **624 passed / 3 failed**。修正过严的恢复检查：不能将当前场景普通 NPC
没有 RE 档案误判为旧存档损坏；验证已保存的 Actor／组件，后续求值仍要求完整局部快照。
另外更新运行时先授权／Session pin 拒绝后的异常码断言。原失败及其参数化用例专项
**4 passed**，11.12s（已包含于 Integration 范围，最终合计不重复计数）。
Focused 中新增 PickLock 测试曾误写参数顺序，修正后上述最终分组全部通过。

新增测试通过隔离 `_check_package_pin`，故意令 Package 与 Session 绑定不同，并断言
verify_ruleset／求值请求／查询请求／指纹仍由 Session 驱动；真实 pin 拒绝测试另行保留。
真实错误绑定结果和查询结果拒绝且不写事实／RNG／回执；SQLite late fault 验证新建场景、
NPC 位置、版本、事件／Outbox 和回执一起回滚。既有权限、重放、指纹冲突、OCC、
pending_choice 以及历史兼容测试复用现有真实公开路径。
