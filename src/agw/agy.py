"""Build agy invocations, run them with timeout and cancellation, parse the NDJSON stream, classify errors."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any

from agw.models import TERM_WAIT_SECONDS, AgyOutcome, ToolCall

POLL_SECONDS = 0.2
STDERR_TAIL_LINES = 20
MAX_STDERR_READ_BYTES = 1024 * 1024       # only the tail of stderr is ever interesting
AGY_ERROR_PREFIX = "AGY_ERROR: "
_POSIX = os.name == "posix"

_EXIT_CODE_RE = re.compile(r"Command exited with code (-?\d+)")
_QUOTA_RE = re.compile(r"quota|rate.?limit|resource.?exhausted|\b429\b|too many requests|out of credits", re.I)
_AUTH_RE = re.compile(
    r"not (logged|signed) in|log ?in required|unauthenticated|please (log|sign) in"
    r"|authentication (failed|required)|account is blocked|terms of service", re.I)


# ---------------------------------------------------------------------------------------------
# argv / settings
# ---------------------------------------------------------------------------------------------
def build_settings(deny_paths: list[str]) -> str:
    """Return the inline --settings JSON denying file-tool writes under each path."""
    return json.dumps({"permissions": {"deny": [f"write_file({p})" for p in deny_paths]}}, separators=(",", ":"))


def build_argv(agy_path: list[str], model: str, schema_path: str, settings_json: str,
               timeout_seconds: int, sandbox: bool) -> list[str]:
    """Return the full agy argv for one headless worker (the brief goes via stdin)."""
    argv = [*agy_path, "--model", model, "--output-format", "stream-json", "--json-schema", schema_path,
            "--settings", settings_json, "--disable-slash-commands", "--dangerously-skip-permissions",
            "--print-timeout", f"{timeout_seconds}s"]
    if sandbox:
        argv.append("--sandbox")
    return argv


# ---------------------------------------------------------------------------------------------
# process handling
# ---------------------------------------------------------------------------------------------
def _signal_group(proc: subprocess.Popen[Any], force: bool = False) -> None:
    """SIGTERM (or SIGKILL when `force`) the whole process group of `proc`; off POSIX, the process only. Never raises."""
    try:
        if _POSIX:
            # proc was started with start_new_session=True, so its pgid equals its pid
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif force:
            proc.kill()
        else:
            proc.terminate()
    except OSError:       # ProcessLookupError / PermissionError: already gone
        pass


def _group_alive(proc: subprocess.Popen[Any]) -> bool:
    if not _POSIX:
        return proc.poll() is None
    try:
        os.killpg(proc.pid, 0)
        return True
    except OSError:
        return False


def _stop_process(proc: subprocess.Popen[Any]) -> None:
    """SIGTERM the process group, give it TERM_WAIT_SECONDS to exit, then SIGKILL whatever is left."""
    _signal_group(proc)
    deadline = time.monotonic() + TERM_WAIT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None and not _group_alive(proc):
            break
        time.sleep(0.05)
    _kill_leftovers(proc)


def _kill_leftovers(proc: subprocess.Popen[Any]) -> None:
    """SIGKILL the group (also catches background children that outlived the leader) and reap the leader."""
    _signal_group(proc, force=True)
    try:
        proc.wait(timeout=TERM_WAIT_SECONDS)
    except subprocess.TimeoutExpired:      # pragma: no cover - SIGKILL cannot be ignored
        pass


def _feed_stdin(stdin: Any, data: bytes) -> None:
    """Write `data` to the child's stdin and close it; runs in a thread so a child that never reads cannot block us."""
    try:
        stdin.write(data)
        stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass


def run_agy(argv: list[str], brief: str, cwd: str, events_path: str, stderr_path: str,
            hard_timeout_seconds: float, cancel: threading.Event,
            env: dict[str, str] | None = None) -> tuple[int, bool, bool]:
    """Run agy with `brief` on stdin; stdout goes to `events_path`, stderr to `stderr_path` (both files, never pipes).

    Returns (exit_code, hard_killed, cancelled). The child gets its own session/process group; on cancel or after
    `hard_timeout_seconds` the whole group is SIGTERMed, then SIGKILLed after TERM_WAIT_SECONDS. Any group members
    left behind after a normal exit are SIGKILLed too, so no worker process is orphaned. `env=None` inherits the
    environment; a dict replaces it. An unlaunchable executable gives (127, False, False).
    """
    with open(events_path, "w", encoding="utf-8") as events_fh, open(stderr_path, "w", encoding="utf-8") as err_fh:
        if cancel.is_set():
            return 1, False, True
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=events_fh, stderr=err_fh,
                start_new_session=_POSIX)
        except FileNotFoundError:
            if os.path.isdir(cwd):
                err_fh.write(f"agw: cannot execute {argv[0] if argv else ''}\n")
            else:
                err_fh.write(f"agw: working directory does not exist: {cwd}\n")
            return 127, False, False
        except OSError as exc:
            err_fh.write(f"agw: cannot execute {argv[0] if argv else ''}: {exc}\n")
            return 127, False, False

        feeder = threading.Thread(target=_feed_stdin, args=(proc.stdin, brief.encode("utf-8", errors="replace")),
                                  daemon=True)
        feeder.start()

        hard_killed = False
        cancelled = False
        started = time.monotonic()
        try:
            while True:
                try:
                    proc.wait(timeout=POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancel.is_set():
                    cancelled = True
                elif time.monotonic() - started > hard_timeout_seconds:
                    hard_killed = True
                if cancelled or hard_killed:
                    _stop_process(proc)
                    break
        finally:
            # Also covers KeyboardInterrupt/unexpected errors: never leave the group running.
            if proc.poll() is None or _group_alive(proc):
                _kill_leftovers(proc)
        feeder.join(timeout=2)
        return proc.returncode if proc.returncode is not None else 1, hard_killed, cancelled


# ---------------------------------------------------------------------------------------------
# stream parsing
# ---------------------------------------------------------------------------------------------
def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, item in value.items():
        number = _as_int(item)
        if number is not None:
            out[str(key)] = number
    return out


def _str(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _read_tail_text(path: str) -> str:
    """Read at most the last MAX_STDERR_READ_BYTES of a file as text; a missing file is empty."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - MAX_STDERR_READ_BYTES))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _parse_agy_error(stderr_text: str) -> dict[str, Any] | None:
    """The last stderr line starting with 'AGY_ERROR: ', parsed as a JSON object; None if absent or unparsable."""
    last: str | None = None
    for line in stderr_text.splitlines():
        if line.startswith(AGY_ERROR_PREFIX):
            last = line[len(AGY_ERROR_PREFIX):]
    if last is None:
        return None
    try:
        parsed = json.loads(last)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _apply_result(outcome: AgyOutcome, res: dict[str, Any]) -> None:
    """Copy a result object (stream 'result' event payload or --output-format json envelope) into `outcome`."""
    status = res.get("status")
    outcome.status = status if isinstance(status, str) and status else "ERROR"
    outcome.error = _str(res.get("error"))
    outcome.finished_reason = _str(res.get("finished_reason"))
    outcome.response = _str(res.get("response"))
    structured = res.get("structured_output")
    outcome.structured = structured if isinstance(structured, dict) else None
    outcome.usage = _usage(res.get("usage"))
    duration = res.get("duration_seconds")
    outcome.duration_seconds = float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else 0.0
    conv = res.get("conversation_id")
    if isinstance(conv, str) and conv:
        outcome.conversation_id = conv


def _tool_call(step_index: int, su: dict[str, Any], previous: ToolCall | None) -> ToolCall:
    info = su.get("tool_info")
    info = info if isinstance(info, dict) else {}
    name = _str(su.get("tool_name")) or _str(info.get("name")) or (previous.name if previous else "") or "unknown"
    params = info.get("parameters")
    params = dict(params) if isinstance(params, dict) else {}
    output = info.get("output")
    output = output if isinstance(output, str) else None
    if previous is not None:             # a later event for the same step replaces the earlier one ...
        params = params or previous.params   # ... but never loses data the later event simply omitted
        output = output if output is not None else previous.output
    exit_code = None
    if name == "run_command" and output is not None:
        match = _EXIT_CODE_RE.search(output)
        exit_code = int(match.group(1)) if match else None
    return ToolCall(step_index=step_index, name=name, params=params, output=output, exit_code=exit_code)


def parse_stream(events_path: str, stderr_path: str, exit_code: int,
                 hard_killed: bool = False, cancelled: bool = False) -> AgyOutcome:
    """Parse agy's stream-json NDJSON and stderr into an AgyOutcome (never raises on bad input).

    Notes on agy behaviour: each tool step emits ACTIVE then DONE events (deduped by step_index). A tool call denied
    by a permission rule may emit no event at all (the step index simply skips) or an ACTIVE event followed by an
    ERROR event; either way it is not recorded as a tool call, so denials are only reliably visible in the worker's
    own report (`blocked_actions`). The stream may end without a result event (killed, crashed).
    """
    outcome = AgyOutcome(exit_code=exit_code)
    tools: dict[int, ToolCall] = {}
    unknown: set[int] = set()
    step_usage: dict[int, dict[str, int]] = {}
    failed: set[int] = set()       # tool steps whose latest state is ERROR (e.g. denied by a permission rule)
    have_result = False

    try:
        events_fh = open(events_path, encoding="utf-8", errors="replace")
    except OSError:
        events_fh = None
    if events_fh is not None:
        with events_fh:
            for raw in events_fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except ValueError:       # JSONDecodeError, and RecursionError-free enough for sane input
                    outcome.bad_lines += 1
                    continue
                if not isinstance(evt, dict):
                    outcome.bad_lines += 1
                    continue
                kind = evt.get("event")
                if kind == "init":
                    init = evt.get("init")
                    if isinstance(init, dict):
                        outcome.model = _str(init.get("model")) or outcome.model
                    conv = evt.get("conversation_id")
                    if isinstance(conv, str) and conv:
                        outcome.conversation_id = conv
                elif kind == "step_update":
                    su = evt.get("step_update")
                    index = _as_int(su.get("step_index")) if isinstance(su, dict) else None
                    if not isinstance(su, dict) or index is None:
                        outcome.bad_lines += 1
                        continue
                    step_type = su.get("step_type")
                    if step_type == "tool":
                        tools[index] = _tool_call(index, su, tools.get(index))
                        info = su.get("tool_info")
                        if su.get("state") == "ERROR" or (isinstance(info, dict) and info.get("error")):
                            failed.add(index)
                        else:
                            failed.discard(index)
                    elif step_type == "unknown":
                        unknown.add(index)
                    usage = _usage(su.get("usage"))
                    if usage:
                        step_usage[index] = usage
                elif kind == "result":
                    res = evt.get("result")
                    if isinstance(res, dict):
                        _apply_result(outcome, res)
                        have_result = True
                    else:
                        outcome.bad_lines += 1

    # A tool step that ended in ERROR did not do its work (a permission-denied write_file shows up as ACTIVE then
    # ERROR in real streams; sometimes no event at all). Recording it would raise false OUT_OF_SCOPE_WRITE flags.
    # run_command is kept anyway: an attempted suspicious command is worth flagging whether or not it ran.
    outcome.tool_calls = [tools[i] for i in sorted(tools) if i not in failed or tools[i].name == "run_command"]
    outcome.unknown_steps = len(unknown)

    stderr_text = _read_tail_text(stderr_path)
    stderr_lines = stderr_text.splitlines()
    while stderr_lines and not stderr_lines[-1].strip():
        stderr_lines.pop()
    outcome.stderr_tail = "\n".join(stderr_lines[-STDERR_TAIL_LINES:])
    outcome.agy_error = _parse_agy_error(stderr_text)
    outcome.retryable = bool(outcome.agy_error.get("retryable")) if outcome.agy_error else False

    if not have_result:
        outcome.status = "MISSING"
        outcome.error = stderr_lines[-1].strip() if stderr_lines else f"agy exited {exit_code} without a result"
        totals: dict[str, int] = {}          # no result event: salvage token usage from the per-step usage
        for usage in step_usage.values():
            for key, value in usage.items():
                totals[key] = totals.get(key, 0) + value
        outcome.usage = totals

    outcome.kind = classify(outcome, stderr_text, hard_killed, cancelled)
    return outcome


def classify(outcome: AgyOutcome, stderr_text: str, hard_killed: bool = False, cancelled: bool = False) -> str:
    """Map an outcome to one of OUTCOME_KINDS (spec section 13, first match wins)."""
    agy_error = outcome.agy_error if outcome.agy_error is not None else _parse_agy_error(stderr_text)
    err_status = _str(agy_error.get("status")) if agy_error else ""
    err_reason = _str(agy_error.get("reason")) if agy_error else ""
    text = f"{outcome.error}\n{stderr_text}"

    if outcome.exit_code == 0 and outcome.status == "SUCCESS":
        return "ok"
    if (hard_killed or outcome.exit_code == 3 or outcome.finished_reason == "print_timeout"
            or err_reason == "print_timeout"):
        return "timeout"
    if outcome.error.strip().lower() == "interrupted" or cancelled:
        return "interrupted"
    if err_status == "RESOURCE_EXHAUSTED" or _QUOTA_RE.search(text):
        return "quota"
    if err_status in ("UNAUTHENTICATED", "PERMISSION_DENIED") or _AUTH_RE.search(text):
        return "auth"
    if "invalid model selection" in text.lower():
        return "model"
    return "error"


# ---------------------------------------------------------------------------------------------
# availability checks
# ---------------------------------------------------------------------------------------------
def _run_captured(argv: list[str], stdin_text: str | None, cwd: str | None, timeout: float) -> tuple[int, str, str, bool]:
    """Run a short command capturing output; on timeout the whole process group is killed.

    Returns (returncode, stdout, stderr, timed_out). Raises OSError if the command cannot be launched.
    """
    proc = subprocess.Popen(
        argv, cwd=cwd, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        start_new_session=_POSIX)
    timed_out = False
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _signal_group(proc, force=True)
        try:
            out, err = proc.communicate(timeout=TERM_WAIT_SECONDS)
        except subprocess.TimeoutExpired:      # pragma: no cover - pipe held by a process outside the group
            out, err = "", ""
    except BaseException:
        _signal_group(proc, force=True)
        proc.wait()
        raise
    else:
        _signal_group(proc, force=True)   # leftover background children
    return proc.returncode if proc.returncode is not None else 1, out or "", err or "", timed_out


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def check_available(agy_path: list[str]) -> tuple[bool, str]:
    """Check that agy can be found and answers --version. Returns (ok, version line or problem description)."""
    not_found = (False, f"agy not found (agy_path={agy_path!r}). Install the Antigravity CLI and run 'agy' once to log in.")
    if not agy_path or not (shutil.which(agy_path[0]) or os.path.isfile(agy_path[0])):
        return not_found
    command = f"'{' '.join(agy_path)} --version'"
    try:
        code, out, err, timed_out = _run_captured([*agy_path, "--version"], None, None, 30)
    except FileNotFoundError:
        return not_found
    except OSError as exc:
        return False, f"{command} failed: {exc}"
    if timed_out:
        return False, f"{command} failed: timed out after 30s"
    if code != 0:
        return False, f"{command} failed: {_first_line(err) or _first_line(out) or f'exit {code}'}"
    return True, _first_line(out)


def live_check(agy_path: list[str], model: str, timeout_seconds: int = 120) -> tuple[str, str]:
    """Run agy once with a trivial prompt. Returns (kind, detail); kind is 'ok' or a classified failure kind."""
    argv = [*agy_path, "--model", model, "--output-format", "json", "--disable-slash-commands",
            "--print-timeout", f"{timeout_seconds}s"]
    with tempfile.TemporaryDirectory(prefix="agw-live-") as tmp:
        try:
            code, out, err, timed_out = _run_captured(argv, "Reply with exactly: PONG", tmp, timeout_seconds + 30)
        except OSError as exc:
            return "error", f"cannot execute {agy_path[0] if agy_path else ''}: {exc}"

    outcome = AgyOutcome(exit_code=code)
    envelope: dict[str, Any] | None = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            inner = parsed.get("result")
            envelope = inner if parsed.get("event") == "result" and isinstance(inner, dict) else parsed
            break
    if envelope is not None:
        _apply_result(outcome, envelope)
    else:
        err_lines = [ln for ln in err.splitlines() if ln.strip()]
        outcome.status = "MISSING"
        outcome.error = err_lines[-1].strip() if err_lines else f"agy exited {code} without a result"
    outcome.agy_error = _parse_agy_error(err)
    outcome.retryable = bool(outcome.agy_error.get("retryable")) if outcome.agy_error else False
    kind = classify(outcome, err, hard_killed=timed_out)
    if kind == "ok":
        return "ok", f"model {model} replied"
    detail = _first_line(outcome.error) or _first_line(err) or f"agy exited {code}"
    return kind, detail
