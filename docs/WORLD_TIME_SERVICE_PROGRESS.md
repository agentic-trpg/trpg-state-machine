# WorldTimeService 实施与兼容记录

2026-10-19 后续：时间协调、显式倍率 Driver、真实 Round 接线及 Schema37 已在当前
`feat/session-time-coordination` 实现；详见 [时间协调](SESSION_TIME_COORDINATION.md)。
下文保留 2026-10-18 批次的历史验证范围，默认实时倍率现为未确定。

日期：2026-10-18。分支：`feat/world-time-service`。基线：最新远端 main
`7aa9ad11c2aea0f659e22f3c70cfa5efb8ecaaec`，开始时工作区干净；保留其已合并的
Openable / Container 修复，不合并 main。仓库及适用祖先目录未发现 AGENTS.md；
没有 check-fast、Makefile 或 CI 检查入口，使用 pytest、Ruff check/format、diff 检查作为等价验证。

读取了当前总控仓库 `MODULE_CONTRACTS.md` §0.3、§8 时间／生命周期与
`WORLD_SUBSYSTEM_DESIGN.md` 的 World 时间约束，检查实际 SM 装配、事实、命令、
Session、迁移、NPC 日程、OB／Gate 和事件证据代码。总控文档的 TimeBinding 细节原为
OPEN，本轮按用户确认设计在本仓库冻结下列局部版本；不宣称外围生命周期已经实现。

## 已实现：唯一时钟与真实生产路径

`StateMachine.world_time` 与 `WorldHandler.time` 是同一 WorldTimeService。
独立构造 WorldHandler 也会装配 Service。现有真实写路径是：

`submit_command → trusted authorization / receipt replay / world_version → savepoint →
WorldHandler._operate → WorldTimeService.advance → WorldFacts.set_tick → event capture →
legacy schedule sync → event persistence / evidence / Outbox / receipt → commit`。

WorldFacts 负责读取／写入时间事实与读取／初始化绑定；唯一权威 tick 仍在
`world_states.game_tick`。Service 不存缓存／第二时钟、不 commit、不实现现实时间循环。
旧 `advance_game_time()` 重复实现已删除；`game_time.py` 保留兼容 DTO 与原日程函数。

公开写入口继续是 `world.advance_time` / `AdvanceGameTime(expected_tick, next_tick)`，
`sm-command/1` 的 DTO 字段不变，数据适配器 `world_command_from_data` 也注册该命令。
当前整数 tick 范围保持 `0..2147483647`，不倒退、不接受 bool、无隐式 RNG 消耗。
同 tick 无时间事件／world_version 变化，但仍可靠记录回执；拒绝／冲突也保存原有回执。
重放先重新鉴权，再读取回执；存储失败回滚全部事实、RNG、证据、Outbox 与回执。
`game.time_advanced.payload.previous_tick / game_tick` 提供本次实际区间；秒制事件额外带
`time_protocol=world-time/1`，legacy 事件形状不变。无推进时实际区间为空。
战斗期间沿用 `unsupported_combat_world_operation`，没有绕过限制的额外推进入口。

## 协议与初始化

| 版本 | tick 意义 | 初始化 / 持久化 |
|---|---|---|
| `game-clock/1` / LegacyTimeBinding | 原抽象整数单位；与现实秒的关系未知 | 旧存档、未提供配置的兼容 Session；绑定表无该 Session 行；不补造日历／昼夜 |
| `world-time/1` / TimeBinding | 固定 1 tick = 1 游戏世界秒 | 创建时显式选择完整配置，保存于 `world_time_bindings`；tick 0 是起始日期／日内时刻 |

完整 `TimeBinding` 字段：`schema_version`, `day_length`, `start_day`, `start_day_tick`,
`cycle`, `day_start`, `night_start`。默认日长 86400、起始日 1、日内秒 0、
cycle=`alternating`、day_start=21600、night_start=64800。日内时刻均是配置日长内的秒数，
小时仍固定 3600 秒，自定义短日无需能写成传统 24 小时钟面。

日长为正整数，起始日为正整数，日内时刻和阶段切换必须在 `[0, day_length)`。
交替周期要求两个不同的切换时刻；day_start 到 night_start 的模日区间为白天，
其余为夜晚，允许任一阶段跨午夜。`cycle=none` 必须同时设置两个切换时刻为 None；
阶段／阶段边界返回 None，日历仍有效。非法类型、版本、越界、矛盾规则返回
`StateMachineError(code=invalid_time_binding, issue.path=字段)`。

Package 可以带完整的可选顶层 `time_binding` 映射，由 `time_binding_from_data` 闭合校验。
已有 Package 不增加字段，不改变旧 digest。`CreateSessionRequest.time_binding` 是可选
强类型整体覆盖；None 选 Package 默认，双方均无配置则为 legacy。创建回执指纹只在
实际选择秒制配置时增加绑定，旧创建重放指纹不变；更换绑定复用同一创建 ID 会冲突。
这是最小创建接线；字段级合并、设置界面、完整 Package 默认／覆盖策略、运行时调速归 TIME-01。

Session Snapshot 的 `world.time_binding` 与 `sm.world_time.read(...).binding` 读取已保存配置，
不重读 Package 默认。数据库触发器禁止修改／删除已保存绑定，也禁止在 Session 首次事件发布后补插绑定。Package 真正内容改变仍受
原 package_digest pin 拒绝；旧 Session 必须使用原 Package。没有旧存档转秒 API；
不推测倍率，不对历史 tick、事件、期限、回执或 RNG 做缩放。希望使用秒制应创建新 Session。

## 只读公开查询

以下 `time = sm.world_time`，查询都需要 Session、固定 Package 和可信 `context`：

| API | 结果 |
|---|---|
| `time.read(sid, package, context=ctx)` | WorldTimeSnapshot：binding、game_tick、elapsed、current_day、day_tick、phase、next_calendar_boundary、next_phase_boundary |
| `time.elapsed(sid, package, context=ctx, since_tick=0)` | 当前 tick 减过去起点的 TimeDuration；未来起点拒绝 |
| `time.remaining(sid, target_tick, package, context=ctx)` | 非负剩余时间；到达或超过目标返回零 |
| `time.reached(sid, target_tick, package, context=ctx)` | 当前 tick >= 目标；精确到达为 True |
| `time.to_ticks(sid, amount, unit, package, context=ctx)` | 非负整数 seconds / minutes / hours / days 到 tick；days 用配置日长；溢出拒绝 |
| `time.boundaries(sid, start_tick, end_tick, package, context=ctx, limit=100, after_tick=None)` | BoundaryPage：有界 TimeBoundary 元组与 next_cursor；没有后续页时为 None |

TimeDuration 含 ticks、seconds、minutes、hours、days；非整数分钟／小时／天使用
Python Fraction 保持精确。本地 DTO 不是 HTTP JSON Schema。legacy 支持 ticks 的
elapsed／remaining／reached；无秒尺度的 duration 字段为 None，换算和日历边界查询返回
`unsupported_time_protocol`，不编造传统日长或日出日落。

日历边界为 `day_rolled`，阶段边界为 `day_started`／`night_started`。查询范围严格为
`(start_tick, end_tick]`：起点不计、终点计入；同一 tick 的多种边界合为一条。
续页以返回的 next_cursor 传给 after_tick，排除游标自身，避免重漏。默认 limit=100，
允许 1..1000，最多直接计算 limit+1 个唯一边界，不逐秒迭代，不生成完整大跨度列表。
当前阶段在精确切换时已是新阶段；下一边界严格晚于当前 tick。
下一边界是数学上的下一次发生时刻，可能超过可推进 tick 上限；没有据此扩展写入范围。

## 旧消费隔离与后续责任

已核查 `game_time.sync_npc_schedules`、`observation_lifecycle`、`npc_modes`、
`_migration_16.sql`／`_migration_17.sql`：旧 schedule start/end、OB TTL 100、Gate 周期 20／最长等待 60／余量 10
均仍为 legacy 抽象 tick。没有决定其秒制对应时长。日程同步继续在原命令事务调用，
保留 missed/active/completed/interrupted 事件、NPC 版本增量、旧任务取消及原回滚。
秒制 Session 创建时拒绝带配置型 NPC State 的 Package，并拒绝 NPC observation access 与宿主聚合感知投影；
返回 `unsupported_time_consumer`。允许未配置 NPC 的物理 Actor，不能借此开启 OB／Gate。
日程同步对秒制无配置 NPC 返回空，若发现遗留 Runtime 则拒绝，防止旧期限误读。
该隔离是受控不支持边界，不是 NPC 秒制日程功能已交付。

| 后续依赖 | 本轮状态 |
|---|---|
| TIME-01 设置与完整初始化策略 | 仅完成固定绑定和最小 Package／Session 接线 |
| TimeDriver | 未实现现实时间采样、倍率、暂停／恢复；当时倍率尚未实现；后续已取消 1:6 默认设定，默认倍率仍未确定 |
| Combat 时间接入 | 未实现；整个 Round 约 6 游戏秒，每个 Turn 不各加 6 秒 |
| Activity / Effects / RE | 未实现旅行、休息、仪式、期限／到期效果／周期伤害／豁免 |
| 物品与环境 | 未实现燃料、熄灭、充能、天气、实际光照变化 |
| TIME-08 NPC 日程 | 保留原 legacy 同步；未来由 NPC 业务消费时钟和实际区间 |
| OB / Gate / LLM | 保留旧协议；秒制时长／调度接线待专门设计 |
| Game Runtime | 未实现跨组件期限扫描、有序分段推进和中途选择／恢复协调 |

月份、季节、月相、天文模拟不在范围。阶段是时钟换算结果，不代表照明事实已改变。

## 验证

最终代码验证（2026-10-18）：

| 检查 | 实际结果 |
|---|---|
| `.venv/Scripts/python.exe -m pytest -q -o faulthandler_timeout=45 --basetemp=.test-tmp/time-full-final` | **1420 passed**，2 warnings，510.04 秒；包含下面的 42 项新增时间专项，以及旧 NPC 日程、OB／Gate、事件证据、迁移、Openable／Container 回归 |
| `.venv/Scripts/python.exe -m pytest tests/test_world_time_service.py -q --basetemp=.test-tmp/time-focused-confirmed` | **42 passed**，2 warnings，7.86 秒；真实公开入口和独立 WorldHandler 装配均覆盖 |
| `.venv/Scripts/python.exe examples/world_station.py` | exit 0；真实嵌套容器／锁／共享门／光源／机关、新进程恢复、receipt replay；最终 world_version=32、event_seq=49 |
| `.venv/Scripts/python.exe -m ruff check src tests` | All checks passed |
| `.venv/Scripts/python.exe -m ruff format --check src tests` | 157 files already formatted |
| `git diff --check`；README／支持矩阵／本文本地链接和代码围栏检查 | 通过 |
| 真实 Package loader 的非法 bool 日长输入 | invalid / invalid_time_binding / day_length，结构化错误 |

本机完整权限下取得上述最终 pytest / 示例退出结果。早期受限环境遇到系统临时目录权限与
原有用例停滞，已停止本任务启动的停滞进程，并用仓库内 basetemp 完整重跑；
那些中断运行不计为通过证据。两项 warning 来自依赖 lark 的 sre_parse／sre_constants 弃用提示。

新增 `test_world_time_service.py` 使用真实 Session、公开 TypedCommand、真实 Service、
实际 Schema31 源码生成的存档、独立子进程、SQLite 故障触发器与迁移失败注入。
原 NPC 日程、OB／Gate、证据和迁移用例继续回归；跨平台子进程搜索路径使用 os.pathsep。
