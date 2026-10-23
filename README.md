# Codex 指挥，dsh 干活

Codex 负责方案、边界判断与独立验收，dsh（DeepSeek Harness）负责明确的实现和机械操作。适合批量修改、补测试、修复检查错误和迁移。

当前桥接版本：**2.0.0**。MCP 和批处理共用执行器，分别记录执行结果和独立验收结果。

```text
用户目标 → Codex 确定方案与验收 → dsh 实现 → Codex 审查差异、亲自复验 → 交付
```

个人维护项目，与 OpenAI、DeepSeek 无隶属关系。不固定模型，不承诺固定的额度或费用节省。

## 内容

- [Skill](skills/dsh-delegate/SKILL.md) 与[任务模板](skills/dsh-delegate/references/task-templates.md)。
- [MCP 入口](bridge/dsh_mcp.py)：dsh_run、dsh_resume、只读 dsh_bridge_info。
- [共享执行器](bridge/dsh_runner.py)：JSON 事件、真实 session ID、超时、日志与工作目录锁。
- [批处理入口](scripts/plan-exec)：规划、执行、独立验证与可选的显式文件提交。
- [离线回归测试](tests/)：假 CLI 与临时 Git 仓库，不调用模型。

## 前提

macOS/Linux（需要 POSIX 进程组与 flock），Python 3.10+；批处理还需要可用的 Git。MCP 注册和默认只读规划需要已安装的 Codex CLI；--exec-only 可省去规划调用。按 [DeepSeek Harness 官方说明](https://github.com/deepseek-ai/deepseek-harness)安装并配置 dsh。模型、认证与网络地址由 dsh 自身管理。

```sh
dsh --version
dsh headless --help
python3 --version
```

上述检查不调用模型。本地核实的 dsh 为 0.2.0-rc.2，依赖 headless --json、从 stdin 读取任务及 --session-id；升级后先确认接口兼容。dsh 需要读取其配置目录并访问配置的模型服务。

## 安装与更新 skill

```sh
git clone https://github.com/CheChc/codex-dsh-delegate.git
cd codex-dsh-delegate
mkdir -p "$HOME/.agents/skills"
# 已有同名目录时先备份、合并或替换；避免两处安装同名 skill。
if test ! -e "$HOME/.agents/skills/dsh-delegate"; then
  cp -R skills/dsh-delegate "$HOME/.agents/skills/dsh-delegate"
fi
```

个人 skill 目录参考 [Codex skills 官方文档](https://developers.openai.com/codex/skills)。**更新 skill 不会更新或重载 MCP 程序。** 已有旧桥接也应检查它实际运行的版本。

## 配置 MCP

在仓库根目录注册绝对路径，之后保留该目录：

```sh
codex mcp add dsh -- python3 "$PWD/bridge/dsh_mcp.py"
codex mcp list
```

在 ~/.codex/config.toml **已有的** [mcp_servers.dsh] 表内增加或修改外层超时，保留 command、args 等其他字段，不重复创建同名表：

```toml
tool_timeout_sec = 300
```

内部 timeout_sec 默认 240 秒，应小于当前客户端外层超时并留出返回余量；300/240 是示例配置。配置项参考 [Codex MCP 官方文档](https://developers.openai.com/codex/mcp)。

重新打开会话或重启客户端以加载更新后的 MCP。随后调用 dsh_bridge_info，确认 version=2.0.0、bridge_path、runner_path 和 dsh_path、node_path。若本机采用复制安装，**dsh_mcp.py 与 dsh_runner.py 必须一起更新并放在同一目录**。

dsh 通常经 /usr/bin/env node 启动，因此应在 **MCP 实际继承的 PATH** 下检查 dsh --version 和 headless --help。本机验证 Node 22 可正常运行当前 dsh；另一套 Node 20 曾出现退出码为 0 但完全空输出。空输出会报告 protocol_error，不能当成功。必要时在该 MCP 服务的 env.PATH 中配置已验证的 Node 所在目录；仓库不硬编码机器路径。

## 交互使用与结果

在 Codex 中提出任务：

> 用 $dsh-delegate。先确定方案、允许修改的文件和验收命令，让 dsh 分批实现，由你审查差异并亲自复验。

新实现给出接口、约束和验收标准即可；精确替换才提供完整替换内容。每批最多修改 5 个文件，并按依赖关系、预计耗时与恢复边界拆分。

工具文本返回 JSON，主要字段如下：

| 字段 | 含义 |
|---|---|
| version / task_id | 桥接版本与本次运行标识 |
| status | completed、failed、timed_out、protocol_error、launch_error、invalid_input 或 busy |
| session_id | 从真实 session 事件获得；没有收到时可能为 null |
| exit_code | CLI 退出码；启动前失败可能为 null |
| verification_status | MCP 执行器始终为 not_run，仍需 Codex 独立验收 |
| final_text / stderr_tail | 有长度限制的答复和诊断摘要 |
| log_dir | 完整事件、诊断与 result.json 的本地目录 |
| usage_samples | 提供方报告的逐步用量样本；缺失为 null，不是零费用或完整账单 |
| error | 失败的诊断说明 |

completed 需要正常退出、真实 session 和 final 事件且没有协议错误，**只表示执行结束**。不从答复中的“测试通过”推断验收通过。重试和续跑前先核对真实差异与执行状态。

## 批处理 plan-exec

无需全局安装，可直接在仓库中执行：

```sh
python3 scripts/plan-exec -C /absolute/path/to/project \
  --file src/parser.py --file tests/test_parser.py \
  --verify 'python3 -m unittest discover -s tests -v' \
  '修复空输入异常，保留非空输入行为'
```

默认先让 Codex 只读规划，再交 dsh 执行，最后由本地进程独立验证。--exec-only PLAN.md 使用已有非空方案；--plan-only 只生成方案。模型与推理强度可通过 -m / -e 显式设置。

**提交必须显式追加 --commit，并指定 1 至 5 个 --file。** 使用 Git 根目录作为 workdir。已有目标必须已跟踪且起始干净，新目标必须尚不存在且未被忽略；带 skip-worktree/assume-unchanged 标记的目标会拒绝。其他文件原有的 staged、unstaged、untracked 内容会作为基线保留。存在可执行的提交相关 hooks 时拒绝自动提交，保留 hooks 并提示手工提交，不跳过仓库检查。

执行器比较任务前后 Git 状态、内容哈希、index 条目及 HEAD/分支；给定文件范围时，执行或验证造成的越界变化会报告 scope_violation，保留现场且不提交。提交只包含本次实际变化的允许路径，Git 路径按字面解释。worker 或验证命令自行提交、切换分支也会报告越界，不会回滚其已发生的操作。该检查不能阻止 worker 先写入越界文件或提交，也不协调外部编辑器；被忽略的范围外文件、Git 元数据和中途修改后复原不受完整监控，不能当作安全沙箱。

| 批处理状态 | 结果 |
|---|---|
| execution_failed / verification_failed | 非零退出，不提交 |
| unverified | 无显式或可确认的验收命令，非零退出，不提交 |
| scope_violation / commit_failed / invalid_input / busy / planning_failed | 非零退出，不宣称完成 |
| verified | 执行和独立验收通过，未提交 |
| committed | 验收与范围检查通过，仅提交允许的实际变化文件 |
| no_changes | 验收通过但没有本批变化，不创建空提交 |
| plan_only | 仅生成方案，不执行 worker |

每次方案、结果和验证输出保存到目标项目独立的 .plan-exec/run-UUID/，可能含私有代码和诊断。仓库的 .gitignore 不会自动应用到其他项目；建议在目标项目的 .git/info/exclude 加入 .plan-exec/，防止之后 git add -A 公开日志。非 Git 目录可执行、验证，但不提供 Git 范围对比和自动提交。独立验证超时用 --verify-timeout，worker 超时用 --timeout-sec，默认均 240 秒。

如需固定入口，可把 scripts/plan-exec 与 bridge/dsh_runner.py 一起放入用户 bin 目录；替换前备份已有入口。单独复制其中一个不能完成升级。

## 超时、锁与日志边界

- 同一 canonical workdir 的协作调用串行执行；批处理的锁覆盖规划到提交。使用不同状态根、不同或相互嵌套的工作根目录，以及其他程序的写入不受此锁协调。
- 内部超时会终止 POSIX 进程组，保留已写日志和已获得的 session ID；文件可能部分完成。只有确认旧任务停止后才决定续跑或重试。
- 当前仍是同步桥接，没有异步查询/取消工具。外层客户端断开、强制终止或脱离进程组的后台任务不能保证自动清理；running 记录可能过时。
- 明确受沙箱阻止的配置读取或网络错误，停止原样重试并说明所需权限；其他 DNS/认证故障按实际原因处理，不复制凭据排障。
- 默认运行记录位于 ~/.local/state/codex-dsh-delegate，可用 DSH_BRIDGE_STATE_DIR 配置。每次日志目录0700、文件0600，含原始事件，可能包含任务中的私有代码和推理文本；这些不会作为工具摘要全部回传。日志不自动清理，应按实际保留需求管理，不纳入公开仓库。
- 任何失败都不能证明“没有写入”。保护用户既有改动，禁止全局回滚或盲目重跑非幂等操作。

## 验证

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile bridge/dsh_mcp.py bridge/dsh_runner.py scripts/plan-exec
git diff --check
```

测试覆盖错误状态传播、显式文件提交、用户工作保留、路径保护、超时、会话续跑与互斥。它们不证明模型实现一定正确，也不替代每项任务的主代理独立验收。
