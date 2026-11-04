"""Shared data types and constants for agw. Pure: no I/O."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

# ---- CLI exit codes -------------------------------------------------------------------------
EXIT_OK = 0
EXIT_TASKS_FAILED = 1
EXIT_USAGE = 2
EXIT_ENV = 3
EXIT_INTERRUPTED = 130

# ---- enums (as tuples of allowed strings) -----------------------------------------------------
MODES = ("read", "edit")
TASK_STATUSES = ("ok", "failed", "error", "timeout", "skipped")
REPORT_STATUSES = ("done", "partial", "blocked")      # Report.status may also be "missing"
OUTCOME_KINDS = ("ok", "timeout", "interrupted", "quota", "auth", "model", "error")

# ---- flag codes ----------------------------------------------------------------------------------
OUT_OF_SCOPE_WRITE = "OUT_OF_SCOPE_WRITE"
SUSPICIOUS_COMMAND = "SUSPICIOUS_COMMAND"
READ_MODE_CHANGES = "READ_MODE_CHANGES"
SCOPE_VIOLATION = "SCOPE_VIOLATION"
BLOCKED_ACTIONS = "BLOCKED_ACTIONS"
NO_REPORT = "NO_REPORT"
REPORT_MISMATCH = "REPORT_MISMATCH"
NO_CHANGES = "NO_CHANGES"
LARGE_DIFF = "LARGE_DIFF"
DELETED_FILES = "DELETED_FILES"
MAIN_TREE_CHANGED = "MAIN_TREE_CHANGED"
DIRTY_BASE = "DIRTY_BASE"
OVERLAP = "OVERLAP"
DENY_RULE_DROPPED = "DENY_RULE_DROPPED"
BRANCH_SWITCHED = "BRANCH_SWITCHED"
NO_ACCEPT = "NO_ACCEPT"

WARN_FLAGS = frozenset({
    OUT_OF_SCOPE_WRITE, SUSPICIOUS_COMMAND, READ_MODE_CHANGES, SCOPE_VIOLATION, BLOCKED_ACTIONS,
    NO_REPORT, REPORT_MISMATCH, MAIN_TREE_CHANGED, OVERLAP, BRANCH_SWITCHED,
})
INFO_FLAGS = frozenset({NO_CHANGES, LARGE_DIFF, DELETED_FILES, DIRTY_BASE, DENY_RULE_DROPPED, NO_ACCEPT})

# Build/test caches that are never part of a worker's deliverable: not committed, not in patches, not "changes".
CACHE_EXCLUDES = ["__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/"]

# ---- tunables (constants, not config) --------------------------------------------------------
LARGE_DIFF_LINES = 400
TAIL_LINES = 15
MAX_COMMANDS_SHOWN = 8
COMMAND_TRUNCATE = 72
READ_FINDINGS_WORDS = 400
EDIT_NOTES_WORDS = 120
TRANSIENT_RETRY_DELAY_SECONDS = 20
MAX_TRANSIENT_RETRIES = 1
KILL_GRACE_SECONDS = 60
TERM_WAIT_SECONDS = 10
MAX_RUNS_LISTED = 20
STALE_RUN_HOURS_WINDOWS = 24
SNAPSHOT_BASE_LABEL = "snapshot of uncommitted changes"

READ_TOOLS = frozenset({
    "read_file", "view_file", "list_dir", "find_by_name", "grep_search",
    "read_url_content", "read_resource", "search_web", "command_status",
})
PATH_PARAM_KEYS = frozenset({"path", "targetfile", "target_file", "absolutepath", "file", "filepath", "file_path"})

REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "partial", "blocked"]},
        "summary": {"type": "string"},
        "findings": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "blocked_actions": {"type": "array", "items": {"type": "string"}},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "findings", "files_changed", "blocked_actions", "concerns"],
}


@dataclass(frozen=True)
class Flag:
    code: str
    detail: str

    @property
    def level(self) -> str:
        return "warn" if self.code in WARN_FLAGS else "info"


@dataclass
class Config:
    model: str = "gemini-3.8-flash-high"
    max_parallel: int = 3
    timeout_seconds: int = 1200
    sandbox: bool = False
    retry_limit: int = 2
    accept: list[str] = field(default_factory=list)
    accept_timeout_seconds: int = 600
    setup: list[str] = field(default_factory=list)
    deny_write: list[str] = field(default_factory=lambda: ["~/.ssh", "~/.gnupg", "~/.aws"])
    agy_path: list[str] = field(default_factory=lambda: ["agy"])
    worktree_dir: str = ""
    sources: dict[str, str] = field(default_factory=dict)   # key -> "default" | "<file path>" | "env" | "cli"


@dataclass
class Task:
    id: str
    goal: str
    mode: str = "read"
    title: str = ""
    context: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    accept: list[str] | None = None          # None = not given (edit -> config.accept, read -> [])
    report: str = ""
    model: str | None = None
    timeout_seconds: int | None = None
    attempt: int = 1


@dataclass
class TaskSet:
    tasks: list[Task]
    base: str = "HEAD"
    snapshot_dirty: bool = False
    context: list[str] = field(default_factory=list)


@dataclass
class ToolCall:
    step_index: int
    name: str
    params: dict[str, Any] = field(default_factory=dict)
    output: str | None = None
    exit_code: int | None = None             # run_command only, parsed from output


@dataclass
class AgyOutcome:
    exit_code: int
    status: str = "MISSING"                  # "SUCCESS" | "ERROR" | "MISSING" (no result event)
    kind: str = "error"                      # one of OUTCOME_KINDS, set by agy.classify
    error: str = ""
    finished_reason: str = ""
    response: str = ""
    structured: dict[str, Any] | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    unknown_steps: int = 0
    bad_lines: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    model: str = ""
    conversation_id: str = ""
    agy_error: dict[str, Any] | None = None
    retryable: bool = False
    stderr_tail: str = ""                    # last 20 lines of stderr


@dataclass
class AcceptResult:
    command: str
    exit_code: int                           # -1 = timed out
    tail: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


@dataclass
class Report:
    status: str                              # "done" | "partial" | "blocked" | "missing"
    summary: str = ""
    findings: str = ""
    files_changed: list[str] = field(default_factory=list)
    blocked_actions: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)


@dataclass
class DiffStat:
    files: list[tuple[str, str]] = field(default_factory=list)   # (status letter A/M/D/T, repo-relative path)
    insertions: int = 0
    deletions: int = 0


@dataclass
class TaskResult:
    task_id: str
    mode: str
    title: str
    status: str
    reason: str = ""
    attempt: int = 1
    max_attempts: int = 3
    duration_seconds: float = 0.0
    model: str = ""
    branch: str = ""                         # "" for read tasks and edit tasks without a commit
    base_sha: str = ""
    head_sha: str = ""
    diff: DiffStat | None = None             # None for read tasks / not run
    accept: list[AcceptResult] = field(default_factory=list)
    commands: list[ToolCall] = field(default_factory=list)   # run_command calls only
    flags: list[Flag] = field(default_factory=list)
    report: Report = field(default_factory=lambda: Report(status="missing"))
    usage: dict[str, int] = field(default_factory=dict)
    task_dir: str = ""
    worker_ran: bool = False                 # True if agy was started for this task


@dataclass
class RunResult:
    run_id: str
    base_sha: str
    base_ref: str
    main_root: str
    started: str                             # ISO 8601, local time, seconds precision
    duration_seconds: float
    tasks: list[TaskResult]
    flags: list[Flag] = field(default_factory=list)
    exit_code: int = 0
    status: str = "done"                     # "running" | "done" | "interrupted" | "abandoned"
    pid: int = 0
    pid_start: str = ""                      # process start-time token, guards against pid reuse
    retry_limit: int = 2


def to_jsonable(obj: Any) -> Any:
    """Convert dataclasses (recursively) into JSON-serializable dicts/lists."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    return obj


def _filtered(cls: type, d: dict[str, Any]) -> dict[str, Any]:
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in d.items() if k in names}


def report_from_dict(d: dict[str, Any]) -> Report:
    return Report(**_filtered(Report, d))


def task_result_from_dict(d: dict[str, Any]) -> TaskResult:
    d = dict(_filtered(TaskResult, d))
    if d.get("diff") is not None:
        diff = d["diff"]
        d["diff"] = DiffStat(files=[(s, p) for s, p in diff.get("files", [])],
                             insertions=diff.get("insertions", 0), deletions=diff.get("deletions", 0))
    d["accept"] = [AcceptResult(**_filtered(AcceptResult, a)) for a in d.get("accept", [])]
    d["commands"] = [ToolCall(**_filtered(ToolCall, c)) for c in d.get("commands", [])]
    d["flags"] = [Flag(**_filtered(Flag, f)) for f in d.get("flags", [])]
    d["report"] = report_from_dict(d.get("report") or {"status": "missing"})
    return TaskResult(**d)


def run_result_from_dict(d: dict[str, Any]) -> RunResult:
    d = dict(_filtered(RunResult, d))
    d["tasks"] = [task_result_from_dict(t) for t in d.get("tasks", [])]
    d["flags"] = [Flag(**_filtered(Flag, f)) for f in d.get("flags", [])]
    return RunResult(**d)
