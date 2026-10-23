"""Run ``dsh headless`` for one task and persist a machine-readable result.

This module intentionally depends only on the Python standard library.  It is
the bridge-side runner used by the delegate workflow.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

VERSION = "2.0.0"
DEFAULT_TIMEOUT = 240

_MAX_JSONL_LINE = 1024 * 1024
_FINAL_TEXT_LIMIT = 12_000
_STDERR_TAIL_LIMIT = 3_000


class WorkspaceBusy(RuntimeError):
    """Raised when another runner already holds the workspace lock."""


def _state_dir() -> Path:
    override = os.environ.get("DSH_BRIDGE_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "state" / "codex-dsh-delegate"


def _canonical_workdir(workdir: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.path.abspath(os.fspath(workdir)))


def _lock_digest(canonical_workdir: str) -> str:
    return hashlib.sha256(os.fsencode(canonical_workdir)).hexdigest()


def _lock_path(workdir: str | os.PathLike[str]) -> Path:
    digest = _lock_digest(_canonical_workdir(workdir))
    return _state_dir() / "locks" / f"{digest}.lock"


def _make_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _open_private_file(path: Path) -> Any:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def workspace_lock(workdir: str | os.PathLike[str]) -> Iterator[None]:
    """Take a non-blocking advisory lock for *workdir*.

    The lock file is keyed by the SHA-256 of the canonical workspace path and
    lives under the bridge state directory.  The file is deliberately not
    removed when the lock is released; unlinking lock files introduces races
    between processes opening and locking them.
    """

    lock_path = _lock_path(workdir)
    _make_private_dir(lock_path.parent)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                raise WorkspaceBusy(
                    f"workspace is busy: {_canonical_workdir(workdir)}"
                ) from exc
            raise
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


def _result(
    *,
    version: str = VERSION,
    task_id: str | None,
    status: str,
    session_id: str | None,
    exit_code: int | None,
    elapsed_sec: float,
    final_text: str = "",
    stderr_tail: str = "",
    log_dir: str | None,
    usage_samples: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "version": version,
        "task_id": task_id,
        "status": status,
        "session_id": session_id,
        "exit_code": exit_code,
        "elapsed_sec": elapsed_sec,
        "final_text": final_text,
        "stderr_tail": stderr_tail,
        "log_dir": log_dir,
        "usage_samples": usage_samples,
        "verification_status": "not_run",
        "error": error,
    }


def _validation_problem(
    task: Any,
    workdir: Any,
    timeout_sec: Any,
    session_id: Any,
) -> str | None:
    if not isinstance(task, str) or not task.strip():
        return "task must be a nonempty string"
    try:
        candidate = os.fspath(workdir)
    except (TypeError, ValueError):
        return "workdir must be a path string"
    if not isinstance(candidate, str):
        return "workdir must be a path string"
    if not candidate:
        return "workdir must not be empty"
    if not os.path.isabs(candidate):
        return "workdir must be an absolute path"
    if not os.path.isdir(candidate):
        return f"workdir is not an existing directory: {candidate}"
    if (
        isinstance(timeout_sec, bool)
        or not isinstance(timeout_sec, int)
        or timeout_sec <= 0
    ):
        return "timeout_sec must be a positive integer"
    if session_id is not None:
        if not isinstance(session_id, str):
            return "session_id must be a nonempty string"
        if not session_id.strip():
            return "session_id must not be blank"
        if session_id.startswith("-"):
            return "session_id must not start with '-'"
    return None


def _validation_error(
    task: Any,
    workdir: Any,
    timeout_sec: Any,
    session_id: Any,
) -> bool:
    return _validation_problem(task, workdir, timeout_sec, session_id) is not None


def _atomic_write_result(path: Path, result: dict[str, Any]) -> None:
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(result, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _publish_result(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    try:
        _atomic_write_result(path, result)
        return result
    except Exception as exc:
        failed = dict(result)
        failed["status"] = "launch_error"
        message = f"cannot publish result to {path}: {exc}"
        if result.get("error"):
            message = f"{result['error']}; {message}"
        failed["error"] = message
        try:
            _atomic_write_result(path, failed)
        except Exception:
            pass
        return failed


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait()
    except OSError:
        pass


def _tail_text(path: Path, limit: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        # UTF-8 uses at most four bytes per character.  The extra bytes make
        # sure a character cut at the start of the read is not needed in the
        # final ``limit`` characters.
        read_size = min(size, limit * 4 + 4)
        handle.seek(size - read_size)
        data = handle.read(read_size)
    return data.decode("utf-8", "replace")[-limit:]


def _limited_lines(path: Path) -> Iterator[bytes | None]:
    """Yield lines (without the trailing newline), or None for overlong lines."""

    with path.open("rb") as handle:
        while True:
            raw = handle.readline(_MAX_JSONL_LINE + 1)
            if not raw:
                return
            if raw.endswith(b"\n"):
                content = raw[:-1]
                if len(content) > _MAX_JSONL_LINE:
                    yield None
                else:
                    yield content
                continue
            if len(raw) > _MAX_JSONL_LINE:
                yield None
                # Consume the rest of this physical line before moving on.
                while True:
                    rest = handle.readline(_MAX_JSONL_LINE + 1)
                    if not rest or rest.endswith(b"\n"):
                        break
                    if len(rest) <= _MAX_JSONL_LINE:
                        break
                continue
            # readline returned less than its limit and did not see a newline,
            # so this is the final line in the file.
            yield raw


def _parse_stdout(
    path: Path,
    requested_session_id: str | None,
) -> dict[str, Any]:
    session_id: str | None = None
    final_text = ""
    final_seen = False
    usage_samples: list[dict[str, Any]] = []
    error_event = False
    json_error = False
    protocol_error = False

    for content in _limited_lines(path):
        if content is None:
            json_error = True
            continue
        try:
            event = json.loads(content.decode("utf-8"))
        except Exception:
            # Includes UnicodeDecodeError and JSONDecodeError, plus parser
            # limits such as RecursionError for deeply nested records.
            json_error = True
            continue
        if not isinstance(event, dict):
            json_error = True
            continue

        event_type = event.get("type")
        if event_type == "session":
            observed = event.get("sessionId", event.get("session_id"))
            if not isinstance(observed, str) or not observed:
                protocol_error = True
            else:
                if session_id is not None and observed != session_id:
                    protocol_error = True
                elif session_id is None:
                    session_id = observed
                if (
                    requested_session_id is not None
                    and observed != requested_session_id
                ):
                    protocol_error = True
        elif event_type == "final":
            text = event.get("text")
            if not isinstance(text, str):
                protocol_error = True
            else:
                final_text = text
                final_seen = True
        elif event_type == "status":
            if event.get("phase") == "step_end":
                usage = event.get("usage")
                if usage is None:
                    continue
                if isinstance(usage, dict):
                    usage_samples.append(usage)
                else:
                    protocol_error = True
        elif event_type == "error":
            error_event = True

    return {
        "session_id": session_id,
        "final_text": final_text,
        "final_seen": final_seen,
        "usage_samples": usage_samples or None,
        "error_event": error_event,
        "json_error": json_error,
        "protocol_error": protocol_error,
    }


def _execute_run(
    task: str,
    workdir: str,
    timeout_sec: int,
    requested_session_id: str | None,
) -> dict[str, Any]:
    task_id = str(uuid.uuid4())
    started = time.monotonic()
    try:
        state_dir = _state_dir()
        run_dir = state_dir / "runs" / task_id
        _make_private_dir(run_dir)
    except Exception as exc:
        return _result(
            task_id=task_id,
            status="launch_error",
            session_id=None,
            exit_code=None,
            elapsed_sec=round(time.monotonic() - started, 3),
            log_dir=None,
            error=f"cannot create run directory: {exc}",
        )

    stdout_path = run_dir / "stdout.jsonl"
    stderr_path = run_dir / "stderr.log"
    result_path = run_dir / "result.json"
    initial = _result(
        task_id=task_id,
        status="running",
        session_id=None,
        exit_code=None,
        elapsed_sec=0.0,
        log_dir=str(run_dir),
    )
    try:
        _atomic_write_result(result_path, initial)
    except Exception as exc:
        return _publish_result(
            result_path,
            _result(
                task_id=task_id,
                status="launch_error",
                session_id=None,
                exit_code=None,
                elapsed_sec=round(time.monotonic() - started, 3),
                log_dir=str(run_dir),
                error=f"cannot write initial result file {result_path}: {exc}",
            ),
        )

    stdout_file = None
    stderr_file = None
    proc: subprocess.Popen[bytes] | None = None
    timed_out = False
    launch_problem = False
    execution_problem = False
    error_detail: str | None = None

    try:
        stdout_file = _open_private_file(stdout_path)
        stderr_file = _open_private_file(stderr_path)
        command = ["dsh", "headless", "--json"]
        if requested_session_id is not None:
            command.extend(["--session-id", requested_session_id])
        command.append("-")
        try:
            proc = subprocess.Popen(
                command,
                cwd=workdir,
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
        except Exception as exc:
            launch_problem = True
            error_detail = (
                "Cannot start dsh; check PATH, installation and permissions: "
                f"{exc}"
            )

        if proc is not None:
            try:
                proc.communicate(
                    input=task.encode("utf-8"),
                    timeout=timeout_sec,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_group(proc)
            except Exception as exc:
                execution_problem = True
                error_detail = f"dsh communication failed: {exc}"
                _kill_process_group(proc)
    except Exception as exc:
        launch_problem = True
        error_detail = f"cannot prepare dsh logs or process: {exc}"
    finally:
        for handle in (stdout_file, stderr_file):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass

    elapsed_sec = round(time.monotonic() - started, 3)
    exit_code = proc.returncode if proc is not None else None
    try:
        stderr_tail = _tail_text(stderr_path, _STDERR_TAIL_LIMIT)
    except OSError as exc:
        stderr_tail = ""
        launch_problem = True
        error_detail = f"cannot read stderr log {stderr_path}: {exc}"

    parsed = {
        "session_id": None,
        "final_text": "",
        "final_seen": False,
        "usage_samples": None,
        "error_event": False,
        "json_error": False,
        "protocol_error": False,
    }
    if not launch_problem:
        try:
            parsed = _parse_stdout(stdout_path, requested_session_id)
        except Exception as exc:
            launch_problem = True
            error_detail = f"cannot parse stdout log {stdout_path}: {exc}"

    if launch_problem or execution_problem:
        status = "launch_error"
    elif timed_out:
        status = "timed_out"
    elif exit_code != 0:
        status = "failed"
    elif parsed["error_event"]:
        status = "failed"
    elif (
        parsed["json_error"]
        or parsed["protocol_error"]
        or parsed["session_id"] is None
        or not parsed["final_seen"]
    ):
        status = "protocol_error"
    else:
        status = "completed"

    error_message = error_detail
    if status == "launch_error" and error_message is None:
        error_message = "dsh launch failed"
    elif status == "timed_out":
        error_message = f"dsh timed out after {timeout_sec}s"
    elif status == "failed" and error_message is None:
        if exit_code != 0:
            error_message = f"dsh exited with code {exit_code}"
        else:
            error_message = "dsh reported an error event"
    elif status == "protocol_error" and error_message is None:
        error_message = "dsh produced incomplete or invalid JSONL output"

    final_result = _result(
        task_id=task_id,
        status=status,
        session_id=parsed["session_id"],
        exit_code=exit_code,
        elapsed_sec=elapsed_sec,
        final_text=parsed["final_text"][:_FINAL_TEXT_LIMIT],
        stderr_tail=stderr_tail,
        log_dir=str(run_dir),
        usage_samples=parsed["usage_samples"],
        error=error_message,
    )
    return _publish_result(result_path, final_result)


def run_dsh(
    task: str,
    workdir: str,
    timeout_sec: int = DEFAULT_TIMEOUT,
    session_id: str | None = None,
    *,
    lock: bool = True,
) -> dict[str, Any]:
    """Run one task through ``dsh headless --json`` and return its result.

    ``lock=False`` is for callers that already hold :func:`workspace_lock`.
    Invalid input and lock contention are reported as result dictionaries
    instead of escaping as exceptions.
    """

    problem = _validation_problem(task, workdir, timeout_sec, session_id)
    if problem is not None:
        return _result(
            task_id=None,
            status="invalid_input",
            session_id=None,
            exit_code=None,
            elapsed_sec=0.0,
            log_dir=None,
            error=problem,
        )

    try:
        workdir_path = _canonical_workdir(workdir)
        if lock:
            try:
                with workspace_lock(workdir_path):
                    return _execute_run(task, workdir_path, timeout_sec, session_id)
            except WorkspaceBusy as exc:
                return _result(
                    task_id=None,
                    status="busy",
                    session_id=None,
                    exit_code=None,
                    elapsed_sec=0.0,
                    log_dir=None,
                    error=str(exc),
                )
        return _execute_run(task, workdir_path, timeout_sec, session_id)
    except Exception as exc:
        return _result(
            task_id=None,
            status="launch_error",
            session_id=None,
            exit_code=None,
            elapsed_sec=0.0,
            log_dir=None,
            error=f"runner failed before launching dsh: {exc}",
        )
