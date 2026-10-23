"""Regression tests for the previously reproduced unsafe commit behavior."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
from support import Fixture, ROOT

class PlanExecTests(Fixture):
    def setUp(self):
        super().setUp()
        self.plan = self.base / "PLAN.md"
        self.plan.write_text("Only update the explicitly permitted src.py file.\n")
        self.git("init", "-q")
        self.git("config", "user.name", "Contract Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.base / "no-hooks"))
        for name in ("src.py", "outside.txt", "user-staged.txt", "user-dirty.txt"):
            (self.workdir / name).write_text("baseline\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.before = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.workdir,
                                       text=True, stderr=subprocess.STDOUT).strip()

    def invoke(self, *, verify="true", commit=True, files=("src.py",), use_plan=True,
               extra=(), write=True):
        cmd = [sys.executable, str(ROOT / "scripts/plan-exec"), "-C", str(self.workdir)]
        if use_plan:
            cmd += ["--exec-only", str(self.plan)]
        if verify is not None:
            cmd += ["--verify", verify]
        if commit:
            cmd.append("--commit")
        for rel in files:
            cmd += ["--file", rel]
        cmd += list(extra) + ["fixture repair"]
        with patch.dict(os.environ, {
            "DSH_FIXTURE_WRITE": write if isinstance(write, str) else ("src.py" if write else ""),
        }):
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=15)
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            result = {}
        return proc, result

    def assert_no_commit(self):
        self.assertEqual(self.git("rev-parse", "HEAD"), self.before)

    def test_worker_failure_never_commits_or_reports_success(self):
        with patch.dict(os.environ, {"DSH_FIXTURE_MODE": "failure"}):
            proc, result = self.invoke()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "execution_failed", result)
        self.assertEqual(result["worker"]["exit_code"], 7)
        self.assert_no_commit()

    def test_verification_failure_never_commits_or_reports_success(self):
        proc, result = self.invoke(verify="exit 9")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "verification_failed", result)
        self.assertEqual(result["verification"]["exit_code"], 9)
        self.assert_no_commit()

    def test_missing_verification_is_unverified_and_cannot_commit(self):
        proc, result = self.invoke(verify=None)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "unverified", result)
        self.assert_no_commit()

    def test_only_explicit_files_are_committed_user_work_is_preserved(self):
        (self.workdir / "user-staged.txt").write_text("user staged work\n")
        self.git("add", "user-staged.txt")
        (self.workdir / "user-dirty.txt").write_text("user unstaged work\n")
        (self.workdir / "user-untracked.txt").write_text("user untracked work\n")
        staged_before = self.git("diff", "--cached", "--binary")
        dirty_before = self.git("diff", "--", "user-dirty.txt")
        proc, result = self.invoke()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "committed", result)
        self.assertEqual(self.git("show", "--format=", "--name-only", "HEAD"), "src.py")
        self.assertEqual(self.git("diff", "--cached", "--binary"), staged_before)
        self.assertEqual(self.git("diff", "--", "user-dirty.txt"), dirty_before)
        self.assertEqual((self.workdir / "user-untracked.txt").read_text(), "user untracked work\n")
        self.assertIn("user-untracked.txt", self.git("ls-files", "--others", "--exclude-standard"))
        self.assertNotIn("user-untracked.txt", self.git("ls-tree", "-r", "--name-only", "HEAD"))

    def test_worker_scope_violation_keeps_evidence_without_commit(self):
        with patch.dict(os.environ, {"DSH_FIXTURE_MODE": "out_of_scope"}):
            proc, result = self.invoke()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "scope_violation", result)
        self.assertEqual((self.workdir / "outside.txt").read_text(), "unexpected\n")
        self.assert_no_commit()

    def test_verification_scope_violation_is_detected(self):
        proc, result = self.invoke(verify="printf 'changed\\n' > outside.txt")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "scope_violation", result)
        self.assert_no_commit()

    def test_commit_requires_explicit_file_scope(self):
        proc, _ = self.invoke(files=())
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.calls.exists())
        self.assert_no_commit()

    def test_path_traversal_git_metadata_and_symlink_escape_are_rejected(self):
        external = self.base / "external.txt"
        external.write_text("private\n")
        (self.workdir / "escape").symlink_to(external)
        for name in ("../external.txt", ".git/config", "escape", ".plan-exec/result.json"):
            with self.subTest(path=name):
                proc, _ = self.invoke(files=(name,))
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertFalse(self.calls.exists())
        self.assertEqual(external.read_text(), "private\n")
        self.assert_no_commit()

    def test_dirty_target_is_rejected_before_worker(self):
        (self.workdir / "src.py").write_text("user owns this change\n")
        proc, _ = self.invoke()
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.calls.exists())
        self.assertEqual((self.workdir / "src.py").read_text(), "user owns this change\n")
        self.assert_no_commit()

    def test_empty_plan_never_starts_worker(self):
        self.plan.write_text(" \n")
        proc, _ = self.invoke()
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.calls.exists())
        self.assert_no_commit()

    def test_plan_only_and_planner_failure(self):
        proc, result = self.invoke(commit=False, use_plan=False, extra=("--plan-only",))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "plan_only", result)
        self.assertFalse(self.calls.exists())
        for mode in ("failure", "empty"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"CODEX_FIXTURE_MODE": mode}):
                proc, _ = self.invoke(commit=False, use_plan=False)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertFalse(self.calls.exists())
        self.assert_no_commit()

    def test_verification_timeout_is_a_failure(self):
        started = time.monotonic()
        proc, result = self.invoke(verify="sleep 10", extra=("--verify-timeout", "1"))
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "verification_failed", result)
        self.assertLess(time.monotonic() - started, 6)
        self.assert_no_commit()

    def test_repeat_run_does_not_reuse_a_plan_or_create_an_empty_commit(self):
        proc, result = self.invoke()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        head = self.git("rev-parse", "HEAD")
        proc2, result2 = self.invoke()
        self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
        self.assertEqual(result2.get("status"), "no_changes", result2)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_active_hooks_refuse_auto_commit_without_bypassing_checks(self):
        hooks = self.base / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.git("config", "core.hooksPath", str(hooks))
        proc, result = self.invoke()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "invalid_input", result)
        self.assertFalse(self.calls.exists())
        self.assert_no_commit()


    def test_unused_allowed_path_does_not_break_a_valid_commit(self):
        proc, result = self.invoke(files=("src.py", "not-created.txt"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "committed", result)
        self.assertEqual(self.git("show", "--format=", "--name-only", "HEAD"), "src.py")

    def test_git_pathspec_magic_is_treated_as_a_literal_filename(self):
        name = ":(glob)*"
        (self.workdir / "user-untracked.txt").write_text("user work\n")
        proc, result = self.invoke(files=(name,), write=name)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "committed", result)
        self.assertEqual(self.git("show", "--format=", "--name-only", "HEAD"), name)
        self.assertNotIn("user-untracked.txt", self.git("ls-tree", "-r", "--name-only", "HEAD"))

    def test_symlink_to_git_metadata_is_rejected_before_worker(self):
        (self.workdir / "metadata-alias").symlink_to(self.workdir / ".git/config")
        proc, _ = self.invoke(files=("metadata-alias",), commit=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_verified_without_commit_reports_verified(self):
        proc, result = self.invoke(commit=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "verified", result)
        self.assert_no_commit()

    def test_mutating_commit_hook_cannot_include_user_staged_work(self):
        (self.workdir / "user-staged.txt").write_text("user staged work\n")
        self.git("add", "user-staged.txt")
        staged = self.git("diff", "--cached", "--binary")
        hooks = self.base / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\ngit add user-staged.txt\n")
        hook.chmod(0o755)
        self.git("config", "core.hooksPath", str(hooks))
        proc, result = self.invoke()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "invalid_input", result)
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.git("diff", "--cached", "--binary"), staged)
        self.assert_no_commit()

    def test_existing_ignored_target_is_preserved_and_worker_not_started(self):
        (self.workdir / ".git/info/exclude").write_text("ignored.txt\n")
        target = self.workdir / "ignored.txt"
        target.write_text("user ignored work\n")
        proc, result = self.invoke(files=("ignored.txt",), write="ignored.txt")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "invalid_input", result)
        self.assertFalse(self.calls.exists())
        self.assertEqual(target.read_text(), "user ignored work\n")
        self.assert_no_commit()

    def test_new_ignored_target_is_rejected(self):
        (self.workdir / ".git/info/exclude").write_text("ignored.txt\n")
        proc, result = self.invoke(files=("ignored.txt",), write="ignored.txt")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.workdir / "ignored.txt").exists())
        self.assert_no_commit()

    def test_special_index_flags_cannot_hide_user_target_changes(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                self.git("update-index", flag, "src.py")
                (self.workdir / "src.py").write_text("hidden user work\n")
                proc, result = self.invoke()
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertFalse(self.calls.exists())
                self.assertEqual((self.workdir / "src.py").read_text(), "hidden user work\n")
                self.git("update-index", "--no-assume-unchanged", "--no-skip-worktree", "src.py")
                (self.workdir / "src.py").write_text("baseline\n")
        self.assert_no_commit()

    def test_worker_commit_is_detected_even_with_clean_worktree(self):
        with patch.dict(os.environ, {"DSH_FIXTURE_MODE": "self_commit"}):
            proc, result = self.invoke(write=False)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "scope_violation", result)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), self.before)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "unauthorized worker commit")
        self.assertEqual(result["verification"]["status"], "not_run")

    def test_verifier_commit_is_detected_even_with_clean_worktree(self):
        proc, result = self.invoke(write=False,
            verify="printf 'verifier work\\n' > outside.txt && git add outside.txt && git commit -qm 'verifier commit'")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "scope_violation", result)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "verifier commit")

    def test_branch_change_without_head_change_is_detected(self):
        proc, result = self.invoke(write=False, verify="git checkout -qb unexpected-branch")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "scope_violation", result)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.before)
        self.assertEqual(self.git("branch", "--show-current"), "unexpected-branch")

    def test_post_index_change_hook_is_rejected_before_status_can_run_it(self):
        (self.workdir / "user-staged.txt").write_text("user staged work\n")
        self.git("add", "user-staged.txt")
        # Make status refresh the index if optional writes are enabled.
        (self.workdir / "src.py").write_text("baseline\n")
        staged = self.git("diff", "--cached", "--binary")
        hooks = self.base / "hooks"
        hooks.mkdir()
        hook = hooks / "post-index-change"
        hook.write_text("#!/bin/sh\nprintf triggered > hook-ran\n"
                        "git -c core.hooksPath=/nonexistent reset -q HEAD -- user-staged.txt\n")
        hook.chmod(0o755)
        self.git("config", "core.hooksPath", str(hooks))
        proc, result = self.invoke()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(result.get("status"), "invalid_input", result)
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.workdir / "hook-ran").exists())
        self.assertEqual(self.git("diff", "--cached", "--binary"), staged)
        self.assert_no_commit()
