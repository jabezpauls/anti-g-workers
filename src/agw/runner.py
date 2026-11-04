"""Orchestration: run directories, the per-task pipeline, the worker pool, cancellation, cleanup and run listing."""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import traceback
import typing
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agw.agy import build_argv, build_settings, parse_stream, run_agy
from agw.brief import effective_accept, render_brief
from agw.gitops import (GitError, RepoInfo, add_worktree, commit_all, delete_branch, diff_snapshots, diffstat,
                        dirty_paths, git, list_branches, prune_worktrees, remove_worktree, resolve_base,
                        snapshot_tree, worktree_changes, write_patch)
from agw.models import (DENY_RULE_DROPPED, DIRTY_BASE, EXIT_ENV, EXIT_INTERRUPTED, EXIT_OK, EXIT_TASKS_FAILED,
                        EXIT_USAGE, KILL_GRACE_SECONDS, MAIN_TREE_CHANGED, MAX_RUNS_LISTED, MAX_TRANSIENT_RETRIES,
                        REPORT_SCHEMA, SNAPSHOT_BASE_LABEL, STALE_RUN_HOURS_WINDOWS, TRANSIENT_RETRY_DELAY_SECONDS,
                        AcceptResult, Config, DiffStat, Flag, RunResult, Task, TaskResult, TaskSet,
                        to_jsonable)
from agw.summary import fmt_duration, render_summary
from agw.verify import (decide_status, normalize_report, overlap_flags, path_within, run_acceptance, task_flags)

BRAIN_DIR = os.path.expanduser("~/.gemini/antigravity-cli/brain")

# A run directory without a readable run.json that was touched this recently is a run being created right now.
FRESH_SECONDS = 120
MAX_PATHS_IN_FLAG = 5
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_RUN_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
# Repo-selecting variables from a parent git process (a git hook, say) must not leak into worker, setup or
# acceptance commands: tests that run git would otherwise operate on the wrong repository.
_SCRUBBED_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX")


class RunError(Exception):
    """A run could not start (nothing was run). `exit_code` is the exit code agw should return."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclasses.dataclass
class _Scratch:
    """What run_task must undo afterwards, whatever happens inside the pipeline."""
    worktree_added: bool = False


@dataclasses.dataclass
class RunContext:
    run_id: str
    run_dir: pathlib.Path
    repo: RepoInfo
    config: Config
    taskset: TaskSet
    model: str                       # cli --model override or config.model
    base_sha: str
    worktree_root: str               # <worktree_dir>/<repo_slug>/<run_id> (realpath)
    deny_base: list[str]
    temp_root: str
    brain_dir: str
    schema_path: str
    cancel: threading.Event
    stop_new: threading.Event
    stop_reason: list[str]           # stop_reason[0] is set together with stop_new
    retry_delay: float
    progress: typing.TextIO | None
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)


# ---- small helpers ---------------------------------------------------------------------------------------------

def runs_root(repo: RepoInfo) -> pathlib.Path:
    """Where this repository's run records live: inside the git common dir, so `git status` never sees them."""
    return pathlib.Path(repo.common_dir) / "agw" / "runs"


def new_run_id(now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now()
    return now.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def repo_slug(main_root: str) -> str:
    return f"{os.path.basename(main_root.rstrip(os.sep)) or 'root'}-{hashlib.sha1(main_root.encode()).hexdigest()[:8]}"


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _capped(items: list[str], limit: int = MAX_PATHS_IN_FLAG) -> str:
    shown = ", ".join(items[:limit])
    extra = len(items) - limit
    return f"{shown} +{extra} more" if extra > 0 else shown


def _write_text(path: str | os.PathLike[str], text: str) -> None:
    """Write a file atomically (readers never see a half-written run.json)."""
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json(path: str | os.PathLike[str], data: Any) -> None:
    _write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}


def _counts_text(statuses: list[str]) -> str:
    parts = [f"{statuses.count('ok')} ok"]
    for status in ("failed", "error", "timeout", "skipped"):
        n = statuses.count(status)
        if n:
            parts.append(f"{n} {status}")
    return ", ".join(parts)


# ---- run records -------------------------------------------------------------------------------------------------

def _run_names(repo: RepoInfo) -> list[str]:
    """Names of the run directories, newest first."""
    try:
        entries = [e for e in os.scandir(runs_root(repo)) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    return sorted((e.name for e in entries if _NAME_RE.fullmatch(e.name)), reverse=True)


def resolve_run_id(repo: RepoInfo, run_id: str) -> str:
    """`last` becomes the newest run id; an id must name an existing run. Raises KeyError(run_id) otherwise."""
    names = _run_names(repo)
    if run_id == "last" and names:
        return names[0]
    if run_id in names:
        return run_id
    raise KeyError(run_id)


def load_run(repo: RepoInfo, run_id: str) -> dict:
    """The parsed run.json ({} if unreadable). Raises KeyError(run_id) when there is no such run."""
    if run_id not in _run_names(repo):
        raise KeyError(run_id)
    return _read_json(runs_root(repo) / run_id / "run.json") or {}


def is_run_alive(run_json: dict) -> bool:
    """True if the run claims to be running and its process still exists (Windows: started < 24 h ago)."""
    if run_json.get("status") != "running":
        return False
    if os.name == "posix":
        pid = run_json.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True            # the process exists but belongs to someone else
        except OSError:
            return False
        return True
    try:
        started = datetime.datetime.fromisoformat(str(run_json.get("started")))
    except ValueError:
        return False
    return datetime.datetime.now() - started < datetime.timedelta(hours=STALE_RUN_HOURS_WINDOWS)


def list_runs(repo: RepoInfo) -> list[dict]:
    """The newest MAX_RUNS_LISTED runs: run_id, status, started, n_tasks, counts."""
    rows: list[dict] = []
    for name in _run_names(repo)[:MAX_RUNS_LISTED]:
        run_dir = runs_root(repo) / name
        data = _read_json(run_dir / "run.json")
        tasks_file = _read_json(run_dir / "tasks.json")
        n_tasks = len(tasks_file.get("tasks", [])) if tasks_file else 0
        if data is None:
            status, started, counts = "unknown", "", "-"
        else:
            status = str(data.get("status", "unknown"))
            if status == "running" and not is_run_alive(data):
                status = "abandoned"
            started = str(data.get("started", ""))
            done = [t.get("status", "") for t in data.get("tasks", []) if isinstance(t, dict)]
            counts = _counts_text(done) if done else "-"
        rows.append({"run_id": name, "status": status, "started": started, "n_tasks": n_tasks, "counts": counts})
    return rows


# ---- cleanup -----------------------------------------------------------------------------------------------------

def _run_state(run_dir: pathlib.Path) -> tuple[dict, bool]:
    """(run.json contents or {}, alive?). A run dir with no readable run.json that was touched in the last
    FRESH_SECONDS is a run being created right now, so it counts as alive."""
    data = _read_json(run_dir / "run.json")
    if data is not None:
        return data, is_run_alive(data)
    return {}, _is_fresh(run_dir)


def _is_fresh(path: str | os.PathLike[str]) -> bool:
    try:
        return time.time() - os.stat(path).st_mtime < FRESH_SECONDS
    except OSError:
        return False


def _remove_worktree_root(repo: RepoInfo, root: str) -> None:
    """Remove a run's worktree directory (every task worktree, git's records of them, then the directory)."""
    real = os.path.realpath(root)
    if path_within(repo.main_root, real) or path_within(repo.common_dir, real):
        return                      # never delete the repository or one of its ancestors
    try:
        children = os.listdir(real)
    except OSError:
        children = []
    for child in children:
        remove_worktree(repo, os.path.join(real, child))
    shutil.rmtree(real, ignore_errors=True)


def _worktree_roots(repo: RepoInfo, config: Config) -> dict[str, str]:
    """Candidate run worktree directories {realpath: run id}: the repo's slug dir plus the roots recorded in run.json."""
    found: dict[str, str] = {}
    slug_dir = os.path.join(config.worktree_dir, repo_slug(repo.main_root))
    try:
        entries = os.listdir(slug_dir)
    except OSError:
        entries = []
    for entry in entries:
        path = os.path.join(slug_dir, entry)
        if os.path.isdir(path) and not os.path.islink(path):
            found[os.path.realpath(path)] = entry
    for name in _run_names(repo):
        recorded = (_read_json(runs_root(repo) / name / "run.json") or {}).get("worktree_root")
        if (isinstance(recorded, str) and os.path.isabs(recorded) and os.path.basename(recorded) == name
                and os.path.isdir(recorded) and not os.path.islink(recorded)):
            found[os.path.realpath(recorded)] = name
    return found


def _clean_stale(repo: RepoInfo, config: Config) -> tuple[int, int]:
    """Mark dead runs abandoned and remove the worktree dirs of every run that is not alive: (dirs, abandoned)."""
    abandoned = 0
    alive: set[str] = set()
    for name in _run_names(repo):
        run_dir = runs_root(repo) / name
        data, is_alive = _run_state(run_dir)
        if is_alive:
            alive.add(name)
        elif data.get("status") == "running":
            data["status"] = "abandoned"
            try:
                _write_json(run_dir / "run.json", data)
            except OSError:
                pass
            abandoned += 1
    removed = 0
    for root, name in _worktree_roots(repo, config).items():
        if name in alive or (name not in _run_names(repo) and _is_fresh(root)):
            continue
        _remove_worktree_root(repo, root)
        removed += 1
    prune_worktrees(repo)
    return removed, abandoned


def _remove_run(repo: RepoInfo, config: Config, run_id: str) -> int:
    """Delete one (not alive) run: worktrees, `agw/<run>/*` branches and the run dir. Returns branches deleted."""
    for root, name in _worktree_roots(repo, config).items():
        if name == run_id:
            _remove_worktree_root(repo, root)
    prune_worktrees(repo)
    return _delete_run_branches(repo, run_id, rmtree_dir=runs_root(repo) / run_id)


def _delete_run_branches(repo: RepoInfo, run_id: str, rmtree_dir: pathlib.Path | None = None) -> int:
    branches = list_branches(repo, f"agw/{run_id}/")
    for branch in branches:
        delete_branch(repo, branch)
    remaining = set(list_branches(repo, f"agw/{run_id}/"))
    if rmtree_dir is not None:
        shutil.rmtree(rmtree_dir, ignore_errors=True)
    return len([b for b in branches if b not in remaining])


def clean(repo: RepoInfo, config: Config, run_id: str | None = None, all_runs: bool = False) -> str:
    """`agw clean` (spec section 5). Raises ValueError(message) for a run that is alive, KeyError for an unknown run."""
    if run_id is not None:
        real = resolve_run_id(repo, run_id)
        _data, alive = _run_state(runs_root(repo) / real)
        if alive:
            pid = _data.get("pid")
            raise ValueError(f"run {real} is still running" + (f" (pid {pid})" if pid else ""))
        branches = _remove_run(repo, config, real)
        return f"removed run {real}: {branches} branch(es)"
    if not all_runs:
        dirs, abandoned = _clean_stale(repo, config)
        return f"cleaned: {dirs} worktree dir(s), {abandoned} abandoned run(s)"
    _clean_stale(repo, config)
    runs = branches = 0
    for name in _run_names(repo):
        _data, alive = _run_state(runs_root(repo) / name)
        if alive:
            continue
        branches += _remove_run(repo, config, name)
        runs += 1
    # Branches whose run record is already gone (for example after a manual `rm -r`) are leftovers too.
    for branch in list_branches(repo, "agw/"):
        parts = branch.split("/")
        if len(parts) >= 3 and _RUN_ID_RE.fullmatch(parts[1]) and parts[1] not in _run_names(repo):
            delete_branch(repo, branch)
            branches += 1
    return f"removed {runs} run(s), {branches} branch(es)"


# ---- one task -----------------------------------------------------------------------------------------------------

def _say(ctx: RunContext, text: str) -> None:
    if ctx.progress is not None:
        with ctx.lock:
            try:
                ctx.progress.write(text + "\n")
                ctx.progress.flush()
            except (OSError, ValueError):
                pass


def _not_started_reason(ctx: RunContext) -> str:
    if ctx.cancel.is_set():
        return "interrupted"
    if ctx.stop_new.is_set():
        return ctx.stop_reason[0] if ctx.stop_reason else "stopped"
    return ""


def _drop_untouched_branch(ctx: RunContext, branch: str) -> None:
    """Delete a task branch that still points at the base commit (nothing was committed on it).

    A branch that holds real commits is never deleted here."""
    if not branch:
        return
    proc = git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], ctx.repo.main_root, check=False)
    if proc.returncode == 0 and proc.stdout.strip() == ctx.base_sha:
        delete_branch(ctx.repo, branch)


def _run_setup(ctx: RunContext, task: Task, wt: str, env: dict[str, str], task_dir: pathlib.Path) -> tuple[str, bool]:
    """Run the setup commands one by one. Returns (failure text or "", interrupted?)."""
    log_path = task_dir / "setup.log"
    pieces: list[str] = []
    failure = ""
    interrupted = False
    for index, command in enumerate(ctx.config.setup):
        tmp_log = str(task_dir / f".setup-{index}.log")
        results = run_acceptance([command], wt, ctx.config.accept_timeout_seconds, tmp_log, env, cancel=ctx.cancel)
        try:
            with open(tmp_log, encoding="utf-8", errors="replace") as fh:
                pieces.append(fh.read())
            os.unlink(tmp_log)
        except OSError:
            pass
        if not results:                                  # cancelled before this command started
            interrupted = True
            break
        code = results[0].exit_code
        if code != 0:
            interrupted = ctx.cancel.is_set()
            failure = f"`{command}` exit {code}"
            break
    if pieces:
        _write_text(log_path, "".join(pieces))
    return failure, interrupted


def _commit_message(ctx: RunContext, task: Task) -> str:
    subject = " ".join((task.title or task.goal[:60]).split())
    return f"agw {ctx.run_id}/{task.id}: {subject}"


def _collect_edit_changes(ctx: RunContext, task: Task, res: TaskResult, wt: str, branch: str,
                          baseline: set[str]) -> list[str]:
    """Commit the worker's changes on the task branch and write diff.patch. Returns the changed paths."""
    head = commit_all(wt, _commit_message(ctx, task), baseline)
    if head is None:
        # The worker may have committed by itself despite the brief; do not lose that work.
        current = git(["rev-parse", "HEAD"], wt).stdout.strip()
        if current != ctx.base_sha:
            head = current
    diff = diffstat(ctx.repo.main_root, ctx.base_sha, head) if head else DiffStat()
    if head and diff.files:
        tip = git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], ctx.repo.main_root, check=False)
        if tip.stdout.strip() != head:      # for example the worker switched branches before committing
            git(["update-ref", f"refs/heads/{branch}", head], ctx.repo.main_root)
        write_patch(ctx.repo.main_root, ctx.base_sha, head, str(pathlib.Path(res.task_dir) / "diff.patch"))
        res.branch, res.head_sha, res.diff = branch, head, diff
        return [path for _letter, path in diff.files]
    if head:                    # committed, but the net change is empty: reset the branch so run_task drops it
        git(["update-ref", f"refs/heads/{branch}", ctx.base_sha], ctx.repo.main_root)
    res.branch, res.head_sha, res.diff = "", "", DiffStat()      # run_task deletes the untouched branch afterwards
    return []


def _execute(ctx: RunContext, task: Task, res: TaskResult, scratch: _Scratch, wt: str,
             branch: str) -> tuple[str, str]:
    """The task pipeline after the start checks. Fills `res`; returns (status, reason)."""
    cfg = ctx.config
    edit = task.mode == "edit"
    task_dir = pathlib.Path(res.task_dir)
    timeout = task.timeout_seconds or cfg.timeout_seconds

    def not_started(reason: str) -> tuple[str, str]:
        return decide_status(mode=task.mode, not_started_reason=reason, setup_failure="", outcome=None,
                             timeout_seconds=timeout, report=res.report, changes=[], accept=[])

    reason = _not_started_reason(ctx)
    if reason:
        return not_started(reason)

    try:
        add_worktree(ctx.repo, wt, ctx.base_sha, branch or None)
    except GitError as exc:
        return "error", f"git: {_first_line(exc.stderr) or str(exc)}"
    except OSError as exc:            # for example worktree_dir is not writable
        return "error", f"cannot create the worktree directory: {exc.strerror or exc} ({exc.filename})"
    scratch.worktree_added = True
    wt = os.path.realpath(wt)
    _say(ctx, f"[agw] {task.id} started")

    env = _child_env()
    env.update({"AGW_MAIN_ROOT": ctx.repo.main_root, "AGW_WORKTREE": wt, "AGW_RUN_ID": ctx.run_id,
                "AGW_TASK_ID": task.id})

    setup_failure, interrupted = _run_setup(ctx, task, wt, env, task_dir)
    if interrupted:
        return "error", "interrupted: setup was stopped"
    reason = _not_started_reason(ctx) if not setup_failure else ""
    if reason:                       # cancelled, or another worker hit quota/auth, while this one was in setup
        return not_started(reason)
    if setup_failure:
        return decide_status(mode=task.mode, not_started_reason="", setup_failure=setup_failure, outcome=None,
                             timeout_seconds=timeout, report=res.report, changes=[], accept=[])

    baseline = set(worktree_changes(wt, set()))
    accept = effective_accept(task, cfg)
    brief = render_brief(task, ctx.taskset.context, wt, accept)
    _write_text(task_dir / "brief.md", brief)

    settings = build_settings(ctx.deny_base + ([wt] if not edit else []))
    _write_text(task_dir / "agy-settings.json", settings)
    argv = build_argv(cfg.agy_path, res.model, ctx.schema_path, settings, timeout, cfg.sandbox)
    events_path, stderr_path = str(task_dir / "events.ndjson"), str(task_dir / "agy.stderr.txt")

    retries = 0
    res.worker_ran = True
    while True:
        exit_code, hard_killed, cancelled = run_agy(
            argv, brief, wt, events_path, stderr_path, timeout + KILL_GRACE_SECONDS, ctx.cancel, env)
        outcome = parse_stream(events_path, stderr_path, exit_code, hard_killed, cancelled)
        if (outcome.retryable and outcome.kind not in ("ok", "timeout", "auth", "interrupted")
                and retries < MAX_TRANSIENT_RETRIES):
            retries += 1
            if ctx.cancel.wait(ctx.retry_delay):
                outcome.kind, outcome.error = "interrupted", "interrupted"
                break
            continue
        break
    res.usage = dict(outcome.usage)
    res.commands = [c for c in outcome.tool_calls if c.name == "run_command"]

    if outcome.kind in ("quota", "auth"):
        with ctx.lock:
            if not ctx.stop_new.is_set():
                ctx.stop_reason[:] = ["quota exhausted" if outcome.kind == "quota" else "auth failure"]
                ctx.stop_new.set()

    report = normalize_report(outcome.structured, outcome.response)
    res.report = report

    changes: list[str]
    if edit:
        changes = _collect_edit_changes(ctx, task, res, wt, branch, baseline)
    else:
        changes = worktree_changes(wt, baseline)
        res.diff = None

    accept_results: list[AcceptResult] | None = None        # None: acceptance did not apply to this task
    if outcome.kind == "ok" and report.status != "blocked" and not (edit and not changes) and accept:
        accept_results = run_acceptance(accept, wt, cfg.accept_timeout_seconds, str(task_dir / "accept.log"), env,
                                        cancel=ctx.cancel)
    res.accept = accept_results or []

    res.flags = task_flags(
        task=task, worktree=wt, worktree_base=cfg.worktree_dir, temp_root=ctx.temp_root, brain_dir=ctx.brain_dir,
        changes=changes, tool_calls=outcome.tool_calls, report=report, diff=res.diff,
        worker_ran_ok=outcome.kind == "ok")

    status, why = decide_status(
        mode=task.mode, not_started_reason="", setup_failure="", outcome=outcome, timeout_seconds=timeout,
        report=report, changes=changes, accept=res.accept)
    if accept_results is not None and ctx.cancel.is_set() and (
            len(accept_results) < len(accept) or accept_results[-1].tail[-1:] == ["(interrupted)"]):
        status, why = "error", "interrupted: acceptance checks were stopped"
    return status, why


def run_task(ctx: RunContext, task: Task) -> TaskResult:
    """Run one task end to end. Never raises for task-level problems; always removes the task's worktree."""
    started = time.monotonic()
    cfg = ctx.config
    task_dir = ctx.run_dir / task.id
    wt = os.path.join(ctx.worktree_root, task.id)
    branch = f"agw/{ctx.run_id}/{task.id}" if task.mode == "edit" else ""
    res = TaskResult(
        task_id=task.id, mode=task.mode, title=task.title, status="error", attempt=task.attempt,
        max_attempts=1 + cfg.retry_limit, model=task.model or ctx.model, base_sha=ctx.base_sha,
        task_dir=str(task_dir))
    scratch = _Scratch()
    try:
        task_dir.mkdir(parents=True, exist_ok=True)
        res.status, res.reason = _execute(ctx, task, res, scratch, wt, branch)
    except Exception as exc:  # noqa: BLE001 - one task must never take the whole run down
        res.status, res.reason = "error", f"internal error: {type(exc).__name__}: {exc}"
        try:
            _write_text(task_dir / "internal-error.txt", traceback.format_exc())
        except OSError:
            pass
    finally:
        # The worktree must go first: git refuses to delete a branch that is checked out.
        if scratch.worktree_added or os.path.lexists(wt):
            try:
                remove_worktree(ctx.repo, wt)
            except Exception:  # noqa: BLE001 - execute_run sweeps the whole worktree root again at the end
                pass
        if branch and not res.branch:          # an edit task that kept no result must not leave a branch behind
            try:
                _drop_untouched_branch(ctx, branch)
            except Exception:  # noqa: BLE001
                pass
    res.duration_seconds = time.monotonic() - started
    _write_json(task_dir / "result.json", to_jsonable(res))
    _say(ctx, f"[agw] {task.id} {res.status} ({fmt_duration(res.duration_seconds)})")
    return res


# ---- the whole run --------------------------------------------------------------------------------------------------

def _exit_code(results: list[TaskResult], flags: list[Flag], cancel: threading.Event) -> int:
    if cancel.is_set():
        return EXIT_INTERRUPTED
    if any(r.reason.startswith("auth:") or r.reason == "not started: auth failure" for r in results):
        return EXIT_ENV
    if any(r.status != "ok" for r in results) or any(f.level == "warn" for f in flags) or any(
            f.level == "warn" for r in results for f in r.flags):
        return EXIT_TASKS_FAILED
    return EXIT_OK


def execute_run(taskset: TaskSet, config: Config, repo: RepoInfo, *, model_override: str | None = None,
                cancel: threading.Event | None = None, progress: typing.TextIO | None = None,
                retry_delay: float = TRANSIENT_RETRY_DELAY_SECONDS) -> RunResult:
    """Run every task of `taskset` in parallel worktrees and return the finished RunResult (spec section 3).

    Raises RunError (nothing was run, no state left behind) when the base cannot be resolved or git is unusable.
    """
    if cancel is None:
        cancel = threading.Event()
    t0 = time.monotonic()
    try:
        clean(repo, config)
    except Exception:  # noqa: BLE001 - stale cleanup is best effort and must never block a run
        pass

    try:
        before = snapshot_tree(repo.main_root)
    except GitError as exc:
        raise RunError(f"cannot inspect the working tree: {_first_line(exc.stderr) or exc}", EXIT_ENV) from exc
    try:
        base_sha, base_ref = resolve_base(repo, taskset.base, taskset.snapshot_dirty)
    except GitError as exc:
        raise RunError(f"base: cannot resolve {taskset.base!r}: {_first_line(exc.stderr) or exc}", EXIT_USAGE) from exc

    root = runs_root(repo)
    run_dir: pathlib.Path | None = None
    run_id = ""
    try:
        root.mkdir(parents=True, exist_ok=True)
        for _attempt in range(20):
            run_id = new_run_id()
            try:
                (root / run_id).mkdir()
            except FileExistsError:
                continue
            run_dir = root / run_id
            break
    except OSError as exc:
        raise RunError(f"cannot create the run directory in {root}: {exc.strerror or exc}", EXIT_ENV) from exc
    if run_dir is None:
        raise RunError(f"cannot create a unique run directory in {root}", EXIT_ENV)

    worktree_root = os.path.join(config.worktree_dir, repo_slug(repo.main_root), run_id)
    started = datetime.datetime.now().isoformat(timespec="seconds")
    run_json: dict[str, Any] = {"run_id": run_id, "pid": os.getpid(), "status": "running", "started": started,
                                "worktree_root": worktree_root}
    results: list[TaskResult] = []
    try:
        _write_json(run_dir / "tasks.json", to_jsonable(taskset))
        _write_json(run_dir / "report.schema.json", REPORT_SCHEMA)
        _write_json(run_dir / "run.json", run_json)

        flags: list[Flag] = []
        dirty = dirty_paths(before)
        if dirty and base_ref != SNAPSHOT_BASE_LABEL:
            flags.append(Flag(DIRTY_BASE, f"{len(dirty)} uncommitted path(s) not visible to workers"))
        deny_base = [repo.main_root]
        if not path_within(repo.common_dir, repo.main_root):
            deny_base.append(repo.common_dir)
        deny_base.extend(config.deny_write)
        kept: list[str] = []
        for entry in dict.fromkeys(deny_base):
            if path_within(worktree_root, entry):
                flags.append(Flag(DENY_RULE_DROPPED, entry))
            else:
                kept.append(entry)

        ctx = RunContext(
            run_id=run_id, run_dir=run_dir, repo=repo, config=config, taskset=taskset,
            model=model_override or config.model, base_sha=base_sha, worktree_root=worktree_root,
            deny_base=kept, temp_root=os.path.realpath(tempfile.gettempdir()), brain_dir=BRAIN_DIR,
            schema_path=str(run_dir / "report.schema.json"), cancel=cancel, stop_new=threading.Event(),
            stop_reason=[], retry_delay=retry_delay, progress=progress)

        with ThreadPoolExecutor(max_workers=config.max_parallel) as pool:
            try:
                futures = [pool.submit(run_task, ctx, task) for task in taskset.tasks]
                results = [future.result() for future in futures]
            except BaseException:
                cancel.set()          # stop the workers before the pool waits for them
                raise

        try:
            after = snapshot_tree(repo.main_root)
        except GitError as exc:
            after = before
            sys.stderr.write(f"agw: warning: could not re-check the main working tree: "
                             f"{_first_line(exc.stderr) or exc}\n")
        changed = diff_snapshots(before, after)
        if changed:
            flags.append(Flag(MAIN_TREE_CHANGED,
                              f"{_capped(changed)} (changed during run; may be you or Claude - check)"))
        flags.extend(overlap_flags(results))

        interrupted = cancel.is_set()
        result = RunResult(
            run_id=run_id, base_sha=base_sha, base_ref=base_ref, main_root=repo.main_root, started=started,
            duration_seconds=time.monotonic() - t0, tasks=results, flags=flags,
            exit_code=_exit_code(results, flags, cancel), status="interrupted" if interrupted else "done",
            pid=os.getpid(), retry_limit=config.retry_limit)
        _write_text(run_dir / "summary.txt", render_summary(result))
        _write_json(run_dir / "run.json", to_jsonable(result))
        return result
    except BaseException:
        run_json["status"] = "abandoned"
        try:
            _write_json(run_dir / "run.json", run_json)
        except OSError:
            pass
        raise
    finally:
        _remove_worktree_root(repo, worktree_root)
        try:
            prune_worktrees(repo)
        except Exception:  # noqa: BLE001
            pass
