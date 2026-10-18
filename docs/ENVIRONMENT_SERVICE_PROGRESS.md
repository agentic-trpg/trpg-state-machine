# EnvironmentService 实施记录

日期：2026-10-18。仓库：`agentic-trpg/trpg-state-machine`。
分支：`feat/scene-environment-service`，基线为 fetch 后确认的最新
`origin/main@9ce60cbe4fdbf91dce6ffef811969482369fb08a`，工作区开始时干净。
不合并 main。仓库及适用祖先路径未发现 AGENTS.md，没有额外 CI/check-fast 入口；
遵循 README 的 pytest、Ruff check/format 和 diff 检查。

读取当前总控 `MODULE_CONTRACTS.md` v0.17 的领域／Scene／环境责任、
`STATE_MACHINE_ARCHITECTURE.md`、`WORLD_SUBSYSTEM_DESIGN.md`，并检查本仓库 World W1–W8、
OSS 总体／实施记录和 WorldTimeService 记录。目标契约不是已实现 API；独立实现 Scene
Environment 不要求 Scene 门面或 Grid/Terrain 重构。检查了真实 CommandRuntime、
SessionAccess、WorldHandler、WorldFacts、SQLite migration、EventCapture／Outbox、
PackageEffects、CharacterFacts 的当前数据能力，未升级 RE/SRD pin。

## 实现与行为

新增 environment_models / environment_commands / environment，Schema 34 追加空环境表，
WorldFacts 读写，StateMachine／WorldHandler 实际装配。强类型受控初始化、基础条件、
局部来源添加／替换／移除、静态防护和显式批量场景修改经统一命令事务执行。
批量先验证全部候选；一次最终变化一个版本和 environment.changed；净空操作无版本／事件，
仍记录回执。未访问／未初始化场景修改不被后续初值覆盖。

组合规则、只读查询、未知／不支持状态、授权与披露、接口和完整生产接入边界见
[服务说明](ENVIRONMENT_SERVICE.md)。测试使用真实服务／Session／SQLite／命令路径；
WO、Effects、角色高度提供方明确为契约 fixture，不代表这些外部组件已生产接通。

查询测试对比全部数据库表及 total_changes，并执行 `PRAGMA query_only=ON`。
真实 OSS 测试覆盖四向来风、静态／动态挡雨、[0,2) 与 [4,6) 中间空隙及上界不含。
Effects fixture 验证自然薄雾与法术浓雾同时存在、移动和移除后自然来源保留。
新解释器读取真实存档、resume、旧命令重放；历史 `main@9ce60cb` 解释器建立 Schema33
存档后升级，验证每个旧表／列／行不变、迁移故障原子回滚及旧 WorldTime 绑定／tick 保留。
旧回归测试将“当前存储版本”期望从 33 更新为 34；历史迁移的新增表白名单加入
scene_environments，并明确断言旧存档中该表为空。历史版本、故障注入目标和旧行保留校验不变。

## 验证记录

`uv run --locked` 最初因沙箱 uv 缓存权限和 hatchling 下载 DNS 不可用未完成，
随后使用仓库已有 `.venv/Scripts/python.exe`（相同锁定依赖环境）直接运行。
实际命令与最终结果：

- `.venv/Scripts/python.exe -m pytest tests/test_environment_service.py -q
  --basetemp=.test-tmp/environment-expanded-final`：**40 passed**，exit 0。
- 原生完整套件按 PowerShell 实际排序后的 `tests/test_*.py` 文件分四区，
  分区 0／1／2／3 分别 **364 / 420 / 362 / 398 passed**，全部 exit 0，
  合计 **1,544 passed**；耗时 224.14 / 148.26 / 139.07 / 237.73 秒。
  核对 70 个文件的分区并集、无重复、各区收集数与通过数完全一致。
  初次失败均为历史迁移的当前版本／新增表断言未同步，修正后重跑受影响分区；
  已通过分区 2 不包含随后修改的测试文件，未重复计入。
- `python -m ruff check .`、`python -m ruff format --check .`、`git diff --check`：
  全部通过，234 个文件格式一致。
- 五份更新 Markdown 的本地链接／fence 校验通过；服务文档 Python 示例在真实 Session
  中执行初始化及查询成功。
- `.venv/Scripts/python.exe examples/world_station.py`：exit 0，真实新进程恢复及回执重放；
  终点 scene.workshop / 1,0 / world_version 32 / event_seq 49。
- 实际 Engine / SRD 均为 0.6.0，direct_url commit_id 均为
  `d596161060555630019163ab301e1d25fd816549`；pyproject.toml / uv.lock 未改。
- 仅有既有 lark `sre_parse` / `sre_constants` 弃用警告。

完整分区的实际选择与调用（各区使用独立 basetemp，原生并行运行）：

```powershell
$testFiles = @(Get-ChildItem tests/test_*.py | Sort-Object Name)
$members = @(for ($index=$part; $index -lt $testFiles.Count; $index+=4) {
    $testFiles[$index].FullName
})
.venv/Scripts/python.exe -m pytest -q -x @members `
    --basetemp=.test-tmp/environment-shard-$part `
    -o faulthandler_timeout=45 --durations=10
```

## 交付与共享工作区保护

提交前检测到外部操作把共享目录切到 `feat/world-mechanism-service`，HEAD 仍为原基线。
保护检查在暂存前中止，没有在该分支暂存／提交。把本次 38 个任务文件按 SHA256 保存，
复制到 `.test-tmp/environment-delivery` 的独立 Git 工作树，继续交付原定
`feat/scene-environment-service`；共享目录分支及所有文件保持不动。
完整回归结束后核对共享文件与交付快照无差异；隔离工作树明确使用自己的 src 导入，
再次运行 **40 passed**（20.69 秒），Ruff／格式／diff 检查通过。
仅暂存上述任务文件，提交推送原定分支，不合并 main。具体提交及远端身份随最终交付报告提供。

## 631d957 审查修复：批量指纹与隐藏范围披露（2026-10-18）

工作位置为上述原分支工作树，开始时干净；fetch 后本地／远端原分支均为
631d95752101f9faeb286af1b75f4b812fa43ca6。重新检查适用 AGENTS.md（未发现）、
现有注册表、wire 协议、指纹／回执、环境事实和公开／内部查询。不改共享目录的
feat/world-mechanism-service 或其改动，不合并 main。

先新增并实际运行 `tests/test_environment_review_regressions.py`：修复前
**7 failed / 2 passed**。失败明确覆盖 Add/Replace 成功和拒绝回执被冒用、指纹类型碰撞、
无类型历史回退和公开点／范围探测；保留这些失败断言后修复生产代码。

环境编码由 environment_operation_to_data 统一生成，复用已有 ENVIRONMENT_COMMANDS
和带标签 wire 联合，保留操作类型／内容／顺序；环境单操作和其他已合并命令历史编码不变。
旧批量回执的类型歧义不可恢复，明确拒绝无类型哈希回退，保留原记录供受控核对。

四个公开查询统一使用整个 Scene／字段的披露规则；受隐藏来源影响的字段及依赖结果
全场景 unknown，贡献为空，关联防护状态同样屏蔽。公开分段不使用隐藏范围；内部投影
继续真实合成。更新原范围测试为“保留查询、返回场景统一 unknown”，未删除查询接口。
新增 SQLite 全表／total_changes／query_only 断言验证查询只读，以及隐藏来源移动、
数值／身份变化、公开来源混合时的点／区域／范围／单位输出一致性。

实际验证（使用共享仓库现有 `.venv/Scripts/python.exe`，工作目录为本分支工作树）：

- `python -m pytest tests/test_environment_review_regressions.py -q
  --basetemp=.test-tmp/environment-review-red`：修复前 **7 failed / 2 passed**。
- `python -m pytest tests/test_environment_review_regressions.py tests/test_environment_service.py
  -q --basetemp=.test-tmp/environment-review-confirmed`：**50 passed**（12.40 秒）。
  随后补强恢复及分别只改变隐藏身份／数值／范围的断言，新回归单独执行
  `--basetemp=.test-tmp/environment-review-final-focused`：**10 passed**（2.50 秒）。
  原有 40 项和最终增强的 10 项均包含在下述最终全套中。
- 全套沿用上述 PowerShell 按文件排序、索引 mod 4 的分区算法，实际调用
  `python -m pytest -q -x @members --basetemp=.test-tmp/environment-review-shard-$part
  -o faulthandler_timeout=45 --durations=10`。最终分区 0／1／2／3 为
  **441 / 375 / 388 / 350 passed**，全部 exit 0，合计 **1,554 passed**；
  耗时 309.46 / 315.20 / 132.52 / 243.23 秒。核对 71 个测试文件分区并集无遗漏／重复，
  通过数与全套收集数完全一致，覆盖命令／历史指纹、OSS、权限、持久化和恢复。
- 分区 2 首轮受到外层 PYTHONPATH 指向当前 src 的影响，Schema8 解释器误读新源码；
  只移除测试进程外层 PYTHONPATH，保留 pytest 项目自身的 `pythonpath=src`，
  确认旧解释器实际导入 `.test-tmp/legacy-sm8/src`、存储版本为 8，再以
  `--basetemp=.test-tmp/environment-review-shard-2-clean-env` 完整重跑该分区通过。
  没有改历史测试或生产代码来接受该环境错误，失败轮次不计入最终合计。
- `python -m ruff check .`、`python -m ruff format --check .`、`git diff --check`：
  全部通过，235 个文件格式一致；两份 Environment Markdown 链接和 fence 检查通过。
- 最终回归期间核对 3 个生产文件及 2 个测试文件 SHA256 未变。仅有原有 lark
  `sre_parse` / `sre_constants` 弃用警告。未改 Schema、Package、依赖 pin 或其他消费者。

本次仅提交上述 3 个生产文件、2 个测试文件、2 份 Environment 文档，并推送
feat/scene-environment-service；不合并 main，具体提交号随最终报告提供。
