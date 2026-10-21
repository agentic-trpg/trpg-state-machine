# Session memory runtime implementation — 2026-10-22

Architecture Decision Status **[DECIDED]**。当前已支持生产入口的统一读取范围、候选隔离、
增量持久提交、提交后 RAM 发布及故障暂停协议 **[IMPLEMENTED]**；验收范围和剩余成本见下文。
不据此宣称未来动态生成、任意 Effects、多人产品或任意战斗阶段跨进程续玩已经实现。
上游规范为逻辑 sibling `agentic-trpg/docs/MODULE_CONTRACTS.md` §0.3、§2.3–§2.4、§4、§6，
World §2.1b 及演进计划 §8.1a。既有带日期 Review／Progress 保留原始证据。

## 基线与范围

- 原 cwd：`D:/Projects/agentic-trpg/trpg-state-machine`；目标分支
  `refactor/session-memory-runtime`；起始 HEAD／fetch 后 origin/main
  `3aaceccf1b1b492f8dd99e3bec16b9379ac6dc77`，无需合入更新。
- 原工作区仅有用户未跟踪的 `新建 文本文档.txt`。实现隔离于
  `.worktrees/session-memory-runtime`、分支 `codex/session-memory-runtime`；未覆盖该文件。
- 架构文档来自 sibling main `5619e7aa840ca6b6474877be39d73a6dcfd1f085`；RE 只读基线
  `7bb02b955609f68fc78ff57dac2d51a0c2333366`。没有修改这两个仓库。
- SM 的 Engine／SRD pin 仍为 `a011eba3ea2226e22d8081294ee38d3ec193b325`；Schema40、
  Evaluator、Snapshot、Command／Receipt 公开协议均未升级。无新增依赖。
- 不 merge main、不创建 PR、不写 ChatGPT Pages。提交与远端结果在最终交付记录报告。

## 真实调用链与事实归属

`StateMachine` 继续装配既有领域；WorldFacts、CharacterFacts、CombatRecords、ActivityRecords、
NPC／Perception 继续保存和验证自己的事实。没有新增领域 executor 或包含所有业务的 SessionState。

`Store.connection → RuntimeConnection → SessionMemory` 是所有既有记录调用方的共同读取边界。
内部 SQL 字符串保留为兼容接口：已加载的等值范围通过 RAM 字典／身份及关系桶读取，跨投影
共享完整标量行；不为命中复查同批 SQL。冷范围在同一读取快照中加载。复杂读取保留有界、
带完整 SQL 表依赖／可识别精确范围依赖的结果。事务入口／结束使用 SQLite 元数据 fence，
不是扫描世界事实验证缓存。

`scoped_read` 记录实际底层依赖，跨操作复用解析值，返回隔离副本；保存点回滚清除操作值，
恢复稀疏候选和依赖版本。领域／授权方法没有因命中而改变公开权限入口。World 普通命令的
初始 `_snapshot` 枚举已由 `SessionRuntime.admit` 的必需局部事实检查替代；明确请求
`read_session` 时才构造完整 SessionSnapshot。

纯 JSON／Schema 解码按编码内容与固定 Schema 身份复用，输出深复制，领域前提与授权仍每次
执行。库存局部选择使用 ComponentInputs 的 owner／instance 身份索引，不再通过 SQL
json_each 重新投影已加载聚合事实；阶段取证也复用相同地图的 Schema 解码，不省略实际阶段捕获。

| 当前事实／消费者 | RAM／候选输入及持久位置 | 版本／边界 |
|---|---|---|
| Session／授权／规则身份 | `sessions`；SessionAccess 每次核对当前上下文及固定 pin | world_version／event_seq 与原绑定；缓存不授权调用者 |
| 角色、HP、资源、库存、机械状态 | `characters`、`actors`、`mechanical_actors/components`；CharacterFacts 的局部角色读取和 ComponentInputs owner／身份索引 | 原 domain version／ReadSet；聚合 JSON 改变后必要解析另计 |
| 对象、物品、门锁、耐久、容器、空间 | WorldFacts 的标量范围、容器父子桶及祖先／后代局部闭包 | 唯一位置／归属；Schema40 原触发器和事务约束保留 |
| 地图／环境／光照 | GridMaps 从同版本记录读编码；Environment 双版本／提供方协议保持；场景根、携带及后代通过 RAM 关系读取 | 准备地图仍按内容键复用；无光源时用实例成员存在性快速结束 |
| Combat／RE／RNG | RulesAdapter 从 RAM 角色／组件／地图构造固定 DTO；独立 RNGContext 解析复用 | Stateless RE 在 scope 外求值；原结果身份／OCC／RNG 转换检查保留 |
| Activity／TimeDriver／机关重置 | 原活动记录、World 唯一 game_tick、时间工作／Driver 元数据 | 不新增时钟；原倍率、工作键、generation、期限及重试语义保持 |
| NPC Meta／Task／Result／模式／输出 | NPC 当前视图与 Task 解析复用；Meta 与 Actor／已授权 KnownFacts 局部组合 | NPC 自身版本和任务资格；不机械推进 world_version |
| 事件／阶段证据／感知／可靠工作 | 捕获前后阶段使用同一候选读取；EventPersistence／原队列及 Outbox 共同持久化 | 历史和可靠查询可显式读持久记录；不以当前缓存替换历史证据 |

## 修改、提交与故障

1. 外部 RE／LLM 等等待沿原有事务外入口执行，固定输入不能被后续修改原地改变。
2. `Store.transaction(write=True)` 取得进程内同数据库 gate，先固定只读 SQL／RAM 范围。
   World／NPC／时间各自的授权、幂等、版本和局部候选判断仍在原领域执行。
3. 第一条实际持久化语句才通过 `begin_persistence` 结束准备读事务、取得
   `BEGIN IMMEDIATE`，重验持久元数据，并重建已有保存点。期间外部事实改变则暂停／重建，
   不将较新 SQL 拼到旧 RAM。保存点和整体回滚丢弃候选，不修改已发布记录。
4. 原有增量 SQL 与约束提交事实、版本、RNG、阶段证据、Receipt、Outbox 和可靠进度。
   `SessionRuntime.stage_outcome` 只是事务内的版本／事件准备；它不再叫 `publish`。
5. COMMIT 返回后，SessionMemory 在同一 gate 内增量发布受影响范围及查询索引、依赖。
   私有索引可更新，已交出的 Record／DTO／GridView 不被修改。完成后才正常退出并返回成功。

TEMP 捕获触发器逐连接安装，包含级联及 Schema40／Environment 触发器产生的实际行变化，
不增加事实表、字段或可独立写的派生权威。普通域组件仍不能自行 commit。
现有必要的跨域中间候选检查和阶段取证保留在有界的共同操作内；没有在持久写事务等待 RE／LLM。

| 故障 | 实际行为 |
|---|---|
| 准备／SQL／明确 COMMIT 前失败 | rollback，丢弃候选及待发布值；旧引用、已发布行、RNG 和回执不受污染 |
| COMMIT 返回异常且连接已退出事务 | `commit_outcome_unknown`，暂停有关 Session；按原稳定键／持久回执对账，不能直接重新执行 |
| SQL 已提交，全部或部分 RAM 发布失败 | `committed_publication_pending`；不伪报“没有提交”，也不再用 rollback 声称撤销已提交状态 |
| 暂停后的读取／修改 | `session_reconciliation_required`；同数据库的其他 Session 仍可服务 |
| `resume_session` 对账 | 清除旧表示，按持久实例、绑定、结构、机械／RNG 验收重建；失败保持暂停。原 Driver 释放，需显式重新 attach，不补离线时长 |
| 提交／发布成功但响应丢失 | 原请求重试得到原 Receipt；不重掷、不重复扣费／转移 |
| 旧读取事务在另一连接提交后结束 | 仍可持有原固定值，但元数据 fence 取消其当前缓存发布，不覆盖新状态 |
| Outbox／输出交接失败 | 原可靠记录与重试语义保留，不回滚已提交世界事实 |

进程内 gate 是数据库级的保守实现，SQLite 仍负责跨连接持久串行及必要约束。
这不是多人产品或多服务器共享世界的完成声明；独立读者可继续明确固定的旧范围。
同库写准备也串行；候选沿既有未提交 SQL 行与稀疏 RAM 覆盖共同表达，首条 DML 后必要的
级联、阶段取证和中间局部检查仍在持久事务内。没有另造 SQL 解释器或每命令复制整个 Session，
也不宣称所有领域 CPU 准备已经移出持锁范围；RE／LLM 及外部等待保持在范围外。

## 索引、容量、卸载与维护

- Schema40 增加兼容的私有 `spatial_object_lookup(session_id,object_id)` 索引。
  原空间触发器按该组合删除派生行，缺少对应索引时，局部移动会扫描无关空间单元。
  新建库直接安装；已有 Schema40 在打开时的迁移锁内补齐并校验声明。表、字段、
  user_version 和公开 DTO 均不改变，原触发器与约束保留。旧读取快照及实例事实不受影响。
- RAM 范围维护按实际列组合查找受影响范围，再按主键更新行／桶；不因修改一个对象复制或
  枚举整个已加载 Session。查询自己要求完整列表时，其输出枚举成本仍存在。
- `descendant_objects`／`related_objects` 使用局部父子索引；保留循环终止及领域拒绝、
  必需兄弟聚合、原／新祖先容量、访问与绑定检查。ContainmentState 仍是隔离操作候选。
- `total_weight` 的跨操作结果绑定完整候选行和实例配置依赖；改变关系、数量或有效重量
  会重算／失效。None 仍表示未知。未新增部分数量转移；现有 portable unit 仍限制 quantity=1。
  已支持销毁仍按原 shell／内容关系语义处理，没有按整棵子树作无依据扣减。
- 机关触发／组件索引跨命令复用；实例成员与配置改变会失效，纯 activation/status 改变
  不重新扫描配置。去重、有界级联、次数／频率和原后果均保留。
- 运行读取缓存默认 2,048 项、32 MiB 标量载荷预算；这是载荷预算，不能冒充完整 Python
  堆上限。依赖元数据也有修剪上限。只加载实际访问范围，不预装整个世界／历史。
- `unload_session` 授权后释放该 Session 的运行值、地图、环境派生与 Driver；不进入场景、
  不增加访问计数／奖励。再次读取冷加载同版本持久实例。close 清所有自有值和句柄，旧 DTO
  由持有者自行释放。RE 固定规则素材不是每 Session 可变事实。
- 场景数据按实际范围首次访问加载；当前版本外的未访问场景／历史不预装。暂不提供独立
  unload_scene API，冷范围依容量／LRU 淘汰，再读使用新的固定读取范围补入；Session 卸载
  释放其所有场景缓存。这不改变领域场景生命周期、访问次数、入场或奖励。
- 显式 `upgrade_instance_configs` 在同一 gate 内排空修改、释放旧表示，安装及校验既有
  实例后发布。存储迁移仍在打开运行层前的锁定迁移事务内完成。没有添加在线任意修复／导入 API；
  外部脚本不可把私有裸连接当作活跃 Session 的第二写入入口。

## 验收与成本记录

运行实现固定在 `bda06d1a288d27d13ad4ce861593cbe5bf764585`；最终测试树固定在
`ef287b9e350be3442f42f8dc2cf3d1ca3e7fa653`，二者运行源码完全相同，后续仅补充验收文档。
此前历史 2,015 项及旧 SQL 数字不计入本轮。
新增测试通过真实公开入口覆盖热读取／RE 输入、PutItem／TakeItem、提交前／未知／提交后及
部分发布失败、响应重放、旧引用、跨连接旧读、独立进程恢复、卸载／容量及五个合成客户端争用。
原 NPC／Activity／时间／事件／授权／迁移回归共同保护其他生产调用链。
另补充 `test_session_memory_rng_failures.py`，直接观察真实 Engine 的输入和 RNG transition，
分别注入提交前、结果未知、发布前及发布完成后返回异常；检查持久 RNG、RAM、旧 Snapshot、
Receipt 和重试不重复求值。该文件已纳入最终固定提交的完整回归。

事件投递回归原先读取 attempts 时未限定 consumer，delivery／perception 两行在无 ORDER BY
查询下的返回顺序不保证。RAM 索引暴露此测试歧义：delivery 实际为 2、perception 为 0。
最终断言精确选择 delivery，并用独立 SQLite 连接同时核对两行；整个事件文件 11 项通过，
没有修改投递实现或减弱重试要求。此前未完成／发现歧义的整库运行不计为最终通过。

最终整库按 collection 中 91 个测试文件做三个互斥组（655／636／751 项），各自独立
basetemp／JUnit，在同一固定提交执行。分组只用于本地验收，不新增项目 CI tier 或依赖。
最终交付核对 JUnit node 集合与完整 collection 完全相同、无重复和遗漏。

执行环境为原仓库 `.venv` 的锁定 Python 3.12，`UV_OFFLINE=1`、显式 UV_PYTHON／
UV_PROJECT_ENVIRONMENT／仓库 UV_CACHE_DIR。使用 `--no-sync` 避免隔离 worktree 重写原
editable 安装；pytest 的 `pythonpath=src` 与基准程序的显式路径解析到本工作树。历史迁移
fixture 继续使用各自的旧锁定环境，不以当前 Engine 绑定替代。

| 已执行检查 | 退出码／结果 |
|---|---|
| `uv run --locked --no-sync ruff check src tests examples` | 0 |
| `uv run --locked --no-sync ruff format --check src tests examples` | 0，212 files |
| `git diff --check`／暂存差异检查 | 0 |
| 修改文档的本地链接／围栏检查 | 0，158 个链接，无无效目标／未闭合围栏 |
| `uv run --locked --no-sync pytest --collect-only -q` | 0，2,042 项 |
| `uv run --locked --no-sync python tests/_session_memory_benchmark.py` | 0，见下表 |
| 完整回归组 1 | 0，655 passed，JUnit 654.697s |
| 完整回归组 2 | 0，636 passed，JUnit 637.438s |
| 完整回归组 3 | 0，751 passed，JUnit 650.180s |
| JUnit／完整 collection 集合审计 | 0，2,042 项；重复／遗漏／意外节点均为 0；failure／error／skip 均为 0 |

每组只报告相同两项既有 Lark `sre_parse`／`sre_constants` 弃用警告。数字只表示本地执行结果，
不将本地检查作为 GitHub Actions 结果。阶段性 focused 检查不重复累计到 2,042 项。

完整回归实际使用 `uv run --locked --no-sync pytest -q @fileArgs`，加独立
`--basetemp=.test-tmp/memory-acceptance-N --junitxml=.test-tmp/memory-acceptance-N.xml`
及 `-o faulthandler_timeout=60`。`fileArgs` 来自本工作树
`.test-tmp/session-memory-shards.json`；重跑全部范围也可直接使用仓库默认 `pytest -q`。
原始日志、JUnit、退出码和测量 JSON 保留于该隔离工作树 `.test-tmp`，不提交数据库产物。
集合审计结果保存为 `memory-acceptance-audit.json`；三个基准目录的测试数据库在完成后清理，
不清理原工作区的历史测试目录、其他注册 worktree 或 UV cache 链接目标。

### 可复现实测

`uv run --locked --no-sync python tests/_session_memory_benchmark.py`（exit 0），合成实例，
同一目标子树，先执行两轮 Put／Take 预热，再测 10 次公开 get_object 和 20 条公开 Put／Take。
第二组增加 150 个无关对象及 150 个远端角色，其中 50 个对象带物品数据，对象分布于现有
两个场景；额外场景／端口规模由 scoped runtime 的独立用例验证。输出位于本工作树
`.test-tmp/session-memory-measurements-final.json`；辅助程序仅创建并清理自己拥有的临时数据库。
本表来自固定源码 `bda06d1` 的独立测量；测量完成后才启动最终完整回归。

| 计数／测量 | 小实例 | 无关增长 150 |
|---|---:|---:|
| 10 次热读事实 SELECT／返回行／JSON 解析 | 0／0／0 | 0／0／0 |
| 10 次热读 RAM 行访问 | 130 | 130 |
| 20 条命令 SELECT／返回行 | 180／80 | 180／80 |
| SELECT 返回编码字节 | 69,350 | 69,350 |
| RAM 行访问／发布行更新 | 808／160 | 808／160 |
| 完整 Snapshot 构造 | 0 | 0 |
| 四条命令 SQLite VM 指令（独立计数阶段） | 8,356 | 8,356 |
| COMMIT 总时间／RAM 发布总时间（20 条，ms） | 57.5611／27.6674 | 55.4287／26.3766 |
| 操作范围总时间（20 条，ms） | 539.0203 | 543.4341 |
| 命令延迟中位／最大值（ms） | 28.8826／34.7622 | 30.0071／32.9309 |
| 缓存编码载荷字节（不是完整 Python 堆） | 606,418 | 769,134 |
| 新增 tracemalloc 保留／峰值字节 | 1,063,656／1,134,339 | 2,987,664／3,057,867 |

热读仍有 BEGIN 和 SQLite 元数据 fence（10 次共 30 条 PRAGMA），并执行权限／披露投影。
两组命令全局 `json.loads` 均计 160 次，均来自 EventPersistence 的新事件／持久证据；
顶层 InventoryState、MechanicalGrid 和 TypeAdapter JSON 校验均为 0。
这些计数独立于内容键缓存的 `json_parses=0`／`model_parses=0`；后者不能证明所有 JSON 解析为零。
`parsed_builds=159` 是 memo 方法体计算次数，含轻量包装，不等于 159 次 JSON 解析。
Pydantic 计数不是所有嵌套模型构造的总数。Snapshot 计数单独记录，不与这些混算。
当前必要阶段证据、聚合结构及持久幂等／可靠记录仍有成本，没有据此宣称所有修改零解析。

延迟包含 tracemalloc、JSON／模型和 SQL 包装计数；属于本机诊断样本，不是生产 SLO、
吞吐承诺或优化前后因果对比。SQLite VM 补充返回行计数看不到的内部触发器扫描；新增确定性
测试将返回行、RAM 访问、发布和 VM 一起比较，不以耗时相近作为规模证明。
tracemalloc 大组的新增堆分配高于小组，不能用标量载荷或本次样本声称完整 Python 堆恒定；
容量压力及关闭释放另由确定性测试验证。

仍存在必要的首次／冷范围、未知幂等键、历史／可靠工作和持久约束 SQL；不声称整个运行时零 SQL。
机械组件沿用聚合 JSON，修改后的必要重解析／整值写放大仍存在。完整 SessionSnapshot、合法
机械闭包、场景光照／几何消费者仍可能与其明确依赖规模相关；没有宣称全地图算法或区域
Snapshot 增量计算已经完成。活跃战斗、PendingChoice／Continuation 任意阶段精确续玩仍不支持。

## 实际外层写入口清单

以下为固定源码 AST 清点的 21 个函数、28 个外层写 scope；领域 Facts／Records 的 SQL 写入参与这些 scope，
不各自提交。`TimeDriver` callback 通过原 submit_command；SessionTime、Activity、机关及
PackageEffects 的内部变化参与调用方 scope。另有 Store 打开前迁移这一明确维护边界。
表中 RuntimeConnection 两项是受控内部 SQL 兼容边界，不是新游戏业务 API。

| 文件 | 实际外层写入口 | ef287b9 函数行 | 写 scope 数 |
|---|---|---:|---:|
| `commands.py` | `submit_command` | 348 | 1 |
| `environment.py` | `bind_providers` | 155 | 1 |
| `events.py` | `deliver_event` | 105 | 3 |
| `mechanical_runtime.py` | `_submit_mechanical` | 121 | 1 |
| `npc_evaluation.py` | `create_npc_evaluation_task` | 102 | 1 |
| `npc_modes.py` | `get_npc_memory_gate_input` | 65 | 1 |
| `npc_modes.py` | `request_npc_interaction` | 464 | 1 |
| `npc_modes.py` | `set_npc_inactive` | 471 | 1 |
| `npc_modes.py` | `route_npc_observations` | 477 | 2 |
| `npc_modes.py` | `report_npc_evaluation_failure` | 613 | 1 |
| `npc_outputs.py` | `dispatch_npc_output` | 203 | 2 |
| `npc_results.py` | `submit_npc_evaluation_result` | 441 | 1 |
| `perception.py` | `grant_npc_access` | 280 | 1 |
| `perception.py` | `_project_perception` | 333 | 3 |
| `rule_execution.py` | `submit` | 34 | 1 |
| `service.py` | `upgrade_instance_configs` | 427 | 1 |
| `session_creation.py` | `create` | 64 | 1 |
| `session_memory.py` | `execute` | 651 | 1 |
| `session_memory.py` | `executemany` | 729 | 1 |
| `session_runtime.py` | `prepare_timed_command` | 151 | 2 |
| `time_driver.py` | `__init__` | 60 | 1 |
