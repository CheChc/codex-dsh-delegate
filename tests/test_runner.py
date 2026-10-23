"""Execution invariants using fake dsh and private temporary state."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from unittest.mock import patch
from support import Fixture, ROOT
import dsh_runner as runner


class RunnerTests(Fixture):
    def run_worker(self, **kwargs):
        return runner.run_dsh("literal $(never execute); 'quoted'\nnext line",
                              str(self.workdir), **kwargs)

    def test_success_preserves_session_arguments_usage_and_private_logs(self):
        result = self.run_worker()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["session_id"], "session-fixture")
        data = json.loads(result["final_text"])
        self.assertEqual(data["argv"], ["headless", "--json", "-"])
        self.assertIn("$(never execute)", data["task"])
        self.assertEqual(result["usage_samples"], [{"inputTokens": 3, "outputTokens": 2}])
        self.assertEqual(result["verification_status"], "not_run")
        self.assertNotIn("PRIVATE_THINKING_FIXTURE", json.dumps(result))
        folder = Path(result["log_dir"])
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for name in ("stdout.jsonl", "stderr.log", "result.json"):
            self.assertEqual(stat.S_IMODE((folder / name).stat().st_mode), 0o600)
        self.assertEqual(json.loads((folder / "result.json").read_text()), result)

    def test_resume_uses_the_exact_known_session(self):
        result = self.run_worker(session_id="session-known")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["session_id"], "session-known")
        self.assertEqual(json.loads(result["final_text"])["argv"],
                         ["headless", "--json", "--session-id", "session-known", "-"])

    def test_missing_usage_is_unknown(self):
        with patch.dict(os.environ, {"DSH_FIXTURE_MODE": "no_usage"}):
            result = self.run_worker()
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["usage_samples"])

    def test_invalid_inputs_do_not_start_worker(self):
        cases = [
            ("", str(self.workdir), 1, None),
            ("task", "relative", 1, None),
            ("task", str(self.workdir / "missing"), 1, None),
            ("task", str(self.workdir), True, None),
            ("task", str(self.workdir), 0, None),
            ("task", str(self.workdir), "3", None),
            ("task", str(self.workdir), 1, " "),
            ("task", str(self.workdir), 1, "--option"),
        ]
        for task, directory, timeout, sid in cases:
            with self.subTest(args=(task, directory, timeout, sid)):
                result = runner.run_dsh(task, directory, timeout, sid)
                self.assertEqual(result["status"], "invalid_input", result)
        self.assertFalse(self.calls.exists())

    def test_missing_cli_has_actionable_error(self):
        with patch.dict(os.environ, {"PATH": str(self.base / "absent")}):
            result = self.run_worker()
        self.assertEqual(result["status"], "launch_error")
        self.assertTrue(result.get("error"), result)

    def test_nonzero_exit_keeps_partial_changes_and_session(self):
        with patch.dict(os.environ, {"DSH_FIXTURE_MODE": "failure"}):
            result = self.run_worker()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["session_id"], "session-fixture")
        self.assertIn("EXPECTED_FAILURE", result["stderr_tail"])
        self.assertEqual((self.workdir / "partial.txt").read_text(), "partial\n")

    def test_timeout_keeps_session_and_partial_output_on_disk(self):
        with patch.dict(os.environ, {"DSH_FIXTURE_MODE": "timeout"}):
            result = self.run_worker(timeout_sec=3)
        self.assertEqual(result["status"], "timed_out", result)
        self.assertEqual(result["session_id"], "session-fixture")
        self.assertIn("PARTIAL_ERROR", result["stderr_tail"])
        self.assertIn("PARTIAL_OUTPUT", (Path(result["log_dir"]) / "stdout.jsonl").read_text())
        self.assertEqual(json.loads((Path(result["log_dir"]) / "result.json").read_text())["status"],
                         "timed_out")

    def test_bad_or_missing_terminal_events_cannot_report_success(self):
        for mode in ("no_final", "bad_json", "long_line", "wrong_session"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"DSH_FIXTURE_MODE": mode}):
                result = self.run_worker(session_id="session-known")
                self.assertEqual(result["status"], "protocol_error", result)
        with patch.dict(os.environ, {"DSH_FIXTURE_MODE": "error_event"}):
            self.assertEqual(self.run_worker()["status"], "failed")

    def test_canonical_workspace_alias_is_locked(self):
        alias = self.base / "alias"
        alias.symlink_to(self.workdir, target_is_directory=True)
        with runner.workspace_lock(str(self.workdir)):
            self.assertEqual(runner.run_dsh("task", str(alias))["status"], "busy")
        self.assertEqual(self.run_worker()["status"], "completed")

    def test_lock_is_cross_process_and_released_on_process_exit(self):
        code = ("import sys,time;sys.path.insert(0,sys.argv[1]);"
                "from dsh_runner import workspace_lock;"
                "lock=workspace_lock(sys.argv[2]);lock.__enter__();"
                "print('LOCKED',flush=True);time.sleep(30)")
        proc = subprocess.Popen([sys.executable, "-c", code, str(ROOT / "bridge"),
                                 str(self.workdir)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "LOCKED")
            self.assertEqual(self.run_worker()["status"], "busy")
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            proc.stdout.close()
        self.assertEqual(self.run_worker()["status"], "completed")

    def test_log_open_failure_does_not_start_worker(self):
        with patch.object(runner, "_open_private_file", side_effect=OSError("fixture denied")):
            result = self.run_worker()
        self.assertEqual(result["status"], "launch_error")
        self.assertFalse(self.calls.exists())

    def test_timeout_kills_descendants(self):
        with patch.dict(os.environ, {"DSH_FIXTURE_MODE": "tree_timeout"}):
            result = self.run_worker(timeout_sec=3)
        self.assertEqual(result["status"], "timed_out")
        pid = int((self.workdir / "child.pid").read_text())
        try:
            proc = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)],
                                  capture_output=True, text=True)
        except PermissionError:
            self.skipTest("This execution sandbox cannot inspect processes")
        state = proc.stdout.strip()
        self.assertTrue(not state or state.startswith("Z"), state)
