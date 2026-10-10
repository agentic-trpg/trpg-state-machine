# Agentic TRPG — State Machine Foundation

Batch 1 实现了一个可运行的本地闭环：**验证原创合成 Adventure Package → 创建隔离 Session → 初始化/重访 Scene → 修改实际状态 → SQLite 持久化 → 新进程恢复**。

State Machine 是全部已实现可变游戏状态的唯一权威拥有者和提交者。Package 是不可变模板；返回的 Snapshot 是独立副本，修改副本不会写入数据库。已接入固定候选 SHA 的真实 D&D 基础攻击及受限胜利/TPK 关闭，同时保留隔离的合成测试路径；没有第二份运行态权威。正常关闭后可以保存并在新进程恢复；活跃战斗跨进程精确续玩仍不支持。

## 安装与验证

SM-10 已固定 Engine/SRD 候选 `602dcb8d448e670049427bbb751d6ed226005298`
（Evaluator `/8`），加入首次 Combat 前的 `read_noncombat_snapshot(...)`、
完整 Snapshot V2 机械事实及唯一存储的 XP。Schema 9 不静默升级旧 Session 的
RulesetBinding；旧 `/4` 环境用于真实历史存档迁移测试。
详见 [版本与字段归属](docs/SM10_VERSIONED_STATE.md)。本批完整测试 326 项通过。

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。运行时依赖 PyYAML、固定 Git SHA 的 dnd5e-engine / dnd5e-srd-data 及其必要依赖；SQLite 使用 Python 标准库，pytest/Ruff 是开发依赖。`uv.lock` 固定完整解析结果。真实异步 Engine 测试在 Windows 需要事件循环使用的本地 socketpair；受限环境须允许该 OS 功能，测试不调用外部服务。

```powershell
uv sync --locked --group dev
uv run --locked python -m pytest -q
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
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
| `save_session(...)` | 所有修改已同步提交；校验并读取非活跃战斗存档，不接收整份状态覆盖，不额外推进版本；活跃 Combat 明确拒绝 |
| `resume_session(...)` | 恢复非活跃战斗 Session；不重放初始模板，不迁移 Package；身份/Digest/Ruleset 不一致或活跃 Combat 时明确拒绝 |
| `combat.initialize / combat.intent` | 同一个 TypedCommand / Receipt 入口；只初始化受审 Package Encounter，再调用真实 Engine 基础攻击 |
| `combat.close` | 提交真实 Engine 的受限终局、XP 和 CombatEnded；完整原子性与持久幂等，详见 [SM-11](docs/SM11_COMBAT_CLOSURE.md) |
| `rules.check` | 上游 CheckPayload，可信 GM 裁决授权，真实非战斗 Ability/Skill 检定；空 Delta、RNG、CheckRolled 与回执原子提交，详见 [SM-12](docs/SM12_REAL_CHECKS.md) |
| `submit_command_async(...)` | 真实 combat.intent 的异步入口；复用相同幂等键与提交逻辑 |
| `read_combat_snapshot(...) / read_rng_context(...)` | 从 SQLite 重建实际上游类型，供受信审计；不等于活跃战斗续玩许可 |
| `query_action_availability(...)` | 授权、过滤的真实只读可用性查询；无 RNG、Receipt 或写入 |

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

`TrustedContext` 只表示集成信任边界：本库不实现登录、认证服务或主体/玩家目录。受信适配层必须先核验控制关系；本库再核验请求的 `player_id` 与可信上下文一致，并限制后续访问为原主体/玩家。内部完整 Snapshot 仅供可信调用者；NPC 只能通过现有 Scene View / Inbox 接口取得权限过滤后的信息。

## 基础合成 Package 子集

`tests/fixtures/lantern_station.yaml` 是完全原创、按 CC0-1.0 发布的合成数据。没有 First Blush 原文、地图或派生故事内容。

格式为 UTF-8 单文件 YAML，包含 `manifest/world/scenes/actors/pc_templates/events/rule_bindings` 逻辑数据；本地格式版本是 **`state-machine-foundation/1`**，不宣称冻结了总控仓库的完整 Adventure Package Schema。

- 校验必填/额外字段、命名空间化 ID（小写点分 ID、最长 128 字符）、作用域唯一性、Scene/Location/Actor 引用、入口和地点对应关系、Scene 可达性及多个内置 PC Templates。创建严格选择一个玩家、一个 PC Template；未知模板拒绝。
- 基础绑定是 `synthetic.foundation / synthetic-data/1 / none/1`，仅包含 HP 初值及空 resources/inventory。SM-4 的 synthetic.atomic 和 SM-6 的真实 D&D 绑定是明确隔离的扩展，不能互相冒充。
- World/Scene flags 仅允许命名空间为 `flag.` 的布尔值。对象状态仅支持合成 `closed/open`。出口只能是无条件的已定义 Scene 连接。
- NPC 是无自主行为的合成身份，`controller.kind=scripted_event` 仅保存静态策略标记，不执行脚本。按初始 Scene 首次需要时创建一次；另一 Scene 的身份引用不会克隆或重置 NPC。PC 的实际 Actor ID 由 State Machine 分配。
- 基础绑定的 `world_facts` 为空，`rule_bindings/resources` 为空，inventory 为空；其他绑定的受限扩展见 SM-4 / SM-6。D&D 只接受显式完整的 `rule_bindings.combat_foundation`，不提供通用 Encounter、脚本或任意机械更新接口。Compatibility 未解决项/适配说明仍须为空。
- 必需来源/许可/审阅元数据只做结构检查，不证明版权合法、剧情可通关、规则正确或战斗平衡。
- SafeLoader 禁用类实例化、重复键、别名/合并；只接受普通字符串、整数、布尔、null、列表/映射。时间戳、浮点数、二进制等拒绝；大小上限 1 MiB、嵌套上限 32 层、节点上限 20,000。

摘要算法为本地 **`foundation-json/1`**：对完整已解析/验证数据，以排序键、紧凑分隔符、`ensure_ascii=False`、`allow_nan=False` 生成 JSON，再对 UTF-8 字节求 SHA-256，表示为 `sha256:<hex>`。包含内容/来源元数据，排除文件路径、键顺序、注释和排版；不进行 Unicode 归一化。创建 Fingerprint 另含 `fingerprint_schema=foundation-create/1`、Package 身份/版本/摘要、生效 RulesetBinding 和 players；命令键与可信主体在独立主键中。这些编码仅为本仓库 Batch 1 实施选择。

## 状态修改与持久化边界

SM-2 introduced `submit_command(TypedCommand, package, *, context)` and authorized `get_command_receipt(session_id, command_id, package, *, context)`. Local schema `sm-command/1` retains enter_scene, set_scene_flag, set_object_state and move_actor; later batches add rule_operation, combat.initialize and combat.intent with exact typed payloads. NPC movement/combat requires the adapter-issued `TrustedContext.controlled_npc_ids` grant. The compatibility `_apply` routes through this same pipeline. Unsupported active-combat traversal/relocation/object operations are refused.

Receipts use durable `(session_id, command_id)` keys and a canonical fingerprint binding command, principal/player, package digest and ruleset. Replays reauthorize, same key/different content raises idempotency_conflict. Business rejections/version conflicts persist rejected/conflict receipts; commits persist committed receipts. Schema/auth/storage errors remain structured exceptions. State/version/events/receipt and event-time position evidence commit atomically. No-op commands save receipts without advancing world_version or event_seq.

- SQLite 使用 **WAL、foreign_keys=ON、synchronous=FULL、busy_timeout=5000ms**。创建在一个 `BEGIN IMMEDIATE` 短事务内写入 Session、World、入口 Scene、必要 Actor、选中 Character、独立 RNG、内部审计记录和创建键/原结果；任何阶段乃至延迟外键提交失败均全部回滚。
- Package 读取/验证、指纹计算、UUID 准备在写事务外进行；写事务内没有网络/模型/规则求值。持久唯一键和事务内再查创建键处理并发重入；失败不保留占位 Session 或幂等 claim。
- Scene 主键是 `(session_id, scene_id)`，首次初始化对象与 flags；重访保留实例。Actor 的 Scene 归属在唯一 actors 行；战斗格点位置在唯一 CombatState.actor_zone 中，当前拒绝无法完整表达机械副作用的战斗中场景转移。全部运行态表按 Session 隔离。
- 初始 `world_version=0`。每次实际状态修改成功提交推进一次；无变化操作不推进。`event_seq` 是独立的内部审计序号：创建产生两条记录，Scene 转移产生 exit/enter 两条记录，因此不等于 world_version。
- 事件保存稳定 ID、来源命令及受限 Signals，事件时位置证据与持久 Outbox 同事务提交；当前提供受限合成 Perception 投影，完整规则/传播协议未实现。没有 event-only 或 Meta-only 提交能力，未冻结它们的 world_version 策略。
- RNG 单独持久保存，Session 仅保存引用。Foundation 使用占位编码且不掷骰，synthetic.atomic 使用独立合成计数 RNG，真实攻击使用候选 Engine 的 `stdlib-mt19937/1` 状态及独立流版本；不把候选实现宣称为已冻结的通用跨版本规则 RNG 协议。
- 所有修改立即提交；`save_session` 不创建第二份权威快照/检查点。Storage schema 8 automatically migrates schemas 1–7 while preserving package pins/digests and state. Migration locks cover version recheck, all steps, FK validation and commit; interrupted upgrades roll back together. Legacy events/trigger sources have no fabricated historical evidence. Unknown schemas are refused. No online backup tool.

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

这些文档的 `[PROPOSED]` / `[OPEN]` 不因本实现自动冻结。SM-6～SM-9 以本地 `D:\Projects\agentic-trpg\agentic-trpg\docs` 为最高开发依据，以上链接保留初始文档基线。当前支持受限真实基础攻击，不是完整 MVP：**未实现** 通用 PC 构建验证、combat.start/end 协议、完整回合/法术/Reaction、正式非战斗检定、完整声明式条件/奖励链、Quest/Inventory 操作、NPC Meta/完整感知传播、Gate、EVA、LLM Host、视觉系统、活跃战斗安全恢复、通用脚本或插件框架。

仍为 OPEN 或后续事项：正式跨模块 ABI/跨版本 RNG 回放、事件/Meta-only/未来无随机操作的版本策略、完整规则能力闭包、通用系统来源权限、条件 AST、Pending Choice/Continuation、完整感知传播、备份策略及外部 PC 导入。受限完整 Snapshot/Typed Delta 已实际实现和验证，不能等同于所有规则机制均已支持。

SM-2: 112 tests passed, including durable retry after lost response, revoked authorization, concurrent writers, injected transaction rollback and Batch 1 migration. See `docs/batches/SM2.md`.

## SM-3 event pipeline

`read_events(..., after_seq=0)` is an owner-only ordered committed-event query. Each event has a stable SM-owned event_id, event_seq, source_command_id, type, payload and typed Signals. `deliver_event(..., event_seq, sink=...)` executes the sink outside the write transaction and records durable completion; delivery is at least once, so sinks must deduplicate event_id. `delivery_cursor(...)` cannot cross an older incomplete work item. A delivered event is not completed NPC cognition.

Package events support only authoritative event-type/scene triggers, a typed flag_equals condition, set_scene_flag or set_object_state effects, per_session once and explicit visual/auditory scene-scoped Signals (clear/blocked/undetermined evidence). Scene event_definition_ids must match declared definitions. Conditions and effects run synchronously within the command transaction, including first-entry creation. Cycles are bounded by once history and a 32-consequence limit; exceeding the bound rejects and rolls back. No rule effects, downstream rule commands, arbitrary scripts or asynchronous cross-transaction atomicity are claimed. Signal visibility is authored synthetic evidence, not D&D perception rules or physical propagation.

123 tests passed for SM-3. Tests cover restart, signal/event/outbox atomicity, initial-entry triggers, once/cycle/cascade protection, failed delivery after sink acceptance, stable retry IDs, session isolation and cursor gaps. Schema migration preserves old events with legacy.unavailable source and no invented Signals. See docs/batches/SM3.md.

## SM-4 synthetic evaluation boundary

`rule_operation` carries a `RuleOperation` with operation_kind, real actor/target, declared resource and current-scene flag, difficulty and domain. Only `synthetic.atomic / synthetic-data/1 / synthetic-evaluator/1` admits the synthetic evaluator. Its PC Templates may declare namespaced integer resources and unique synthetic item IDs. This is not D&D data or an Engine integration. Foundation packages keep their original digest and placeholder RNG.

The local synthetic `RuleEvaluationRequest/Result`, `NonCombatSnapshot`, `StateDelta`, `RNGTransition` and `SyntheticEvaluator` live in mechanics.py; they are not the real Engine ABI. Detached synthetic input includes all supported characters/resources/inventory/effects, actor positions and world/scenes; RNG is separate. Direct synthetic results cover four statuses. The SM now refuses unsupported choice before evaluation with pending_choice_unsupported; unexpected post-admission needs_choice is an independent protocol error. The real D&D path imports actual versioned upstream types and never uses this synthetic snapshot.

Evaluation uses a copied request outside write transactions. Commit rechecks authorization, world version, full read-set digest, pins, RNG and every Delta target/precondition/invariant under BEGIN IMMEDIATE. HP, resource spend, world flag, RNG, consequences, events, outbox and receipt commit together. Accepted mechanical failure still spends its token. Invalid result or storage failure writes no state or RNG. A committed retry returns the stored receipt before evaluator invocation. Evaluation-response loss retries the same explicit RNG state, giving the same synthetic result; no real remote Engine retry protocol is claimed.

The synthetic stream uses SHA-256 of state_id and counter to generate a 1..6 value. Counter, position and rng_version persist atomically. This deliberately local test encoding is not a frozen RNG contract or statistically reviewed dice protocol. Event-only/Meta-only/no-draw global version policies remain OPEN; mechanical commits currently require an actual world change.

Engine main 64dd920 was inspected read-only. Public resolve_check and rest resolvers are reusable pure fragments, but evaluate, full Snapshot/Delta/RNGTransition and four-status envelopes are absent. The old registry-backed combat API cannot satisfy ADR-001. See docs/batches/SM4.md for exact gaps and actual smoke evidence.

## SM-5 NPC perception foundation

Package NPCs may declare explicit synthetic `senses: {visual: true, auditory: true}`. Missing senses remain unknown; they do not default to true/false. Every new event durably captures actor positions and these senses together with authored Signal scope/occlusion and perception work. Projection never reads a later position to reconstruct history. Known pre-perception history is explicitly archived at the durable compatibility boundary described below; genuine new missing evidence remains unresolved.

`project_event(session_id, event_seq, package, *, context, judgment=None)` processes durable work outside the write transaction. Scene scope, declared senses and clear/blocked evidence give deterministic answers. Only undetermined, adequately evidenced Signals are sent as a same-observer/event JudgmentRequest batch to the optional restricted callback. JudgmentResult must contain exactly one typed Boolean for every requested signal_id; omissions, duplicates, unknown IDs, bad types and exceptions leave work unresolved. No real JEV/LLM Host is implemented.

One EventObservation aggregates all perceived Signal content per observer/event/projection-version. All false completes without an Observation. Partial/unresolved decisions do not complete the event or advance the contiguous perception cursor. Observations and inactive-NPC inbox references commit atomically and deduplicate on retry. `perception_cursor(...)` tracks projection completion, not cognition completion. There is no ACK, Agent activation, EVA, belief or goal update.

Owner-only `grant_npc_access(...)` assigns an authenticated principal to an existing NPC. NPCContext is session/observer bound and checked against this durable grant on every `read_observations(...)` and `get_scene_view(...)`. NPCs cannot use owner snapshots or read_events. NPC observations expose opaque observation_id, observer_id and perceived channel/content only; event type/source command/true actor/secret target/internal trace remain in storage. Inbox reads retain records and are not cognition ACKs. A Session-wide cursor conservatively withholds later observations across any earlier unresolved gap.

`get_scene_view` returns CurrentPerceptualView for NPCs: own current scene and currently visible actors/objects in the synthetic clear-scene model, with unresolved status for missing senses. It never infers a past causal actor from current presence. Owner access returns the ordinary current SceneInstance. Physical LOS, distance, walls, D&D senses/checks and secret-content authoring validation remain unsupported.

168 tests pass, including 22 perception tests: two observers/multiple Signals, event-time movement, hidden activity, malformed/timeout JEV, all-false/known occlusion, durable unresolved gaps, inbox rollback, concurrent deduplication, authorization revocation/isolation and fresh-process reading of real persisted inbox rows. See docs/batches/SM5.md.

## Stabilization of SM-2–SM-5

Evaluator configuration and Result RulesetBinding mismatches raise structured
`StateMachineError(code="ruleset_binding_mismatch")`, without a receipt, state/RNG
change or event. Correcting configuration allows the original command to retry.
Unsupported operations still return their durable business rejection. Mechanical
`event_cascade_limit` rolls back the candidate HP/resources/world/RNG/events/trigger
history inside a savepoint, then atomically saves the rejected receipt. Replays,
including after restart, return that receipt without evaluating again. Protocol,
evaluation and storage failures are not cached as business rejections.

Schema 6 adds `legacy_perception_boundaries(session_id, through_event_seq, reason)`.
The locked source schema 1–4 proves that all existing events predate perception.
For already upgraded schema 5, migration identifies only a contiguous prefix with
old `event-evidence/1`, or missing evidence plus the migration-generated source,
empty Signals and no matching real command/creation key. Classification stops at
the first other event. The stored reason is `legacy_event_evidence_unavailable`;
original events, evidence and Work remain, with no fabricated Signals, false
decisions or historical Observations. `project_event` returns `legacy_unavailable`
for this archived prefix. The perception cursor orders work **after** this explicit
boundary; it does not assert historical projection or cognition completion.
Delivery work and its cursor are unaffected. Genuine new pending work, unknown
senses and missing event-time evidence still hold later Inbox delivery.

This is a local migration compatibility policy, not a frozen cross-module epoch
or ACK protocol. It cannot recover lost evidence or invent senses for an old pinned
Package. Ambiguous schema-5 provenance stays unresolved conservatively. Package
digest mismatches still refuse resume; no silent Package rewrite is provided.

191 tests pass (168 existing + 23 stabilization cases), including real historical
runtime subprocesses, schema 1–4 upgrades, already-upgraded schema-5 mixed history,
nonzero RNG/receipts, concurrent initialization/upgrades, migration process exit,
new Observation delivery and new unresolved cursor gaps. Historical tests require
Git and the five fixed commits in `tests/test_migrations.py` to be available to
`git archive`; shallow clones/source archives need that repository history first.
The original old Loader/Fixture tests are separate from forward-authored senses
fixtures passed through the old trusted typed-package service boundary; they do
not claim old YAML loaders supported senses.

See [Stabilization Review](docs/reviews/SM_STABILIZATION_REVIEW.md) for commands,
evidence, compatibility and the merge recommendation. F5 (exact triggering-event
reference) and F6 (full versioned Snapshot/read-set and PendingChoice protocol)
were known follow-up work at stabilization. SM-9 links new trigger sources and
records F6's scoped acceptance matrix; old unavailable sources remain audited.
The stabilization itself added no real D&D Adapter or NPC EVA.

## Real Engine integration (SM-6)

The candidate D&D Engine and SRD data dependencies are pinned to
`a7225ebaa4914f6db4ae72f3f6454c355ea7baac`. Schema 7 persists complete restricted
mechanical components and separate RNG. `combat.initialize` accepts only the
reviewed encounter embedded in a D&D-bound Package; trusted audit reads rebuild
actual upstream `CombatSnapshot` / `RNGContext` from SQLite. Active combat
save/resume is explicitly refused under SM-02. See
[the mapping and compatibility notes](docs/SM6_COMBAT_FOUNDATION.md).

SM-7 adds real PC `combat.intent` evaluation and closed Typed Delta/RNG/Event/
Receipt/Outbox atomic commits. Sync callers use `submit_command`; async callers
use `submit_command_async`. Supported tests cover ordinary explicit basic attacks,
misses, Temp HP and damage resistance/immunity/vulnerability. Choice, complex
mechanics and the unimplemented upstream `combat.end` remain restricted. Read
[the execution and error boundaries](docs/SM7_REAL_ATTACK.md); active combat
audit recovery must not be described as safe continuation.

SM-8 adds trusted NPC `combat.intent` through that same pipeline and read-only
`query_action_availability`. Explicit ordinary weapons and bundled Bandit's basic
`scimitar` / `light-crossbow` actions are tested; unsupported/unknown actions fail
closed. Availability is conditional and does not grant execution rights or reserve
resources. See [NPC authorization and query boundaries](docs/SM8_NPC_AVAILABILITY.md).

SM-9 upgrades storage to Schema 8 and links new Trigger History entries to exact
committed events. Unrecoverable old trigger sources are explicitly audited.
Committed basic attacks feed only a generic visible Signal through the existing
Perception/Inbox pipeline. Integration tests verify real state, RNG, receipts and
observations in a fresh process. They explicitly refuse active-combat continuation;
the candidate has no stateless combat.end, so completed-combat safe save/resume
remains **Blocked**. See [hardening evidence and F6 scope](docs/SM9_INTEGRATION_HARDENING.md).
