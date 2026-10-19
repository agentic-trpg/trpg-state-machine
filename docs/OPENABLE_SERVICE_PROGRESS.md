# OpenableService 范围与验收记录

2026-10-20：操作侧、jam／force／lock policy／DC 改读持久实例配置，状态及凭证继续由原表独占。
原外部 ABI 阻塞已由 a011eba 整合提交解除，Engine／SRD 同时固定该完整 SHA。
Openable 外层 /1 使用真实嵌套 Evaluation /24、Snapshot /17、evaluator /37 和 stage facts /2；
冻结素材与普通求值共用，合法失败 accepted、工具检查及原子提交保持。uncovered 阶段不改写覆盖。
详见 [实例闭包与冻结素材接入](INSTANCE_CONFIGURATION_PROGRESS.md)；下文旧 pin／数字为历史批次。

**状态：部分交付，未完成原任务的完整四阶段范围。** 阶段 3 的战斗费用、魔法封闭及生命周期未实现；拒绝测试不计为玩法完成。

基线：SM `a8e7a6293b800a15256fa81971f43b1ee0d60553`；分支 `feat/world-openable-service`。
开始时核实 HEAD 与 origin/main 为 0/0、工作区干净。GC5 stash `GC5 work before Openable refactor` 未恢复或修改；未创建额外 worktree。SM 及祖先目录未发现适用 AGENTS.md。RE 遵守其根目录 AGENTS.md，未合并 main 或修改 upstream。

设计依据：已读取项目与 Downloads 中的 `agentic-trpg-architecture-contracts-2026-10-17-v0.4.md`（未找到带 `(1)` 的文件名；两份 SHA 不同但文本比较无差异），以及 MODULE_CONTRACTS、WORLD_SUBSYSTEM_DESIGN、SM 文档和现有测试。历史完成状态不作为本次验收证据。

## 实际完成范围

| 范围 | 原有能力与本次变化 | 状态 |
|---|---|---|
| 普通门／箱盖开关、钥匙、多锁、门闩、操作侧、触及、共享门 | 原有 W1-W8 能力；本次统一提取，删除 Handler 锁具业务和机关直接开合写入 | 已实现、已验证 |
| 重复操作、真实钥匙实例与可取用性 | 保留原有校验；新增重复开门及只读查询回归 | 已验证 |
| 撬锁成功／有效失败／非法请求 | 新增 PickLock 与真实 RE 工具检定；只解开指定锁，保留其他锁／门闩，不自动开门 | 已实现、已验证（非战斗受限角色范围） |
| 强行开启、工具条件、解除卡住 | 新增 ForceOpen／ClearJam、显式后果、撬棍条件；失败无凭空损坏或陷阱 | 已实现、已验证（非战斗） |
| 卡住方向 | 新增 prevent_opening／prevent_closing；旧 prevent_transitions 默认与旧存档保留 | 已实现、已验证 |
| NPC／Activity／Package／检定后果／机关开关 | 原有调用方统一走 OpenableService；远程机关仍需可信图资格 | 已实现；既有回归与最终全量验证覆盖 |
| 空间终结／原子性 | 新增共享 RuleExecution；规则后果也执行最终空间检查；成功检定的非法空间结果整体回滚 | 已实现、已验证 |
| 容器访问、光照、摧毁状态 | 继续读取同一 WorldFacts；原有耐久应用复用，不另写攻击／伤害公式 | 原有能力；相关回归验证，真实物体攻击未完成 |
| 查询信息隔离 | 隐去凭据、DC、force method 配置；未知 lock_id 返回通用原因，查询不写状态／回执或掷骰 | 已验证（当前公开对象／公开锁策略范围；未实现隐秘锁发现模型） |
| 战斗物体互动／Utilize／Fast Hands | 保留 guard；新增真实活动战斗的原子拒绝与回执回归 | 未完成；拒绝测试不是费用集成 |
| Arcane Lock／Knock | 核对 2024 规则；没有新增占位法术接口 | 未完成 |
| 新 NPC EVA 撬锁／强行开启词汇 | 可信直接 NPC 命令使用统一执行；未增加 EVA Result DTO／自动 handoff 转换 | 未完成 |

## 架构与命令

后续 ContainerService 专项新增最小只读接口 `allows_contents(facts, sid, oid)` 与
`contents_accessible(sid, oid)`：前者统一 shell 访问语义，后者组合祖先链。
WorldFacts.item_accessible、ContainerService.require_contents_access 和对象内容查询复用该语义；
开合／锁具状态仍只有既有 WorldFacts 一份。详见 [容器专项记录](CONTAINER_SERVICE_PROGRESS.md)。
此接口补充不改变下列 Openable 战斗／魔法等未完成边界。

WorldFacts 保有唯一对象实际状态及必要一致性保护；OpenableService 组织状态转换与检查，不缓存第二份状态。WorldHandler 保留流程和通用位置／授权辅助。WorldObjectService 调用服务，不回调 Handler 执行锁具业务。空间计算、HP、容器、事件／感知与角色机械事实仍由各自所有者管理。

`world.pick_lock`: `PickLock(scene_id, object_id, lock_id, tool_instance_id, expected_cell)`。
`world.force_open`／`world.clear_jam`: `ForceOpen`／`ClearJam(scene_id, object_id, method_id, tool_instance_id, expected_cell)`；无工具方法用显式 null。
继续使用 sm-command/1、query_world_action、既有 submit_command/submit_command_async、command_id 指纹、OCC、回执、RNG、事件和共享短事务。玩家不因操作而获得 GM rules.check 授权；客户端不能传 DC、成功结果、trusted、bypass_checks 或任意 patch。

新定义要求 Package `/7`。锁 pick_policy 为 unconfigured／srd_lock／scenario；特殊锁必须显式 pick_dc，旧锁不会自动获得普通装备锁规则。ForceMethod 封闭字段为 method_id、ability、skill、dc、required_tool、leverage_applicable、unlock_lock_id、clear_bar、clear_jam、open_after。成功只应用声明后果，剩余阻碍与最终空间约束仍有效。旧 Package `/1`–`/6` 原始 digest 与现有对象行保留；方向性卡住策略来自固定定义，无需存储迁移。

RE 求值在 SQLite 事务外；提交前重验快照、版本、RNG 和对象操作资格。非法／过期请求无机械副作用，有效失败提交实际 CheckRolled 与 RNG。已完成操作不重复掷骰或生成事件。解除卡住事件已通过现有有序 Perception 队列形成授权 NPC Observation。

## 具体剩余依赖

| 依赖 | 实际缺口 |
|---|---|
| Combat／RE 费用 | 无物体交互费用请求、免费互动计量及获准预算 delta；未完成角色特性费用组合 |
| 战斗环境 | _spatial_combat_guard 仍拒绝闭合通路门；机械快照无完整动态门环境投影 |
| Episode | src 中没有 Turn／Round Episode 提交与消费实现；既有机械事件感知不能冒充目标模型 |
| Effects／世界目标法术 | 缺少获准的物体法术结果、材料／法术位支付与对象效果生命周期链 |
| TimeBinding | game-clock/1 是抽象 tick；v0.4 C12 明确禁止未经迁移改称秒，无法直接把口令／压制期限写成 tick |
| 真实破坏入口 | 现有内部耐久 settlement 不等于真实 RE 物体攻击，也不是独立锁 HP |

这些需要继续实现对应所有者的契约和测试，不是 D&D 规则不允许，也不是权限或网络阻塞。没有为了此次提取提前重构全部 Combat／Effects／时钟或新增细粒度战斗感知流水线。

## 规则来源

已在线核对官方 [SRD 5.2.1 PDF](https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.1.pdf)：Tools pp.93–94、Crowbar p.97、Lock p.98、Arcane Lock p.110、Knock pp.143–144。普通装备锁按修订规则，撬棍仅在适用杠杆条件下提供优势；没有混入 2014 Arcane Lock 提高 DC 的规则。站位、公开场景、活跃效果／角色特性及战斗拒绝等保守限制是当前工程覆盖边界。

## 提交与依赖

| 仓库／阶段 | 分支与提交 | 说明 |
|---|---|---|
| SM 阶段 1 | feat/world-openable-service / e33d722 | 统一现有业务 |
| SM 阶段 2 | 同分支 / 7d12ea0 | 撬锁／强行开启／解除卡住、共享规则提交 |
| SM 阶段 3 | 同分支 / 590446c | 原子拒绝一致性及明确未完成的依赖审计；不是战斗／魔法完成 |
| SM 阶段 4 | 本记录所属最终交付提交（分支 HEAD） | 一致性测试、文档、最终依赖与交付检查 |
| RE 支持 | feat/world-openable-support / cf808270、d5961610 | 从实际旧固定 SHA 67555240 开始，未采用最新 main 假定能力 |
| RE 分支 HEAD | 74d9e0800c22a740614af72930dd77dd3c064561 | 已推送并核对远端；末提交仅补验证记录 |
| SM 最终依赖 | d596161060555630019163ab301e1d25fd816549 | Engine 与 SRD 精确固定同一运行代码提交；锁文件检查、锁定安装及两包 direct_url 元数据验证通过 |

## 验证记录

- SM 阶段 1：62 项通过；阶段 2 冻结相关回归：126 项通过；后续操作侧提取相关 44 项通过。
- SM 新增行为 19 项、阶段 3 拒绝边界 10 项、阶段 4 一致性／输入校验 6 项均通过，并包含于最终完整回归。
- RE integration planner 因原有未跟踪 .evaluation-worktree 选择全工作区；tooling 24 项、Data 671 passed / 1 skipped、Bridge 139、Demo 54 通过；静态检查、3 个示例和严格 MkDocs 通过。
- RE 通用签名修复后的完整引擎重跑：8,859 passed，636.67s。最终撬棍局部修正：8 项专项及严格类型通过。属于分步修复／恢复的集成计划证据，**不是最后一次单命令全工作区 gate 或撬棍修正后重新冻结的完整 RE 全量**。
- SM 最终 uv lock --check、uv sync --locked 与两包安装元数据核对通过；完整文件分区 342 / 368 / 331 / 254，共 **1,295 passed**，全部退出码 0，与完整收集的 1,295 项一致。覆盖真实 World 示例及新进程恢复。Ruff、格式检查（152 文件）与 diff 检查通过。推送远端 SHA 由最终报告核对。

初次 pytest 因默认临时目录权限失败，后续使用仓库内 basetemp；原有 RE 故障注入回归曾因新关键字失败，已恢复通用入口调用签名并通过 474 项专项和完整引擎重跑；未削弱断言。无远端 CI 已通过的声明。


最终分区清单 `.test-tmp/openable-final-manifest.json`：64 个文件，无重复／遗漏。
成功日志 `.test-tmp/openable-frozen-p0.log`（186.53s）、`p1.log`（204.10s）、
`openable-frozen-p2-repaired.log`（159.44s）、`p3.log`（214.78s）；p1／p3 的完整前缀同 p0。
第一次完整串行检查暴露共享异常捕获过宽：畸形 XP 结果被误当领域拒绝，已恢复其抛错／无回执协议；36 项专项通过。
最终分区曾发现安装校验仍期望旧 SHA，已同步为明确升级的精确 SHA，未弱化匹配断言，只重跑受影响分区并通过。
最后交付的生产代码在全部成功分区间保持冻结；安装校验的单行测试预期变更已由修复分区覆盖。

**完整原任务仍未完成；1,295 项通过不是战斗费用、魔法生命周期或真实物体攻击完成的证据。**

## 2026-10-17 审阅修复

基线与修复前 HEAD：`018ea8cc4452b6e412eb3c92bb40e753efb9337d`；在原分支、原目录修复，开始时工作区干净，无额外 worktree。未修改 RE 仓库及依赖，未涉及 ContainerService 接口或重构；GC5 stash 保持原对象 `d24e26fb305b37c04794a10cfa1c2bd5e0826ca5`。

| 审阅项 | 本轮新增修复／验证 | 状态 |
|---|---|---|
| locks 非 mapping 条目异常 | 在默认字段合并前校验类型；公开 YAML 加载的空条目、null、字符串、数字、列表返回 invalid / schema_error 与条目路径；保留未知字段、引用和策略约束 | 已实现；专项验证 |
| 合法旧 Package | 单锁、多锁公开加载；对照基线校验函数计算的 digest 金值，解释与 digest 不变 | 已验证 |
| 已解锁 PickLock | 先校验身份、授权、访问及操作侧；查询 available / already_unlocked（该锁已解开，无须撬锁）；执行仅提交幂等回执，不检查工具／撬锁策略，不建立规则请求，不消耗 RNG、事件或世界版本 | 已实现；专项验证 |
| 多锁与访问协议 | 错误／不存在工具、仍锁住的另一把锁、未知锁、无权限、错误场景、超出触及、操作侧、过期版本、重复提交与关闭数据库重放 | 已验证；不自动打开对象 |
| 原始操作事件审计 | 在既有 rules.check_rolled 的 adjudication.openable_operation 保存 command_type 和原 payload；保留通用检定与 source_command_id；无需历史事件包含新字段 | 已实现；真实 PickLock / ForceOpen / ClearJam 成功与有效失败、落盘重开、幂等及回滚验证 |
| 感知边界 | 审计字段不生成 Signals，无重复事件，无新增 LLM / JEV / EVA 调用；EventPersistence 无需理解撬锁业务 | 已验证事件 Signals 隔离；现有投影回归覆盖 |
| 跨平台测试 | 新进程重放测试 PYTHONPATH 改用 os.pathsep.join；本轮其他新增测试无同类硬编码；保留重启和数据库全表不变断言 | 已实现；新进程回归验证 |

本轮最终验证（旧段落的 1,295 项属于审阅基线的历史结果）：

- 新增回归 33 项；Openable／Package／规则提交／事件追溯／重启及回滚专项合计 **210 passed**，退出码 0。
- 完整测试文件清单 `.test-tmp/openable-review-manifest.json`：65 个文件，核对无重复或遗漏；四分区 **375 / 368 / 331 / 254，共 1,328 passed**，退出码全部为 0。日志 `.test-tmp/openable-review-full-p0.log` 至 `p3.log`；各组 pytest 时间 149.24 / 153.48 / 180.71 / 163.54 秒。生产代码在全部最终回归期间保持冻结。
- Ruff 检查、格式检查（217 文件）、git diff --check 通过；uv lock --check --offline 通过，安装的 Engine／SRD 元数据 SHA 均为未变更的 `d596161060555630019163ab301e1d25fd816549`。首次受限沙箱的 uv 缓存无权限，改在正常本机缓存核对成功。
- 初轮新增测试的地图越界夹具及适配器属性名错误已修正；未跳过或削弱测试。仅有现存 Lark 正则模块弃用警告，无远端 CI 通过的声明。

完整 Openable 原任务仍未完成：战斗费用、Arcane Lock／Knock 魔法生命周期及真实物体攻击等上述未完成项没有因本轮修复改变状态。
