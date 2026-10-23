# Codex 指挥，dsh 干活

把已确定的实现交给 dsh（DeepSeek Harness），由 Codex 负责方案、边界判断与独立验收。适合批量修改、按错误清单修复、补测试、机械迁移。

```text
用户目标 → Codex 读上下文、确定方案与验收
                    ↓ dsh_run / dsh_resume
            dsh 改文件、运行检查
                    ↓
          Codex 审查差异、亲自复验 → 交付
```

这是一个个人维护的 Codex skill，附带可选的本地 MCP 桥接脚本，与 OpenAI、DeepSeek 无隶属关系。不固定模型，也不承诺固定的额度或费用节省。

## 仓库内容

- [SKILL.md](skills/dsh-delegate/SKILL.md)：委托边界、执行流程、超时恢复与验收要求。
- [任务模板](skills/dsh-delegate/references/task-templates.md)：普通实现、精确替换、返修示例。
- [MCP 桥接脚本](bridge/dsh_mcp.py)：用 Python 标准库把本机 dsh 暴露为两个工具。
- [离线测试](tests/test_bridge.py)：模拟 dsh，验证工具传参、错误与超时，不访问 DeepSeek。

## 前提

1. 本地 Codex 支持 skills 和 stdio MCP。
2. 已按 [DeepSeek Harness 官方说明](https://github.com/deepseek-ai/deepseek-harness)安装、配置 dsh，并能运行 headless 任务。模型和认证由 dsh 自身管理。
3. 使用本仓库桥接脚本时需要 Python 3.10+；Codex 启动的环境能找到 `dsh` 命令。
4. dsh 能读取其配置目录（通常为 `~/.dsh/`，以实际配置为准），并能访问配置的模型服务。

可先检查，不会调用模型：

```sh
dsh --version
dsh headless --help
python3 --version
```

本地验证过的 dsh 版本为 `0.2.0-rc.2`；桥接依赖 `headless -` 和 `--session-id`。升级后应先确认这些接口仍存在。

## 安装 skill

```sh
git clone https://github.com/CheChc/codex-dsh-delegate.git
cd codex-dsh-delegate
mkdir -p "$HOME/.agents/skills"
# 目标不存在时才复制；已有同名 skill 请先备份，再手动合并或替换。
if test ! -e "$HOME/.agents/skills/dsh-delegate"; then
  cp -R skills/dsh-delegate "$HOME/.agents/skills/dsh-delegate"
fi
```

Codex 当前官方个人 skill 目录为 `~/.agents/skills`；如果安装器使用其他有效目录，请保留一种安装方式，避免同名 skill 重复。复制 skill 不会自动注册 MCP。

## 配置 MCP（已有 dsh 工具可跳过）

在仓库根目录执行以下命令，注册的是绝对路径，因此之后请保留这个仓库目录。Python 和 dsh 必须能被 Codex 进程找到。

```sh
codex mcp add dsh -- python3 "$PWD/bridge/dsh_mcp.py"
codex mcp list
```

在本地 `~/.codex/config.toml` 中**已有的** `[mcp_servers.dsh]` 表内设置外层超时，不要重复创建同名表：

```toml
# 保留 codex mcp add 写入的 command、args 等字段
tool_timeout_sec = 300
```

桥接脚本的 `timeout_sec` 默认 240 秒。每次调用应使内部超时小于 MCP 客户端的外部超时，并留出返回结果的余量；300/240 是示例配置，不是所有环境的上限。桥接脚本串行执行任务；同一工作目录不要从其他会话并发改写。

如果 Codex 未显示新工具或 skill，重新打开会话或重启客户端。工具名可能带客户端前缀，识别末尾的 `dsh_run`、`dsh_resume` 即可。

配置依据：[Codex skills 官方文档](https://developers.openai.com/codex/skills)、[Codex MCP 官方文档](https://developers.openai.com/codex/mcp)。

## 使用

在 Codex 中提出任务，例如：

> 用 $dsh-delegate。检查这个项目后确定修复方案，让 dsh 按文件分批实现；你审查 diff 并亲自运行验收命令。

Codex 应先给出明确方案，再调用工具。工具参数示意（工作目录请换成实际绝对路径）：

```json
{
  "workdir": "/absolute/path/to/project",
  "timeout_sec": 240,
  "task": "## 目标\n修复已定位的问题\n## 涉及文件\nsrc/example.py\n## 具体做法\n按已审查方案修改，保留公共接口\n## 验收\n运行项目对应检查并报告退出码\n## 禁止\n不修改其他文件，不提交、不推送"
}
```

普通请求每次最多修改 5 个文件，较大任务拆批；每批应能在超时预算内完成。先查看工作区已有改动，避免覆盖用户工作。dsh 的自述不能替代 Codex 的差异审查和复验。

## 超时与失败

- **超时后先检查状态**：文件可能已经部分写入。查看差异、进程与检查结果，确认旧任务停止后再决定下一步，不能直接重跑整批。
- **会话续跑有条件**：只有取得可信的真实 session ID 才使用 `dsh_resume`。桥接脚本透传文本，不保证每次拿到新会话 ID；不能根据不存在的 ID 续跑。
- **权限或 DNS 报错**：若明确是会话沙箱拦截配置读取或网络，停止重试，说明原因并请用户调整权限（如当前客户端的 Full access）。已确认权限正常时，继续排查 dsh 配置、网络或服务地址，不能把所有 DNS 故障都归因于沙箱。
- **凭据错误**：由用户按 dsh 官方流程在本地修复，不把密钥写入任务、仓库或诊断报告。
- **退出码为 0**：只表示 CLI 正常结束，仍需核实实际变更与验收结果。
- **取消与限制**：这是精简的同步 stdio 桥接，未实现 MCP 运行中取消。POSIX 内部超时会尝试终止进程组并保留部分输出；外层客户端断开、脱离进程组的后台任务仍可能继续，必须检查状态。输出会裁剪，完整验证输出应由 Codex 本地复验获取。

## 本地验证

在仓库根目录执行：

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile bridge/dsh_mcp.py
git diff --check
```

测试使用临时目录和模拟 CLI，不读取真实 dsh 凭据、不调用模型。它们验证桥接行为；真实模型执行质量仍由每项任务的独立验收负责。本仓库不附带 dsh、凭据、用户会话、私人笔记或机器专属配置。
