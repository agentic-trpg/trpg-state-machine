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

## 与最新 origin/main 整合及 Schema35（2026-10-18）

用户随后明确授权把最新 main 整合进原 ENV 分支，不授权把 ENV 合并到 main。
fetch 后 main 为 `afe6871f550fef395ee48b3af5c7cb7f712071f7`，原 ENV 分支为
`06c7af8fedc7a7db89f4b8f78a60b61bf9d37b4d`。适用 AGENTS.md 未发现。
原交付 worktree 起始干净，将其在原 HEAD 分离后，在新的干净
`.test-tmp/environment-main-integration` worktree 检出原 ENV 分支；
`git merge --no-ff --no-commit origin/main` 仅产生 migration34 的 add/add 冲突。
原目录分支、HEAD 和全部 38 个既有未提交文件保留，用执行前 SHA256 快照核对。

解决方案：逐字保留 main 的 `_migration_34.sql`；ENV 移至 `_migration_35.sql`，
user_version=35。存储加载器按 1～35 顺序执行；在既有 SQLite 迁移锁／事务内部，
按环境表、机关表、键和激活索引识别两种真实 Schema34。旧 ENV34 保留环境 JSON，
补齐原样机关 migration34，随后 ENV35 复用已验证的环境表。main34 不重复映射机关；
未知／部分结构无写入拒绝。失败回滚包含兼容修复、后续 DDL、所有数据和版本。
当前版本测试期望同步为35，历史版本断言和旧行／旧列保留断言保持原语义。

逐项核对自动合并的 CommandRuntime／注册表／历史指纹、WorldFacts、WorldHandler、
Package、OSS 候选校验和防护投影、WorldTime：保留 ENV 批量类型编码和场景／字段
公开披露修复，同时保留 main 的机关操作／生命周期、事件触发、顺序候选与定时重置。
Package `/9` ENV 预留与 main `/10` 机关模型均保留；外部 Effects／角色高度 provider、
PKG-01／SESSION-01 自动环境导入等边界没有新增生产接线。

新增 `tests/test_environment_mechanism_migration.py`，通过 `git archive` 分别运行
`9ce60cb`、`afe6871`、`06c7af8` 的真实旧解释器，创建两个 Session、已激活／未激活机关、
命令回执和已推进时间；ENV34 另含已访问与未访问场景基础条件／局部来源。
验证所有旧表／列／行、环境数据和完整机关状态保留、重启无变化、Session 隔离、
新环境命令可用、三线程旧 ENV34 同时打开只升级一次；分别在34复制后和35末尾
注入失败，确认 SQLite schema／全表数据／版本完全回滚并可重试。
新数据库升级／失败空库、未知／部分结构拒绝也有实际测试。

实际验证命令（cwd 为新 worktree；专项使用共享 Python，全套使用下述独立环境）：

- `python -m pytest -q -x tests/test_environment_mechanism_migration.py
  tests/test_mechanism_migration.py tests/test_mechanism_service.py
  tests/test_environment_service.py tests/test_environment_review_regressions.py
  --basetemp=.test-tmp/integration-focused -o faulthandler_timeout=45 --durations=10`：
  **90 passed**，74.74 秒，exit 0。

首轮全套使用共享虚拟环境时，四个分区各在新进程恢复测试失败：共享环境的
editable `.pth` 指向原目录旧 src，子进程因此拒绝 Schema35。没有更改断言或原目录；
执行共享 Python 的 `-m venv --without-pip .venv`，在新 worktree 的 site-packages
写入 `integration-dependencies.pth`，依次引用本 worktree 的 src 和共享已安装依赖目录。
明确验证独立 Python 的 `trpg_state_machine.__file__` 位于新 worktree。
移除外层 PYTHONPATH，真实历史解释器仍使用各自明确的旧源码路径；重新完整执行四区。
初次失败轮次不计入最终通过数。

全套实际调用（各分区原生并行；独立 basetemp，日志和文件清单均留在本 worktree）：

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONUNBUFFERED='1'
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
$env:UV_CACHE_DIR='D:\Projects\agentic-trpg\trpg-state-machine\.uv-cache'
$testFiles=@(Get-ChildItem tests/test_*.py | Sort-Object Name)
$members=@(for($index=$part;$index -lt $testFiles.Count;$index+=4){
    $testFiles[$index].FullName
})
.venv/Scripts/python.exe -m pytest -q -x @members `
    --basetemp=.test-tmp/integration-isolated-$part `
    -o faulthandler_timeout=45 --durations=10
```

核对74个测试文件的分区并集无遗漏／重复，全套 `--collect-only -q` 为1597项。
提交前 fetch 的一次 GitHub 连接失败，HTTP/1.1 重试成功；origin/main 仍为 afe6871，
远端 ENV 分支仍为06c7af8，没有覆盖新的远端提交。

最终四区0／1／2／3分别为 **377 / 439 / 387 / 394 passed**，合计
**1597 passed**，退出码全部为0；耗时366.00 / 206.87 / 277.07 / 338.93秒。
通过数与1597项完整收集数一致，覆盖环境、机关、命令指纹、OSS、权限和恢复回归。
只有既有 lark `sre_parse`／`sre_constants` 弃用警告。
`python -m ruff check .`、`python -m ruff format --check .`、
`git diff --check`、`git diff --cached --check` 均通过，241个文件格式一致；
README及环境／机关两组文档的本地链接与代码块 fence 检查通过。
整套回归后207个源码／测试文件的 SHA256 与运行中快照一致；
main migration34 的 Git blob 为 `b800bd23018bf70cf1ca30cbe1df6e41d7c0e8d5`，
与 origin/main 完全相同。原目录38个既有修改文件字节不变。

选择性暂存本次整合文件和已合并的 main 内容，在原 ENV 分支生成合并提交，
正常推送该分支，不强制推送、不修改 main。最终提交号及远端核对结果随交付报告提供。
