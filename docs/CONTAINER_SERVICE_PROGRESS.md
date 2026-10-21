# ContainerService 专项修复

2026-10-20 冻结规则后续：Engine／SRD 同 pin a011eba，容器 require_unbound 的规则素材读取
共享 WorldHandler 注入的固定对象；实例字段、邻接索引与重量 memo 的权威／生命周期不变。
回归与当前限制见 [本批记录](INSTANCE_CONFIGURATION_PROGRESS.md)。

## 2026-10-20 内容物索引、候选关系及重量复用

ContainerService._rows 将一次读取的 objects 装配为 ContainmentState；直接子关系只建一次，
subtree／total_weight／audit／require_unbound 复用，WOS 列表共用同一读取上下文。
relocate 更新候选邻接关系并清空重量 memo；对象行只读。没有 Service／Store 全局状态缓存，
拒绝、回滚及新事务不会复用旧结果；objects.container_id 仍唯一权威。

101 节点检查计数 **10201 → 101**；102 节点两层检查 **10404 → 102**。
同一状态重复遍历不增加扫描／索引构建；子树重量及定义读取每节点一次。
循环／非容器已有内容物／缺失关系在未知重量下仍拒绝。重量／数量／体积／两层嵌套规则保持。
实际 SQLite 计划原来选择 Session PK，Schema39 将既有 objects_container 扩展到
(session_id,container_id,object_id)，支持有序直接内容物读取。
配置实例闭包、生命周期、事务和最终验证见 [本轮记录](INSTANCE_CONFIGURATION_PROGRESS.md)。
下文为此前范围修复的历史记录。

基线：`9b35d1949896aad56359ea52b2211cc05c2b709a`，开始时 main 工作区干净。
交付分支：`codex/container-service-fix`；不合并 main，不修改 RE/SRD pin、GC5 stash 或无关模块。
仓库（包含隐藏目录，排除依赖与临时工作区）及祖先目录未发现适用 AGENTS.md；
不存在 check-fast 脚本或命令配置，使用实际 pytest、Ruff、format 与 diff 检查。

## 核实与进度

| 问题 | 基线代码证据 | 本轮结果 |
|---|---|---|
| 无关容器阻断正常取放 | validate 修改候选父链接后无条件 audit(candidate)；audit 遍历 Session 全部容器 | 已修复；修复前公开命令回归稳定拒绝 unknown/无关超重，修复后放入及取出通过 |
| 嵌套深度 | 每条父链接计数，逐祖先 nesting_limit 与硬上限 8；容器自身未计入 | 已统一为完整路径最多两个容器；普通物品不计；加载子树的整体移动也检查 |
| 访问判断重复 | WorldFacts.item_accessible、require_contents_access 与 view 分别比较 closed/locked/destroyed | 已统一到 Openable 的只读 shell/祖先链语义 |
| 装备误绑定 | require_unbound 将角色 slug 装备及 other_hand_occupied 应用于地上独立实例；任意 worn_armor 又拒绝所有 armor（包括未装备盾牌） | 已修复为仅检查原本由该角色拥有的移动实例／子树；身体护甲匹配实际类别；同类别／slug／手部身份歧义和已装备盾牌仍拒绝 |
| 禁用效果误拒绝 | effect_states 只比 target_id，没有判断 disabled | 已修复；其他角色效果原本已排除，本轮增加回归；操作角色活跃效果保护保留 |
| 材料与生命周期 | 材料按 inventory_instance_id 检查；无容器转移的卸装／调谐／物品效果生命周期链 | 保留材料及子树保护；明确拒绝移动需要调谐／被动效果生命周期的实例，无暗中卸装或改效果 |
| 事务、权限、幂等 | 既有命令授权、OCC、command_effects savepoint、回执与库存一致性检查 | 沿用；新增转移／投影／after evidence 故障回滚、查询无写、重放、无 RNG 验证 |

## 实际调用路径与事实所有权

`PutItem` / `TakeItem` 在闭合 command registry 中注册。
`submit_command` → 权限、指纹重放、expected_world_version → `command_effects` savepoint
→ WorldHandler（spatial.py）的 `operate` → WorldObjectService.operate
→ ContainerService.operate / validate → WorldFacts.transfer_item
→ CharacterFacts.project_item_transfer → 原有事件与最终一致性检查／回执。
取放的 before/after evidence 及父链接、库存投影与事件提交共用同一短事务。
公开 `query_world_action` → WorldObjectService.query / validate → 同一 ContainerService.validate。
查询不做转移、不写回执、不消耗 RNG；版本仍须执行时重验。

WorldFacts 的 objects 排他位置（地面／直接 carrier／container_id）仍是唯一位置与归属真相。
祖先得到有效位置、根 carrier；CharacterFacts 的 inventory 是同事务投影。
空间判断继续使用现有 public scope、WorldFacts.placement、基础拓扑可见性及移动拓扑邻接触及；
没有在 ContainerService 中实现空间算法、开锁、开关转换或规则费用。

## 准入范围与必要数据

先从原关系收集移动实例的完整子树及原祖先，再在候选关系收集目标祖先。
对这三部分的并集验证候选路径、父容器资格、容量、fit/content policy：

- 移动的每个子树节点都检查移动后的完整祖先链，拒绝自含、间接循环、缺失父节点及第三个容器。
- 原容器及全部原祖先检查移出后的约束，目标容器及全部新祖先检查移入后的约束。
- 相关容器容量计算读取其全部直接内容物；递归重量包含各内容物的完整子树及各实例自身重量 × quantity。
- 体积保持已有语义：直接内容物的外部体积 × quantity，装入袋内不另加一次袋内体积。
- 相关容器阈值 `unknown` 仍拒绝（包括取出后），`null` 表示明确无该维度限制。
  有限重量／体积约束所需的未知测量仍拒绝；不把未知当零。fit/content metadata 保持必需。
- 无关容器的局部容量、fit 等问题不参与准入；全表行读取是关系／聚合查找依赖，不代表全局容器验证。
  `audit(rows, package)` 保留为独立全局诊断；命令使用 `audit(..., affected=...)`。

`nesting_limit` 旧 Package 字段继续按 1..8 解析，以保留旧摘要及存档格式；运行时统一两层，
旧字段不再调节本操作的层数。示例与新夹具使用 2。允许“背包 → 钥匙包 → 钥匙”；
既有三层存档不被静默改写，相关转移依据候选完整路径准入，移出以恢复两层可以成功。

命令层原有机械事实／库存闭包完整性检查仍保留。该修复没有使非法库存、缺失 owner 或损坏数据库合法化。
活跃导航、战斗、操作角色条件／浓度／活跃效果等原有规则边界仍拒绝；不能据本轮验证声称完成费用裁决。

## Openable 访问契约

`OpenableService.allows_contents(facts, sid, oid)`：只读单个 shell，读取既有 WorldFacts；
完整 shell 关闭、上锁或门闩封闭时不可访问；已打开但卡住的 aperture 仍可访问，
卡住按既有方向策略阻止相应转换。
摧毁 shell 保持已有策略：保留包含关系、允许访问其内容物；不存在新散落或摧毁概率。

`WorldFacts.item_accessible(..., include_self=False)`：逐祖先调用上述 shell 查询。
默认 include_self=True 仍禁止使用已摧毁实例自身，保持钥匙／库存消费者契约。
`OpenableService.contents_accessible(sid, oid)` 组合祖先链和本 shell；
`ContainerService.require_contents_access` 先验证 container capability，再复用此查询。
`WorldObjectService.view` 同样复用，封闭内容返回 None，隐藏内容不再做重量聚合。
外箱关闭时 get_object 内袋返回通用 invalid_target，列表不显示内袋／物品；重新打开恢复。
这些只读接口不包含空间触及或命令权限，调用方仍必须建立授权和空间范围。

## 后续待办与范围排除

| 所有者 | 未完成事项 |
|---|---|
| 物品数量管理 | 部分数量转移、拆分／合并；本轮始终整体移动一个实例，保留原 quantity/charges |
| NPC Evaluation / Activity | 新的 NPC 容器意图到命令接线；现有已授权直接 NPC PutItem / TakeItem 复用同一路径 |
| RE / Durability | 容器摧毁时每个直接内容物实例 60% 概率整组摧毁、真实物体攻击及结果提交 |
| Equipment / Effects | 按实例卸装、解除调谐、物品被动效果生命周期；当前相关转移明确拒绝 |
| World / 专项规则 | 液体泄漏、散落模拟、特殊魔法容器规则；均未实现 |

未重构 WorldTimeService、EnvironmentService、Event／Episode／OB 系统。

## 验证记录

修复前公开命令专项：4 failed，准确复现无关 unknown／超重阻断和两个第三层绕过问题。
后续护甲专项修复前 1 failed / 1 passed，复现穿轻甲时未装备盾牌误拒绝，并确认相关轻甲仍拒绝。
最终生产代码冻结后实际结果（均退出码 0）：

| 命令／范围 | 结果 |
|---|---|
| `.venv/Scripts/python.exe -m pytest tests/test_container_admission.py -q --basetemp=.test-tmp/container-final-50` | **50 passed**，16.13s |
| 下列 27 文件最终组合，`-q -o faulthandler_timeout=30 --basetemp=.test-tmp/container-combination-final` | **469 passed**，176.22s；包含全部 50 项专项，不与专项累加 |
| `.venv/Scripts/python.exe -m ruff check src tests examples` | All checks passed |
| `.venv/Scripts/python.exe -m ruff format --check src tests examples` | 161 files already formatted |
| `git diff --check` | 通过 |

最终组合日志 `.test-tmp/container-combination-final.log`；包含真实 World 示例及新进程恢复、
机械库存／命令／迁移／锁具／Openable／事件证据与故障注入回归。
仅有现存 Lark 正则模块弃用警告。没有执行仓库全量，也没有远端 CI 通过声明。
本机 Engine/SRD direct_url 元数据均核对为未变更的 `d596161060555630019163ab301e1d25fd816549`。
不引用历史全量或中断运行作为本轮通过证据。

可复现的组合文件选择（27 文件，不是仓库全量）：

```powershell
$containerFiles = @(
  'tests/test_container_admission.py', 'tests/test_item_transfers.py',
  'tests/test_lockable_passages.py', 'tests/test_lockable_boundaries.py',
  'tests/test_lockable_migration.py', 'tests/test_gc1_doors_and_effects.py',
  'tests/test_conditional_world_effects.py', 'tests/test_event_local_evidence.py',
  'tests/test_event_traceability.py', 'tests/test_rf4_position_evidence.py',
  'tests/test_migrations.py', 'tests/test_commands.py'
) + @(Get-ChildItem tests/test_world_*.py,tests/test_openable_*.py |
  ForEach-Object { $_.FullName })
.venv/Scripts/python.exe -m pytest @containerFiles -q -o faulthandler_timeout=30 `
  --basetemp=.test-tmp/container-combination-final
```

初次受限沙箱组合运行中断，**没有完成结果**。单用例使用
`-vv -o faulthandler_timeout=20` 定位 Windows asyncio 创建本机 socketpair 时
停在 socket.accept / _fallback_socketpair / _make_self_pipe，尚未执行 RE 求值。
自动审批后在本机沙箱外重跑，未修改事件循环、生产代码或断言来绕过环境限制。
护甲类别最终修正前的同范围组合为 467 passed（180.21s）；这不是最后代码的最终 gate。


## 2026-10-20 跨仓库对齐与证据适用范围

本文件带日期批次／分支／Schema／“未发现 AGENTS”属于原时点证据；最新 main 为 Schema39，实例配置与冻结素材生产装配见 [Instance Progress](INSTANCE_CONFIGURATION_PROGRESS.md)，时间协调见 [Time Progress](SESSION_TIME_COORDINATION_PROGRESS.md)。旧缺口表不能覆盖上述已完成切片。当前开发入口为仓库 AGENTS.md；共同规范只维护于顶层 MC §2.3、§6.5–§6.6。全局审查、局部准入、实际 SQL／投影范围及剩余重构详见 [校验范围审查](reviews/RUNTIME_VALIDATION_SCOPE_REVIEW.md)。本次只更新文档，没有运行新增业务／规模测试。


## 2026-10-20 Scoped runtime validation refactor

本批实现采用 Schema40 的事务内派生占格索引和结构验收记录，普通命令由领域
局部准入保护，操作内复用读取，提交不再默认完整空间审查或库存重建。
逐项能力、读取范围、存档兼容、实际计数及本批验证结果见
[系统性重构验收](reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md)。
上文历史测试数字与旧 Schema/待办保持原批次语境，不作为本轮验证结果。

## 2026-10-21 全系统复用原则与跨操作重量缓存目标

**Architecture Decision Status：`[DECIDED]`；跨操作内存重量缓存 Implementation Status：`[NOT IMPLEMENTED]`。** 共同规范见 [项目 MC §2.3](https://github.com/agentic-trpg/agentic-trpg/blob/docs/runtime-reuse-and-references/docs/MODULE_CONTRACTS.md#23-运行时数据复用派生-snapshot-与版本有效性-decided)，容器应用见 [World §2.1b](https://github.com/agentic-trpg/agentic-trpg/blob/docs/runtime-reuse-and-references/docs/WORLD_SUBSYSTEM_DESIGN.md#21b-容器重量派生与跨操作复用)，规则／世界观／工程资料见 [统一入口](https://github.com/agentic-trpg/agentic-trpg/blob/docs/runtime-reuse-and-references/docs/REFERENCES.md)。链接指向配套未合并文档分支；逻辑工作区中对应 `../agentic-trpg/docs/`，不表示 main 已包含本次补充。

本次静态核对源码为 `refactor/scoped-runtime-validation@4341f69cbb560edd9c9c1b1bd907ed6c599c0ed0`，**该重构分支尚未合并**；核实的 origin/main 为 `7927a382738fc97e2420967c3e1c6280c717d1fb`，不能将该分支能力写成 main 已有能力。

- [world_containers.py（固定 SHA）](https://github.com/agentic-trpg/trpg-state-machine/blob/4341f69cbb560edd9c9c1b1bd907ed6c599c0ed0/src/trpg_state_machine/world_containers.py) 的 `_rows` 每次建立 ContainmentState；`weights` 是单次操作状态内 memo，`relocate` 更新候选邻接并清空它，没有发布为 Service／Store 跨操作缓存。
- `total_weight` 合计实例自身重量 × quantity 与全部后代；容量检查对直接内容物分别求 total_weight 后求和，因此“内容物重量”和“容器自身加内容物总重量”不同。未知重量传播为 None，不能按零计算。现有数量读取不表示支持部分数量转移。
- `validate` 在移动候选上覆盖移动子树、原祖先与目标祖先的必要关系／聚合闭包；[test_container_admission.py](https://github.com/agentic-trpg/trpg-state-machine/blob/4341f69cbb560edd9c9c1b1bd907ed6c599c0ed0/tests/test_container_admission.py)、[test_world_containers.py](https://github.com/agentic-trpg/trpg-state-machine/blob/4341f69cbb560edd9c9c1b1bd907ed6c599c0ed0/tests/test_world_containers.py) 及[重构验收记录](reviews/SCOPED_RUNTIME_VALIDATION_REFACTOR.md)提供对应源码／历史证据。本次没有重新运行这些测试或重测性能。

本次确定的后续设计是按需建立、跨操作复用的内存缓存，不要求新增持久化总重量字段。仅在修改前缓存有效且完整掌握变化时按差量更新；移动整个容器按真实子树贡献作用于旧／新必要祖先，共同祖先合并净变化。数量、有效重量、关系和实际摧毁后果都须维护依赖；旧值或变化不可靠时失效并按需重算。未知、权限、容量、循环、嵌套、版本及事务检查保持。当前摧毁 shell 保留内容并开放访问，不能因“摧毁”一词自动扣掉整个子树；未实现的新摧毁规则不能冒充既有行为。

本批只追加设计／实现差异与查阅要求，不改代码、Schema40、依赖 pin、规则数据或 CI；没有实现跨操作缓存，也不改写上文历史日期、测试数字和证据范围。
