# Session 时间协调实施记录

2026-10-19，分支 `feat/session-time-coordination`；起始 HEAD
`555421327a29fe6e1228b68d046307c69c88d9d2`。工作区开始时干净；适用目录没有 AGENTS.md，
没有 check-fast／CI 统一脚本，使用 pytest、Ruff check／format、文档链接及 diff 检查。
没有合并、更新或 rebase main，没有修改 RE／SRD pin 或上游仓库。

职责、接口和明确不支持项见 [时间协调契约](SESSION_TIME_COORDINATION.md)。

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
