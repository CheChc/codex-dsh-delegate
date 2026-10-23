"""MCP wire tests: malformed input recovery and delegation contract."""
import importlib.util
import json
import subprocess
import sys
from support import Fixture, ROOT

spec = importlib.util.spec_from_file_location("dsh_bridge", ROOT / "bridge/dsh_mcp.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class BridgeTests(Fixture):
    def rpc(self, requests):
        process = subprocess.run([sys.executable, str(ROOT / "bridge/dsh_mcp.py")],
                                 input="".join(json.dumps(x) + "\n" for x in requests),
                                 text=True, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        return [json.loads(line) for line in process.stdout.splitlines()]

    def test_health_reports_actual_runtime_version_without_running_dsh(self):
        response = bridge.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "dsh_bridge_info", "arguments": {}}})
        result = json.loads(response["result"]["content"][0]["text"])
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(result["version"], "2.0.0")
        self.assertFalse(self.calls.exists())

    def test_invalid_tool_then_valid_tool_does_not_crash_stdio(self):
        responses = self.rpc([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": {}, "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "dsh_run",
                        "arguments": {"task": "roundtrip", "workdir": str(self.workdir)}}},
        ])
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertFalse(responses[1]["result"]["isError"])
        result = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["verification_status"], "not_run")
        self.assertEqual(result["session_id"], "session-fixture")

    def test_notifications_cannot_execute_a_tool(self):
        responses = self.rpc([{"jsonrpc": "2.0", "method": "tools/call",
                               "params": {"name": "dsh_run", "arguments":
                                          {"task": "no", "workdir": str(self.workdir)}}}])
        self.assertEqual(responses, [])
        self.assertFalse(self.calls.exists())

    def test_unknown_fields_and_missing_resume_id_are_rejected(self):
        for name, args in [
            ("dsh_run", {"task": "t", "workdir": str(self.workdir), "unexpected": True}),
            ("dsh_resume", {"task": "t", "workdir": str(self.workdir)}),
            ("dsh_bridge_info", {"task": "unexpected"}),
        ]:
            with self.subTest(name=name):
                failed, text = bridge.run_tool(name, args)
                self.assertTrue(failed)
                self.assertEqual(json.loads(text)["status"], "invalid_input")
        self.assertFalse(self.calls.exists())

    def test_protocol_errors_recover_for_following_requests(self):
        raw = "not json\n[]\n" + json.dumps({"jsonrpc": "2.0", "id": 3,
                                             "method": "tools/list"}) + "\n"
        proc = subprocess.run([sys.executable, str(ROOT / "bridge/dsh_mcp.py")],
                              input=raw, text=True, capture_output=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["error"]["code"], -32600)
        self.assertEqual({t["name"] for t in responses[2]["result"]["tools"]},
                         {"dsh_run", "dsh_resume", "dsh_bridge_info"})
