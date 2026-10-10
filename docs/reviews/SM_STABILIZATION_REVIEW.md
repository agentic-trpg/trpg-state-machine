# SM Stabilization Review

日期：2026-10-10。分支：`feat/sm-b2-b5-runtime-pipeline`。
基准：`0a3837d8a0ce83a47b2c8ef4bf37046562d712f5`。

结论：F1～F4 **Fixed**；完整回归 **191 passed**（原有 168 + 新增 23）。
没有确认本轮引入的新 P0/P1/P2。建议在人工审阅、接受下述 Foundation
能力边界和 F5/F6 后续事项后，将 SM-2～SM-5 作为合成 Foundation 合并。
这不等于完整 MVP、真实 Engine Adapter 或完整跨模块协议验收；本轮不执行 Merge 或创建 PR。

## 依据、范围与方法

读取本地 Meta `docs/` 的 ADR-001、MODULE_CONTRACTS v0.8、
STATE_MACHINE_ARCHITECTURE v0.10、AGENT_ARCHITECTURE v0.9，并核对
Adventure Package v0.6、MVP Scope v0.11 的边界。
遵循 Accepted ADR 的单一权威、Contracts §2/§4.3/§5/§7 的事务、
错误分类、幂等和事件时感知约束。PROPOSED/OPEN 没有升级为全项目冻结接口。

将 Global Review F1～F4 与实际 Python、SQL、调用链和测试逐项核对。
最终独立只读审阅分为机械错误/事务和迁移/历史感知两条；另进行最终交付一致性核查。
本轮仅修改 State Machine，未修改 Meta/Rule Engine Repository，未扩展业务操作。

实际修改/新增共 11 个文件：

- 运行时：`src/trpg_state_machine/mechanical_runtime.py`、`service.py`、`events.py`、
  `perception.py`、新增 `_migration_6.sql`。
- 测试：`tests/test_mechanics.py`、`test_commands.py`、新增 `test_migrations.py`、
  新增 `_legacy_database_worker.py`。
- 文档：`README.md`、新增 `docs/reviews/SM_STABILIZATION_REVIEW.md`。

## 修复及证据

| 项目 | 根因 / 修复前 | 实际修复 / 修复后 | 文件与测试 |
|---|---|---|---|
| F1 / P1 / Fixed | Evaluator Binding 不匹配让 result=None，随后保存 rejected/unsupported_ruleset；配置修复仍回放拒绝 | 求值前独立检查配置；Result Binding 单独校验；返回 ruleset_binding_mismatch，无 receipt/RNG/世界/Event 写入；原命令可重试；真正 unsupported operation 仍业务拒绝 | mechanical_runtime.py；test_evaluator_configuration_error_is_not_a_receipt（2例）、test_result_binding_error_can_retry_original_command；既有 unsupported/invalid/timeout 回归 |
| F2 / P1 / Fixed | Migration 3 为旧记录标记缺来源，Migration 5 为所有旧事件补未完成感知 Work；连续水位永久停在缺历史证据处，隐藏新 Inbox | Schema 6 保存明确 legacy prefix 和不可恢复原因；保留原事件/证据/Work，不冒称完成；新水位只在该边界之后计算；新真实未决工作仍阻塞 | _migration_6.sql、service.py、events.py、perception.py；test_migrations.py 的真实旧库、mixed schema5、同 Session 新观察、缺口/隔离/重启测试 |
| F3 / P2 / Fixed | user_version 在写锁外缓存；等待者重复执行已完成迁移 | 单连接 BEGIN IMMEDIATE 内读版本、选择并逐语句执行完整升级、校验连续版本/FK 后统一提交；等待者重新读到最新版本；避免 executescript 隐式 COMMIT | service.py；并发空库 / Schema1/4/5，首次读版本在真实事务内；迁移中途进程退出恢复 |
| F4 / P2 / Fixed | 机械候选后的级联超限直接抛异常；资源等已回滚，但无拒绝回执，每次重试重新求值 | 候选写入使用 Savepoint；仅 event_cascade_limit 是当前允许持久化的确定性拒绝；回滚候选后保存原 receipt；Schema/Result/配置/SQLite/求值故障独立、可重试 | mechanical_runtime.py；跨域候选、级联超限、原 ID 重复、重启查询、receipt/trigger_history 实际 SQL 故障注入（3例） |

机械拒绝验证了候选 HP、token 支付、Flag、门、trigger_history、RNG、版本、
Events 和双 Outbox 均无部分写入；只有拒绝 receipt 落盘。拒绝回执写入故障时，
整个事务不留下回执，修复存储故障后可用同 ID/相同 RNG 重试。
配置错误修复后又遇到级联拒绝的组合路径同样有测试。

求值仍在写事务外。提交时继续重验权限、Package/Ruleset pins、世界版本、
完整支持 Snapshot digest、独立 RNG 和封闭 Delta。Receipt 唯一键、Fingerprint、
Command 类型和授权查询语义没有改变。已存在的 receipt 优先于当前 Evaluator 配置回放。

## SQLite 兼容性与历史边界

- Schema 从 5 前向升级到 **6**，新增唯一一张基础设施表
  `legacy_perception_boundaries(session_id, through_event_seq, reason)`，带 Session/事件外键。
  不新增游戏状态副本、服务、感知状态机或 ACK。
- 启动前在锁外读取受信 SQL，统一迁移器去掉脚本自己的 BEGIN/COMMIT/FK 开关，
  逐条 execute。Schema 1–6 所需步骤在同一 SQLite 写事务内提交，未用 IF NOT EXISTS
  掩盖重复/不完整迁移；原 schema 1 既有定义不变。
- Migration 4 需要重建被引用的 RNG 表，因此仅在尚未开放业务的初始化连接上，
  BEGIN 前 FK=OFF；提交前 foreign_key_check，结束后恢复并核验 FK=ON。
  失败回滚并关闭连接。WAL 切换在迁移事务外，针对同时打开时的 SQLITE_BUSY 有限等待。
  正常运行保持 WAL、synchronous=FULL、busy_timeout=5000。
- 锁内来源 Schema <5 可证明全部已有事件早于感知能力，记录完整已有历史水位。
  对已经在 Schema 5 的库，只识别确定旧来源的连续前缀：evidence/1，或缺 Evidence、
  legacy.unavailable 来源、空 Signals 且没有匹配真实命令/创建键。
  新 evidence/2、未知 senses、JEV 未决、真实同名命令缺 Evidence 均不自动归档。
- 原历史 Work 保持原 done/attempts/error，而不是强行 done=1；原始事实、Evidence、
  Signals、Receipt、World/Scene/Actor/Character/RNG 和 pins 不重写。
  `project_event` 对归档前缀返回 **legacy_unavailable**；reason 为
  **legacy_event_evidence_unavailable**。这是无法恢复的历史投影处置，不表示 false、
  已投影或认知处理完成。
- Perception cursor 在明确边界之后按最早真实 pending Work 连续推进。
  新 Observation 已写入但更早新 Work 未决时仍隐藏；补齐真实较早工作后按源序交付。
  普通 delivery 的旧 Work 和 Cursor 完全沿用原协议。
- 无自动回退到旧可执行版本的保证：旧程序不认识 Schema 6 会明确拒绝打开。
  未实现备份/降级工具；没有静默 Package 迁移。

这是本仓库受限历史兼容政策，不冻结未来跨模块 epoch/保留协议。没有恢复丢失证据，
也没有给旧 Package 凭空添加 senses。旧 pinned Package 没有 senses 时，新 Signal
仍可能真正未决。Schema5 来源不明确的记录也保守保持未决，不能凭当前状态猜测历史。

## 测试真实性、数量与执行

新增机械测试 **6**，迁移/历史感知测试 **17**，合计 **23**。
原有 168 测试继续运行，只把已有 migration 验收的最终 Schema 断言从 5 更新为 6。
总计 **191 passed in 47.47s**；Ruff check、format check、diff check 全部通过。

实际命令（受限 Windows 环境使用仓库临时目录）：

```powershell
$env:UV_CACHE_DIR = "$PWD/.uv-cache"
$env:PYTHONUTF8 = "1"
uv sync --locked --group dev
uv run --locked python -m pytest -q --basetemp=.test-tmp/stabilization-final
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
```

真实历史运行时来自 Git 只读 archive，在独立 Python 子进程中执行：

| Schema | 旧实现 Commit |
|---|---|
| 1 | 182efbe6b6b331fea274b5ae7863a8888e791339 |
| 2 | 86ac1bf816d917c155cadf436097eb6b6f81a6f4 |
| 3 | 1e289a9b57d72d955777ff1d781b1ba90f89112f |
| 4 | 16291de08b41ef147a2389bdb3d02235fa65cb0e |
| 5（旧迁移/混合新 Work） | 0a3837d8a0ce83a47b2c8ef4bf37046562d712f5 |

测试环境需要 Git 和这些历史对象；浅克隆/源码压缩包需要先补足历史。
测试不复制拼造旧 SQLite 行，也不 Mock 持久化结果。

四个原始旧版本用各自 Loader 和原 Fixture 实际创建两个 Session、进入/修改/离开 Scene；
升级后独立 SQLite 查询逐列比对全部已有表的原行，核验 FK/integrity、pins 和版本。
另用真实 Schema4 合成机械命令产生非零 RNG、资源支付及 Receipt，升级后原值保持。

为了验证旧 Session 的**新观察交付**，单独提供人工合成、由当前 Loader 完整验证的
forward-authored senses Package，经旧运行时的受信 typed-package 服务边界建库：
旧 Loader 只读取其支持的结构，服务输入固定完整已审核内容的 Digest，旧运行时不使用 senses。
这不是旧 YAML Loader 支持 senses 的证据；原 Loader/Fixture 兼容测试与该边界测试明确分开。
新运行时仍按同一完整 Digest 加载，不篡改存档 pin，不补造历史 Evidence。
在同一旧 Session 中提交新的真实事件并生成/读取 Observation，顺序缺口、双 Session 隔离、
重启和重复迁移均验证实际数据库。另经旧 Schema5 代码提交/投影真实新事件，测试迁移时
已存在后来 Inbox、较早真 pending 和故意丢失 Evidence 的同名命令。

并发测试四个独立连接实际同时初始化/升级，追踪首次版本读取确实处于迁移事务中；
异常恢复测试在 Migration4 实际 DROP RNG 表后，或 Migration6 实际历史/Cursor 写入后，
使用子进程 `os._exit(73)` 退出。重新打开前，旧版本和所有旧行完整恢复；随后升级成功。
这是进程退出恢复证据，不外推为硬件断电、任意磁盘损坏恢复。

## 独立审阅与剩余事项

独立只读机械审阅运行 29 mechanics 测试通过；迁移/事件/命令初审组合 40 测试通过，
另 22 perception 测试及随后新增的非零 RNG 测试通过。最终独立交付审阅再次运行
17 migration 测试（24.24s）通过，collect-only 确认共 191；核对全部 11 个修改文件。
最终主审执行全量 191 测试，
核对实际 Diff、模块边界、异常分类、Savepoint、FK、历史水位、数据库断言及交付文件。
最初测试设施的 Windows tar realpath 权限失败改为受信 Git ZIP archive，
旧 Schema1 私有操作 import 路径修正后通过；未将这些设施失败算作成功证据。
独立审阅指出的 import 格式问题已修复并重新检查。

未确认新增 P0/P1/P2；F1～F4 对当前支持范围的阻塞已解除。

- **F5 / P2 留后续**：trigger_history 仍只有 source_command_id，没有准确触发 event_id。
  当前 once/事务保护保持有效，审计完整性未在本轮扩展。
- **F6 / P2 留后续**：本地合成协议仍非完整共同版本化 Snapshot/read-set；
  needs_choice 仍为 pending_choice_unsupported，未实现 PendingChoice/PendingOperation。
  不能宣称完整四态提交或 Module Contracts P0 Adapter。
- **Blocked / NOT IMPLEMENTED**：真实 D&D Adapter、Combat/复杂规则依赖闭包、
  NPC EVA/Meta/Memory/Belief、完整传播/感知检定、Gate、LLM Host。
- **OPEN**：全项目 RNG 编码/回放、event/meta-only world_version、完整共同 wire Schema、
  后续迁移/备份和更复杂历史保留策略。本轮没有冻结这些协议。

建议作为受限合成 Foundation Merge，并持续跟踪 F5/F6；真实 MVP/跨模块协议仍需后续验收。
执行合并的决定保留给人工。本次只 Commit + Push，完成后停止。
