# ag-workers: implementation plan

Spec: `docs/superpowers/specs/2026-11-05-ag-workers-design.md`. It is **the source of truth** for every behaviour,
format and message. **Every implementer must read the whole spec before starting**, plus
`docs/research/agy-cli-findings.md`. This plan fixes the order, the file ownership, the exact interfaces, and the tests.

## Ground rules (all tasks)

- Python ≥ 3.11, **standard library only** in `src/` and `bin/`. pytest is the only dev dependency.
- Dev environment (created in T1; later tasks reuse it): `python3 -m venv .venv && .venv/bin/python -m pip install -q 'pytest>=8'`.
  If pip has no network, fall back to `uv venv .venv && uv pip install --python .venv/bin/python 'pytest>=8'`.
- Run tests with `.venv/bin/python -m pytest -q` from the repo root. `pyproject.toml` puts `src` and `tests` on
  `pythonpath`.
- **TDD**: write the listed tests first, watch them fail, then implement until green. Do not delete or weaken a
  listed test. You may add tests.
- Only create or modify the files your task owns (listed per task). If you believe another task's file needs a
  change, do not edit it: write the needed change in your final report.
- No real `agy` in tests (except `tests/test_live_e2e.py`, which is opt-in). Never read or write `~/.config/agw` in
  tests: the `agw_env` fixture sets `AGW_CONFIG`.
- Type hints on all public functions. Every module starts with a one-line docstring stating its purpose.
- All filesystem path comparisons use `os.path.realpath`. Paths stored in results are realpaths.
- Subprocess text I/O: `text=True, encoding="utf-8", errors="replace"`.
- Commit at the end of your task with only your files: `git add <your files> && git commit -m "<task title>"`. If git
  reports `index.lock`, wait 2 s and retry (parallel tasks commit to the same repo). Commit messages end with the
  line `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` if the dispatcher's instructions say so.

## Task graph

```
T1 scaffold + models + fake agy + fixtures + conftest      (sequential, first)
        │
        ├── T2 config.py + tasks.py              ┐
        ├── T3 gitops.py                          │  PARALLEL: disjoint files,
        ├── T4 agy.py                             │  all depend only on T1
        ├── T5 brief.py + verify.py + summary.py  │
        └── T6 plugin packaging, SKILL.md, README ┘
        │
T7 runner.py + cli.py + integration + live test file        (sequential, after T2–T6)
        │
T8 final verification: full suite, plugin validate, live smoke, README accuracy  (sequential, last)
```

| task | files owned | depends on | parallel with |
|---|---|---|---|
| T1 | `pyproject.toml`, `.gitignore`, `bin/agw`, `src/agw/__init__.py`, `src/agw/__main__.py`, `src/agw/models.py`, `tests/conftest.py`, `tests/fake_agy/fake_agy.py`, `tests/fixtures/*`, `tests/test_models.py`, `tests/test_fake_agy.py` | — | none |
| T2 | `src/agw/config.py`, `src/agw/tasks.py`, `tests/test_config.py`, `tests/test_tasks.py` | T1 | T3 T4 T5 T6 |
| T3 | `src/agw/gitops.py`, `tests/test_gitops.py` | T1 | T2 T4 T5 T6 |
| T4 | `src/agw/agy.py`, `tests/test_agy.py` | T1 | T2 T3 T5 T6 |
| T5 | `src/agw/brief.py`, `src/agw/verify.py`, `src/agw/summary.py`, `tests/test_brief.py`, `tests/test_verify.py`, `tests/test_summary.py` | T1 | T2 T3 T4 T6 |
| T6 | `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `skills/delegate/SKILL.md`, `README.md`, `LICENSE` | T1 | T2 T3 T4 T5 |
| T7 | `src/agw/runner.py`, `src/agw/cli.py`, `tests/test_runner.py`, `tests/test_cli_run.py`, `tests/test_live_e2e.py` | T1–T6 | none |
| T8 | fixes anywhere, plus `docs/research/agy-cli-findings.md` (append only) | T7 | none |

---

## T1: Scaffold, shared contracts, fake agy harness (sequential)

**Goal.** Create the package skeleton, the shared data contract (`models.py`, verbatim below), the fake `agy`, the test
fixtures and the shared pytest fixtures that every later task builds on.

### Files

**`pyproject.toml`** (verbatim):

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "ag-workers"
version = "0.1.0"
description = "Delegate well-specified work from Claude Code to Gemini workers via the official Antigravity agy CLI."
readme = "README.md"
requires-python = ">=3.11"
license = { text = "MIT" }
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
agw = "agw.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src", "tests"]
markers = ["live: real agy end-to-end test; runs only with AGW_LIVE=1"]
```

**`.gitignore`**: `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `*.egg-info/`, `build/`, `dist/`.

**`bin/agw`** (verbatim; `chmod +x`):

```python
#!/usr/bin/env python3
"""agw launcher: runs the agw package bundled next to this file (no install needed)."""
import os
import sys

if sys.version_info < (3, 11):
    sys.stderr.write("agw: error: Python 3.11+ is required (found %d.%d)\n" % sys.version_info[:2])
    sys.exit(3)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "src"))
from agw.cli import main  # noqa: E402

sys.exit(main())
```

**`src/agw/__init__.py`**: `"""agw: hand work from Claude Code to Gemini workers via the official agy CLI."""` and
`__version__ = "0.1.0"`.

**`src/agw/__main__.py`**: `from agw.cli import main` and `raise SystemExit(main())`. (`agw.cli` arrives in T7; until
then `python -m agw` fails, and that is expected.)

**`src/agw/models.py`** (verbatim; this is the cross-task contract and **must not be changed by T2–T6**. T7 may add
fields only with a default value, appended at the end):

```python
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

WARN_FLAGS = frozenset({
    OUT_OF_SCOPE_WRITE, SUSPICIOUS_COMMAND, READ_MODE_CHANGES, SCOPE_VIOLATION, BLOCKED_ACTIONS,
    NO_REPORT, REPORT_MISMATCH, MAIN_TREE_CHANGED, OVERLAP,
})
INFO_FLAGS = frozenset({NO_CHANGES, LARGE_DIFF, DELETED_FILES, DIRTY_BASE, DENY_RULE_DROPPED})

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
```

**`tests/fixtures/`**: run these exact commands from the repo root:

```bash
F=tests/fixtures; S=docs/research/samples; mkdir -p $F
sed -e 's#/work/probe/p1_edit/repo#__CWD__#g' $S/edit-existing-file.ndjson > $F/edit-existing-file.ndjson
sed -e 's#/work/probe/p4_deny/repo#__CWD__#g' -e 's#/work/probe/p4_deny/outside#__OUTSIDE__#g' $S/deny-rule-write.ndjson > $F/deny-rule-write.ndjson
sed -e 's#/work/probe/p8/wt#__CWD__#g' -e 's#/work/probe/p8/main#__OUTSIDE__#g' $S/worktree-structured-output.ndjson > $F/worktree-structured-output.ndjson
sed -e 's#/work/probe/p3_timeout/repo#__CWD__#g' $S/print-timeout.ndjson > $F/print-timeout.ndjson
cp $S/print-timeout.stderr.txt $F/print-timeout.stderr.txt
cp $S/invalid-model.ndjson $F/invalid-model.ndjson
cp $S/invalid-model.stderr.txt $F/invalid-model.stderr.txt
sed -e 's#/work/probe/p6_ask/repo#__CWD__#g' $S/ask-question-headless.ndjson > $F/ask-question-headless.ndjson
sed -e 's#/work/probe/p13d/repo#__CWD__#g' -e 's#/work/probe/p13d/outside#__OUTSIDE__#g' $S/sandbox-bypass.ndjson > $F/sandbox-bypass.ndjson
sed -e 's#/work/repo#__CWD__#g' $S/stream.ndjson > $F/write-and-run.ndjson
grep -l '/work/' $F/* && echo "UNSANITIZED PATHS LEFT" || echo "fixtures clean"
```

Then create `tests/fixtures/minimal-success.ndjson` with exactly these 4 lines:

```
{"event":"init","conversation_id":"00000000-0000-4000-8000-000000000000","init":{"model":"gemini-3.8-flash-high","cwd":"__CWD__","tools":["read_file","write_file","edit_file","run_command"],"permission_mode":"always-proceed"}}
{"event":"step_update","step_update":{"conversation_id":"00000000-0000-4000-8000-000000000000","step_index":0,"state":"DONE","step_type":"user_input"}}
{"event":"step_update","step_update":{"conversation_id":"00000000-0000-4000-8000-000000000000","step_index":1,"state":"DONE","step_type":"agent_response","text_delta":"DONE","usage":{"input_tokens":1000,"output_tokens":100,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":1100}}}
{"event":"result","result":{"conversation_id":"00000000-0000-4000-8000-000000000000","status":"SUCCESS","response":"DONE\n","duration_seconds":1.0,"num_turns":1,"usage":{"input_tokens":1000,"output_tokens":100,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":1100}}}
```

**`tests/fake_agy/fake_agy.py`**: implement exactly the behaviour in spec §14 "Fake agy". Additional precision:

- CLI parsing: scan `sys.argv[1:]`. If the first arg is `--version`, print `1.5.0` and exit 0. If it is `models`,
  print the two model lines and exit 0. Otherwise parse the value of `--print-timeout` (forms `120s`, `2m`, `0s` =
  none) and `--output-format`. Every other flag and value is ignored, but logged.
- Read all of stdin as the prompt. `task_id` is from the regex `^Task ID: (\S+)$` (re.M), else `""`.
- Log record: one JSON line `{"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": prompt, "task_id": task_id}`,
  appended to `$FAKE_AGY_LOG` if that is set.
- Scenario lookup: `scen = data["tasks"].get(task_id, data.get("default", {}))`. If `scen` is a list, use the counter
  file `<state>/<task_id or "_default">.count` (an int, starting at 0) to pick item `min(n, len-1)`, then write `n+1`.
  A missing `$FAKE_AGY_SCENARIOS` means scenario `{}`.
- Order of effects: run `actions` → emit the fixture's non-`result` events (placeholders substituted) → emit
  `extra_events` → sleep `sleep` (honouring `--print-timeout`) → write `stderr` → emit the result event → exit with
  `exit_code`.
- `{"write": p}` creates parent dirs. A relative `p` is relative to the cwd. `{"shell": cmd}` uses
  `subprocess.run(cmd, shell=True, cwd=cwd)`.
- The result event is
  `{"event":"result","result":{"conversation_id": <from fixture init or "fake">, "status": result_status, "response": ..., "duration_seconds": 0.1, "num_turns": 1, "usage": ...}}`,
  plus `"error"` if non-empty, `"finished_reason"` if given, and `"structured_output"` (and
  `"json_schema": {"type":"object"}`) unless `structured_output` is JSON `null`. The default `structured_output` is
  `{"status":"done","summary":"fake worker done","findings":"","files_changed":[],"blocked_actions":[],"concerns":[]}`.
- Print-timeout path: emit `{"event":"result","result":{...,"status":"ERROR","error":"print timeout after <T>s with the turn still in progress; the response is partial","finished_reason":"print_timeout"}}`,
  write the 3 lines of `tests/fixtures/print-timeout.stderr.txt` (with `5s` replaced by `<T>s`) to stderr, and exit 3.
- SIGTERM handler (POSIX): emit the interrupted result
  (`status:"ERROR"`, `error:"interrupted"`, `finished_reason:"interrupted"`), write `error: interrupted` to stderr,
  `sys.stdout.flush()`, and `os._exit(1)`.
- All stdout writes are `print(json.dumps(evt), flush=True)`.
- `--output-format json`: run the same scenario (actions, sleep and timeout handling included), but print ONLY the
  result object (the inner `result` dict, without the `{"event":"result",…}` wrapper) as one JSON line, and no
  other events.

**`tests/conftest.py`**: provides these, with exactly these names:

```python
REPO_ROOT: pathlib.Path          # repository root (parents[1] of this file)
FAKE_AGY: pathlib.Path           # tests/fake_agy/fake_agy.py
FIXTURES: pathlib.Path           # tests/fixtures

def toml_dumps(data: dict) -> str
    # serializes str / int / bool / list[str] values as TOML "key = value" lines (json.dumps for strings and lists works for TOML basic strings/arrays)

def run_git(args: list[str], cwd: pathlib.Path) -> str     # subprocess git, check=True, returns stdout

@pytest.fixture
def git_repo(tmp_path) -> pathlib.Path
    # tmp_path/"repo": `git init -q -b main`; local config user.name=Test, user.email=test@example.com,
    # commit.gpgsign=false; files app.py ("def add(a, b):\n    return a + b\n"),
    # tests/test_app.py ("from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"),
    # README.md ("demo\n"); `git add -A`; `git commit -q -m init`. Returns the realpath.

@dataclasses.dataclass
class AgwEnv:
    root: pathlib.Path; repo: pathlib.Path; config_path: pathlib.Path; worktree_dir: pathlib.Path
    outside: pathlib.Path; log_path: pathlib.Path; scenarios_path: pathlib.Path; state_dir: pathlib.Path
    def write_config(self, **overrides) -> None
        # writes config_path from base {"agy_path": [sys.executable, str(FAKE_AGY)],
        # "worktree_dir": str(worktree_dir), "deny_write": []} updated with overrides
    def set_scenarios(self, default: dict | None = None, tasks: dict | None = None) -> None
        # writes {"default": default or {}, "tasks": tasks or {}} to scenarios_path; clears state_dir counters
    def calls(self) -> list[dict]      # parsed lines of log_path ([] if missing)

@pytest.fixture
def agw_env(tmp_path, git_repo, monkeypatch) -> AgwEnv
    # creates outside/, wt/, fake-state/; write_config(); set_scenarios();
    # monkeypatch.setenv: AGW_CONFIG, FAKE_AGY_LOG (tmp/"fake-agy.log.jsonl"), FAKE_AGY_SCENARIOS
    # (tmp/"scenarios.json"), FAKE_AGY_OUTSIDE, FAKE_AGY_STATE; monkeypatch.delenv("AGW_AGY", raising=False);
    # monkeypatch.chdir(git_repo); creates tmp/"systmp" and monkeypatch.setattr(tempfile, "tempdir", str(that)) so
    # the "system temp" agw sees does NOT contain outside/ or wt/ (otherwise temp-dir ignore rules would hide
    # out-of-scope writes in tests). All paths are realpaths.
```

### Tests to write first

`tests/test_models.py`:

1. `to_jsonable(RunResult(...))` with a nested TaskResult (diff, accept, commands, flags, report) survives
   `json.dumps`/`json.loads`, and `run_result_from_dict` gives back an equal object (`==`). DiffStat files come back
   as tuples.
2. `Flag("OVERLAP", "x").level == "warn"`; `Flag("LARGE_DIFF", "x").level == "info"`.
3. `WARN_FLAGS` and `INFO_FLAGS` are disjoint and their union contains all 14 flag codes.
4. `task_result_from_dict` ignores unknown keys.

`tests/test_fake_agy.py` (run the fake through `subprocess.run([sys.executable, str(FAKE_AGY), ...], input=..., cwd=tmp dir, env=...)`):

1. `--version` prints `1.5.0`; `models` prints 2 lines.
2. Default scenario: stdout is NDJSON; the last event is `result` with status SUCCESS and the default
   `structured_output`; exit 0; the log has one record with `task_id` parsed from `Task ID: abc` in stdin.
3. The `edit-existing-file.ndjson` fixture: `__CWD__` is replaced by the cwd in tool paths; the fixture's own result
   is replaced (`response` is the JSON of structured_output).
4. `actions` write a file in the cwd and one under `FAKE_AGY_OUTSIDE`, and both exist afterwards.
5. A per-task list scenario: the first call uses item 0 and the second uses item 1 (exit codes 1 then 0).
6. `sleep: 5` with `--print-timeout 1s`: exit 3 within 4 s; the result has `finished_reason == "print_timeout"`;
   stderr contains `AGY_ERROR: ` with `"reason":"print_timeout"`.
7. `structured_output: null`: the result has no `structured_output` key.
8. (POSIX only, `skipif` on Windows) start with `sleep: 30`, send SIGTERM after 1 s: exit 1, and the last line is
   the interrupted result.
9. `--output-format json` with the default scenario: stdout is exactly one line, a JSON object with
   `status == "SUCCESS"` and no `event` key.

### Done when

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -q 'pytest>=8'
.venv/bin/python -m pytest -q tests/test_models.py tests/test_fake_agy.py   # all pass
python3 -c "import sys; sys.path.insert(0,'src'); import agw.models as m; print(m.REPORT_SCHEMA['required'])"
grep -l '/work/' tests/fixtures/* ; test $? -eq 1
```

---

## T2: config.py + tasks.py (parallel)

**Goal.** Config loading, merging and validation (spec §8), plus tasks-file parsing and validation (spec §6).

### Interfaces

`src/agw/config.py`:

```python
class ConfigError(Exception): ...
CONFIG_KEYS: tuple[str, ...] = ("model", "max_parallel", "timeout_seconds", "sandbox", "retry_limit", "accept",
                                "accept_timeout_seconds", "setup", "deny_write", "agy_path", "worktree_dir")
def user_config_path() -> pathlib.Path
    # $AGW_CONFIG if set; elif os.name == "nt": Path(os.environ["APPDATA"]) / "agw" / "config.toml";
    # elif $XDG_CONFIG_HOME: that / "agw" / "config.toml"; else ~/.config/agw/config.toml
def default_worktree_dir() -> str
    # realpath(os.path.join(tempfile.gettempdir(), f"agw-{user}")), user = getpass.getuser() or "user" on any exception
def load_config(main_root: str | None, overrides: dict[str, object] | None = None) -> Config
    # defaults -> user file -> <main_root>/.agw.toml (if main_root) -> AGW_AGY env -> overrides (None values ignored)
    # validates every layer; normalizes agy_path to list[str]; worktree_dir "" -> default_worktree_dir(),
    # otherwise realpath(expanduser); deny_write entries -> realpath(expanduser(p)); sources filled for every key
def render_config(cfg: Config) -> str
    # one line per key in CONFIG_KEYS order: f"{key} = {toml_value}  # {source}"; strings/lists as json.dumps,
    # bools lower-case, ints plain; ends with "\n"
```

The `ConfigError` message format is `"<file>: <key>: <problem>"`. Problems: `unknown key`, `must be a string`,
`must be an integer`, `must be a boolean` (an int is not a bool and a bool is not an int), `must be a list of strings`,
`must be between <lo> and <hi>`, `must be a string or a list of strings`, `must not be empty`, and
`invalid TOML: <msg>`.

`src/agw/tasks.py`:

```python
class TaskFileError(ValueError):
    def __init__(self, errors: list[str]): ...      # self.errors = errors; str(self) = "\n".join(errors)
def parse_taskset(data: object) -> TaskSet           # raises TaskFileError listing ALL errors
def load_taskset(source: str, stdin: typing.TextIO) -> TaskSet
    # source "-" reads stdin; else reads the file (utf-8). Missing file -> TaskFileError([f"{source}: file not found"]);
    # bad JSON -> TaskFileError([f"{label}: invalid JSON: {e.msg} (line {e.lineno} col {e.colno})"]) where label = "stdin" or source
def check_attempts(taskset: TaskSet, retry_limit: int) -> list[str]
    # returns error strings: f"tasks[{i}].attempt: {a} exceeds 1 + retry_limit ({retry_limit}); take the task over instead of retrying"
```

Error path format: `tasks[<i>].<field>: <message>`, or `<field>:` for top-level keys, or `(root): must be an object`.
Messages: `unknown key`, `required`, `must be a string`, `must be a non-empty string`, `must be a list of non-empty strings`,
`must be "read" or "edit"`, `must match ^[a-z0-9][a-z0-9-]{0,39}$`, `duplicate id "<id>"`, `must be at most 80 characters`,
`must be an integer between 60 and 7200`, `must be an integer >= 1`, `must be a boolean`, `must be 1`,
`must contain 1 to 20 tasks`, and `snapshot_dirty requires base "HEAD"`.

### Tests to write first

`tests/test_config.py` (use `monkeypatch` for env vars and `tmp_path` for files):

1. No files: `load_config(None)` equals the defaults; `worktree_dir == default_worktree_dir()`; `deny_write` is
   expanded to absolute paths; every `sources` value is `"default"`.
2. A user file overrides `model` and `max_parallel`; a project `.agw.toml` overrides `model` again; the sources show
   the respective file paths.
3. Lists are replaced, not merged (user `accept=["a"]`, project `accept=["b"]` gives `["b"]`).
4. `overrides={"model": "m2", "max_parallel": None}`: model is `m2` with source `cli`, and max_parallel is unchanged.
5. `AGW_AGY=/x/agy` gives `agy_path == ["/x/agy"]`, source `env`. `agy_path = "agy"` in a file gives `["agy"]`.
6. Each validation error, one test each, asserting the message contains file, key and problem: an unknown key,
   `max_parallel = 0`, `max_parallel = 9`, `sandbox = 1`, `timeout_seconds = true`, `accept = "x"`,
   `agy_path = []`, `retry_limit = 6`, and invalid TOML.
7. `render_config` prints 11 lines, in `CONFIG_KEYS` order, with `# default` / the path suffixes.
8. `user_config_path()` respects `AGW_CONFIG` and then `XDG_CONFIG_HOME`.

`tests/test_tasks.py`:

1. A minimal valid file (`{"tasks":[{"id":"a","goal":"g"}]}`) gives the defaults: mode read, accept None, attempt 1.
2. The full example from spec §11 SKILL.md parses into the expected fields.
3. Each rule gives exactly its message (one parametrized test, one case per message listed above).
4. Several errors at once are all reported, in document order.
5. `load_taskset("-", io.StringIO(...))` works; invalid JSON reports line and col; a missing file is reported.
6. `"accept": []` is kept as `[]`, not None.
7. `check_attempts` with `retry_limit=2`: attempt 3 is OK, attempt 4 is an error with the exact message.

### Done when

`.venv/bin/python -m pytest -q tests/test_config.py tests/test_tasks.py` passes.

---

## T3: gitops.py (parallel)

**Goal.** Every git operation agw needs, tested against real temporary repos (the `git_repo` fixture from conftest).

### Interfaces

`src/agw/gitops.py`:

```python
class GitError(Exception):
    def __init__(self, args: list[str], returncode: int, stderr: str): ...   # attrs: args_list, returncode, stderr
@dataclasses.dataclass
class RepoInfo:
    main_root: str     # realpath(`git rev-parse --show-toplevel`)
    common_dir: str    # realpath(`git rev-parse --path-format=absolute --git-common-dir`)
    head_sha: str      # `git rev-parse --verify -q HEAD` or "" when there are no commits
    branch: str        # `git symbolic-ref -q --short HEAD` or "" when detached

def git(args: list[str], cwd: str, *, check: bool = True, input: str | None = None,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]
    # runs ["git", *args]; env = os.environ + {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"} + env;
    # when returncode != 0 and ".lock" in stderr: retry up to 5 times sleeping 0.2 * 2**i;
    # check and still failing -> GitError
def repo_info(cwd: str) -> RepoInfo                            # GitError when not inside a repo
def resolve_base(repo: RepoInfo, ref: str, snapshot_dirty: bool) -> tuple[str, str]
    # snapshot_dirty: sha = `git stash create`.strip(); non-empty -> (sha, SNAPSHOT_BASE_LABEL), else fall through
    # sha = `git rev-parse --verify <ref>^{commit}`; label = repo.branch if ref == "HEAD" and repo.branch
    #       else ("detached" if ref == "HEAD" else ref)
def snapshot_tree(root: str) -> dict[str, str]
    # {"__HEAD__": head sha or ""} plus, for each entry of `git status --porcelain=v1 -z --untracked-files=all`
    # (for renames/copies use the new path; skip the extra orig-path field), path -> f"{XY}:{blob}" where blob is
    # from one `git hash-object --stdin-paths` call over the entries that are regular files, else "-"
def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> list[str]
    # sorted paths whose value differs or which exist on one side only; "__HEAD__" is reported as "HEAD"
def dirty_paths(snapshot: dict[str, str]) -> list[str]         # sorted keys except "__HEAD__"
def add_worktree(repo: RepoInfo, path: str, base_sha: str, branch: str | None) -> None
    # os.makedirs(parent); with a module-level threading.Lock:
    #   branch: `git worktree add -q -b <branch> <path> <base_sha>`;  None: `git worktree add -q --detach <path> <base_sha>`
def remove_worktree(repo: RepoInfo, path: str) -> None
    # `git worktree remove --force <path>` (check=False); shutil.rmtree(path, ignore_errors=True); prune_worktrees(repo)
def worktree_changes(wt: str, ignore: set[str]) -> list[str]
    # paths from `git status --porcelain=v1 -z --untracked-files=all` in wt (renames: new path), minus ignore, sorted
def commit_all(wt: str, message: str, ignore: set[str]) -> str | None
    # `git add -A`; if ignore: `git reset -q -- <ignored paths>` (only paths that exist in the index or are staged);
    # `git diff --cached --quiet` exit 0 -> return None;
    # else `git -c user.name=agw -c user.email=agw@localhost -c commit.gpgsign=false commit -q --no-verify -m <message>`
    # and return `git rev-parse HEAD`
def diffstat(repo_dir: str, base: str, head: str) -> DiffStat
    # `git diff --no-renames -z --name-status base head` -> files [(letter, path)] in git order;
    # `git diff --no-renames -z --numstat base head` -> sum insertions/deletions ("-" for binary counts as 0)
def write_patch(repo_dir: str, base: str, head: str, out_path: str) -> None
    # `git diff --binary --no-renames base head` written to out_path (utf-8, errors=surrogateescape not needed: write bytes)
def apply_patch(root: str, patch_path: str) -> tuple[str, str]
    # spec §5 agw apply steps 1-3; returns ("applied" | "applied-3way" | "conflict", stderr text)
def list_branches(repo: RepoInfo, prefix: str = "agw/") -> list[str]
    # `git for-each-ref --format=%(refname:short) refs/heads/<prefix>` (sorted)
def delete_branch(repo: RepoInfo, branch: str) -> None         # `git branch -D <branch>`, check=False
def prune_worktrees(repo: RepoInfo) -> None                    # `git worktree prune`, check=False
```

`write_patch` must write bytes (`subprocess.run(..., capture_output=True)` without text) so binary patches survive.
`git()` stays text-based.

### Tests to write first

`tests/test_gitops.py` (all against `git_repo` and `tmp_path`):

1. `repo_info` from the repo root and from a subdirectory gives the same realpaths; `branch == "main"`; `head_sha` is
   40 hex. Outside a repo (a fresh `tmp_path` dir) it raises `GitError`.
2. On a repo with no commits, `repo_info(...).head_sha == ""`.
3. `resolve_base(repo, "HEAD", False) == (head, "main")`. On a detached HEAD the label is `detached`.
   `resolve_base(repo, "<sha>", False)` has the label `<sha>`. An unknown ref raises GitError.
4. `snapshot_dirty=True` with a modified tracked file: a sha different from HEAD, labelled `SNAPSHOT_BASE_LABEL`,
   and the working tree is unchanged afterwards (`git status` is the same as before). With a clean tree it falls back
   to HEAD.
5. `snapshot_tree` and `diff_snapshots`: modifying an **already modified** file again is detected; a new untracked
   file is detected; a commit is detected as `HEAD`; no change gives `[]`.
6. `add_worktree` with a branch creates the dir and the branch `agw/r/t`; detached mode creates no branch.
   `remove_worktree` deletes the dir, and `git worktree list` no longer shows it. Calling `remove_worktree` twice
   does not raise.
7. `worktree_changes` lists modified, added and deleted paths, and respects `ignore`.
8. `commit_all` in an edit worktree: it returns a sha, the branch points to it, the commit author is `agw`, and
   ignored paths are not committed. With no changes it returns None.
9. `diffstat` and `write_patch`: one modified, one added and one deleted file give the correct letters and counts.
   The patch applies cleanly to a fresh checkout of base (`git apply --check`).
10. `apply_patch`: a clean apply gives `"applied"` and the changes show up unstaged in the main tree. When the main
    tree has a conflicting edit on the same line it gives `"conflict"`. When the main tree has a non-overlapping edit
    in the same file it gives `"applied"` or `"applied-3way"` (either is accepted) and both edits are present.
11. `list_branches` and `delete_branch` behave as specified.
12. `git()` lock retry: monkeypatch `subprocess.run` so the first 2 calls return rc=128 with stderr containing
    `index.lock` and the 3rd succeeds; `git()` returns success after 3 calls.

### Done when

`.venv/bin/python -m pytest -q tests/test_gitops.py` passes.

---

## T4: agy.py (parallel)

**Goal.** Building the agy invocation, running it with timeout and cancellation, parsing NDJSON, and classifying
errors (spec §5 doctor `live`, §13 `classify`, findings doc).

### Interfaces

`src/agw/agy.py`:

```python
def build_settings(deny_paths: list[str]) -> str
    # json.dumps({"permissions": {"deny": [f"write_file({p})" for p in deny_paths]}}, separators=(",", ":"))
def build_argv(agy_path: list[str], model: str, schema_path: str, settings_json: str,
               timeout_seconds: int, sandbox: bool) -> list[str]
    # [*agy_path, "--model", model, "--output-format", "stream-json", "--json-schema", schema_path,
    #  "--settings", settings_json, "--disable-slash-commands", "--dangerously-skip-permissions",
    #  "--print-timeout", f"{timeout_seconds}s"] + (["--sandbox"] if sandbox else [])
def run_agy(argv: list[str], brief: str, cwd: str, events_path: str, stderr_path: str,
            hard_timeout_seconds: float, cancel: threading.Event,
            env: dict[str, str] | None = None) -> tuple[int, bool, bool]
    # returns (exit_code, hard_killed, cancelled). Popen with stdin=PIPE, stdout -> events file, stderr -> stderr file
    # (both opened "w", utf-8). Write the brief to stdin and close it (ignore BrokenPipeError). Poll every 0.2 s.
    # On cancel or elapsed > hard_timeout: proc.terminate(); wait TERM_WAIT_SECONDS; then proc.kill() if still alive.
    # FileNotFoundError on Popen -> return (127, False, False) and write "agw: cannot execute <argv[0]>" to stderr_path.
def parse_stream(events_path: str, stderr_path: str, exit_code: int,
                 hard_killed: bool = False, cancelled: bool = False) -> AgyOutcome
    # reads NDJSON: init -> model, conversation_id; step_update with step_type "tool" -> ToolCall per step_index
    # (a later event for the same step_index replaces the earlier one; ordered by step_index);
    # run_command exit_code from output regex r"Command exited with code (-?\d+)"; step_type "unknown" counted
    # once per distinct step_index in unknown_steps; result -> status, error, finished_reason, response,
    # structured_output (only if dict), usage (ints), duration_seconds. Non-JSON lines counted in bad_lines.
    # stderr: stderr_tail = last 20 lines; agy_error = json of the last line starting "AGY_ERROR: " (None on
    # failure); retryable = bool(agy_error.get("retryable")) if agy_error else False.
    # Missing result: status "MISSING", error = last non-empty stderr line or f"agy exited {exit_code} without a result".
    # Finally outcome.kind = classify(outcome, stderr_text, hard_killed, cancelled).
def classify(outcome: AgyOutcome, stderr_text: str, hard_killed: bool = False, cancelled: bool = False) -> str
    # exactly spec §13 "agy.classify", in order
def check_available(agy_path: list[str]) -> tuple[bool, str]
    # found if shutil.which(agy_path[0]) or os.path.isfile(agy_path[0]); then run [*agy_path, "--version"] (timeout 30 s)
    # -> (True, first stdout line) ; not found -> (False, f"agy not found (agy_path={agy_path!r}). Install the Antigravity CLI and run 'agy' once to log in.")
    # version command failing -> (False, f"'{' '.join(agy_path)} --version' failed: <stderr first line>")
def live_check(agy_path: list[str], model: str, timeout_seconds: int = 120) -> tuple[str, str]
    # in a fresh tempfile.TemporaryDirectory: run [*agy_path, "--model", model, "--output-format", "json",
    # "--disable-slash-commands", "--print-timeout", f"{timeout_seconds}s"] with stdin "Reply with exactly: PONG",
    # subprocess timeout timeout_seconds + 30. Parse stdout JSON envelope into an AgyOutcome
    # (status/error/finished_reason/response), classify it with the stderr text.
    # Returns (kind, detail): ("ok", f"model {model} replied") or (kind, first line of error/stderr).
```

### Tests to write first

`tests/test_agy.py`:

1. `build_settings(["/a", "/b c"])` gives exactly `{"permissions":{"deny":["write_file(/a)","write_file(/b c)"]}}`.
2. `build_argv` with and without sandbox gives the exact lists.
3. `parse_stream` on each fixture. Copy the fixture into `tmp_path`, replacing `__CWD__` with `/w` and `__OUTSIDE__`
   with `/o`, and write the stderr file (empty, or the `.stderr.txt` fixture):
   - `edit-existing-file.ndjson`: tool calls `[read_file, edit_file, write_file, run_command, run_command]` with step
     indexes `[2, 4, 6, 8, 10]`, run_command exit codes `[1, 0]`; status SUCCESS; kind ok (exit 0); usage
     `input_tokens == 52339`.
   - `deny-rule-write.ndjson`: one run_command (step 3) with exit code 1; no write_file tool call.
   - `worktree-structured-output.ndjson`: structured has status `partial` and one blocked action.
   - `print-timeout.ndjson` with its stderr and exit 3: kind timeout, `agy_error["reason"] == "print_timeout"`,
     `retryable False`, finished_reason `print_timeout`.
   - `invalid-model.ndjson` with exit 1: status ERROR, kind model.
   - `ask-question-headless.ndjson`: `unknown_steps == 1`.
   - an empty events file with exit 1 and stderr `boom\n`: status MISSING, error `boom`, kind error.
   - a file with one garbage line plus the minimal fixture: `bad_lines == 1`, still parsed.
4. `classify` table (parametrized), one case per rule: SUCCESS/0 → ok; hard_killed → timeout; exit 3 → timeout;
   error `interrupted` → interrupted; cancelled → interrupted; `AGY_ERROR` status `RESOURCE_EXHAUSTED` → quota; text
   `Quota exceeded for model` → quota; `429 Too Many Requests` → quota; `not logged in` → auth; `Your account is
   blocked due to a Terms of Service violation.` → auth; `invalid model selection` → model; anything else → error.
5. `run_agy` with the fake agy (`[sys.executable, str(FAKE_AGY)]`, env with `FAKE_AGY_SCENARIOS`):
   - normal: exit 0, the events file has a result line, and the fake log's `stdin` equals the brief.
   - `sleep: 30`, `hard_timeout_seconds=1`: returns `hard_killed=True` within 15 s, and the process is gone.
   - cancel event set after 0.5 s with `sleep: 30`: `cancelled=True`; exit code is 1 on POSIX (the fake handles
     SIGTERM).
   - nonexistent executable: `(127, False, False)`.
6. `check_available` with the fake: `(True, "1.5.0")`; with `["/nonexistent/agy"]`: False plus the install hint.
7. `live_check` with the fake agy (T1's fake supports `--output-format json`): the default scenario gives
   `("ok", ...)`; scenario `{"result_status": "ERROR", "error": "not logged in", "exit_code": 1}` gives a kind
   of `"auth"`.

### Done when

`.venv/bin/python -m pytest -q tests/test_agy.py` passes.

---

## T5: brief.py + verify.py + summary.py (parallel)

**Goal.** The pure logic: brief rendering (spec §10), flags, scope, report normalization and status decision (spec
§7, §9), acceptance execution, and all text rendering (spec §5 `show`, §9).

### Interfaces

`src/agw/brief.py`:

```python
def effective_accept(task: Task, config: Config) -> list[str]
    # task.accept if not None; else config.accept if task.mode == "edit"; else []
def render_brief(task: Task, shared_context: list[str], worktree: str, accept: list[str]) -> str   # spec §10 exactly
```

`src/agw/verify.py`:

```python
def run_acceptance(commands: list[str], cwd: str, timeout_seconds: int, log_path: str,
                   env: dict[str, str]) -> list[AcceptResult]
    # runs ALL commands in order: subprocess.run(cmd, shell=True, cwd=cwd, env=env, stdout=PIPE, stderr=STDOUT,
    # text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds). The log file gets, per command:
    # f"$ {cmd}\n{output}" + ("" if output.endswith("\n") or not output else "\n") + f"[exit {code}] ({dur:.1f}s)\n\n"
    # On timeout: exit_code -1, output = partial output (exc.output, decoded if bytes) + f"\n(timed out after {timeout_seconds}s)".
    # tail = last TAIL_LINES lines of output.rstrip("\n").splitlines()
def normalize_rel(path: str, worktree: str) -> str
    # absolute paths inside worktree -> relative; "\\" -> "/"; strip a leading "./"; other absolute paths unchanged
def normalize_report(structured: object, response: str) -> Report        # spec §7
def path_within(path: str, root: str) -> bool
    # realpath both (os.path.normcase on Windows); True if equal, or root is a prefix by path components
def matches_scope(path: str, globs: list[str], worktree: str | None = None) -> bool   # spec §6 scope semantics
def is_suspicious_command(cmd: str, worktree: str, temp_root: str) -> bool          # spec §9
def path_params(call: ToolCall) -> list[str]
    # values (str only) of params whose key.lower() is in PATH_PARAM_KEYS
def task_flags(*, task: Task, worktree: str, worktree_base: str, temp_root: str, brain_dir: str,
               changes: list[str], tool_calls: list[ToolCall], report: Report,
               diff: DiffStat | None, worker_ran_ok: bool) -> list[Flag]
    # spec §9 flags table; output order: OUT_OF_SCOPE_WRITE, SUSPICIOUS_COMMAND, READ_MODE_CHANGES,
    # SCOPE_VIOLATION, BLOCKED_ACTIONS, NO_REPORT, REPORT_MISMATCH, NO_CHANGES, LARGE_DIFF, DELETED_FILES.
    # `changes` = repo-relative changed paths (edit: from diff; read: worktree_changes).
    # NO_REPORT and NO_CHANGES require worker_ran_ok. Lists in details are joined with ", ";
    # truncation adds f" +{k} more".
def overlap_flags(results: list[TaskResult]) -> list[Flag]
    # one Flag(OVERLAP, detail) if any path is changed by >= 2 edit results with diff;
    # detail = "; ".join(f"{path} ({', '.join(ids)})" for up to 5 paths, sorted by path)
def decide_status(*, mode: str, not_started_reason: str, setup_failure: str, outcome: AgyOutcome | None,
                  timeout_seconds: int, report: Report, changes: list[str],
                  accept: list[AcceptResult]) -> tuple[str, str]
    # spec §9 status decision, in order. not_started_reason is e.g. "quota exhausted" -> ("skipped", "not started: quota exhausted");
    # setup_failure is e.g. "`npm ci` exit 1" -> ("error", "setup failed: `npm ci` exit 1")
```

`src/agw/summary.py`:

```python
def fmt_duration(seconds: float) -> str          # spec §9: 45s, 2m05s, 1h03m (round to nearest second first)
def render_summary(run: RunResult) -> str         # spec §9 exactly; ends with "\n"
def render_task_detail(tr: TaskResult) -> str
def render_runs_list(runs: list[dict]) -> str
    # runs: dicts with keys run_id, status, started, counts (e.g. "2 ok, 1 failed"), n_tasks;
    # line: f"{run_id}  {status:<11} {started}  {n_tasks} task(s): {counts}"; no runs -> "no runs\n"
def render_events(tool_calls: list[ToolCall]) -> str
    # one line per call: f"{step_index:>4} {name} {detail}" where detail = CommandLine + f" [{exit_code}]" for
    # run_command (exit "?" if None), else the first path param, else json.dumps(params)[:120]; none -> "(no tool calls)\n"
```

`render_task_detail` format (exact lines; `-` for empty values):

```
task <id> (<mode>) status <status>[: <reason>]
title: <title or ->
attempt <a>/<max> | model <model> | <duration> | gemini <in>k in / <out>k out
branch: <branch or -> | base <sha7 or -> | head <sha7 or ->
diff: <same text as the summary diff line, without the branch part>
  <letter> <path>                       (one line per file)
accept:
  $ <command> [exit <code>] (<dur>s)    (one line per command; "accept: none" on one line if empty)
flags:
  <CODE>: <detail>                      ("flags: none" on one line if empty)
commands (<n>):
  [<exit>] <full command line>
report:
  <json.dumps(to_jsonable(report), indent=2) with every line indented 2 spaces>
files: <task_dir>
```

### Tests to write first

`tests/test_brief.py`:

1. A golden test: an edit task with every field set, shared context of 1 paragraph, and accept of 2 commands. It
   renders **exactly** the expected string. The expected string is written by hand in the test from spec §10 (the
   spec text is authoritative; the test author derives the expected text from it, not from the implementation).
2. A golden test: a read task with only id and goal. It has no Context, Read-these-files, Steps, Constraints or
   Acceptance sections, and has the READ-ONLY scope text and the read findings text.
3. Edit without scope uses the minimal-change scope text. `task.report` appends ` Cover: …`.
4. `effective_accept`: all 4 combinations (edit/read × accept None/given), plus `[]` given.
5. The rendered brief matches `^Task ID: (\S+)$` (re.M) with the id, ends with exactly one `\n`, and has no
   `⟨` characters.

`tests/test_verify.py`:

1. `run_acceptance`: `["exit 0", "echo hi; exit 3"]` gives exit codes `[0, 3]`, tail `["hi"]`, and the log contains
   both `$` lines. A timeout case (`sleep 5` with timeout 1) gives `-1` and `(timed out after 1s)`. (POSIX shell
   syntax; `skipif` on Windows.)
2. `normalize_report`: a valid dict; a non-dict; a bad status; non-list fields coerced; None with a response →
   `missing`, with findings = the response.
3. `normalize_rel` cases: absolute-inside, `./x`, a Windows backslash, absolute-outside.
4. `path_within`: equal, child, sibling with a shared string prefix (`/a/main` vs `/a/main2` → False), parent.
5. `matches_scope`: `src/` matches `src/a/b.py`; `src/**/*.py` matches `src/a/b.py` and `src/b.py`; `*.md` matches
   `README.md`; an exact path; a non-match. An existing directory without a trailing slash matches its children when
   `worktree` is given.
6. `is_suspicious_command` (parametrized):
   - True: `echo x > /etc/passwd`, `rm -rf ~/foo`, `cp a ../../outside.txt`, `git push origin main`,
     `git -C /other commit -m x`, `sed -i s/a/b/ /home/u/.bashrc`.
   - False: `pytest -q`, `ls -la /usr/lib`, `cat /etc/hosts`, `python3 a.py 2>/dev/null`, `echo hi > out.txt`,
     `rm build/x.o`, `cp a.txt <temp_root>/x`.
7. `task_flags`, one test per flag code (all 10 task-level codes), with minimal inputs; plus "no flags" for a clean
   edit; plus the read-mode case where a tool wrote to `<worktree_base>/other-task/x`, which IS flagged even though it
   is under temp. A write under `brain_dir` is not flagged; a write under temp outside worktree_base is not flagged.
8. `overlap_flags`: two edit results sharing `a.py` give one OVERLAP flag with detail `a.py (t1, t2)`; read results
   are ignored.
9. `decide_status` table: one case per rule 1–10 of spec §9, in order, plus precedence (timeout beats acceptance
   failure; blocked beats no-changes).

`tests/test_summary.py`:

1. `fmt_duration`: 0→`0s`, 45→`45s`, 59.6→`1m00s`, 125→`2m05s`, 3780→`1h03m`.
2. A golden test: the exact example in spec §9 (build the RunResult in the test to produce precisely that text; the
   expected string is copied from the spec).
3. A read task with multi-line findings: the `findings:` block is indented by 4 spaces. Edit notes collapse newlines.
4. More than 8 commands gives ` (+k more)`; a 100-char command is truncated to 72 chars ending in `...`; an unknown
   exit code shows `[?]`.
5. A skipped task shows the `error: not started: quota exhausted` line and no diff/accept/report lines.
6. The header counts omit zero categories; token sums round to the nearest k.
7. `render_task_detail` contains every listed section; `render_runs_list([])` gives `no runs\n`; `render_events`
   formats a run_command with its exit code and a write_file with its path.

### Done when

`.venv/bin/python -m pytest -q tests/test_brief.py tests/test_verify.py tests/test_summary.py` passes.

---

## T6: Plugin packaging, SKILL.md, README, LICENSE (parallel)

**Goal.** The Claude Code plugin and marketplace files, the skill, and the public README.

### Files

- `.claude-plugin/plugin.json`: **verbatim** from spec §16.
- `.claude-plugin/marketplace.json`: **verbatim** from spec §16.
- `skills/delegate/SKILL.md`: **verbatim** from spec §11 (everything between the outer ```` fences, without those
  fences).
- `LICENSE`: the standard MIT license text, `Copyright (c) 2026 jabezpauls`.
- `README.md`: follow the outline in spec §17, section by section, in that order, with these exact headings:
  `# ag-workers`, `## Account safety`, `## Safety model`, `## Requirements`, `## Install`, `## Usage`,
  `## How it works`, `## The agw CLI`, `## Tasks file`, `## Configuration`, `## Cost notes`, `## Troubleshooting`,
  `## Development`, `## License`. Content rules:
  - Copy facts only from the spec and `docs/research/agy-cli-findings.md`. Do not invent flags, commands or numbers.
  - The Account safety section must contain the sentence: "ag-workers only drives the official `agy` CLI. Never use
    OAuth proxies or token-reuse tools with your Google account: Google treats that as a Terms of Service violation
    and bans accounts for it."
  - The Safety model section must state, as bullet points: workers run with `--dangerously-skip-permissions`; a git
    worktree is not a security boundary; `agy --sandbox` does not confine writes (so it is off by default); the
    deny-rule guardrail covers file tools only; shell commands can write anywhere your user can; detection is
    best-effort; commit or stash your work first.
  - Install: the commands from spec §17 item 5, plus a note that `OWNER` must be replaced with the GitHub account
    that hosts the repo.
  - The CLI section has a table of the 6 commands and the exit-code table from spec §5.
  - Configuration: the full table from spec §8 and both TOML examples.
  - Development: `python3 -m venv .venv && .venv/bin/python -m pip install 'pytest>=8'`,
    `.venv/bin/python -m pytest -q`, `AGW_LIVE=1 .venv/bin/python -m pytest -q -m live tests/test_live_e2e.py`,
    `claude plugin validate .`, `claude --plugin-dir . plugin details ag-workers`.

### Tests

None automated (T8 checks README accuracy). Validation commands:

### Done when

```bash
claude plugin validate .                              # ✔ Validation passed
claude plugin validate .claude-plugin/plugin.json     # ✔ Validation passed
claude plugin validate --strict .                     # exit 0
claude --plugin-dir . plugin details ag-workers       # shows "Skills (1)  delegate"
grep -c "disable-model-invocation: true" skills/delegate/SKILL.md   # 1
```

---

## T7: runner.py + cli.py + integration tests (sequential, after T2–T6)

**Goal.** Wire everything together exactly per spec §3, §5, §9, §13, and prove it end to end with the fake agy.

### Interfaces

`src/agw/runner.py`:

```python
BRAIN_DIR = os.path.expanduser("~/.gemini/antigravity-cli/brain")
def runs_root(repo: RepoInfo) -> pathlib.Path            # Path(repo.common_dir) / "agw" / "runs"
def new_run_id(now: datetime.datetime | None = None) -> str   # now.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
def repo_slug(main_root: str) -> str                      # f"{basename}-{hashlib.sha1(main_root.encode()).hexdigest()[:8]}"
@dataclasses.dataclass
class RunContext:
    run_id: str; run_dir: pathlib.Path; repo: RepoInfo; config: Config; taskset: TaskSet
    model: str                  # cli --model override or config.model
    base_sha: str; worktree_root: str          # <worktree_dir>/<repo_slug>/<run_id> (realpath)
    deny_base: list[str]; temp_root: str; brain_dir: str; schema_path: str
    cancel: threading.Event; stop_new: threading.Event; stop_reason: list[str]   # stop_reason[0] when set
    retry_delay: float; progress: typing.TextIO | None
def execute_run(taskset: TaskSet, config: Config, repo: RepoInfo, *, model_override: str | None = None,
                cancel: threading.Event | None = None, progress: typing.TextIO | None = None,
                retry_delay: float = TRANSIENT_RETRY_DELAY_SECONDS) -> RunResult
def run_task(ctx: RunContext, task: Task) -> TaskResult
def resolve_run_id(repo: RepoInfo, run_id: str) -> str   # "last" -> newest; unknown -> raises KeyError(run_id)
def load_run(repo: RepoInfo, run_id: str) -> dict        # parsed run.json
def list_runs(repo: RepoInfo) -> list[dict]
    # newest first (sorted by run_id desc), max MAX_RUNS_LISTED; each dict: run_id, status (run.json, with
    # "running" shown as "abandoned" if not is_run_alive), started, n_tasks (len of tasks.json "tasks"),
    # counts ("<k> ok, <k> failed…" from run.json tasks in the summary-header style, or "-" if no tasks yet)
def is_run_alive(run_json: dict) -> bool
def clean(repo: RepoInfo, config: Config, run_id: str | None = None, all_runs: bool = False) -> str
    # spec §5 agw clean; raises ValueError(message) when run_id refers to an alive run
```

**`execute_run` steps** (exact order):

1. `clean(repo, config)` (stale cleanup; ignore its message).
2. Create `run_dir = runs_root(repo)/run_id` and write `tasks.json` (`to_jsonable(taskset)`), `report.schema.json`
   (`REPORT_SCHEMA`), and `run.json` = `{"run_id", "pid": os.getpid(), "status": "running", "started"}`.
3. `before = snapshot_tree(repo.main_root)`; `(base_sha, base_ref) = resolve_base(repo, taskset.base, taskset.snapshot_dirty)`.
4. Run flags: `DIRTY_BASE` if `dirty_paths(before)` is non-empty and `base_ref != SNAPSHOT_BASE_LABEL`. Then
   `deny_base` = `[main_root]` + (`[common_dir]` if not `path_within(common_dir, main_root)`) + `config.deny_write`.
   Any entry `p` where `path_within(worktree_root, p)` is dropped with `Flag(DENY_RULE_DROPPED, p)`.
5. `ThreadPoolExecutor(max_workers=config.max_parallel)`: submit `run_task` for every task in file order, and collect
   the results in file order.
6. `after = snapshot_tree(repo.main_root)`; if `diff_snapshots` is non-empty, add `MAIN_TREE_CHANGED` with the spec
   detail. Append `overlap_flags(results)`.
7. Exit code: `EXIT_INTERRUPTED` if cancel is set; elif any result reason starts with `auth:` or
   `not started: auth failure`, `EXIT_ENV`; elif any status != ok or any warn flag (task or run), `EXIT_TASKS_FAILED`;
   else `EXIT_OK`.
8. Build the `RunResult` (`status` = `interrupted` if cancelled, else `done`; `pid`; `retry_limit`), and write
   `run.json` (`to_jsonable`) and `summary.txt` (`render_summary`). Return it.

**`run_task` steps** (exact order; wrap the whole thing in try/except. An unexpected exception gives status `error`,
reason `internal error: <Type>: <msg>`, a traceback in `<task_dir>/internal-error.txt`, and the worktree is still
removed):

1. If `ctx.cancel` is set, return skipped with `not started: interrupted`. If `ctx.stop_new` is set, return skipped
   with `not started: <stop_reason[0]>`.
2. `task_dir = run_dir/task.id`; `wt = worktree_root/task.id`; `branch = f"agw/{run_id}/{task.id}"` for edit, else
   `None`. `add_worktree`. A GitError gives status error, reason `git: <first stderr line>`.
3. env = `os.environ` + `AGW_MAIN_ROOT`, `AGW_WORKTREE`, `AGW_RUN_ID`, `AGW_TASK_ID`.
4. Setup commands: each via `subprocess.run(cmd, shell=True, cwd=wt, env=env, capture_output=True, text=True,
   timeout=config.accept_timeout_seconds)`, with output appended to `setup.log`. The first failure sets
   `setup_failure` and skips steps 5–11.
5. `baseline = set(worktree_changes(wt, set()))`.
6. `accept = effective_accept(task, config)`; `brief = render_brief(task, taskset.context, wt, accept)`; write
   `brief.md`.
7. `deny = ctx.deny_base + ([wt] if read)`; `settings = build_settings(deny)`; write `agy-settings.json` (the
   settings JSON).
8. `model = task.model or ctx.model`; `timeout = task.timeout_seconds or config.timeout_seconds`; build the argv.
   Loop: `run_agy(..., hard_timeout_seconds=timeout + KILL_GRACE_SECONDS, cancel=ctx.cancel, env=env)` →
   `parse_stream`. If `outcome.retryable` and kind not in (`ok`, `timeout`, `auth`, `interrupted`) and fewer than
   `MAX_TRANSIENT_RETRIES` retries have been used, wait `ctx.retry_delay` seconds (`ctx.cancel.wait(...)`; stop if
   cancelled) and loop again.
9. kind `quota`: `stop_new.set()`, `stop_reason = ["quota exhausted"]`. kind `auth`: the same, with `"auth failure"`.
10. `report = normalize_report(outcome.structured, outcome.response)`.
11. Changes. Edit: `head = commit_all(wt, f"agw {run_id}/{task.id}: {task.title or task.goal[:60]}", baseline)`
    (always, even on timeout or error, to keep partial work). With a head: `diff = diffstat(main_root, base_sha, head)`,
    `write_patch(..., task_dir/"diff.patch")`, `changes = [p for _, p in diff.files]`. With no head: `diff = DiffStat()`,
    `changes = []`, `delete_branch(branch)`, `branch = ""`. Read: `changes = worktree_changes(wt, baseline)`,
    `diff = None`.
12. Acceptance runs only if `outcome.kind == "ok"`, `report.status != "blocked"`, not (edit and no changes), and
    `accept` is non-empty: `run_acceptance(accept, wt, config.accept_timeout_seconds, task_dir/"accept.log", env)`.
13. `flags = task_flags(...)` with `worker_ran_ok = (outcome.kind == "ok")`, `worktree_base = config.worktree_dir`.
14. `status, reason = decide_status(...)`.
15. `remove_worktree(repo, wt)` (in `finally`).
16. Build the `TaskResult` (`commands` = the outcome's run_command calls; `max_attempts = 1 + config.retry_limit`;
    `worker_ran`), write `result.json`, and if `progress` is set, print `[agw] <id> <status> (<dur>)` to it.
    (`[agw] <id> started` is printed after step 2.)

`src/agw/cli.py`: `main(argv: list[str] | None = None) -> int`. It implements spec §5 exactly. It catches argparse's
`SystemExit` and returns its code. `--version` prints `agw 0.1.0`. Error messages go to stderr as
`agw: error: <message>`.

- `run`: `repo_info(os.getcwd())` (GitError → exit 3 with the spec message; `head_sha == ""` → exit 3) → `load_config(repo.main_root, {"model": args.model, "max_parallel": args.parallel})`
  (`ConfigError` → 2) → `load_taskset(args.tasks, sys.stdin)` (`TaskFileError` → print each error line → 2) →
  `check_attempts` (→ 2) → `check_available(config.agy_path)` (→ 3). Then install SIGINT/SIGTERM handlers that set
  the cancel event (only when on the main thread; restore them afterwards). Call
  `execute_run(taskset, config, repo, cancel=cancel, progress=sys.stderr if args.progress else None)` (the `--model` override already reached `config.model` through `load_config`), print
  `render_summary` to stdout, and return `result.exit_code`.
- `show`, `apply`, `clean`, `doctor`, `config`: as specified in spec §5 (including `show RUN` without a
  summary.txt), using `runner`, `summary` and `gitops`. Unknown run or
  task → 2. `apply` checks: task mode edit and `diff.patch` exists, else 2.
- `doctor`: the checks in spec §5 order. The `git` version is parsed from `git --version` with the regex
  `(\d+)\.(\d+)`.

`tests/test_live_e2e.py`: the module-level
`pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("AGW_LIVE") != "1", reason="set AGW_LIVE=1 to run against real agy")]`.
It implements spec §14 "Live e2e": a temp repo (`calc.py` with `def mul(a, b): return a + b` and a failing
`tests/test_calc.py` asserting `mul(3, 4) == 12`); the user config sets `model = os.environ.get("AGW_LIVE_MODEL", "gemini-3.8-flash-low")`,
`timeout_seconds = 600` and `deny_write = []`. Tasks: `fix-mul` (edit, scope `["calc.py"]`, accept
`["python3 -m pytest -q tests/test_calc.py"]`) and `explain` (read, goal "Describe what calc.py contains and what the
test checks."). It runs via `subprocess.run([sys.executable, str(REPO_ROOT/"bin"/"agw"), "run", "-"], input=..., cwd=repo, timeout=1500)`
with `AGW_CONFIG` pointing to the temp config. Assertions are as in the spec. It prints the summary (`-s` shows it).

### Tests to write first

`tests/test_runner.py` (call `execute_run` directly; `retry_delay=0`):

1. A single ok edit task: the fake action writes `app.py` plus the structured report `done` with
   `files_changed ["app.py"]`. Result `ok`; branch `agw/<run>/<id>` exists with one commit whose diff touches `app.py`;
   `diff.patch` exists; the worktree dir is gone; run.json status `done`; `summary.txt` equals `render_summary(result)`.
2. The fake agy log shows argv containing `--disable-slash-commands`, `--dangerously-skip-permissions`,
   `--print-timeout 1200s`, no `--sandbox`, and a `--settings` JSON whose deny list contains the main repo root and
   does NOT contain the worktree (edit). For a read task the deny list DOES contain its own worktree. stdin equals
   `brief.md`.
3. Read ok: findings come from the report; no branch is left; `diff is None`.
4. Read with a fake write action: `READ_MODE_CHANGES`, and the changes do not reach any branch or the main tree.
5. Acceptance failure: `accept ["python3 -c 'import sys; sys.exit(1)'"]` gives `failed`, `acceptance failed`, and the
   tail is captured.
6. Out-of-scope write: an `extra_events` write_file event with path `__OUTSIDE__/x` gives `OUT_OF_SCOPE_WRITE`.
7. Main tree change: a fake action `{"shell": "echo x >> <repo>/README.md"}` gives the run flag `MAIN_TREE_CHANGED`,
   exit code 1.
8. Timeout (instant, simulated): task `timeout_seconds: 60`, scenario `{"exit_code": 3, "result_status": "ERROR",
   "error": "print timeout after 60s with the turn still in progress; the response is partial",
   "finished_reason": "print_timeout", "structured_output": null}`. Status `timeout`, reason
   `worker timed out after 60s`, and no acceptance is run (the accept.log is absent).
9. Quota: with `max_parallel=1`, task 1 returns an ERROR with `error "Quota exceeded"`. Task 1 is error `quota: …`,
   task 2 is `skipped` `not started: quota exhausted`, exit 1.
10. Auth: same as 9 with `"not logged in"`. Exit code `EXIT_ENV`.
11. Transient retry: task scenario list `[ERROR with stderr AGY_ERROR {"retryable":true,…}, default success]`. The
    result is ok and the fake log has 2 calls for that task.
12. Parallel: 3 tasks with `sleep: 1` each and `max_parallel=3` finish in under 3 s total (the duration measured in
    the test).
13. Setup: `setup = ["echo dep > dep.txt"]` with an edit task writing `app.py`: `dep.txt` is NOT in the diff. A
    failing setup (`exit 7`) gives error `setup failed: \`exit 7\` exit 7`, and the fake log has 0 calls.
14. OVERLAP: two edit tasks both write `app.py` → the run flag OVERLAP.
15. `DIRTY_BASE` when README.md is modified before the run; with `snapshot_dirty: true` it is not set and the worker
    sees the modification (a fake action `{"shell": "cat README.md > seen.txt"}` in an edit task, and the diff
    contains `seen.txt` with the modified content).
16. Cancel: a `threading.Event` set by a timer after 1 s with 2 tasks (`sleep: 30`, `max_parallel=1`). The first is
    `error` (`interrupted: …`), the second `skipped`, the exit is 130, run.json status `interrupted`, and no worktree
    dirs are left.
17. `clean`: create a run, then fake a stale run.json (status running, pid 999999): `clean(repo, cfg)` marks it
    abandoned. `clean(repo, cfg, run_id)` deletes its branches and dir. `clean(..., all_runs=True)` removes the rest.

`tests/test_cli_run.py` (`agw.cli.main([...])` with `capsys`; stdin via `monkeypatch.setattr("sys.stdin", io.StringIO(...))`):

1. `main(["--version"])` prints `agw 0.1.0`.
2. `run -` with a valid task: exit 0, stdout starts with `agw ` and contains `[ok]`.
3. Invalid tasks JSON: exit 2, stderr lists the errors. `attempt` over the limit: exit 2.
4. Outside a git repo (chdir to tmp): exit 3. `AGW_AGY=/nonexistent`: exit 3 with the install hint.
5. `show` lists the run; `show last` prints the summary; `show last <task> --diff` prints a patch; `--brief` prints
   the brief; `--events`, `--report` and `--log` work; an unknown task gives 2.
6. `apply last <task>` applies to the main tree (`git diff` shows the change; nothing committed), exit 0. A second
   apply gives a conflict or already-applied failure, exit 1. Applying a read task gives exit 2.
7. `clean last` gives exit 0 and the branch is gone. `clean --all` gives exit 0.
8. `config` prints 11 lines. `doctor` with the fake agy gives exit 0 and lines starting `ok   `.
   `doctor --live` with the fake gives exit 0.
9. A subprocess test: `[sys.executable, "bin/agw", "--version"]` and `bin/agw run -` with the env from `agw_env`
   both work.

### Done when

```bash
.venv/bin/python -m pytest -q          # entire suite passes; live test reported as skipped
.venv/bin/python bin/agw --version     # agw 0.1.0
```

---

## T8: Final integration and verification (sequential, last)

**Goal.** Prove the whole thing works, in the way users will run it, and fix what doesn't.

Steps (fix any failure in the owning file, then re-run everything):

1. `.venv/bin/python -m pytest -q` → all pass; the live test is skipped.
2. `claude plugin validate .`, `claude plugin validate --strict .` and
   `claude plugin validate .claude-plugin/plugin.json` → all pass.
   `claude --plugin-dir . plugin details ag-workers` → `Skills (1)  delegate`. Record the always-on token number; if
   it differs from "~84" in README/spec, update the README.
3. Live e2e: `AGW_LIVE=1 .venv/bin/python -m pytest -q -s -m live tests/test_live_e2e.py` → pass. It uses
   `gemini-3.8-flash-low`. If it fails because of agy behaviour drift (for example new event tool names), fix the
   code, add the observation to `docs/research/agy-cli-findings.md` (append only, "verified, not assumed" style,
   with the date and agy version from `agy --version`), and add a sanitized fixture plus a unit test for it.
4. A manual smoke test in a throwaway repo under the system temp dir (never in `$HOME` or this repo):
   `git init`, add a file and commit; run `<repo>/bin/agw run -` with one read task and one edit task (model
   `gemini-3.8-flash-low`). Check the summary format against spec §9 by eye, then run `agw show last`,
   `agw apply last <edit-task>` and `agw clean --all`. Confirm `git branch --list 'agw/*'` is empty and the
   worktree dir is gone.
5. `bin/agw doctor --live` → all ok.
6. README accuracy: every command, flag, config key, default and exit code in README.md must match the code
   (`agw --help`, `agw run --help`, `agw config`) and spec. Every README claim about agy must appear in
   `docs/research/agy-cli-findings.md`. Fix the README where it disagrees.
7. `git status` is clean apart from intended changes; commit with the message `chore: final verification fixes` if
   anything changed.

### Done when

Steps 1–6 all pass, with their outputs pasted into the final report: test counts, validate output, the live test
result, the smoke summary text, and doctor output.
