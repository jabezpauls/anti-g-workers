"""Verification logic: acceptance commands, report normalization, path and scope helpers, flags, status."""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from typing import BinaryIO

from agw.models import (
    BLOCKED_ACTIONS, COMMAND_TRUNCATE, BRANCH_SWITCHED, DELETED_FILES, LARGE_DIFF, NO_ACCEPT, LARGE_DIFF_LINES, NO_CHANGES, NO_REPORT,
    OUT_OF_SCOPE_WRITE, OVERLAP, PATH_PARAM_KEYS, READ_MODE_CHANGES, READ_TOOLS, REPORT_MISMATCH,
    REPORT_STATUSES, SCOPE_VIOLATION, SUSPICIOUS_COMMAND, TAIL_LINES, AcceptResult, AgyOutcome, DiffStat,
    Flag, Report, Task, TaskResult, ToolCall,
)

# Acceptance output kept in memory and in the log is capped; the tail (where failures show) is what survives.
MAX_ACCEPT_OUTPUT_BYTES = 200_000
MAX_DETAIL_PATHS = 5
MAX_OUT_OF_SCOPE_PATHS = 3
MAX_SUSPICIOUS_COMMANDS = 2
MAX_OVERLAP_PATHS = 5
MAX_ERROR_CHARS = 160


# ---- acceptance commands --------------------------------------------------------------------------

def _read_capped(handle: BinaryIO) -> str:
    """Read a temp output file as text, keeping at most MAX_ACCEPT_OUTPUT_BYTES from its end."""
    size = handle.seek(0, os.SEEK_END)
    start = max(0, size - MAX_ACCEPT_OUTPUT_BYTES)
    handle.seek(start)
    text = handle.read().decode("utf-8", errors="replace")
    if start:
        text = f"[... output truncated: first {start} bytes omitted ...]\n" + text
    return text


def _kill_tree(proc: "subprocess.Popen[bytes]") -> None:
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _wait_for_exit(proc: "subprocess.Popen[bytes]", timeout_seconds: float,
                   cancel: threading.Event | None) -> str:
    """Wait for proc. Returns "exit", "timeout" or "cancel" (the last only when `cancel` gets set)."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        try:
            proc.wait(timeout=min(0.2, remaining))
            return "exit"
        except subprocess.TimeoutExpired:
            pass
        if cancel is not None and cancel.is_set():
            return "cancel"


def run_acceptance(commands: list[str], cwd: str, timeout_seconds: int, log_path: str,
                   env: dict[str, str], cancel: threading.Event | None = None) -> list[AcceptResult]:
    """Run every acceptance command in order (shell, in cwd). Output is capped; timeouts give exit -1.

    If `cancel` is set, the running command's process group is killed (exit -1, tail ends with "(interrupted)")
    and the remaining commands are not started.
    """
    results: list[AcceptResult] = []
    if not commands:
        return results
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as log:
        for command in commands:
            if cancel is not None and cancel.is_set():
                break
            interrupted = False
            started = time.monotonic()
            exit_code = 0
            with tempfile.TemporaryFile() as out:
                try:
                    proc = subprocess.Popen(
                        command, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                        stdout=out, stderr=subprocess.STDOUT,
                        start_new_session=(os.name != "nt"))
                except OSError as exc:
                    output = f"agw: cannot start command: {exc}"
                    exit_code = 127
                else:
                    waited = _wait_for_exit(proc, timeout_seconds, cancel)
                    if waited == "exit":
                        exit_code = proc.returncode
                        output = _read_capped(out)
                        if os.name != "nt":
                            _kill_tree_group_leftovers(proc)
                    else:
                        _kill_tree(proc)
                        proc.wait()
                        output = _read_capped(out)
                        if output and not output.endswith("\n"):
                            output += "\n"
                        interrupted = waited == "cancel"
                        output += "(interrupted)" if interrupted else f"(timed out after {timeout_seconds}s)"
                        exit_code = -1
            duration = time.monotonic() - started
            log.write(f"$ {command}\n{output}")
            if output and not output.endswith("\n"):
                log.write("\n")
            log.write(f"[exit {exit_code}] ({duration:.1f}s)\n\n")
            log.flush()
            tail = output.rstrip("\n").splitlines()[-TAIL_LINES:]
            results.append(AcceptResult(command=command, exit_code=exit_code, tail=tail,
                                        duration_seconds=duration))
            if interrupted:
                break
    return results


def _kill_tree_group_leftovers(proc: "subprocess.Popen[bytes]") -> None:
    """Background children a finished command left in its process group must not outlive it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


# ---- reports ---------------------------------------------------------------------------------------

def _as_text(value: object) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _as_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item if isinstance(item, str) else str(item) for item in value]


def normalize_report(structured: object, response: str) -> Report:
    """Always return a Report; a missing/invalid structured report keeps the raw response as findings."""
    if isinstance(structured, dict) and structured.get("status") in REPORT_STATUSES:
        return Report(
            status=structured["status"],
            summary=_as_text(structured.get("summary")),
            findings=_as_text(structured.get("findings")),
            files_changed=_as_list(structured.get("files_changed")),
            blocked_actions=_as_list(structured.get("blocked_actions")),
            concerns=_as_list(structured.get("concerns")),
        )
    return Report(status="missing", findings=(response or "").strip())


# ---- paths and scope -----------------------------------------------------------------------------

def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def path_within(path: str, root: str) -> bool:
    """True if path equals root or lies below it (by path components, after realpath)."""
    if not path or not root:
        return False
    p, r = _norm(path), _norm(root)
    if p == r:
        return True
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:        # different drives, or mixed absolute/relative
        return False


def normalize_rel(path: str, worktree: str) -> str:
    """Absolute paths inside the worktree become relative; separators become '/'; leading './' is dropped."""
    if os.path.isabs(path) and path_within(path, worktree):
        path = os.path.relpath(os.path.realpath(path), os.path.realpath(worktree))
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:].lstrip("/")
    return path


def _glob_variants(pattern: str) -> list[str]:
    """Expand `**/` into 'one or more directories' (`*/`, since * crosses '/') and 'zero directories'."""
    index = pattern.find("**/")
    if index < 0:
        return [pattern.replace("**", "*")]
    head, rest = pattern[:index], pattern[index + 3:]
    return [head + "*/" + tail for tail in _glob_variants(rest)] + [head + tail for tail in _glob_variants(rest)]


def matches_scope(path: str, globs: list[str], worktree: str | None = None) -> bool:
    """Scope semantics of spec section 6 (POSIX relative paths; `*` also crosses '/')."""
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    for raw in globs:
        glob = raw.replace("\\", "/")
        while glob.startswith("./"):
            glob = glob[2:]
        if not glob:
            continue
        if glob.endswith("/"):
            if path.startswith(glob):
                return True
            continue
        if worktree is not None and os.path.isdir(os.path.join(worktree, glob)):
            if path == glob or path.startswith(glob + "/"):
                return True
        if any(fnmatch.fnmatchcase(path, variant) for variant in _glob_variants(glob)):
            return True
    return False


# ---- command heuristics --------------------------------------------------------------------------

_GIT_STATE = re.compile(
    r"\bgit\s+(-C\b|push|pull|commit|reset|checkout|switch|rebase|merge|stash|clean|branch|tag|worktree|remote|config)\b")
_WRITE_INDICATOR = re.compile(
    r"(>|\btee\b|\brm\b|\bmv\b|\bcp\b|\bsed\s+-i|\bchmod\b|\bchown\b|\bln\b|\btouch\b|\bmkdir\b|\brmdir\b"
    r"|\binstall\b|\bdd\b)")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_REDIRECT_SPLIT = re.compile(r"[<>|;&]")


def _one_line(text: str) -> str:
    return re.sub(r"[\r\n\t]+", " ", text)


def _truncate(text: str, limit: int = COMMAND_TRUNCATE) -> str:
    text = _one_line(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _command_tokens(cmd: str) -> list[str]:
    try:
        raw = shlex.split(cmd)
    except ValueError:
        raw = cmd.split()
    tokens: list[str] = []
    for token in raw:
        # Redirections glued to a path (`>/etc/x`, `2>/dev/null`) must still expose the path.
        for piece in _REDIRECT_SPLIT.split(token):
            piece = piece.lstrip("0123456789")      # fd numbers in `2>file`; spec strips leading digits
            if piece:
                tokens.append(piece)
    return tokens


def is_suspicious_command(cmd: str, worktree: str, temp_root: str) -> bool:
    """Heuristic (spec section 9): state-changing git, or a write-looking command touching outside paths."""
    if _GIT_STATE.search(cmd):
        return True
    if not _WRITE_INDICATOR.search(cmd):
        return False
    for token in _command_tokens(cmd):
        if not (token.startswith(("/", "~")) or _DRIVE.match(token) or ".." in token):
            continue
        candidate = os.path.expanduser(token)
        if not os.path.isabs(candidate):
            candidate = os.path.join(worktree, candidate)
        candidate = os.path.normpath(candidate)
        if path_within(candidate, worktree) or path_within(candidate, temp_root):
            continue
        if candidate == "/dev" or candidate.startswith("/dev/"):
            continue
        return True
    return False


# ---- flags --------------------------------------------------------------------------------------

def path_params(call: ToolCall) -> list[str]:
    """String values of a tool call's path-like parameters."""
    values: list[str] = []
    for key, value in call.params.items():
        if isinstance(key, str) and key.lower() in PATH_PARAM_KEYS and isinstance(value, str) and value:
            values.append(value)
    return values


def command_line(call: ToolCall) -> str:
    """The shell command of a run_command call ('' if absent)."""
    for key, value in call.params.items():
        if isinstance(key, str) and key.lower() in ("commandline", "command") and isinstance(value, str):
            return value
    return ""


def _capped(items: list[str], limit: int) -> str:
    shown = ", ".join(items[:limit])
    extra = len(items) - limit
    return f"{shown} +{extra} more" if extra > 0 else shown


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _out_of_scope_paths(tool_calls: list[ToolCall], worktree: str, worktree_base: str, temp_root: str,
                        brain_dir: str) -> list[str]:
    found: list[str] = []
    for call in tool_calls:
        if call.name in READ_TOOLS or call.name == "run_command":
            continue
        for raw in path_params(call):
            expanded = os.path.expanduser(raw)
            absolute = expanded if os.path.isabs(expanded) else os.path.join(worktree, expanded)
            if path_within(absolute, worktree):
                continue
            if path_within(absolute, brain_dir):
                continue
            if path_within(absolute, temp_root) and not path_within(absolute, worktree_base):
                continue
            found.append(raw)
    return _dedupe(found)


def task_flags(*, task: Task, worktree: str, worktree_base: str, temp_root: str, brain_dir: str,
               changes: list[str], tool_calls: list[ToolCall], report: Report,
               diff: DiffStat | None, worker_ran_ok: bool, accept_count: int | None = None,
               branch_switched: str = "") -> list[Flag]:
    """All task-level flags (spec section 9), in the documented order."""
    flags: list[Flag] = []
    edit = task.mode == "edit"

    outside = _out_of_scope_paths(tool_calls, worktree, worktree_base, temp_root, brain_dir)
    if outside:
        flags.append(Flag(OUT_OF_SCOPE_WRITE, _capped(outside, MAX_OUT_OF_SCOPE_PATHS)))

    suspicious = _dedupe([_truncate(command_line(c)) for c in tool_calls
                          if c.name == "run_command" and command_line(c)
                          and is_suspicious_command(command_line(c), worktree, temp_root)])
    if suspicious:
        flags.append(Flag(SUSPICIOUS_COMMAND, _capped(suspicious, MAX_SUSPICIOUS_COMMANDS)))

    if branch_switched:
        flags.append(Flag(BRANCH_SWITCHED, branch_switched))

    if not edit and changes:
        flags.append(Flag(READ_MODE_CHANGES, _capped(changes, MAX_DETAIL_PATHS)))

    if edit and task.scope:
        violating = [p for p in changes if not matches_scope(p, task.scope, worktree)]
        if violating:
            flags.append(Flag(SCOPE_VIOLATION, _capped(violating, MAX_DETAIL_PATHS)))

    if report.blocked_actions:
        flags.append(Flag(BLOCKED_ACTIONS, f"{len(report.blocked_actions)} action(s)"))

    if report.status == "missing" and worker_ran_ok:
        flags.append(Flag(NO_REPORT, "worker returned no structured report"))

    if edit and report.status == "done":
        changed = set(changes)
        claimed = _dedupe([normalize_rel(p, worktree) for p in report.files_changed])
        missing = [p for p in claimed if p not in changed]
        if missing:
            flags.append(Flag(REPORT_MISMATCH, "claimed but unchanged: " + _capped(missing, MAX_DETAIL_PATHS)))

    if edit and worker_ran_ok and not changes and report.status != "blocked":
        flags.append(Flag(NO_CHANGES, "edit task produced no changes"))

    if edit and changes and accept_count == 0:
        flags.append(Flag(NO_ACCEPT, "edit not verified by any acceptance command; review the diff"))

    if diff is not None and diff.insertions + diff.deletions > LARGE_DIFF_LINES:
        flags.append(Flag(LARGE_DIFF, f"{diff.insertions + diff.deletions} lines changed"))

    if diff is not None:
        deleted = [p for letter, p in diff.files if letter == "D"]
        if deleted:
            flags.append(Flag(DELETED_FILES, _capped(deleted, MAX_DETAIL_PATHS)))
    return flags


def overlap_flags(results: list[TaskResult]) -> list[Flag]:
    """One OVERLAP flag listing paths changed by two or more edit tasks."""
    owners: dict[str, list[str]] = {}
    for result in results:
        if result.mode != "edit" or result.diff is None:
            continue
        for _letter, path in result.diff.files:
            ids = owners.setdefault(path, [])
            if result.task_id not in ids:
                ids.append(result.task_id)
    shared = sorted(path for path, ids in owners.items() if len(ids) >= 2)
    if not shared:
        return []
    detail = "; ".join(f"{path} ({', '.join(owners[path])})" for path in shared[:MAX_OVERLAP_PATHS])
    extra = len(shared) - MAX_OVERLAP_PATHS
    if extra > 0:
        detail += f" +{extra} more"
    return [Flag(OVERLAP, detail)]


# ---- status --------------------------------------------------------------------------------------

def _error_reason(outcome: AgyOutcome) -> str:
    lines = [ln.strip() for ln in outcome.error.splitlines() if ln.strip()]
    first = lines[0] if lines else "no details"
    if len(first) > MAX_ERROR_CHARS:
        first = first[: MAX_ERROR_CHARS - 3] + "..."
    return f"{outcome.kind}: {first}"


def decide_status(*, mode: str, not_started_reason: str, setup_failure: str, outcome: AgyOutcome | None,
                  timeout_seconds: int, report: Report, changes: list[str],
                  accept: list[AcceptResult]) -> tuple[str, str]:
    """Final task status and reason; the first matching rule of spec section 9 wins."""
    if not_started_reason:
        return "skipped", f"not started: {not_started_reason}"
    if setup_failure:
        return "error", f"setup failed: {setup_failure}"
    if outcome is None:
        return "error", "error: agy did not run"
    if outcome.kind == "timeout":
        return "timeout", f"worker timed out after {timeout_seconds}s"
    if outcome.kind != "ok":
        return "error", _error_reason(outcome)
    if report.status == "blocked":
        return "failed", "worker blocked"
    if mode == "edit" and not changes:
        return "failed", "no changes"
    if any(r.exit_code != 0 for r in accept):
        return "failed", "acceptance failed"
    if report.status == "partial":
        return "failed", "worker reported partial"
    if mode == "read" and report.status == "missing" and not report.findings.strip():
        return "failed", "no report"
    return "ok", ""
