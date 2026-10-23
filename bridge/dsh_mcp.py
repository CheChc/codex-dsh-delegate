#!/usr/bin/env python3
"""Minimal synchronous stdio MCP adapter for dsh. Python standard library only."""
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dsh_runner  # noqa: E402

VERSION = dsh_runner.VERSION
DEFAULT_TIMEOUT = dsh_runner.DEFAULT_TIMEOUT
PROTOCOL_VERSION = "2024-11-05"

_PROPERTIES = {
    "task": {"type": "string", "minLength": 1, "description": "自包含任务，含目标、文件、做法、验收和禁止事项。"},
    "workdir": {"type": "string", "description": "现有工作目录的绝对路径。"},
    "timeout_sec": {"type": "integer", "minimum": 1,
                    "description": f"内部超时秒数，默认 {DEFAULT_TIMEOUT}；应短于客户端外层超时。"},
}
TOOLS = [
    {
        "name": "dsh_run",
        "description": "将明确的一批实现交给 dsh；Codex 必须独立审查差异并复验。",
        "inputSchema": {"type": "object", "properties": dict(_PROPERTIES),
                        "required": ["task", "workdir"], "additionalProperties": False},
    },
    {
        "name": "dsh_resume",
        "description": "用已确认的真实 session ID 续跑；不要猜测 ID 或盲目重试超时任务。",
        "inputSchema": {"type": "object", "properties": {
            **_PROPERTIES, "session_id": {"type": "string", "minLength": 1}},
            "required": ["task", "workdir", "session_id"], "additionalProperties": False},
    },
    {
        "name": "dsh_bridge_info",
        "description": "只读返回桥接版本、路径、Python、dsh 可执行文件和超时建议；不读取配置、不调用模型。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def bridge_info():
    return {
        "version": VERSION,
        "bridge_path": str(Path(__file__).resolve()),
        "runner_path": str(Path(dsh_runner.__file__).resolve()),
        "python_executable": sys.executable,
        "dsh_path": shutil.which("dsh"),
        "node_path": shutil.which("node"),
        "default_timeout_sec": DEFAULT_TIMEOUT,
        "suggested_outer_timeout_sec": 300,
    }


def _invalid_input(message):
    payload = {"version": VERSION, "status": "invalid_input", "error": message}
    return True, json.dumps(payload, ensure_ascii=False)


def run_tool(name, arguments):
    if not isinstance(name, str):
        return _invalid_input("tool name must be a string")
    if name not in {"dsh_run", "dsh_resume", "dsh_bridge_info"}:
        return _invalid_input(f"Unknown tool: {name}")
    if not isinstance(arguments, dict):
        return _invalid_input("arguments must be an object")
    if name == "dsh_bridge_info":
        if arguments:
            return _invalid_input("dsh_bridge_info takes no arguments")
        return False, json.dumps(bridge_info(), ensure_ascii=False)

    allowed = {"task", "workdir", "timeout_sec"}
    if name == "dsh_resume":
        allowed.add("session_id")
    unexpected = set(arguments) - allowed
    if unexpected:
        names = ", ".join(sorted(map(str, unexpected)))
        return _invalid_input(f"Unexpected argument(s): {names}")
    missing = [key for key in ("task", "workdir") if key not in arguments]
    if name == "dsh_resume" and "session_id" not in arguments:
        missing.append("session_id")
    if missing:
        return _invalid_input(f"Missing required argument(s): {', '.join(missing)}")

    workdir = arguments["workdir"]
    if not isinstance(workdir, str) or not workdir or not Path(workdir).is_absolute():
        return _invalid_input("workdir must be an absolute path")
    if name == "dsh_resume":
        session_id = arguments["session_id"]
        if not isinstance(session_id, str) or not session_id.strip():
            return _invalid_input("session_id must be a nonempty string")

    result = dsh_runner.run_dsh(
        arguments["task"],
        workdir,
        timeout_sec=arguments.get("timeout_sec", DEFAULT_TIMEOUT),
        session_id=arguments.get("session_id"),
    )
    return result.get("status") != "completed", json.dumps(result, ensure_ascii=False)


def rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def dispatch(req):
    if not isinstance(req, dict):
        return rpc_error(None, -32600, "Invalid request")
    request_id = req.get("id")
    if (req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str)
            or isinstance(request_id, (dict, list, bool))):
        return rpc_error(None, -32600, "Invalid request")
    # Notifications never trigger a tool execution or a response.
    if "id" not in req:
        return None
    params = req.get("params", {})
    if not isinstance(params, dict):
        return rpc_error(request_id, -32602, "params must be an object")
    method = req["method"]
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "codex-dsh-bridge", "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        failed, text = run_tool(params.get("name"), params.get("arguments", {}))
        result = {"isError": failed, "content": [{"type": "text", "text": text}]}
    else:
        return rpc_error(request_id, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            response = rpc_error(None, -32700, "Parse error")
        else:
            response = dispatch(req)
        if response is not None:
            # stdout is reserved for MCP; never log raw requests or task text.
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
