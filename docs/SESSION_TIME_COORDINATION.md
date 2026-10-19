# Session 游戏时间运行与协调

2026-10-19；`feat/session-time-coordination`。采用“统一游戏时间，界面自行显示，
后台按需推进并结算”。当前存储 Schema37；RE/SRD pin 不变。上游职责依据为
MODULE_CONTRACTS §0.3、§8 的唯一事实所有者和共同短事务约束。本文件描述本仓库
实际接口；不将完整 Effects、休息或 NPC 秒制策略称为已经实现。

## 三种时间与三种来源

`world_states.game_tick` 是唯一已提交游戏时间，WorldFacts 保存，WorldTimeService
负责验证、推进、换算、日期和昼夜。规则和到期判断只读这个值。WorldTimeService
没有后台循环，也没有效果／休息／战斗业务规则。

TimeDriver 的目标时间是 `基准 tick + 实际单调时长 × 显式倍率`，保留有理数余量，
提交取整数 tick。它代表请求推进到的位置，可能领先已提交时间；不能作为规则事实。
界面显示时间由时间同步 DTO 推算，也可能领先已提交值，同样不能提交业务收益。

| 推进来源 | 生产入口与行为 |
|---|---|
| 实时 | Runtime 使用小型 TimeDriver，注入 `clock_ns()` 和 `scheduler.call_later(seconds, callback)`；只经共同 TypedCommand 请求绝对目标 |
| 手动／活动跳时 | `session.advance_time / AdvanceSessionTime(target_tick, source="manual" 或 "activity")`；活动让出实时推进权，中途停止时不达到原终点，完成后重建基准 |
| Combat | 实际已采用 RE Delta 的 `round_number` 增量由 Runtime 协调；完整 Round 增加 6 tick，Turn 开始／结束及回合持续效果仍由 RE 处理；关闭后重建实时基准 |

保留 `world.advance_time / AdvanceGameTime(expected_tick, next_tick)`，但它与新入口
调用同一个 Runtime 边界流程，不再直接跳过机关到期结算。Combat 期间两个公开
手动入口均拒绝，内部 Round 接线在原 RE 采用 savepoint 内完成，重放不会再加时间。
legacy 的 tick 仍是历史抽象单位，Round 增加的是 6 tick，不对历史值做秒制换算；
只有 `world-time/1` 可以启动实时 Driver。

**没有默认实时倍率。** 删除此前文档中的“现实 1 秒＝游戏 6 秒”目标默认值；也没有
改成 1:1。新 Session 初始化为 paused、倍率未配置。手动、活动和 Combat 不依赖倍率。
`SetTimeRunning("start", 1, 1)` 是显式选择 1:1 的示例；合法倍率为两个正的有界整数，
保存时约分。未指定倍率的 start 只能使用已经授权保存的值，否则返回
`time_rate_unconfigured`。没有 Driver 时保存的 running 状态表现为 detached，不能流逝。

## 使用与生命周期

```python
from trpg_state_machine import SetTimeRunning, TypedCommand

state = sm.read_session(sid, package, context=context)
receipt = sm.submit_command(
    TypedCommand(
        sid,
        "time.start.1",
        state.pc_actor_id,
        "session.set_time_running",
        SetTimeRunning("start", 3, 2),
        state.world_version,
    ),
    package,
    context=context,
)
# context 必须由可信适配层授予 can_advance_time=True。
# 默认 scheduler 使用当前 asyncio loop；同步宿主显式传入同线程调度器。
driver = sm.attach_time_driver(sid, package, context=context)
dto = sm.read_time_sync(sid, package, context=context)
```

所有时间控制和显式推进均经过原授权、版本、指纹、回执和事务。运行组件本身不修改
世界时间、不消费 RNG、不发放收益。无业务边界时不设置每秒唤醒；显示、world_time
和普通快照查询均不写库。玩家业务操作、显式暂停／改倍率和最近边界到达才按需推进。

`SetTimeRunning` 的 action 为 start、pause、rate、cancel。pause 和 rate 先按旧倍率
处理固定生效目标，再更新配置并建立新基准；亚 tick 余量也保存。尚有待处理工作时
不会越过它修改配置；查看 progress，先继续工作再以新 command_id 重提控制。
cancel 只取消 Runtime 的目标进度，不取消领域自己的到期条件或发放活动完成收益；
重新查询仍会发现到期工作。业务模块的取消操作仍由该模块负责；尚有当前／过去到期工作时拒绝删除目标，防止跳过已到期结算。

创建保存最终 TimeBinding 和初始暂停元数据；旧 Session 无运行行时读为暂停／未配置。
恢复读取保存的倍率、mode 和继续目标，从事实查询重建最近边界。进程单调时钟起点
从不保存，恢复后显式 attach 才建立新基准，默认不补算离线时间。
`driver.close()` 取消唤醒、释放所有权；`StateMachine.close()` 释放全部 Driver 和 Store。
释放前未请求按需提交的推算部分不冒充已保存游戏时间；需要精确停点时先提交 pause。

同一数据库／Session 通过本地 OS 文件锁持有一个 Driver，进程退出自动释放。
generation 和组件 token 验证请求，取消的回调另有唤醒代次，旧回调不得提交。
一个 Store／线程拥有 Driver，所有该 Session 的命令应路由到这个共同入口；其他
连接仍能读，尝试从其他连接写会返回 `time_driver_owned`，避免新增更早期限却不能通知
原 Driver。不引入 IPC、分布式租约或第二执行器；未 attach 时仍保留原 SQLite 并发／OCC。
内部领域方法继续受当前授权事务约束，不是另一个 Host 写入口。

正常命令提交后 Runtime 重新查询并重新安排最近唤醒；创建、取消、提前结束和修改期限
不依赖旧任务表。宿主内部接入端口后也须调用 `refresh_time_drivers()`；不得绕过共同
提交入口偷偷修改期限。调度器可能迟到，Driver 使用实际经过时间，处理所有到达／跨过
的边界。失败／拒绝后停止自动重试，`driver.last_error` 保留错误，宿主可修复后 refresh。
调度器回调必须在 Store 所属线程运行，不使用 threading.Timer。

菜单、断线、一般 LLM 等待的暂停政策仍 **OPEN**。宿主可以在相应策略点提交 pause／
start；当前库没有把这些场景自动统一解释为暂停或持续流逝。领域明确返回 waiting／
interrupted 则必须停止推进；继续时不补入等待处理耗费的现实时间。

## 时间同步契约 `session-time-sync/1`

`read_time_sync` 是受 Session 授权的纯读取接口，没有前端项目。

| 字段 | 含义 |
|---|---|
| committed_tick | 已提交的唯一权威游戏时间 |
| estimated_target_tick | 本次响应时实时基准推算的整数目标；暂停、战斗或 suspended 时回到已提交值，未完成目标另列于 pending_target_tick |
| display_base_tick、display_fraction_numerator / denominator | 响应时展示基准及亚 tick 余量；只供显示 |
| rate_numerator / denominator | 已明确保存的游戏 tick／现实秒倍率；未配置均为 None |
| state | running、paused、combat、suspended 或 detached；只有 running 允许客户端继续外推 |
| generation、world_version | 基准失效代次及当前世界版本；客户端丢弃旧响应，业务命令仍使用世界 OCC |
| pending_target_tick、progress_status、progress_reason | 最近推进目标及 completed／pending／waiting／interrupted 状态和停止原因；completed 记录仍可保留用于审计 |

客户端在接收响应时保存**自己的**单调计时基准，显示
`display_base + fraction + 本地接收后时长 × rate`。不传输或比较主机单调时钟绝对值，
不假定两端时钟同起点。传输延迟会造成显示误差，界面接受后续基准校正；它不改变
规则时间。有理数字段需要无损整数编码，不能通过浮点累计抹去余量。

## 边界端口与继续进度

`SessionRuntime.time` 是 Runtime 使用的协调组件；领域实现
`TimeBoundaryConsumer.next_boundary(sid, package)` 和
`settle_one(row, package, command_id) -> DueResult(events, status, reason)`。
每次都读取该领域实际到期条件。协调器不保存第二份期限、规则或时钟。
现有机关由 SQL min(reset_at_tick) 及索引查询，取消／改期立即反映，没有调度副本。

流程为：验证状态 → 取最近边界 → WorldTimeService 推进到边界 → 领域结算一个有界工作项
→ 扩展该阶段事件／结果 → 重新查询；没有边界的最后区间一次推进，不逐秒扫描。
同 tick 仍查询 due；过去新增期限夹到当前 tick 处理，不倒退，也不回补虚构历史。
晚到／反复新增过去工作都消费预算。

每次默认 8 步，可显式配置 1..16；原事件级联上限也继续适用。预算耗尽保存 target、
source、pending；用新的 command_id、相同 source／target 继续，旧 key 永远返回旧回执。
显式时间命令的 committed 回执证明该次**前缀**已可靠提交；必须检查同步 DTO 的
progress_status，不能将它解释成“已完成活动／已达到终点”。业务操作的前同步未完成时
抛出 `time_advance_pending`，保留准入进度，同一命令重试继续，不执行业务操作。

同领域内部使用该领域已有顺序，机关同 tick 重置沿用 object_id 顺序。不同领域同刻
存在依赖且没有已审阅策略时保存 waiting / simultaneous_time_choice，不用任意字典排序
替代选择。内部 `resolve_simultaneous` 是宿主已确认依赖／选择的接线点，业务选择仍应
由领域保存。测试仅验证该契约，不冒充完整玩家／DM 选择 UI。

需要 RE、玩家选择或外部等待时，领域在本事务内保存自己的可靠继续工作并返回 waiting；
Runtime 保存边界／目标并结束短事务，外部求值／选择在事务外进行，再经原领域命令
采用，之后重提原目标。接口禁止在 settle_one 中持有写事务进行外部 I/O。
interrupted 保留实际停点和目标，活动是否继续／取消及收益都由活动模块决定。

## 玩家操作、版本与回执

接入需要权威时间的实际世界交互、导航、真实 Check 和 Combat 进入路径。普通显示、
Availability、地图、对象和保存查询不偷偷推进；纯 GM flag 修改、合成测试规则路径
不新增实时业务语义。已有 Combat 命令本身由 Round 驱动；若前一个 Round 尚有 pending
时间工作，下一个 Combat 操作先完成这些工作，再读取业务快照。

1. 原结构校验、Session／Package pin、Actor 授权、handoff 和幂等重放先执行；原版本
   已陈旧则走原冲突回执，不能借同步覆盖真实陈旧状态。显式时间目标还受 grant／Driver
   token／generation／已实际经过时长校验。
2. 正常实时操作将首次采样的绝对目标、余量、原指纹和版本保存为
   time_command_admissions。存储或求值失败后重试也不重新采样现实时间。
3. 在短事务内分段同步，只把**该操作自身**前同步产生的版本变动映射为 execution_version；
   然后原执行器读取业务快照。同步和领域命令可分别提交：合法时间处理已经发生，即使
   后续领域规则拒绝操作也不会倒退时间。无效结构、无权限、原 OCC 冲突不触发同步。
4. RE 求值继续在事务外；提交仍核对 execution_version、快照和 RNG。其他命令／定时推进
   在此期间改变版本会真实冲突，不自动覆盖。原指纹／command_id 保持不变。
5. 最终回执包含该 command_id 的前同步及业务事件序号，过滤期间其他命令的事件；
   拒绝／冲突可附带已经提交的本命令时间事件。Outbox 和阶段证据依旧由 EventPersistence
   写入。未结束的原操作准入期间，Driver 不抢它的同步目标；客户端应重试原命令。

一次分段批次的 tick、业务状态、RNG、阶段事件／证据、Outbox、进度和时间命令回执遵循
原事务／savepoint 共同提交。前同步本身通过可靠 admission 继续，不在外部等待期间
持有写锁。原始 AdvanceGameTime、命令指纹和旧回执继续兼容。

## 实际已接入与尚未接入

| 模块 | 当前事实 |
|---|---|
| 机关 after_seconds 重置 | 生产接入 next_boundary／settle_one；移除 game.time_advanced 自动重置旧路径，避免重复结算；保留 blocked／resume_status 规则 |
| 固定 RE Combat | 实际采用 Round Delta 后加 6 tick；停止实时推进并在退出后重建基准；没有把回合效果转换成秒期限；活跃战斗跨进程精确续玩限制保留 |
| legacy NPC 日程 | 保留已有 world 命令后的 sync_npc_schedules 语义，未改写成秒制调度；秒制 NPC Runtime／OB／Gate 继续 unsupported；Combat 增加世界 tick 不代表这些旧消费者已完成 Combat 接线 |
| 已有导航 Activity | 保留原一步一步执行与实际中断机制；它没有已确认的时间消耗／收益规则，本轮没有虚构旅行速度或导航完成时长 |
| 活动跳时端口 | 绝对目标、让出实时权、逐边界停止及重新建立基准已经实现；完整休息／旅行完成收益、周期伤害和外部求值继续器仍须对应业务模块实现 |
| Effects／燃料／充能／天气／照明／休息 | 没有生产秒制期限提供方；不将 Calendar／昼夜换算当作照明事实变化；18 秒效果仅为持久化端口测试替身 |

Schema37 只新增运行元数据、推进进度、命令准入表及机关查询索引；不改旧表行／列，
不回算历史 Session，也不为历史 Session 自动填充运行表。迁移、真实机关、固定 RE、
可控时钟／调度器和全仓回归共同验证，结果记录于本分支交付说明。
