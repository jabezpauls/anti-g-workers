"""Command line interface: argparse, the subcommands, exit codes and signal handling."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import traceback
from collections.abc import Callable, Iterator
from typing import NoReturn

from agw import __version__, runner
from agw.agy import check_available, live_check, parse_stream
from agw.config import PROJECT_CONFIG_NAME, ConfigError, default_worktree_dir, load_config, render_config, user_config_path
from agw.gitops import GitError, RepoInfo, apply_patch, repo_info
from agw.models import (EXIT_ENV, EXIT_INTERRUPTED, EXIT_OK, EXIT_TASKS_FAILED, EXIT_USAGE, Config, task_result_from_dict,
                        to_jsonable)
from agw.summary import render_events, render_runs_list, render_summary, render_task_detail
from agw.tasks import TaskFileError, check_attempts, load_taskset
from agw.verify import path_within

MIN_GIT = (2, 31)
MIN_PYTHON = (3, 11)
_TASK_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


class CliError(Exception):
    """A failure that ends the command with `agw: error: <message>` on stderr and the given exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# ---- output helpers -----------------------------------------------------------------------------------------------

def _silence_stdout() -> None:
    """After a broken pipe, point stdout at /dev/null so the interpreter does not complain again at exit."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


def _emit(text: str) -> None:
    """Write text to stdout; survives unencodable characters (C locale) and a closed pipe."""
    try:
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            sys.stdout.write(text.encode(encoding, "replace").decode(encoding))
        sys.stdout.flush()
    except BrokenPipeError:
        _silence_stdout()


def _err(text: str) -> None:
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _fail(message: str, code: int) -> CliError:
    return CliError(message, code)


# ---- shared steps ---------------------------------------------------------------------------------------------------

def _repo(require_commits: bool = False) -> RepoInfo:
    try:
        cwd = os.getcwd()
    except OSError as exc:
        raise _fail(f"cannot determine the current directory: {exc.strerror or exc}", EXIT_ENV) from exc
    try:
        repo = repo_info(cwd)
    except GitError as exc:
        if exc.returncode == 127:
            raise _fail(f"git is not available: {exc.stderr.strip()}", EXIT_ENV) from exc
        raise _fail("not a git repository; agw needs git (git init && git commit first)", EXIT_ENV) from exc
    if require_commits and not repo.head_sha:
        raise _fail("repository has no commits yet", EXIT_ENV)
    return repo


def _config(repo: RepoInfo | None, overrides: dict[str, object] | None = None) -> Config:
    try:
        return load_config(repo.main_root if repo else None, overrides)
    except ConfigError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc


def _resolve_run(repo: RepoInfo, run: str) -> str:
    try:
        return runner.resolve_run_id(repo, run)
    except KeyError:
        raise _fail("no runs in this repository" if run == "last" else f"unknown run '{run}'", EXIT_USAGE) from None


def _task_dir(repo: RepoInfo, run_id: str, task: str) -> str:
    run_dir = runner.runs_root(repo) / run_id
    if not _TASK_NAME_RE.fullmatch(task) or not (run_dir / task).is_dir():
        raise _fail(f"unknown task '{task}' in run {run_id}", EXIT_USAGE)
    return str(run_dir / task)


def _task_result(task_dir: str):
    try:
        with open(os.path.join(task_dir, "result.json"), encoding="utf-8") as fh:
            return task_result_from_dict(json.load(fh))
    except (OSError, ValueError, TypeError) as exc:
        raise _fail(f"no usable result.json for task '{os.path.basename(task_dir)}'", EXIT_USAGE) from exc


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


@contextlib.contextmanager
def _cancel_on_signals(cancel: threading.Event) -> Iterator[None]:
    """SIGINT/SIGTERM (and SIGHUP, unless ignored as under nohup) set `cancel` instead of killing agw, so the
    workers are stopped and the worktrees removed before exit. Only possible on the main thread."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(_signum: int, _frame: object) -> None:
        cancel.set()

    saved: dict[int, object] = {}
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous = signal.getsignal(sig)
            if name == "SIGHUP" and previous == signal.SIG_IGN:
                continue
            signal.signal(sig, handler)
            saved[sig] = previous
        except (ValueError, OSError):
            continue
    try:
        yield
    finally:
        for sig, previous in saved.items():
            try:
                signal.signal(sig, previous)  # type: ignore[arg-type]
            except (ValueError, OSError, TypeError):
                pass


# ---- run -----------------------------------------------------------------------------------------------------------

def _load_tasks(source: str):
    try:
        if source == "-":
            stream = sys.stdin
            raw = getattr(stream, "buffer", None)
            try:
                text = raw.read().decode("utf-8") if raw is not None else stream.read()
            except UnicodeDecodeError:
                raise TaskFileError(["stdin: not valid UTF-8"]) from None
            return load_taskset("-", io.StringIO(text))
        return load_taskset(source, io.StringIO(""))
    except TaskFileError as exc:
        raise _fail("\n".join(exc.errors), EXIT_USAGE) from exc


def cmd_run(args: argparse.Namespace) -> int:
    repo = _repo(require_commits=True)
    config = _config(repo, {"model": args.model, "max_parallel": args.parallel})
    taskset = _load_tasks(args.tasks)
    problems = check_attempts(taskset, config.retry_limit)
    if problems:
        raise _fail("\n".join(problems), EXIT_USAGE)
    available, detail = check_available(config.agy_path)
    if not available:
        raise _fail(detail, EXIT_ENV)

    cancel = threading.Event()
    try:
        with _cancel_on_signals(cancel):
            result = runner.execute_run(taskset, config, repo, cancel=cancel,
                                        progress=sys.stderr if args.progress else None)
    except runner.RunError as exc:
        raise _fail(str(exc), exc.exit_code) from exc
    _emit(render_summary(result))
    return result.exit_code


# ---- show ----------------------------------------------------------------------------------------------------------

_SHOW_VIEWS = ("diff", "log", "brief", "events", "report")


def cmd_show(args: argparse.Namespace) -> int:
    view = next((v for v in _SHOW_VIEWS if getattr(args, v)), None)
    repo = _repo()
    if args.run is None:
        if args.task is not None or view:
            raise _fail("a task or view option needs a RUN", EXIT_USAGE)
        _emit(render_runs_list(runner.list_runs(repo)))
        return EXIT_OK
    run_id = _resolve_run(repo, args.run)
    run_dir = runner.runs_root(repo) / run_id
    if args.task is None:
        if view:
            raise _fail(f"--{view} needs a task: agw show {run_id} TASK --{view}", EXIT_USAGE)
        summary = _read_text(str(run_dir / "summary.txt"))
        if summary is None:
            data = runner.load_run(repo, run_id)
            status = str(data.get("status", "unknown"))
            if status == "running" and not runner.is_run_alive(data):
                status = "abandoned"
            summary = f"run {run_id}: {status} (no summary yet)\n"
        _emit(summary)
        return EXIT_OK

    task_dir = _task_dir(repo, run_id, args.task)
    if view is None:
        _emit(render_task_detail(_task_result(task_dir)))
    elif view == "diff":
        _emit(_read_text(os.path.join(task_dir, "diff.patch")) or "(no diff)\n")
    elif view == "log":
        _emit(_read_text(os.path.join(task_dir, "accept.log")) or "(no acceptance log)\n")
    elif view == "brief":
        _emit(_read_text(os.path.join(task_dir, "brief.md")) or "(no brief)\n")
    elif view == "events":
        outcome = parse_stream(os.path.join(task_dir, "events.ndjson"), os.path.join(task_dir, "agy.stderr.txt"), 0)
        _emit(render_events(outcome.tool_calls))
    else:
        _emit(json.dumps(to_jsonable(_task_result(task_dir).report), indent=2, ensure_ascii=False) + "\n")
    return EXIT_OK


# ---- apply ---------------------------------------------------------------------------------------------------------

def cmd_apply(args: argparse.Namespace) -> int:
    repo = _repo()
    run_id = _resolve_run(repo, args.run)
    task_dir = _task_dir(repo, run_id, args.task)
    result = _task_result(task_dir)
    if result.mode != "edit":
        raise _fail(f"task {args.task} is a read task; there is nothing to apply", EXIT_USAGE)
    patch = os.path.join(task_dir, "diff.patch")
    if not os.path.isfile(patch):
        raise _fail(f"task {args.task} has no diff.patch (it produced no changes); nothing to apply", EXIT_USAGE)
    how, stderr = apply_patch(repo.main_root, patch)
    if how == "applied":
        count = len(result.diff.files) if result.diff else 0
        _emit(f"applied {args.task} ({count} files) to working tree; not committed\n")
        return EXIT_OK
    if how == "applied-3way":
        _emit(f"applied {args.task} with 3-way merge; changes are staged; not committed\n")
        return EXIT_OK
    lines = [f"conflict applying {args.task}:"] + [f"  {line}" for line in stderr.strip().splitlines()]
    lines.append("If git left conflict markers or unmerged paths in the working tree, see `git status` "
                 "and resolve them.")
    _err("\n".join(lines))
    return EXIT_TASKS_FAILED


# ---- clean ---------------------------------------------------------------------------------------------------------

def cmd_clean(args: argparse.Namespace) -> int:
    if args.run is not None and args.all:
        raise _fail("RUN and --all cannot be combined", EXIT_USAGE)
    repo = _repo()
    config = _config(repo)
    try:
        message = runner.clean(repo, config, args.run, args.all)
    except KeyError:
        raise _fail("no runs in this repository" if args.run == "last" else f"unknown run '{args.run}'",
                    EXIT_USAGE) from None
    except ValueError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc
    _emit(message + "\n")
    return EXIT_OK


# ---- config --------------------------------------------------------------------------------------------------------

def _repo_or_none() -> RepoInfo | None:
    try:
        return repo_info(os.getcwd())
    except (GitError, OSError):
        return None


def cmd_config(_args: argparse.Namespace) -> int:
    _emit(render_config(_config(_repo_or_none())))
    return EXIT_OK


# ---- doctor --------------------------------------------------------------------------------------------------------

def _doctor_line(level: str, check: str, detail: str) -> str:
    return f"{level:<5}{check}: {detail}"


def _git_version() -> tuple[str, bool]:
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"git not found ({exc})", False
    text = proc.stdout.strip() or proc.stderr.strip()
    match = re.search(r"(\d+)\.(\d+)", text)
    if proc.returncode != 0 or not match:
        return f"cannot determine the git version ({text or 'exit ' + str(proc.returncode)})", False
    version = (int(match.group(1)), int(match.group(2)))
    needed = ".".join(map(str, MIN_GIT))
    return (text, True) if version >= MIN_GIT else (f"{text}; git {needed} or newer is required", False)


def _directory_problem(path: str) -> str:
    """Why `path` cannot serve as a worktree parent directory ("" when it can be used)."""
    if os.path.isdir(path):
        try:
            with tempfile.TemporaryFile(dir=path):
                return ""
        except OSError as exc:
            return f"not writable: {exc.strerror or exc}"
    if os.path.lexists(path):
        return "exists but is not a directory"
    parent = path
    while parent and not os.path.lexists(parent):
        next_parent = os.path.dirname(parent)
        if next_parent == parent:
            break
        parent = next_parent
    if os.path.isdir(parent) and os.access(parent, os.W_OK | os.X_OK):
        return ""
    return f"cannot be created below {parent}"


def cmd_doctor(args: argparse.Namespace) -> int:
    lines: list[str] = []
    failed = False

    def add(level: str, check: str, detail: str) -> None:
        nonlocal failed
        failed = failed or level == "FAIL"
        lines.append(_doctor_line(level, check, detail))

    version = ".".join(map(str, sys.version_info[:3]))
    needed = ".".join(map(str, MIN_PYTHON))
    if sys.version_info >= MIN_PYTHON:
        add("ok", "python", version)
    else:
        add("FAIL", "python", f"{version}; Python {needed} or newer is required")

    detail, good = _git_version()
    add("ok" if good else "FAIL", "git", detail)

    repo = _repo_or_none()
    config: Config | None = None
    config_error = ""
    try:
        config = load_config(repo.main_root if repo else None, None)
    except ConfigError as exc:
        config_error = str(exc)

    if config is not None:
        agy_path = config.agy_path
    else:
        agy_path = [os.environ["AGW_AGY"]] if os.environ.get("AGW_AGY") else ["agy"]
    available, agy_detail = check_available(agy_path)
    if available:
        resolved = [shutil.which(agy_path[0]) or agy_path[0], *agy_path[1:]]
        add("ok", "agy", f"{' '.join(resolved)} ({agy_detail})")
    else:
        add("FAIL", "agy", agy_detail)

    if config is not None:
        files = [str(p) for p in (user_config_path(),
                                  (os.path.join(repo.main_root, PROJECT_CONFIG_NAME) if repo else None))
                 if p is not None and os.path.isfile(p)]
        add("ok", "config", ", ".join(files) if files else "no config files; built-in defaults")
    else:
        add("FAIL", "config", config_error)

    if repo is None:
        add("warn", "repo", "not inside a git repository (doctor runs anywhere, but agw run needs one)")
    elif not repo.head_sha:
        add("warn", "repo", f"{repo.main_root}: repository has no commits yet (agw run needs one)")
    else:
        add("ok", "repo", f"{repo.main_root} ({repo.branch or 'detached HEAD'} @ {repo.head_sha[:7]})")

    worktree_dir = config.worktree_dir if config is not None else default_worktree_dir()
    problem = _directory_problem(worktree_dir)
    add("FAIL" if problem else "ok", "worktree_dir", f"{worktree_dir}: {problem}" if problem else worktree_dir)

    if repo is not None and path_within(worktree_dir, repo.main_root):
        add("warn", "deny", f"worktree_dir {worktree_dir} is inside the repository, so the write guardrail "
                            "for the main tree is dropped; move worktree_dir outside the repo")
    else:
        add("ok", "deny", "worktrees are outside the repository; the write guardrail applies")

    if args.live:
        if config is None:
            add("FAIL", "live", "skipped: the config is invalid")
        elif not available:
            add("FAIL", "live", "skipped: agy is not available")
        else:
            kind, live_detail = live_check(config.agy_path, config.model)
            if kind == "ok":
                add("ok", "live", live_detail)
            else:
                add("FAIL", "live", f"{kind}: {live_detail}")

    _emit("\n".join(lines) + "\n")
    return EXIT_ENV if failed else EXIT_OK


# ---- parser and main -------------------------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"agw: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="agw", description="Hand well-specified tasks to Gemini workers (agy) in git worktrees, "
                                              "then verify and integrate the results.")
    parser.add_argument("--version", action="version", version=f"agw {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("run", help="run the tasks of a tasks JSON file", description="Run tasks in parallel "
                       "worktrees and print a compact summary.")
    p.add_argument("tasks", nargs="?", default="-", metavar="TASKS", help="tasks JSON file, or - for stdin (default)")
    p.add_argument("--model", help="model for tasks that do not set one (overrides the config)")
    p.add_argument("--parallel", type=int, metavar="N", help="maximum parallel workers, 1-8 (overrides the config)")
    p.add_argument("--progress", action="store_true", help="print one line per task start/finish to stderr")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("show", help="list runs, or show a run or a task")
    p.add_argument("run", nargs="?", help="run id, or 'last'")
    p.add_argument("task", nargs="?", help="task id")
    views = p.add_mutually_exclusive_group()
    views.add_argument("--diff", action="store_true", help="print the task's diff.patch")
    views.add_argument("--log", action="store_true", help="print the acceptance log")
    views.add_argument("--brief", action="store_true", help="print the brief the worker received")
    views.add_argument("--events", action="store_true", help="print the condensed tool-call list")
    views.add_argument("--report", action="store_true", help="print the worker report as JSON")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("apply", help="apply an edit task's patch to the working tree (never commits)")
    p.add_argument("run", help="run id, or 'last'")
    p.add_argument("task", help="task id")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("clean", help="remove stale worktrees, or a run's branches and records")
    p.add_argument("run", nargs="?", help="run id (or 'last') to remove completely")
    p.add_argument("--all", action="store_true", help="remove every run that is not running")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("doctor", help="check the environment")
    p.add_argument("--live", action="store_true", help="also make one real, tiny agy call")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("config", help="print the effective configuration")
    p.set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run agw. Returns the process exit code (it never calls sys.exit itself)."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (EXIT_OK if code is None else EXIT_USAGE)
    handler: Callable[[argparse.Namespace], int] = args.func
    try:
        return handler(args)
    except CliError as exc:
        for line in str(exc).splitlines() or [""]:
            _err(f"agw: error: {line}")
        return exc.code
    except KeyboardInterrupt:
        _err("agw: interrupted")
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - last resort: report, do not dump a bare traceback with exit code 1
        _err(f"agw: error: internal error: {type(exc).__name__}: {exc}")
        _err(traceback.format_exc().rstrip())
        return EXIT_ENV
