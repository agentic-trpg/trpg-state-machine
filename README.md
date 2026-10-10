# Agentic TRPG — State Machine Foundation

Batch 1 实现了一个可运行的本地闭环：**验证原创合成 Adventure Package → 创建隔离 Session → 初始化/重访 Scene → 修改实际状态 → SQLite 持久化 → 新进程恢复**。

State Machine 是全部已实现可变游戏状态的唯一权威拥有者和提交者。Package 是不可变模板；返回的 Snapshot 是独立副本，修改副本不会写入数据库。当前没有 Rule Engine 或第二份运行态权威。

## 安装与验证

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。运行时仅依赖 PyYAML，SQLite 使用 Python 标准库；pytest/Ruff 是开发依赖。`uv.lock` 固定依赖版本。

```powershell
uv sync --locked --group dev
uv run --locked python -m pytest -q
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
```

如果受限 Windows 环境无法使用系统临时目录，可把测试临时目录放在仓库内：

```powershell
New-Item -ItemType Directory -Force .test-tmp | Out-Null
uv run --locked python -m pytest -q --basetemp=.test-tmp/run
```

数据库、WAL/SHM、虚拟环境、缓存和测试临时目录均由 `.gitignore` 排除。数据库应放在支持可靠锁和同步的本地文件系统上。不要只复制正在使用的 `.db` 文件作为备份；当前未提供备份工具。

## 已实现的接口

以下是受信 Python 进程内接口，不是冻结的跨模块网络协议或玩家/NPC View。

| 接口 | 行为 |
|---|---|
| `load_adventure_package(local_yaml_path)` | 安全解析并验证单文件逻辑 Package；返回 `PackageLoadResult`，仅 `valid` 含不可变 Package；`invalid` 返回 `code/path/message`，不暴露半验证包 |
| `StateMachine(database_path)` | 打开/初始化 SQLite 文件，支持上下文管理器与显式 `close()`；每实例一个连接，只在创建它的线程中使用 |
| `create_session(request, package, *, context)` | 可信 `(principal_id, command_id)` 持久幂等；返回原始 `CreationResult` 有限元数据；同键不同 Fingerprint 抛出 `idempotency_conflict` |
| `read_session(session_id, package, *, context)` | 在一致的读事务中验证主体、玩家控制关联和 Package/Ruleset 固定值，返回独立运行态 Snapshot |
| `save_session(...)` | 所有修改已同步提交；此方法校验并读取当前已提交存档，不接收整份状态覆盖，不额外推进版本 |
| `resume_session(...)` | 从数据库恢复同一 Session；不重放初始模板，不做自动迁移；身份/版本、Digest 或 Ruleset 不一致时明确拒绝 |

运行时拒绝使用 `StateMachineError`，通过 `.code` / `.issue` 获取结构化原因；底层 SQLite 故障保留在异常 cause 中。Package 加载采用首错返回策略，当前没有非阻塞 warnings。

最小创建/恢复示例（从仓库根目录执行）：

```python
from trpg_state_machine import (
    CreateSessionRequest,
    PlayerSelection,
    StateMachine,
    TrustedContext,
    load_adventure_package,
)

loaded = load_adventure_package("tests/fixtures/lantern_station.yaml")
if loaded.status != "valid":
    raise ValueError(loaded.errors)
package = loaded.package
assert package is not None

# 只能由已经认证、已核验玩家控制关系的适配层构造，不能从请求 JSON 接收。
context = TrustedContext("authenticated:demo", "player.demo")
request = CreateSessionRequest(
    command_id="cmd.demo.create",
    adventure_id=package.adventure_id,
    package_version=package.package_version,
    package_digest=package.package_digest,
    players=(PlayerSelection("player.demo", "pc_template.visitor"),),
)
with StateMachine("demo.sqlite3") as runtime:
    created = runtime.create_session(request, package, context=context)
    snapshot = runtime.read_session(created.session_id, package, context=context)

with StateMachine("demo.sqlite3") as restarted:
    restored = restarted.resume_session(created.session_id, package, context=context)
    assert restored == snapshot
```

`TrustedContext` 只表示集成信任边界：本库不实现登录、认证服务或主体/玩家目录。受信适配层必须先核验控制关系；本库再核验请求的 `player_id` 与可信上下文一致，并限制后续访问为原主体/玩家。不要把这些内部 Snapshot 直接提供给模型或客户端；当前没有权限过滤的角色视图。

## 当前 Package 子集

`tests/fixtures/lantern_station.yaml` 是完全原创、按 CC0-1.0 发布的合成数据。没有 First Blush 原文、地图或派生故事内容。

格式为 UTF-8 单文件 YAML，包含 `manifest/world/scenes/actors/pc_templates/events/rule_bindings` 逻辑数据；本地格式版本是 **`state-machine-foundation/1`**，不宣称冻结了总控仓库的完整 Adventure Package Schema。

- 校验必填/额外字段、命名空间化 ID（小写点分 ID、最长 128 字符）、作用域唯一性、Scene/Location/Actor 引用、入口和地点对应关系、Scene 可达性及多个内置 PC Templates。创建严格选择一个玩家、一个 PC Template；未知模板拒绝。
- `RulesetBinding` 必须精确等于 `synthetic.foundation / synthetic-data/1 / none/1`。这个合成数据契约仅包含 HP 初值及空 resources/inventory；它没有 D&D 属性、检定、构建合法性或 Rule Engine 求值能力。
- World/Scene flags 仅允许命名空间为 `flag.` 的布尔值。对象状态仅支持合成 `closed/open`。出口只能是无条件的已定义 Scene 连接。
- NPC 是无自主行为的合成身份，`controller.kind=scripted_event` 仅保存静态策略标记，不执行脚本。按初始 Scene 首次需要时创建一次；另一 Scene 的身份引用不会克隆或重置 NPC。PC 的实际 Actor ID 由 State Machine 分配。
- `world_facts` 必须为空列表；`rule_bindings/resources` 必须为空映射，inventory 必须为空列表。Compatibility 的未解决项/适配说明也须为空。Conditions、Encounter、脚本、机械数据、秘密/NPC Meta 等额外字段或非空机制明确拒绝。
- 必需来源/许可/审阅元数据只做结构检查，不证明版权合法、剧情可通关、规则正确或战斗平衡。
- SafeLoader 禁用类实例化、重复键、别名/合并；只接受普通字符串、整数、布尔、null、列表/映射。时间戳、浮点数、二进制等拒绝；大小上限 1 MiB、嵌套上限 32 层、节点上限 20,000。

摘要算法为本地 **`foundation-json/1`**：对完整已解析/验证数据，以排序键、紧凑分隔符、`ensure_ascii=False`、`allow_nan=False` 生成 JSON，再对 UTF-8 字节求 SHA-256，表示为 `sha256:<hex>`。包含内容/来源元数据，排除文件路径、键顺序、注释和排版；不进行 Unicode 归一化。创建 Fingerprint 另含 `fingerprint_schema=foundation-create/1`、Package 身份/版本/摘要、生效 RulesetBinding 和 players；命令键与可信主体在独立主键中。这些编码仅为本仓库 Batch 1 实施选择。

## 状态修改与持久化边界

SM-2 adds `submit_command(TypedCommand, package, *, context)` and authorized `get_command_receipt(session_id, command_id, package, *, context)`. Local schema `sm-command/1` accepts only enter_scene, set_scene_flag, set_object_state and move_actor with matching typed payloads. PC operations require the selected PC. NPC movement additionally requires the adapter-issued `TrustedContext.controlled_npc_ids` grant. The compatibility `_apply` routes through this same pipeline.

Receipts use durable `(session_id, command_id)` keys and a canonical fingerprint binding command, principal/player, package digest and ruleset. Replays reauthorize, same key/different content raises idempotency_conflict. Business rejections/version conflicts persist rejected/conflict receipts; commits persist committed receipts. Schema/auth/storage errors remain structured exceptions. State/version/events/receipt and event-time position evidence commit atomically. No-op commands save receipts without advancing world_version or event_seq.

- SQLite 使用 **WAL、foreign_keys=ON、synchronous=FULL、busy_timeout=5000ms**。创建在一个 `BEGIN IMMEDIATE` 短事务内写入 Session、World、入口 Scene、必要 Actor、选中 Character、独立 RNG、内部审计记录和创建键/原结果；任何阶段乃至延迟外键提交失败均全部回滚。
- Package 读取/验证、指纹计算、UUID 准备在写事务外进行；写事务内没有网络/模型/规则求值。持久唯一键和事务内再查创建键处理并发重入；失败不保留占位 Session 或幂等 claim。
- Scene 主键是 `(session_id, scene_id)`，首次初始化对象与 flags；重访只更新访问次数和 PC 位置。Actor 位置只由 Session 内唯一 Actor 行记录。全部运行态表按 Session 隔离。
- 初始 `world_version=0`。每次实际状态修改成功提交推进一次；无变化操作不推进。`event_seq` 是独立的内部审计序号：创建产生两条记录，Scene 转移产生 exit/enter 两条记录，因此不等于 world_version。
- 事件保存稳定 ID、来源命令及受限 Signals，事件时位置证据与持久 Outbox 同事务提交；当前没有完整 Perception 协议。没有 event-only 或 Meta-only 提交能力，未冻结它们的 world_version 策略。
- RNG 单独持久保存 `state_id / algorithm=unselected / encoding=foundation-placeholder/1 / position=0`，Session 保存引用。没有掷骰、随机流消耗或规则 RNG 协议，也不宣称确定性机械 Replay。
- 所有修改立即提交；`save_session` 不创建第二份权威快照/检查点。Storage schema 3 automatically migrates schema 1/2 while preserving package pins/digests and state. Legacy events have no fabricated historical evidence. Unknown schemas are refused. No online backup tool.

## 自动化验收证据

测试涵盖有效/无效/危险 YAML、所有必要引用与重复 ID、摘要稳定性、Session/快照隔离、可信身份、幂等重试及 Fingerprint 冲突、相同/不同请求的并发创建、四个写入阶段故障与提交失败回滚、Scene 重访/初始化失败、状态与事件原子回滚、版本冲突、Package/Ruleset 固定值和连接关闭恢复。

`tests/test_integration.py` 的完整合成测试执行：

```text
create → enter workshop → mutate door/flag/NPC → leave → revisit → save
       → process exit → fresh process → resume
```

正常关闭及 `os._exit(0)` 提交后异常终止两种路径均由真实子进程执行。恢复前独立 SQLite 连接核验真实行、连续事件序号、外键与完整性；恢复后比较全部 Snapshot/创建结果，确认 `world_version=6`、`event_seq=11`、门仍打开、标志和 NPC 位置保留，且只存在一个 Session/创建映射。没有 Mock 代替持久化或恢复。这证明提交后进程丢失恢复；不等于完成了硬件断电/磁盘故障验证。

## 架构依据与未完成范围

2026-10-10 读取总控仓库最新 main，并核对以下六份文件均对应提交 [`fe6a1e9edeaf2a3e120aa669b6abfe1e819d87f1`](https://github.com/agentic-trpg/agentic-trpg/commit/fe6a1e9edeaf2a3e120aa669b6abfe1e819d87f1)：

- [ADR-001：统一权威状态所有权](https://github.com/agentic-trpg/agentic-trpg/blob/fe6a1e9edeaf2a3e120aa669b6abfe1e819d87f1/docs/ADR-001-UNIFIED-STATE-OWNERSHIP.md)，最高架构约束。
- [State Machine Architecture v0.10](https://github.com/agentic-trpg/agentic-trpg/blob/fe6a1e9edeaf2a3e120aa669b6abfe1e819d87f1/docs/STATE_MACHINE_ARCHITECTURE.md)，重点 §3–§5、§8、§11。
- [Module Contracts v0.8](https://github.com/agentic-trpg/agentic-trpg/blob/fe6a1e9edeaf2a3e120aa669b6abfe1e819d87f1/docs/MODULE_CONTRACTS.md)，重点 §2、§10。
- [Adventure Package Schema v0.6](https://github.com/agentic-trpg/agentic-trpg/blob/fe6a1e9edeaf2a3e120aa669b6abfe1e819d87f1/docs/ADVENTURE_PACKAGE_SCHEMA.md)。
- [Agent Architecture v0.9](https://github.com/agentic-trpg/agentic-trpg/blob/fe6a1e9edeaf2a3e120aa669b6abfe1e819d87f1/docs/AGENT_ARCHITECTURE.md)。
- [MVP Scope v0.11](https://github.com/agentic-trpg/agentic-trpg/blob/fe6a1e9edeaf2a3e120aa669b6abfe1e819d87f1/docs/MVP_SCOPE.md)。

这些文档的 `[PROPOSED]` / `[OPEN]` 不因本实现自动冻结。当前是 SM-3 Foundation，不是完整 MVP：**未实现** Rule Engine Adapter、D&D PC 机械验证、Combat/正式规则检定、完整声明式事件/条件/奖励链、Quest/Inventory 操作、NPC Meta/Perception/Observation、Gate、EVA、LLM Host、视觉系统、活跃战斗恢复、通用脚本或插件框架。

仍为 OPEN 或后续批次事项：规则 RNG 算法/编码与回放、事件/Meta-only 版本策略、完整规则 CharacterState/CombatState/Typed Delta、通用系统来源权限、条件 AST、Pending Choice、完整 Perception/Outbox 协议、更复杂的数据迁移/备份策略及外部 PC 导入。本批没有以空类或 Stub 宣称这些能力完成。

SM-2: 112 tests passed, including durable retry after lost response, revoked authorization, concurrent writers, injected transaction rollback and Batch 1 migration. See `docs/batches/SM2.md`.

## SM-3 event pipeline

`read_events(..., after_seq=0)` is an owner-only ordered committed-event query. Each event has a stable SM-owned event_id, event_seq, source_command_id, type, payload and typed Signals. `deliver_event(..., event_seq, sink=...)` executes the sink outside the write transaction and records durable completion; delivery is at least once, so sinks must deduplicate event_id. `delivery_cursor(...)` cannot cross an older incomplete work item. A delivered event is not completed NPC cognition.

Package events support only authoritative event-type/scene triggers, a typed flag_equals condition, set_scene_flag or set_object_state effects, per_session once and explicit visual/auditory scene-scoped Signals (clear/blocked/undetermined evidence). Scene event_definition_ids must match declared definitions. Conditions and effects run synchronously within the command transaction, including first-entry creation. Cycles are bounded by once history and a 32-consequence limit; exceeding the bound rejects and rolls back. No rule effects, downstream rule commands, arbitrary scripts or asynchronous cross-transaction atomicity are claimed. Signal visibility is authored synthetic evidence, not D&D perception rules or physical propagation.

123 tests passed for SM-3. Tests cover restart, signal/event/outbox atomicity, initial-entry triggers, once/cycle/cascade protection, failed delivery after sink acceptance, stable retry IDs, session isolation and cursor gaps. Schema migration preserves old events with legacy.unavailable source and no invented Signals. See docs/batches/SM3.md.
