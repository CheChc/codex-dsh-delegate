"""Isolated CLI fixtures: never invoke a model or read dsh credentials."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))

FAKE_DSH = r'''
import json, os, subprocess, sys, time
from pathlib import Path
task = sys.stdin.read()
args = sys.argv[1:]
mode = os.environ.get("DSH_FIXTURE_MODE", "success")
with open(os.environ["DSH_FIXTURE_CALLS"], "a") as f:
    f.write(json.dumps({"args": args, "task": task}) + "\n")
sid = args[args.index("--session-id")+1] if "--session-id" in args else "session-fixture"
def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)
emit({"type": "session", "sessionId": "wrong-session" if mode == "wrong_session" else sid,
      "cwd": os.getcwd()})
emit({"type": "thinking", "text": "PRIVATE_THINKING_FIXTURE"})
if os.environ.get("DSH_FIXTURE_WRITE"):
    Path(os.environ["DSH_FIXTURE_WRITE"]).write_text("updated\n")
if mode == "self_commit":
    Path("outside.txt").write_text("worker committed out of scope\n")
    subprocess.run(["git", "add", "outside.txt"], check=True)
    subprocess.run(["git", "commit", "-qm", "unauthorized worker commit"], check=True)
if mode == "out_of_scope":
    Path("outside.txt").write_text("unexpected\n")
if mode in ("timeout", "tree_timeout"):
    if mode == "tree_timeout":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        Path("child.pid").write_text(str(child.pid))
    emit({"type": "text", "text": "PARTIAL_OUTPUT"})
    print("PARTIAL_ERROR", file=sys.stderr, flush=True)
    time.sleep(30)
if mode == "failure":
    Path("partial.txt").write_text("partial\n")
    print("EXPECTED_FAILURE", file=sys.stderr, flush=True)
    sys.exit(7)
if mode == "bad_json":
    print("not JSON", flush=True)
if mode == "long_line":
    print("x" * (1024 * 1024 + 8), flush=True)
if mode == "error_event":
    emit({"type": "error", "message": "EXPECTED_ERROR"})
if mode != "no_usage":
    emit({"type": "status", "phase": "step_end",
          "usage": {"inputTokens": 3, "outputTokens": 2}})
if mode != "no_final":
    emit({"type": "final", "text": json.dumps({"argv": args, "task": task, "cwd": os.getcwd()})})
'''
FAKE_CODEX = r'''
import os, sys
from pathlib import Path
mode = os.environ.get("CODEX_FIXTURE_MODE", "success")
if mode == "failure":
    sys.exit(5)
if mode != "empty":
    flag = "--output-last-message" if "--output-last-message" in sys.argv else "-o"
    Path(sys.argv[sys.argv.index(flag)+1]).write_text("Implement only the permitted files.\n")
print("fixture planning progress, not plan contents")
'''

class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-contract-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.workdir = self.base / "workspace"
        self.workdir.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.state = self.base / "state"
        self.calls = self.base / "calls.jsonl"
        for name, body in (("dsh", FAKE_DSH), ("codex", FAKE_CODEX)):
            executable = self.bin / name
            executable.write_text("#!" + sys.executable + "\n" + body)
            executable.chmod(0o755)
        self.environment = patch.dict(os.environ, {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", os.defpath),
            "DSH_BRIDGE_STATE_DIR": str(self.state),
            "DSH_FIXTURE_MODE": "success",
            "DSH_FIXTURE_CALLS": str(self.calls),
            "DSH_FIXTURE_WRITE": "",
            "CODEX_FIXTURE_MODE": "success",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
