"""Offline contract tests: fake CLI only, no credentials or model calls."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dsh_bridge", ROOT / "bridge/dsh_mcp.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workdir = Path(self.temp.name)
        self.bin = self.workdir / "bin"
        self.bin.mkdir()
        fake = self.bin / "dsh"
        fake.write_text("#!" + sys.executable + "\n" + """
import json, os, subprocess, sys, time
task = sys.stdin.read()
if task == "timeout":
    print("PARTIAL_OUTPUT", flush=True)
    print("PARTIAL_ERROR", file=sys.stderr, flush=True)
    time.sleep(20)
elif task == "tree-timeout":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    open("child.pid", "w").write(str(child.pid))
    print("CHILD_STARTED", flush=True)
    time.sleep(20)
elif task == "failure":
    open("partial.txt", "w").write("written before failure")
    print("EXPECTED_FAILURE", file=sys.stderr)
    sys.exit(7)
else:
    print(json.dumps({"argv": sys.argv[1:], "task": task, "cwd": os.getcwd()}))
""", encoding="utf-8")
        fake.chmod(0o755)
        self.env = patch.dict(os.environ, {"PATH": str(self.bin)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def args(self, task="hello", **extra):
        return {"task": task, "workdir": str(self.workdir), **extra}

    def test_run_stdin_and_argument_boundaries(self):
        task = "literal $(do-not-execute); 'quoted'\nsecond line"
        failed, text = bridge.run_tool("dsh_run", self.args(task))
        self.assertFalse(failed)
        data = json.loads(text.split("=== stdout ===\n")[1].split("\n\n=== stderr")[0])
        self.assertEqual(data["argv"], ["headless", "-"])
        self.assertEqual(data["task"], task)
        self.assertEqual(Path(data["cwd"]).resolve(), self.workdir.resolve())

    def test_resume_passes_known_id(self):
        failed, text = bridge.run_tool("dsh_resume", self.args(session_id="session-known"))
        self.assertFalse(failed)
        data = json.loads(text.split("=== stdout ===\n")[1].split("\n\n=== stderr")[0])
        self.assertEqual(data["argv"], ["headless", "--session-id", "session-known", "-"])

    def test_validation_rejects_invalid_input_without_launch(self):
        invalid = [None, [], self.args(""), self.args(workdir="relative"),
                   self.args(workdir=str(self.workdir / "missing")),
                   self.args(timeout_sec=0), self.args(timeout_sec=True),
                   self.args(timeout_sec="3"), self.args(extra="unexpected")]
        with patch.object(bridge.subprocess, "Popen") as popen:
            for args in invalid:
                with self.subTest(args=args):
                    self.assertTrue(bridge.run_tool("dsh_run", args)[0])
            self.assertTrue(bridge.run_tool("dsh_resume", self.args())[0])
            self.assertTrue(bridge.run_tool("dsh_resume", self.args(session_id="--bad"))[0])
            self.assertTrue(bridge.run_tool("unknown", self.args())[0])
            popen.assert_not_called()

    def test_missing_cli_is_reported(self):
        with patch.dict(os.environ, {"PATH": str(self.workdir / "empty")}):
            failed, text = bridge.run_tool("dsh_run", self.args())
        self.assertTrue(failed)
        self.assertIn("Cannot start dsh", text)

    def test_nonzero_exit_keeps_partial_changes(self):
        failed, text = bridge.run_tool("dsh_run", self.args("failure"))
        self.assertTrue(failed)
        self.assertIn("exit=7", text)
        self.assertIn("EXPECTED_FAILURE", text)
        self.assertEqual((self.workdir / "partial.txt").read_text(), "written before failure")

    def test_timeout_preserves_partial_output(self):
        failed, text = bridge.run_tool("dsh_run", self.args("timeout", timeout_sec=1))
        self.assertTrue(failed)
        self.assertIn("TIMEOUT", text)
        self.assertIn("PARTIAL_OUTPUT", text)
        self.assertIn("PARTIAL_ERROR", text)
        self.assertIn("Inspect files", text)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_timeout_stops_worker_process_group(self):
        failed, text = bridge.run_tool("dsh_run", self.args("tree-timeout", timeout_sec=1))
        self.assertTrue(failed)
        self.assertIn("CHILD_STARTED", text)
        pid = int((self.workdir / "child.pid").read_text())
        # A killed child can briefly remain as a zombie until adopted/reaped.
        state = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.strip()
        self.assertTrue(not state or state.startswith("Z"), state)

    def test_protocol_negotiation_and_notifications(self):
        response = bridge.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {"protocolVersion": "future-version"}})
        self.assertEqual(response["result"]["protocolVersion"], bridge.PROTOCOL_VERSION)
        with patch.object(bridge, "run_tool") as run:
            self.assertIsNone(bridge.dispatch({"jsonrpc": "2.0", "method": "tools/call"}))
            run.assert_not_called()

    def test_invalid_requests_and_recovery(self):
        requests = ["not json", "[]", json.dumps({"jsonrpc": "2.0", "id": 2,
                    "method": "tools/list"}), json.dumps({"jsonrpc": "2.0", "id": 3,
                    "method": "ping", "params": []}), json.dumps({"jsonrpc": "2.0",
                    "id": 4, "method": "unknown"}), json.dumps({"jsonrpc": "2.0",
                    "method": "notifications/initialized"})]
        proc = subprocess.run([sys.executable, str(ROOT / "bridge/dsh_mcp.py")],
                              input="\n".join(requests) + "\n", capture_output=True,
                              text=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(len(responses), 5)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["error"]["code"], -32600)
        self.assertEqual([t["name"] for t in responses[2]["result"]["tools"]],
                         ["dsh_run", "dsh_resume"])
        self.assertEqual(responses[3]["error"]["code"], -32602)
        self.assertEqual(responses[4]["error"]["code"], -32601)
        self.assertEqual(proc.stderr, "")


    def test_stdio_tool_calls_survive_malformed_name(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": {}, "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "dsh_run", "arguments": self.args("roundtrip")}},
        ]
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bridge/dsh_mcp.py")],
            input="".join(json.dumps(req) + "\n" for req in requests),
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertIn("Unknown tool", responses[0]["result"]["content"][0]["text"])
        self.assertFalse(responses[1]["result"]["isError"])
        self.assertIn("roundtrip", responses[1]["result"]["content"][0]["text"])

    def test_output_clipping_keeps_both_ends(self):
        text = bridge.clip("START" + "x" * 100 + "END", 20)
        self.assertTrue(text.startswith("START"))
        self.assertTrue(text.endswith("END"))
        self.assertIn("truncated", text)


if __name__ == "__main__":
    unittest.main()
