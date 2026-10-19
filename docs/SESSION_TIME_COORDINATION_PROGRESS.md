# Session 时间协调实施记录

2026-10-19，分支 `feat/session-time-coordination`；起始 HEAD
`555421327a29fe6e1228b68d046307c69c88d9d2`。工作区开始时干净；适用目录没有 AGENTS.md，
没有 check-fast／CI 统一脚本，使用 pytest、Ruff check／format、文档链接及 diff 检查。
没有合并、更新或 rebase main，没有修改 RE／SRD pin 或上游仓库。

职责、接口和明确不支持项见 [时间协调契约](SESSION_TIME_COORDINATION.md)。
以下保留 2026-10-19 的历史交付证据；2026-10-20 审查修复记录追加在本文末尾。

## 实际变更

- WorldFacts／WorldTimeService 保留唯一游戏时间与绑定；Runtime 的 SessionTime 接入
  原 TypedCommand 和短事务，按业务边界推进，保存有界继续进度，不直接实施到期规则。
- TimeDriver 使用可注入纳秒单调时钟和同线程调度器，保存亚 tick 余量，最近边界唤醒；
  没有边界就没有每秒定时器／写事务。OS 所有权、generation、token 和唤醒代次分别防止
  重复 Driver、旧请求和取消回调。关闭释放资源，重启不补算离线时长。
- 显式 start／pause／rate／cancel、绝对目标手动／活动请求及只读 TimeSync；没有默认
  倍率。玩家和真实 Check 在执行快照前同步，准入保存固定目标与原指纹；只映射自身时间
  产生的版本变动。原回执重放、真实 OCC、RNG、阶段证据和 Outbox 沿用。
- 机关使用自身 reset_at_tick 提供下一边界，保留 blocked／resume_status 等规则，删除
  game.time_advanced 的重复重置路径。连续边界事件采用真实累计序号，来源引用不复用
  前一段偏移。Combat 在实际 RE Round Delta 后推进 6 tick；pending 工作结束前不允许
  下一 Combat 操作，取消不能丢弃 Round 尚欠时间。
- Schema37 新增运行状态、推进进度、命令准入表及机关期限索引；历史行不回写、不填充
  新运行表。迁移测试将“当前版本”断言由 36 更新到 37，并在历史降级 fixture 中移除
  新表、在旧表清单检查中增加新表及其为空的断言；旧行原样检查保留。

## 验证与原有测试同步

`test_session_time_coordination.py` 的 34 项最终专题验证通过，使用可控时钟／调度器，
没有真实 sleep。包括显式倍率／余量、100 次只读显示、迟到及提前／取消／改期、18 秒
持久化端口示例、真实机关单次重置、同 tick／过去重建／预算／选择与中断、玩家及控制
命令失败重试、真实陈旧版本不掩盖、回执只含本命令事件、多 Session、旧回调、重复
Driver／其他连接写拒绝、暂停／恢复／改倍率、恢复及实际 RE Round／关闭接线。

独立新 Python 进程保存／恢复验证通过，两个进程均确认导入本工作区 src；保存 tick 15
和显式 3:2 倍率，恢复为 detached，再以完全不同单调时钟起点 attach，目标仍为 15。
原 `examples/world_station.py` 通过，包括真实新进程恢复和回执重放。

起始 HEAD 中一条 GC14 测试仍要求“NPC 必须等待 PC 访问初始化场景”。在只含原始
HEAD 的独立归档中，以已核对的归档 src 导入路径重现相同失败。a25c35e 已实现 NPC
按需初始化，故只更新该旧测试：验证 NPC 可初始化、PC 不移动／不计访问，以及后续
PC 访问和离开／到达证据。没有为使测试通过改写移动实现。

Windows 受限环境会在既有 asyncio socketpair 中阻塞；仅停止了通过命令行确认属于本
任务的测试进程，然后在获准环境运行，使用独立 `.test-tmp`。Lark 的两个 sre 模块弃用
警告为现有依赖警告。

| 检查 | 实际结果 |
|---|---|
| 全量文件分片 A | 1010 passed、6 failed；6 项均为历史迁移 fixture／表清单未包含 Schema37 |
| 全量文件分片 B | 916 passed |
| 修复后机关迁移文件 | 2 passed，覆盖其中 1 个失败项 |
| 修复后感知迁移文件 | 23 passed，覆盖另外 5 个失败项；旧行保持、新表为空 |
| 最终时间专项 | 34 passed，包括 Combat pending 关闭前继续、不可取消尚未结算的到期工作 |
| 最终合并复测：时间专项＋两份迁移文件 | 59 passed、2 个既有依赖弃用警告 |
| 最终全仓收集 | 1934 项；分片初次收集 1932 项，最后补充的 2 项在最终专项中通过 |
| Ruff check／format、git diff --check | 全部通过 |
| 变更 Markdown 链接／代码块围栏 | 通过 |
| 新进程保存／恢复、World 示例 | 通过 |
| Schema37 故障注入 | 完整回滚；重试旧行相等，历史 Session 的三个新增表均为空 |

六个分片失败项均已针对所在完整文件修复复测，未在这些测试适配之后重新跑两份全量
分片；不将分片初跑写成一次完整全绿运行。生产时间代码的最后边界修改由最终 34 项
专项验证。没有尚未修复的已知测试失败。

## 保留的业务和政策边界

生产期限提供方目前是机关；18 秒 Effects、等待／活动完成示例是端口测试替身。
完整 Effects、周期伤害／豁免、休息／旅行收益、燃料／充能、天气／照明后果、秒制
NPC Runtime／OB／Gate 及 legacy 日程的 Combat 接线没有宣称完成。导航仍保留已有
逐步流程，没有擅自确定移动时长。活跃 Combat 跨进程精确续玩限制保留。
默认倍率和菜单／断线／一般 LLM 等待策略仍 OPEN，由宿主通过明确控制入口接线。

## 2026-10-20 审查修复

审查基线及实际起始 HEAD 均为 `a7c062d47331a9a8ee709cef64a7214abc214e21`，工作区
开始时干净；fetch 后远端 feature 仍为同一提交，没有需保留的后续修改。继续在
`feat/session-time-coordination` 实施，没有合并、rebase 或修改 main，RE／SRD pin 不变。
适用路径仍没有 AGENTS.md，也没有新增前端或完整领域系统。

先增加五项回归并在原实现运行，得到 **5 failed**：重启后玩家 admission 固定目标
提交至 18 而 Driver 仍为 0；重启继续实时 budget 目标后 Driver 落后；无目标及已完成
目标的 cancel 清除实时基准；暂停丢失展示余量。实际修复如下：

- TimeDriver 从已提交事实修复落后的实际基准；Runtime 在前同步成功提交后刷新 Driver，
  定时和下一命令一同恢复。realtime budget 继续同样使用这条路径。写候选／回滚不影响
  Driver；原 admission 的目标、指纹和版本继续保留，重放不推进，普通提交不反复重置
  单调起点／余量，不补算停机时间。
- cancel 按目标来源／状态区分：无目标及 completed 为回执式无操作；realtime pending
  必须继续结算；显式领域等待仅能由领域回复解除；合法活动／手动／中断目标可在当前
  无剩余到期工作的停点取消并重建基准。Combat 尚欠时间限制保留，不取消领域期限。
- 新增内部 TimeWaitConsumer 契约及 Schema38 的 session_time_waits 表／活跃等待索引。
  TypedCommand 的可选 time_wait 引用绑定 Session、具体等待及领域状态版本。共同
  执行器重复校验身份、Actor、领域授权、OCC 和幂等，在已提交边界执行领域回复；由
  领域确认 waiting／continue／interrupt／cancel，再允许继续。旧回复不能释放新等待。
- 获准回复只衔接当前等待原 admission 的精确版本链，保留固定目标和原指纹。等待期间
  其他合法写入仍产生真实冲突。RE 沿用事务外求值和短事务采用；业务／RNG／等待／
  事件／Outbox／回执同事务回滚。不会在等待中先达到终点或提前发放收益。
- TimeSync 正常暂停／detached 保留已保存的亚 tick 展示余量，停止外推；业务等待／
  Combat 仍显示整数停点。增加可选 time_wait 引用，保持旧命令无引用时的历史指纹。

端到端等待成功路径使用 `test_session_time_wait_commands.py` 中的持久化**测试领域
适配器**：从边界 waiting，经真实共同命令入口、领域写入解除等待，再继续／中断。
没有在成功路径直接删领域表行解除等待。包含部分回复、同 tick 第二等待、取消后仍处理
同 tick 工作、权限／身份／Actor／引用／版本错误、过期与重复回复、真实冲突、故障回滚、
重启，以及实际固定 RE Check 的事务外求值与采用期间冲突。这是协调契约验证；没有
合适的生产等待回复消费者，也没有声称当前机关／Combat 已经发生业务死锁。

新增 Schema37 形状数据库迁移测试，保留真实命令生成的运行行、pending 进度、admission、
回执和其他所有旧行／列；DDL 后注入故障完整回滚，重试旧行逐表完全相等，新等待表为空，
再次打开结果稳定。原历史迁移 fixture 移除 Schema38 新表并更新当前 schema 断言，未改
历史 evaluator revision 或真实时间数值。

验证均使用可控时钟／调度器、故障注入；没有用实际 sleep 等待。当前验证范围如下：

| 检查 | 实际结果 |
|---|---|
| 时间协调／恢复／等待／新增迁移专项，连同 WorldTimeService、GameTime、Session Management、Sessions、Commands | 211 passed；其中时间相关四文件共 64 项 |
| Combat Closure、Mechanism Service／World／Effects、相关迁移、Perception Efficiency、Real Checks、Dnd Runtime、Domain Runtime | 230 passed |
| 其余因 Schema38 当前版本断言调整的 14 个完整测试文件 | 415 passed |
| 补充调用方身份拒绝断言后针对该测试复测 | 1 passed |
| 补强“读取显示 DTO 前基准已修复”断言后恢复文件复测 | 10 passed |
| Ruff check／format | 通过，全仓 273 个 Python 文件已格式化 |
| git diff --check、变更文档链接／围栏 | 通过 |

三个互不重叠的回归组共 **856 passed**，另有上述 11 项加强断言后的复测通过；所有命令
均取得最终退出码 0。本次未运行全量 pytest，不能将历史分片
证据或专项测试写成本次全量通过。既有 Lark sre 模块弃用警告保留。Windows 既有 asyncio
受限运行阻塞沿用获准执行环境解决；只停止通过命令行确认属于本任务的受限测试进程。

生产边界提供方仍是机关，Combat 仍为实际 RE Round 接线；完整领域等待回复消费者、
休息／Effects／收益、跨领域同刻选择 UI 等接入待实现。默认倍率及菜单／断线／一般
LLM 等待政策仍 OPEN。
