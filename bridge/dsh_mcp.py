#!/usr/bin/env python3
"""Minimal synchronous stdio MCP adapter for dsh. Python standard library only."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

DEFAULT_TIMEOUT = 240
MAX_CHARS = 20000
PROTOCOL_VERSION = "2024-11-05"

_PROPERTIES = {
    "task": {"type": "string", "minLength": 1, "description": "自包含任务，含目标、文件、做法、验收和禁止事项。"},
    "workdir": {"type": "string", "description": "现有工作目录的绝对路径。"},
    "timeout_sec": {"type": "integer", "minimum": 1, "description": "内部超时秒数，默认 240；应短于客户端外层超时。"},
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
]


def as_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def clip(value, limit=MAX_CHARS):
    text = as_text(value)
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n... [truncated] ...\n" + text[-half:]


def validate(name, arguments):
    if not isinstance(name, str) or name not in {"dsh_run", "dsh_resume"}:
        raise ValueError("Unknown tool")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    allowed = {"task", "workdir", "timeout_sec"}
    if name == "dsh_resume":
        allowed.add("session_id")
    if set(arguments) - allowed:
        raise ValueError("Unexpected argument")
    task = arguments.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task must be a nonempty string")
    workdir = arguments.get("workdir")
    if not isinstance(workdir, str) or not Path(workdir).is_absolute():
        raise ValueError("workdir must be an absolute path")
    if not Path(workdir).is_dir():
        raise ValueError("workdir must be an existing directory")
    timeout = arguments.get("timeout_sec", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise ValueError("timeout_sec must be a positive integer")
    if name == "dsh_resume":
        sid = arguments.get("session_id")
        if not isinstance(sid, str) or not sid.strip() or sid.startswith("-"):
            raise ValueError("session_id must be a known nonempty ID, not an option")
    return task, workdir, timeout


def kill_worker(proc):
    """Kill the process group on POSIX; detached processes may escape it."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def run_tool(name, arguments):
    try:
        task, workdir, timeout = validate(name, arguments)
    except (ValueError, OSError) as exc:
        return True, str(exc)
    cmd = ["dsh", "headless"]
    if name == "dsh_resume":
        cmd += ["--session-id", arguments["session_id"]]
    cmd.append("-")
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, cwd=workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        return True, f"Cannot start dsh; check PATH, installation and permissions: {exc}"
    timed_out = False
    try:
        out, err = proc.communicate(task, timeout=timeout)
    except subprocess.TimeoutExpired as first:
        timed_out = True
        kill_worker(proc)
        try:
            out, err = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired as last:
            # An escaped descendant may still hold the output pipes open.
            out = last.output or first.output
            err = last.stderr or first.stderr
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream and not stream.closed:
                    stream.close()
            proc.wait(timeout=2)
    elapsed = time.monotonic() - started
    status = "TIMEOUT" if timed_out else f"exit={proc.returncode}"
    parts = [f"{status} elapsed={elapsed:.1f}s workdir={workdir}",
             "=== stdout ===\n" + clip(out),
             "=== stderr ===\n" + clip(err, 3000)]
    if timed_out:
        parts.append("Partial changes may exist. Inspect files and running processes before retrying. "
                     "Resume only with a verified session ID; none is inferred by this bridge.")
    elif proc.returncode != 0:
        parts.append("Inspect actual changes before retrying; failure does not imply no writes.")
    return timed_out or proc.returncode != 0, "\n\n".join(parts)


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
                  "serverInfo": {"name": "codex-dsh-bridge", "version": "1.0.0"}}
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
