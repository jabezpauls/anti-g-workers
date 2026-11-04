# ag-workers: design spec

Date: 2026-11-05 · Status: final, ready for implementation · Plan: `docs/superpowers/plans/2026-11-05-ag-workers.md`

Evidence base: `docs/research/agy-cli-findings.md` (verified behaviour of `agy` 1.4.12 and 1.5.0) and
`docs/research/samples/` (sanitized real NDJSON). Where this spec says "verified", the claim comes from those probes.

---

## 1. Goals and non-goals

### Goals

1. Let a Claude Code user hand **well-specified** work to Gemini models in Google Antigravity, through the
   **official `agy` CLI headless mode only**. Claude plans and verifies; Gemini executes. This saves Claude usage.
2. **On demand only**: nothing runs unless the user types `/ag-workers:delegate <task>`. The permanent context cost is
   just the skill description (measured: about 84 tokens).
3. **Clean, structured briefs**. Claude fills structured task fields, and `agw` renders them into one fixed,
   unambiguous brief format that Gemini receives.
4. **Verifiable**. Every worker runs in its own git worktree. `agw` runs the acceptance commands, inspects what the
   worker did, flags anything suspicious, and gives Claude a **compact** summary. Claude reads details only when needed.
5. **Cheap for Claude**: one Bash call to start a run, one compact summary back, and details only on demand.
6. **Polished open-source**: stdlib-only Python CLI, a test suite with no real `agy`, a valid Claude Code plugin and
   marketplace, and an honest README.

### Non-goals

- No OAuth proxies, token reuse, or calling Google APIs directly. Google treats that as a ToS violation and bans
  accounts for it.
- No MCP server: its tool definitions would sit in context in every session.
- No security sandbox. Worktrees and `agy --sandbox` are **not** security boundaries (verified). Safety comes from
  detection plus review.
- No automatic merging or committing on the user's branch. Integration applies a patch to the working tree; the user
  or Claude commits.
- No dependencies between tasks inside one run. Dependent work goes in sequential runs.
- No daemon, status server, TUI, or JSON output mode.
- Windows: best-effort only (CI-free; not tested on Windows).

---

## 2. Critique and changes

The agreed design was sound. Probing `agy` 1.5.0 turned up facts that justify the following changes. Each one is
backed by `docs/research/agy-cli-findings.md` (round 2).

| # | Agreed | Changed to | Why |
|---|---|---|---|
| C1 | `--sandbox` on by default | **`sandbox = false` by default** (still configurable) | Verified: under `--dangerously-skip-permissions` the sandbox's "BypassSandbox" escalation is auto-approved, so it confines nothing. It also made even in-workspace writes fail first (EROFS in a `/tmp` workspace), costing a retry each time. It added cost and gave no safety. |
| C2 | Detection only | **Detection plus one real guardrail**: every worker gets `--settings {"permissions":{"deny":["write_file(<main repo root>)", …]}}` | Verified: deny rules still apply under `--dangerously-skip-permissions`. `write_file(dir)` blocks both `write_file` and `edit_file` under `dir`, matched by path component. It costs nothing and stops the most likely accident: a file tool writing into the user's main tree. It does NOT stop shell commands, and the docs say so. |
| C3 | Worktrees (location unspecified) | Worktrees live **outside the repository**: `<system temp>/agw-<user>/<repo-slug>/<run>/<task>` | Deny beats allow (verified), so a worktree inside the main repo root would be denied too. Temp storage also makes crash leftovers harmless. |
| C4 | Worktree kept for review, then merge | **The worktree is removed as soon as the task finishes.** The result is committed on branch `agw/<run>/<task>` and saved as `diff.patch` | Branches survive cleanup. Leaving N checkouts lying around wastes disk and complicates crash cleanup. |
| C5 | Claude merges good branches | **`agw apply <run> <task>`** applies the task's patch to the working tree (`git apply`, falling back to `git apply --3way`) and does **not** commit | This works on a dirty tree. Nothing is committed on the user's branch without them asking, and overlapping edits are resolved by git's 3-way merge rather than a merge commit. |
| C6 | Brief text written by Claude | Claude writes **structured fields** (goal, context, files, steps, scope, constraints, accept); `agw` renders **one fixed template**, passed to agy **via stdin** | Gemini gets the same clean structure every time (the user's key requirement), and Claude writes less. stdin is verified to work and avoids argv limits and quoting problems (Windows has a 32K command-line limit). |
| C7 | Read workers in worktrees | Kept. Read workers also get a deny rule on **their own worktree**, so the file tools cannot write at all | Keeping worktrees has concrete grounds: (a) one code path for both modes means less code and fewer tests; (b) read workers run tests and commands that drop artifacts (`__pycache__`, coverage files), which would otherwise land in the user's tree; (c) a consistent snapshot of the code even if the user keeps editing. The extra deny makes "read" mean read-only for file tools; shell writes are still detected and discarded. |
| C8 | Retry with a refined brief, up to N | Claude-driven retries, with an `attempt` field that agw **enforces** (`attempt ≤ 1 + retry_limit`). agw itself auto-retries only **transient** agy errors (`AGY_ERROR.retryable == true`), once | Refining a brief takes judgment, so Claude does it. Transient errors don't need Claude's tokens. |
| C9 | "Snapshot `git status` before/after" | Snapshot = HEAD + porcelain status + **content hash of every dirty file** | Porcelain codes alone miss a second modification to a file that was already modified. The flag is worded honestly: the change may have been made by the user or Claude, who keep working during a background run. |
| C10 | (unspecified) | `setup` commands per worktree (config), whose resulting files are **excluded** from the worker's diff | Worktrees lack untracked dependencies (`node_modules`, `.venv`), so acceptance commands would fail. `setup` can, for example, symlink or install them. |
| C11 | (unspecified) | Pass `--disable-slash-commands` | Verified: a brief starting with `/word` is otherwise eaten by agy's slash-command handler, which never calls the model. |
| C12 | `/ag-workers <task>` | `/ag-workers:delegate <task>` | Verified in the docs: plugin skills are **always** namespaced `/<plugin>:<skill>`. `disable-model-invocation: true` is the correct mechanism. The description still sits in context (about 84 tokens, measured with `claude plugin details`). |
| C13 | (unspecified) | A run-level **OVERLAP** flag when two edit tasks changed the same file | Detected cheaply after the run, before integration. It warns Claude that the patches may conflict. |
| C14 | (unspecified) | A quota error stops new tasks from starting (they become `skipped`); an auth error aborts the run with exit 3 | The other workers would fail the same way, so starting them wastes time. |

YAGNI cuts (things deliberately not built): a TOML tasks format, a JSON summary mode, a live status command, task
dependency graphs, automatic merge, per-action approvals, model validation (agy reports bad models itself), a
config-init command, Windows-specific signal handling, and `--settings` passed as a file path (only the inline form
is verified).

Remaining risks are listed in §15.

---

## 3. Architecture

```
User: /ag-workers:delegate <task>
        │  (skill content loads only now)
        ▼
Claude Code ── writes tasks JSON ──► Bash (background): agw run - <<'AGW_TASKS' … AGW_TASKS
                                           │
                                           ▼
                                agw run (Python, stdlib only)
   preflight: git repo? commits? agy found? tasks valid? attempts within limit?
   snapshot main tree (HEAD + status + hashes) ; resolve base commit
   ThreadPool(max_parallel) ── per task ───────────────────────────────────────────┐
   │ 1 git worktree add (edit: -b agw/<run>/<id>, read: --detach) at base           │
   │ 2 setup commands (config) ; record baseline paths to exclude                   │
   │ 3 render brief (fixed template) ─► stdin of:                                   │
   │   agy --model M --output-format stream-json --json-schema <schema>             │
   │       --settings '{"permissions":{"deny":[write_file(<main root>),…]}}'        │
   │       --disable-slash-commands --dangerously-skip-permissions                  │
   │       --print-timeout <T>s [--sandbox]          (cwd = worktree)               │
   │ 4 parse NDJSON: tool calls, commands + exit codes, structured_output, errors   │
   │ 5 collect changes: git add -A (minus baseline) ; edit: commit on branch,       │
   │   diff.patch, diffstat ; read: changes ⇒ flag + discard                        │
   │ 6 acceptance commands in worktree (only if worker succeeded)                   │
   │ 7 flags + status ; result.json ; remove worktree                               │
   └────────────────────────────────────────────────────────────────────────────────┘
   snapshot main tree again ⇒ MAIN_TREE_CHANGED ; OVERLAP ; summary.txt ; run.json
   stdout: compact summary  (Claude Code notifies Claude when the background job ends)
        │
        ▼
Claude: tiered verification ─► agw show (on demand) ─► agw apply ─► run tests ─► agw clean
```

State lives in `<git common dir>/agw/runs/<run_id>/`. Inside `.git`, it never appears in `git status` and is shared by
all worktrees of the repo.

---

## 4. Components

All code lives in `src/agw/`. Each module has one purpose. "Pure" means no subprocesses and no filesystem writes.

| Module | Purpose | Public interface (exact signatures in the plan) | Depends on |
|---|---|---|---|
| `models.py` | Shared dataclasses, constants, exit codes, flag codes, `REPORT_SCHEMA`, dict (de)serialization. Pure. | `Config`, `Task`, `TaskSet`, `ToolCall`, `AgyOutcome`, `AcceptResult`, `Report`, `DiffStat`, `Flag`, `TaskResult`, `RunResult`, `to_jsonable()`, `task_result_from_dict()`, `run_result_from_dict()` | stdlib |
| `config.py` | Load defaults, then user TOML, then project `.agw.toml`, then CLI overrides; validate; render. | `user_config_path()`, `default_worktree_dir()`, `load_config(main_root, overrides)`, `render_config(cfg)`, `ConfigError` | models |
| `tasks.py` | Parse and validate the tasks JSON (from a file or stdin) into a `TaskSet`, collecting all errors. Pure apart from reading the file. | `load_taskset(source, stdin)`, `parse_taskset(data)`, `TaskFileError` | models |
| `brief.py` | Render the fixed worker brief text. Pure. | `render_brief(task, shared_context, worktree, accept)` | models |
| `gitops.py` | Every git invocation: repo discovery, base resolution, snapshots, worktrees, commit, diff, patch apply, branches. | `git()`, `repo_info()`, `resolve_base()`, `snapshot_tree()`, `diff_snapshots()`, `add_worktree()`, `remove_worktree()`, `worktree_changes()`, `commit_all()`, `diffstat()`, `write_patch()`, `apply_patch()`, `list_branches()`, `delete_branch()`, `prune_worktrees()`, `GitError`, `RepoInfo` | models |
| `agy.py` | Build agy argv and settings, run the process with timeout and cancellation, parse NDJSON into `AgyOutcome`, classify errors, check availability. | `build_settings()`, `build_argv()`, `run_agy()`, `parse_stream()`, `classify()`, `check_available()`, `live_check()` | models |
| `verify.py` | Acceptance commands, report normalization, path and scope helpers, flag computation, status decision. | `run_acceptance()`, `normalize_report()`, `path_within()`, `matches_scope()`, `is_suspicious_command()`, `task_flags()`, `overlap_flags()`, `decide_status()` | models |
| `summary.py` | Render the compact run summary, task detail, run list and condensed event list. Pure. | `render_summary()`, `render_task_detail()`, `render_runs_list()`, `render_events()`, `fmt_duration()` | models |
| `runner.py` | Orchestration: run dir, per-task pipeline, thread pool, cancellation, quota/auth abort, run-level flags, cleanup, run listing. | `execute_run()`, `run_task()`, `runs_root()`, `new_run_id()`, `list_runs()`, `load_run()`, `clean()` | all of the above |
| `cli.py` | argparse, the subcommands (`run`, `show`, `apply`, `clean`, `doctor`, `config`), exit codes, signal handlers. | `main(argv=None) -> int` | all |

Other files:

- `bin/agw`: launcher. It checks Python ≥ 3.11, puts `../src` on `sys.path`, and calls `agw.cli:main`. While the
  plugin is enabled, Claude Code puts plugin `bin/` on the Bash tool's PATH (verified in the docs), so Claude runs bare
  `agw`.
- `skills/delegate/SKILL.md`: the workflow Claude follows (§11).
- `tests/fake_agy/fake_agy.py`: a fake `agy` that replays fixtures (§13).

---

## 5. CLI interface

```
agw --version                      print "agw 0.1.0"; exit 0
agw run [TASKS] [--model M] [--parallel N] [--progress]
agw show [RUN [TASK] [--diff | --log | --brief | --events | --report]]
agw apply RUN TASK
agw clean [RUN] [--all]
agw doctor [--live]
agw config
```

`RUN` may be the literal `last`, meaning the newest run in this repository.

### `agw run`

- `TASKS`: path to a tasks JSON file, or `-` for stdin. The default is `-`.
- `--model M`: overrides the config model for tasks that don't set `model`.
- `--parallel N`: overrides `max_parallel` (1–8).
- `--progress`: one line per task start and finish on **stderr** (`[agw] <id> started`,
  `[agw] <id> <status> (<duration>)`). Off by default to save tokens.
- stdout: the compact summary (§9) and nothing else. Errors go to stderr as `agw: error: <message>`.
- The run must be started from inside the git repository (any subdirectory, or a linked worktree).

| exit | meaning |
|---|---|
| 0 | every task `ok`, and no warn-severity flag on any task or on the run |
| 1 | at least one task not `ok`, or at least one warn flag |
| 2 | usage error: invalid arguments, invalid tasks file, invalid config, or `attempt` over the limit |
| 3 | environment error: not a git repo, no commits, agy not found, Python too old, or an agy auth failure |
| 130 | interrupted (SIGINT/SIGTERM). Running workers are stopped, and a summary is still printed |

### `agw show`

- No args: list the 20 newest runs (`render_runs_list`). Exit 0.
- `RUN`: print the saved `summary.txt` of that run. Exit 0. If there is no `summary.txt` yet, print
  `run <RUN>: <status from run.json> (no summary yet)` and exit 0. An unknown run gives exit 2.
- `RUN TASK`: print the full task detail (`render_task_detail`): all commands, all flags, the full report.
- `RUN TASK --diff`: print `diff.patch` (or `(no diff)`). `--log`: `accept.log`. `--brief`: `brief.md`.
  `--events`: the condensed tool-call list (`render_events`). `--report`: the worker report as indented JSON.
- An unknown task gives exit 2.

### `agw apply RUN TASK`

Applies `<task_dir>/diff.patch` to the working tree of the repository's current worktree (the `show-toplevel` of the
cwd). It never commits.

1. `git apply --check --binary diff.patch`. On success, run `git apply --binary diff.patch` and print
   `applied <task> (<n> files) to working tree; not committed`. Exit 0.
2. Otherwise run `git apply --3way --binary diff.patch`. On exit 0, print
   `applied <task> with 3-way merge; changes are staged; not committed`. Exit 0.
3. Otherwise print `conflict applying <task>:` followed by git's stderr indented by 2 spaces. Exit 1.

The run or task is unknown, the task is not an edit task, or it has no `diff.patch`: print an error and exit 2.

### `agw clean`

- No args (stale cleanup; also runs silently at the start of every `agw run`): for each run whose `run.json` says
  `"status": "running"` but whose process is dead (POSIX: `os.kill(pid, 0)` raises `ProcessLookupError`; Windows: the
  run started more than 24 h ago), set its status to `"abandoned"`. Then delete every directory under
  `<worktree_dir>/<repo_slug>/` that belongs to a run which is not alive, and run `git worktree prune`. It keeps
  branches and records. It prints `cleaned: <w> worktree dir(s), <a> abandoned run(s)`. Exit 0.
- `RUN`: removes that run's worktree dirs, its branches `agw/<RUN>/*` and its run dir. If the run is alive, print an
  error and exit 2. It prints `removed run <RUN>: <b> branch(es)`. Exit 0.
- `--all`: does the same for every run that is not alive. It prints `removed <r> run(s), <b> branch(es)`. Exit 0.

### `agw doctor [--live]`

Prints one line per check, `ok   <check>: <detail>` / `warn <check>: <detail>` / `FAIL <check>: <detail>`, in this
order:

1. `python`: version ≥ 3.11
2. `git`: `git --version` ≥ 2.31
3. `agy`: the resolved path, plus the `agy --version` output (FAIL with install hint if not found)
4. `config`: loads and validates; detail = the list of config files used
5. `repo`: inside a git repo with ≥1 commit (warn, not FAIL, if not; doctor may run anywhere)
6. `worktree_dir`: exists or can be created, and is writable
7. `deny`: warn if `worktree_dir` is inside the repo root (the guardrail rule would be dropped)
8. with `--live` only, `live`: runs `agy` once with the stdin prompt `Reply with exactly: PONG`, in an empty temp dir,
   using the config model, `--output-format json` and a 120 s timeout. ok if `status == "SUCCESS"`; otherwise FAIL
   with the classified kind (`auth`, `quota`, `model`, `error`).

Exit 0 if no FAIL, else 3.

### `agw config`

Prints the effective config, one `key = <TOML value>  # <source>` line per key in the §8 order. `source` is
`default`, the config file path, or `cli`. Exit 0; exit 2 if the config is invalid.

---

## 6. Tasks file format

UTF-8 JSON. agw validates it by hand (stdlib only) against the schema below and reports **all** errors at once, one per
line, as `<json path>: <message>` (for example `tasks[1].mode: must be "read" or "edit"`). Unknown keys are errors,
which catches typos.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "agw tasks file",
  "type": "object",
  "additionalProperties": false,
  "required": ["tasks"],
  "properties": {
    "version": {"const": 1, "description": "Optional. Format version."},
    "base": {"type": "string", "minLength": 1, "default": "HEAD",
             "description": "Git ref the worktrees start from. Resolved to a commit at run start."},
    "snapshot_dirty": {"type": "boolean", "default": false,
             "description": "If true and the main tree has uncommitted changes to tracked files, workers start from a snapshot commit of them (git stash create). Untracked files are never included. Requires base == HEAD."},
    "context": {"type": "array", "items": {"type": "string", "minLength": 1}, "default": [],
             "description": "Shared context paragraphs prepended to every task's context."},
    "tasks": {
      "type": "array", "minItems": 1, "maxItems": 20,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id", "goal"],
        "properties": {
          "id":       {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,39}$", "description": "Unique within the file."},
          "mode":     {"enum": ["read", "edit"], "default": "read"},
          "title":    {"type": "string", "maxLength": 80, "default": ""},
          "goal":     {"type": "string", "minLength": 1},
          "context":  {"type": "array", "items": {"type": "string", "minLength": 1}, "default": []},
          "files":    {"type": "array", "items": {"type": "string", "minLength": 1}, "default": []},
          "steps":    {"type": "array", "items": {"type": "string", "minLength": 1}, "default": []},
          "scope":    {"type": "array", "items": {"type": "string", "minLength": 1}, "default": [],
                       "description": "Edit mode: globs (relative to repo root) the worker may change. Empty = unrestricted."},
          "constraints": {"type": "array", "items": {"type": "string", "minLength": 1}, "default": []},
          "accept":   {"type": "array", "items": {"type": "string", "minLength": 1},
                       "description": "Shell commands that must exit 0, run in the worktree. Absent: edit tasks use config `accept`, read tasks use none. [] = none."},
          "report":   {"type": "string", "default": "", "description": "What `findings` must cover."},
          "model":    {"type": "string", "minLength": 1, "description": "Overrides --model and config model."},
          "timeout_seconds": {"type": "integer", "minimum": 60, "maximum": 7200},
          "attempt":  {"type": "integer", "minimum": 1, "default": 1}
        }
      }
    }
  }
}
```

Extra validation rules, beyond the schema:

- Task ids must be unique.
- `snapshot_dirty: true` together with a `base` other than `"HEAD"` is an error.
- `attempt > 1 + retry_limit` (config) is a **run-time** usage error (exit 2):
  `tasks[i].attempt: <a> exceeds 1 + retry_limit (<r>); take the task over instead of retrying`.
  It is checked in `cli` after the config loads.
- A file that is not valid JSON gives the error `<source>: invalid JSON: <msg> (line L col C)`.
- `scope` glob semantics (`verify.matches_scope`): paths are POSIX, relative to the repo root. A pattern ending in `/`,
  or naming an existing directory in the worktree, matches everything under it. `**` matches any number of
  directories. Otherwise `fnmatch.fnmatchcase` is applied to the whole path, where `*` may also cross `/`. That is
  stdlib fnmatch behaviour and is documented as such.

---

## 7. Worker report schema

`REPORT_SCHEMA` in `models.py`. agw writes it to `<run_dir>/report.schema.json` and passes it via `--json-schema <path>`
(the path form is verified). It uses only constructs verified to work with agy: `type`, `properties`, `enum`, `items`
and `required`.

```json
{
  "type": "object",
  "properties": {
    "status": {"type": "string", "enum": ["done", "partial", "blocked"]},
    "summary": {"type": "string"},
    "findings": {"type": "string"},
    "files_changed": {"type": "array", "items": {"type": "string"}},
    "blocked_actions": {"type": "array", "items": {"type": "string"}},
    "concerns": {"type": "array", "items": {"type": "string"}}
  },
  "required": ["status", "summary", "findings", "files_changed", "blocked_actions", "concerns"]
}
```

`verify.normalize_report(structured, response)` always returns a `Report`:

- `structured` is a dict whose `status` is in the enum: each field is coerced (a non-string becomes `str(x)`, a
  non-list becomes `[]`, and each list item becomes `str`). `Report.status` = that status.
- Otherwise: `Report(status="missing", summary="", findings=response.strip(), files_changed=[], blocked_actions=[], concerns=[])`.
  The raw response text is kept as findings so a read task can still deliver.

`files_changed` entries are normalized for comparison: an absolute path inside the worktree becomes relative, `\`
becomes `/`, and a leading `./` is stripped.

---

## 8. Config

Files, merged in order, later wins (whole-value replacement, including lists):

1. built-in defaults
2. the user file: `$AGW_CONFIG` if set; else Windows `%APPDATA%\agw\config.toml`; else
   `$XDG_CONFIG_HOME/agw/config.toml`; else `~/.config/agw/config.toml`. A missing file is fine.
3. the project file: `<main repo root>/.agw.toml`. A missing file is fine.
4. CLI overrides (`--model`, `--parallel`)

The environment variable `AGW_AGY`, if set, replaces `agy_path` with `[os.environ["AGW_AGY"]]` (source `env`).

| key | type | default | rules |
|---|---|---|---|
| `model` | string | `"gemini-3.8-flash-high"` | non-empty |
| `max_parallel` | int | `3` | 1–8 |
| `timeout_seconds` | int | `1200` | 60–7200; per worker (`--print-timeout`) |
| `sandbox` | bool | `false` | passes `--sandbox` when true (see C1) |
| `retry_limit` | int | `2` | 0–5; refined retries allowed after the first attempt |
| `accept` | list[str] | `[]` | default acceptance commands for edit tasks without `accept` |
| `accept_timeout_seconds` | int | `600` | 10–7200; per acceptance command |
| `setup` | list[str] | `[]` | shell commands run in each new worktree before the worker starts |
| `deny_write` | list[str] | `["~/.ssh", "~/.gnupg", "~/.aws"]` | extra paths that file tools may not write (`~` expanded); added to the built-in main-root deny |
| `agy_path` | string or list[str] | `"agy"` | executable, or argv prefix (e.g. `["python3", "/x/fake_agy.py"]`); normalized to list |
| `worktree_dir` | string | `""` | `""` means `<tempfile.gettempdir()>/agw-<getpass.getuser()>`; `~` expanded |

Unknown keys or wrong types give a `ConfigError` (exit 2) naming the file and key. Setup and acceptance commands run
with `shell=True` in the worktree and get these extra environment variables: `AGW_MAIN_ROOT`, `AGW_WORKTREE`,
`AGW_RUN_ID` and `AGW_TASK_ID`.

Example `~/.config/agw/config.toml`:

```toml
model = "gemini-3.8-flash-high"
max_parallel = 3
timeout_seconds = 1200
retry_limit = 2
```

Example project `.agw.toml` (Node project):

```toml
accept = ["npm test --silent"]
setup = ["ln -s \"$AGW_MAIN_ROOT/node_modules\" node_modules"]
```

---

## 9. Summary output format

Printed to stdout by `agw run` and saved as `summary.txt`. Plain ASCII apart from the content of user and worker text.
Every line is exactly as specified; optional lines are omitted when empty.

**Header:**

```
agw <run_id> | base <sha7> (<base_ref>) | <n> task(s): <counts> | <duration> | gemini <in>k in / <out>k out
```

- `<base_ref>` is the branch name, `detached`, or `snapshot of uncommitted changes`.
- `<counts>`: `<k> ok` always, followed by `, <k> failed`, `, <k> error`, `, <k> timeout` and `, <k> skipped`, each
  only when non-zero.
- `<in>`/`<out>`: the sums of `usage.input_tokens`/`usage.output_tokens` across tasks, divided by 1000 and rounded to
  the nearest integer.
- `<duration>`: `fmt_duration` gives `<s>s` under 60 s, `<m>m<ss>s` under 1 h, and `<h>h<mm>m` otherwise
  (e.g. `45s`, `2m05s`, `1h03m`).

**Run-level flags**, one per line right after the header: `! <CODE>: <detail>` for warn severity, and
`- <CODE>: <detail>` for info.

**Task blocks**: one blank line, then:

```
[<status>] <id> <mode> "<title>" <duration> attempt <a>/<max>
```

(`"<title>"` and its space are omitted when the title is empty; `<max>` = `1 + retry_limit`). Then these lines, in
order, each indented by 2 spaces and present only if applicable:

| line | when | format |
|---|---|---|
| `error:` | status `error`/`timeout`/`skipped` | `error: <reason>` |
| `diff:` | edit mode, worker ran | `diff: <f> file(s) +<ins> -<del> \| branch <branch>` or `diff: none` |
| `accept:` | worker ran OK | `accept: pass (<p>/<n>)`; `accept: none`; or `accept: FAIL <p>/<n> passed; first failure \`<cmd>\` exit <code>` followed by up to 15 tail lines of the first failing command, each as `    \| <line>` |
| `ran:` | ≥1 `run_command` | `ran: <cmd> [<exit>]; <cmd> [<exit>]` for at most 8 commands, each truncated to 72 chars with `...`; `[?]` if the exit code is unknown; then ` (+<k> more)` if more |
| `flags:` | ≥1 task flag | `flags: <CODE> <detail>; <CODE> <detail>` |
| `report:` | status `ok` or `failed` and the worker ran | `report: <report.status> - <summary>` (`report: missing` when there is no report) |
| `blocked:` | blocked_actions non-empty | `blocked: <a>; <b>` |
| `concerns:` | concerns non-empty | `concerns: <a>; <b>` |
| `notes:` | edit mode, findings non-empty | `notes: <findings with newlines collapsed to spaces>` |
| `findings:` | read mode, findings non-empty | `findings:` then every findings line indented by 4 spaces |

**Footer**: one blank line, then
`next: agw show <run_id> <task> [--diff|--log|--events]; agw apply <run_id> <task>; agw clean <run_id>`.

Example:

```
agw 20261105-143201-a1b2 | base 3f2a1c9 (main) | 3 task(s): 1 ok, 1 failed, 1 timeout | 6m12s | gemini 412k in / 9k out
- DIRTY_BASE: 2 uncommitted path(s) not visible to workers

[ok] add-retry edit "Retry GETs in http client" 2m10s attempt 1/3
  diff: 2 file(s) +58 -4 | branch agw/20261105-143201-a1b2/add-retry
  accept: pass (1/1)
  ran: python -m pytest -q tests/test_http.py [1]; python -m pytest -q tests/test_http.py [0]
  report: done - Added _with_retry with 0.5s/1s backoff; HttpClient.get uses it; 3 tests added.

[failed] fix-parser edit 4m01s attempt 1/3
  diff: 1 file(s) +3 -1 | branch agw/20261105-143201-a1b2/fix-parser
  accept: FAIL 0/1 passed; first failure `python -m pytest -q tests/test_parser.py` exit 1
    | E       AssertionError: assert ['a', 'b'] == ['a', 'b', '']
    | 1 failed, 11 passed in 0.31s
  flags: SCOPE_VIOLATION setup.cfg
  report: partial - Fixed trailing delimiter handling; empty-field case still failing.

[timeout] map-auth read 20m00s attempt 1/3
  error: worker timed out after 1200s

next: agw show 20261105-143201-a1b2 <task> [--diff|--log|--events]; agw apply 20261105-143201-a1b2 <task>; agw clean 20261105-143201-a1b2
```

### Flags

| code | level | scope | condition | detail |
|---|---|---|---|---|
| `OUT_OF_SCOPE_WRITE` | warn | task | a non-read tool event has a path parameter outside the task's worktree that is not ignored (see below) | up to 3 paths, then `+<k> more` |
| `SUSPICIOUS_COMMAND` | warn | task | `is_suspicious_command` (below) is true for a `run_command` | up to 2 commands truncated to 72 chars, then `+<k> more` |
| `READ_MODE_CHANGES` | warn | task | read mode and the worktree had changes (they are discarded) | up to 5 paths |
| `SCOPE_VIOLATION` | warn | task | edit mode, `scope` non-empty, a changed path does not match it | up to 5 paths |
| `BLOCKED_ACTIONS` | warn | task | `report.blocked_actions` non-empty | `<n> action(s)` |
| `NO_REPORT` | warn | task | `report.status == "missing"` and the worker ran OK | `worker returned no structured report` |
| `REPORT_MISMATCH` | warn | task | edit mode, `report.status == "done"`, and some normalized `files_changed` entry is not in the actual changed set | `claimed but unchanged: <paths, up to 5>` |
| `NO_CHANGES` | info | task | edit mode, worker ran OK, no changes, `report.status != "blocked"` | `edit task produced no changes` |
| `LARGE_DIFF` | info | task | insertions + deletions > 400 | `<n> lines changed` |
| `DELETED_FILES` | info | task | the diff deletes ≥1 file | up to 5 paths |
| `MAIN_TREE_CHANGED` | warn | run | the main-tree snapshot differs between run start and end | `<paths, up to 5> (changed during run; may be you or Claude - check)` |
| `DIRTY_BASE` | info | run | the main tree was dirty at start and the base was not a dirty snapshot | `<n> uncommitted path(s) not visible to workers` |
| `OVERLAP` | warn | run | ≥2 edit tasks with diffs changed the same path | `<path> (<id>, <id>)`, joined by `; `, up to 5 |
| `DENY_RULE_DROPPED` | info | run | a deny path contained the worktree path and was dropped | `<path>` |

**Path-parameter detection.** A tool event's path parameters are the values of keys whose lower-cased name is in
`{"path", "targetfile", "target_file", "absolutepath", "file", "filepath", "file_path"}`. Read tools, which are never
treated as writes: `read_file`, `view_file`, `list_dir`, `find_by_name`, `grep_search`, `read_url_content`,
`read_resource`, `search_web` and `command_status`. `run_command` is handled only by `SUSPICIOUS_COMMAND`. Any other
tool name with a path parameter counts as a write; this is a fallback against agy renaming tools.

**Ignored locations for `OUT_OF_SCOPE_WRITE`** (compared after `os.path.realpath`): a path inside the system temp
dir, unless it is inside `worktree_dir` (another worker's tree is never ignored); and a path inside
`~/.gemini/antigravity-cli/brain`, agy's own artifact dir (verified to exist).

**`is_suspicious_command(cmd, worktree, temp_root)`** is true if either:

- (a) it matches `\bgit\s+(-C\b|push|pull|commit|reset|checkout|switch|rebase|merge|stash|clean|branch|tag|worktree|remote|config)\b`; or
- (b) it matches the write-indicator regex `(>|\btee\b|\brm\b|\bmv\b|\bcp\b|\bsed\s+-i|\bchmod\b|\bchown\b|\bln\b|\btouch\b|\bmkdir\b|\brmdir\b|\binstall\b|\bdd\b)`
  **and** some token refers to a path outside the worktree. Tokens come from `shlex.split(cmd)`, falling back to
  `cmd.split()` on `ValueError`. Leading `<`, `>`, `&` and digits are stripped from a token. A candidate is a token
  starting with `/`, `~`, or a drive letter `X:\`/`X:/`, or a token containing `..`. A candidate is outside if,
  after `expanduser`, joining with the worktree when relative, and `normpath`, it is not inside the worktree, not
  inside `temp_root`, and not under `/dev/`.

### Status decision (`verify.decide_status`), first match wins

1. Not started (quota abort, auth abort, or cancelled before start) → `skipped`, with reason
   `not started: quota exhausted` / `not started: auth failure` / `not started: interrupted`.
2. A setup command failed → `error`, `setup failed: \`<cmd>\` exit <code>`.
3. `outcome.kind == "timeout"` → `timeout`, `worker timed out after <T>s`.
4. `outcome.kind` in `quota`, `auth`, `model`, `interrupted`, `error` → `error`, `<kind>: <first line of outcome.error, max 160 chars>`.
5. `report.status == "blocked"` → `failed`, `worker blocked`.
6. Edit mode and no changes → `failed`, `no changes`.
7. Any acceptance command exited non-zero (or timed out, exit `-1`) → `failed`, `acceptance failed`.
8. `report.status == "partial"` → `failed`, `worker reported partial`.
9. Read mode, `report.status == "missing"` and the findings are empty → `failed`, `no report`.
10. Otherwise → `ok`, reason `""`.

---

## 10. Brief template

`brief.render_brief(task, shared_context, worktree, accept)` returns exactly this text. `{…}` are substitutions;
lines marked `⟨if …⟩` and their section are emitted only when the condition holds; `⟨if …⟩` markers themselves are
not emitted. Sections are separated by exactly one blank line. The text ends with a single `\n`.

```
# Worker brief

Task ID: {task.id}
Mode: {"EDIT" if edit else "READ-ONLY"}
Working directory: {worktree}

You are a worker agent. A senior engineer wrote this brief and will review your work. Follow it exactly.

## Goal
{task.goal}

⟨if shared_context or task.context⟩
## Context
{each paragraph of shared_context then task.context, separated by one blank line}

⟨if task.files⟩
## Read these files first
- {file}

⟨if task.steps⟩
## Steps
1. {step 1}
2. {step 2}

## Scope
⟨edit and task.scope⟩
Create, modify or delete ONLY files matching these paths (relative to the working directory):
- {glob}
Do not touch any other file. If the goal needs a change outside this scope, do not make it; describe it in `concerns`.
⟨edit and not task.scope⟩
Change only the files the goal requires. Keep the change minimal: no unrelated refactors, reformatting or renames.
⟨read⟩
READ-ONLY. Do not create, modify or delete any file, and do not run commands that write files (formatters, code generators, package installs, builds that write into the project). Reading files and running read-only commands or tests is allowed. File-writing tools are disabled for this task.

⟨if task.constraints⟩
## Constraints
- {constraint}

⟨if accept⟩
## Acceptance checks
After you finish, these commands will be run in the working directory. Each must exit with code 0:
- `{command}`
Run them yourself before you finish. If one fails, fix the cause within scope and run it again.

## Rules
1. Stay inside the working directory. Never write outside it, except throwaway files in the system temp directory.
2. Never ask questions; nobody will answer. If something is unclear, choose the most reasonable interpretation and note it in `concerns`.
3. Do not run git commands that change state (commit, push, pull, reset, checkout, switch, rebase, merge, stash, clean, branch, tag). Your changes are collected automatically.
4. Do not install packages or edit dependency manifests or lockfiles unless a step tells you to.
5. If a tool call is denied or fails and you cannot work around it within these rules, record it in `blocked_actions` and continue with the rest of the task.
6. Never claim success you have not verified. If the goal is not fully met, use status "partial" or "blocked" and say why in `summary`.
7. Finish with the JSON report described below and nothing else.

## Report
Return a JSON object with these fields:
- status: "done" if the goal is fully met and verified, "partial" if only part is done, "blocked" if you could not make progress.
- summary: 1-3 sentences on what you did and the result.
- findings: {FINDINGS_TEXT}
- files_changed: paths (relative to the working directory) you created, modified or deleted. Use [] if none.
- blocked_actions: each denied or failed action you could not work around. Use [] if none.
- concerns: risks, assumptions and anything the reviewer should double-check. Use [] if none.
```

`FINDINGS_TEXT`:

- read: `your answer to the goal. Be specific: file paths, function or class names, line numbers, short code excerpts where they help. No filler. At most 400 words.`
- edit: `notes the reviewer needs (design decisions, assumptions, follow-ups). At most 120 words. Use "" if none.`
- If `task.report` is non-empty, append ` Cover: {task.report}` to either.

`accept` is the effective list: `task.accept` if it is not `None`; else `config.accept` for edit mode; else `[]`.
Fake agy finds the task id with the regex `^Task ID: (\S+)$` (multiline).

---

## 11. SKILL.md workflow

`skills/delegate/SKILL.md`, verbatim:

````markdown
---
name: delegate
description: Hand well-specified coding or research tasks to cheap Gemini workers (official Antigravity agy CLI), then verify and integrate the results. Only runs when invoked as /ag-workers:delegate.
disable-model-invocation: true
argument-hint: "<what to get done>"
allowed-tools: Bash(agw *) Bash(git diff *) Bash(git log *) Bash(git status *)
---

# Delegate to Gemini workers (agw)

The user wants this done with Gemini workers, to save Claude usage: $ARGUMENTS

You plan and review; Gemini workers execute through `agw`, a wrapper around the official `agy` CLI. Every worker runs in its own git worktree, and agw checks its result for you. If `agw` is not on PATH, run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/agw"` instead (on Windows: `python`).

## 1. Preflight (once per session)
Run `agw doctor`. If any line says FAIL, stop and tell the user exactly what to fix (install agy, log in by running `agy` once, Python 3.11+, git 2.31+). Do not work around it.

## 2. Decide what to delegate
Delegate a task only if ALL of these hold:
- You can state the goal and what "done" means precisely, without this conversation's history.
- It can be checked: by commands (tests, type checks, lint, build) for edit tasks, or by a concrete question for read tasks.
- Its scope is a known set of files (edit) or a clear area to explore (read).

Good candidates: mechanical edits across many files, tests for existing code, implementing a function whose interface you have fixed, boilerplate, docs, exploring a large codebase, summarizing modules, finding usages, investigating a failing test.

Keep the work yourself when: you already know the small change (a brief would cost more), design questions or ambiguity remain, it is security-sensitive (auth, crypto, permissions, secrets), it needs the user's input, or it spans most of the codebase.

Use `"mode": "read"` (the default) for exploration: file-writing tools are disabled and the `findings` are the result. Use `"mode": "edit"` for code changes. Tasks in one run execute in parallel, so they must be independent and must not edit the same files. Put dependent work in a later run.

## 3. Write the tasks
Gemini performs well only with explicit, structured instructions. agw turns your fields into a fixed brief.
- `goal`: 1-2 sentences describing the outcome: what is true when done.
- `context`: facts the worker needs but can't cheaply discover: conventions, interfaces, decisions already made. Don't paste whole files; list them in `files`.
- `files`: files to read first, most important first.
- `steps`: ordered, concrete, imperative, one action each. Name the exact files, functions and signatures.
- `scope` (edit): globs of the files it may change. Keep it tight.
- `constraints`: must / must-not rules (no new dependencies, keep the public API, style).
- `accept` (edit): fast, targeted commands that prove it works (for example one test file). If omitted, the configured defaults apply.
- `report` (optional): what the findings must cover. For read tasks, the exact questions.
- `model` (optional): the default comes from config. Use a cheaper model for trivial tasks.
Never use vague verbs ("improve", "clean up", "handle properly"); say exactly what to change.

Start the run in the background (Bash with `run_in_background: true` and `timeout: 7200000`), passing JSON on stdin:

```bash
agw run - <<'AGW_TASKS'
{
  "context": ["Python 3.12 project. Tests: python -m pytest -q"],
  "tasks": [
    {
      "id": "add-retry",
      "mode": "edit",
      "title": "Retry GETs in http client",
      "goal": "HttpClient.get retries up to 3 times with backoff on 5xx responses and connection errors.",
      "files": ["src/net/http.py", "tests/test_http.py"],
      "steps": [
        "In src/net/http.py add method _with_retry(self, fn) that calls fn up to 3 times, sleeping 0.5s then 1s between attempts, retrying only on requests.ConnectionError or a response with status >= 500.",
        "Make HttpClient.get call its request through _with_retry.",
        "In tests/test_http.py add tests: success on first try, success on third try, failure after 3 tries. Patch time.sleep."
      ],
      "scope": ["src/net/http.py", "tests/test_http.py"],
      "constraints": ["No new dependencies.", "Keep HttpClient's public signatures unchanged."],
      "accept": ["python -m pytest -q tests/test_http.py"]
    },
    {
      "id": "map-auth",
      "goal": "Explain how an incoming request is authenticated, from middleware to the loaded user object.",
      "report": "Entry points with file:line, token validation steps, where the user is loaded, any caching."
    }
  ]
}
AGW_TASKS
```

While it runs, don't edit files the workers are working on, and don't redo their work.

## 4. Verify, cheapest first
The run ends with a compact summary. For each task:
- `[ok]` and no `flags:` line: trust the acceptance result. For edit tasks, read the diff (`agw show <run> <task> --diff`) only if the area is risky: security, data migrations, concurrency, public API, or deleted files.
- `[failed]`, `[error]`, `[timeout]`, or any `flags:` line: investigate before acting, with `agw show <run> <task>` (details), `--log` (acceptance output) and `--diff`.
- Read tasks: the `findings` are the answer. Spot-check one or two concrete claims (open the cited file:line) before relying on them.
- `OUT_OF_SCOPE_WRITE`, `SUSPICIOUS_COMMAND`, `MAIN_TREE_CHANGED`: check what changed outside the worktree (`git status`, plus the listed paths) and tell the user.
- `SCOPE_VIOLATION`: read the out-of-scope hunks. `REPORT_MISMATCH` / `NO_REPORT`: don't trust the report; rely on the diff and the acceptance results. `READ_MODE_CHANGES`: the changes were discarded; usually harmless. `OVERLAP`: expect conflicts when applying.

## 5. Integrate
For each good edit task, run `agw apply <run> <task>`. This applies the patch to the working tree without committing. Apply tasks one at a time, and resolve any reported conflicts yourself. Then run the project's tests in the main tree. Don't commit unless the user asked you to.

## 6. Retry or take over
For a failed task, find the cause: missing context, an ambiguous step, a wrong acceptance command, or a task that is too hard. If a better brief would fix it, rerun only that task with the fix, the same `id`, and `"attempt": <previous attempt + 1>`. Fix the brief; don't just repeat it. agw refuses attempts beyond its retry limit. After that, or when the task is harder than expected, do it yourself (the worker's attempt is on its branch, shown in the summary) or report back to the user.
Error kinds: `quota` (Gemini quota or rate limit; tell the user, don't retry right away), `auth` (the user must run `agy` and log in), `model` (bad model name), `timeout` (split the task, or set a larger `timeout_seconds`).

## 7. Finish
After integrating a run, run `agw clean <run>` (it removes that run's branches and records). Report to the user: what the workers did, what you verified, what you applied, and anything flagged.
````

---

## 12. Safety model (honest)

What protects you:

1. **A deny rule (a real but partial guardrail).** Each worker's agy session denies the file tools (`write_file`, which
   also covers `edit_file`) for the main repo root, the git common dir if it is outside the root, `~/.ssh`, `~/.gnupg`,
   `~/.aws` and any `deny_write` paths. Read workers are also denied their own worktree. Verified to hold under
   `--dangerously-skip-permissions`.
2. **Detection after the fact.** agw flags file-tool writes outside the worktree, suspicious shell commands, any change
   to the main tree during the run, out-of-scope edits, and reports that don't match reality.
3. **Review before integration.** Nothing reaches your working tree until `agw apply`, and nothing is committed by agw
   on your branches.

What does NOT protect you (the README must state this plainly):

- **Workers run with `--dangerously-skip-permissions`.** They can run any shell command as your user. A command like
  `echo x > ~/somefile` succeeds (verified), and only heuristics or the main-tree snapshot may catch it.
- **A git worktree is not a security boundary**, and **`agy --sandbox` does not confine writes** under
  skip-permissions (verified). The sandbox is off by default because it only adds failed retries.
- Network access is unrestricted: workers can fetch from and push to the internet. `git push` is flagged, not
  prevented.
- Detection is best-effort. A determined or confused model can do damage that no flag reports.
- Run agw only on repositories and machines where you would accept an unattended junior developer running commands
  as you. Commit or stash your work first.

Account safety: agw only invokes the official `agy` binary that you installed and logged into. It never reads, copies
or proxies agy's credentials or OAuth tokens, and it never calls Google APIs itself.

---

## 13. Error handling

| failure | detection | behaviour |
|---|---|---|
| Not in a git repo | `git rev-parse` fails | exit 3: `not a git repository; agw needs git (git init && git commit first)` |
| Repo has no commits | `git rev-parse HEAD` fails | exit 3: `repository has no commits yet` |
| agy not installed | `shutil.which(agy_path[0])` is None and it is not an existing file | exit 3: `agy not found (agy_path=<v>). Install the Antigravity CLI and run 'agy' once to log in.` |
| agy not logged in / account blocked (UNVERIFIED shape) | `classify` → `auth` | the task errors; no new tasks start; exit 3 |
| Quota / rate limit (UNVERIFIED shape) | `classify` → `quota` | the task errors; unstarted tasks are `skipped`; exit 1 |
| Transient agy error | `AGY_ERROR.retryable == true` and kind not timeout/auth | wait 20 s and retry the same brief once (events and stderr are overwritten) |
| Invalid model | error contains `invalid model selection` | `model`, task error |
| Worker timeout | exit 3 / `finished_reason == "print_timeout"` | `timeout`; edit changes are still committed to the branch for inspection; no acceptance |
| agy hangs past its own timeout | agw waits `timeout + 60 s` | `terminate()`, wait 10 s, `kill()`; kind `timeout` |
| agy exits without a result event | no `result` event | status `MISSING`, kind `error`, `error = last stderr line or "agy exited <code> without a result"` |
| Malformed NDJSON line | `json.JSONDecodeError` | the line is skipped and counted (`AgyOutcome.bad_lines`) |
| Worker asks a question | `ask_question` is auto-skipped in headless mode (verified) | no special handling; the brief forbids questions |
| Worker edits the main tree | deny rule blocks file tools; snapshot diff | `MAIN_TREE_CHANGED` (run) and `OUT_OF_SCOPE_WRITE` (task) |
| Two tasks edit the same file | `OVERLAP` | `agw apply` falls back to 3-way and reports conflicts |
| Setup command fails | non-zero exit | task `error`, the worker is not started |
| Acceptance command hangs | `accept_timeout_seconds` | exit `-1`, `(timed out after <n>s)` appended to the log; task failed |
| Dirty main tree | snapshot at start | `DIRTY_BASE` info; workers see HEAD unless `snapshot_dirty` |
| agw interrupted (SIGINT/SIGTERM) | signal handler sets a cancel event | running agy processes get `terminate()`; their tasks end as `error: interrupted`; unstarted ones are `skipped`; worktrees are removed; the summary is printed; exit 130 |
| agw killed hard (SIGKILL) | the next `agw run`/`agw clean` sees a dead pid | the run is marked `abandoned`, its worktrees are removed and pruned. A running agy is bounded by its own `--print-timeout`. |
| Concurrent agw runs on one repo | unique run ids and branch names | git operations retry up to 5 times (0.2 s, 0.4 s, …) when stderr contains `.lock` or `index.lock`. Worktree creation is serialized within a process by a lock. |
| Git lock contention | stderr contains `.lock` | as above |
| Config or tasks invalid | validation | exit 2 with every error listed |
| Running inside a linked worktree | `--show-toplevel` is that worktree | it is treated as the main tree for snapshots, deny and apply; the run dir is still the common dir |

### `agy.classify(outcome)` (first match wins)

1. `exit_code == 0` and `status == "SUCCESS"` → `ok`
2. agw hard-killed it, or `exit_code == 3`, or `finished_reason == "print_timeout"`, or `agy_error.reason == "print_timeout"` → `timeout`
3. `error == "interrupted"`, or agw cancelled it → `interrupted`
4. `agy_error.status == "RESOURCE_EXHAUSTED"`, or the text (`error + "\n" + stderr`) matches `(?i)quota|rate.?limit|resource.?exhausted|\b429\b|too many requests|out of credits` → `quota`
5. `agy_error.status` in (`UNAUTHENTICATED`, `PERMISSION_DENIED`), or the text matches `(?i)not (logged|signed) in|log ?in required|unauthenticated|please (log|sign) in|authentication (failed|required)|account is blocked|terms of service` → `auth`
6. The text contains `invalid model selection` → `model`
7. Otherwise → `error`

`AGY_ERROR` parsing: the last stderr line starting with `AGY_ERROR: `; the rest is parsed as JSON, and a parse failure
means `None`. `retryable = bool(agy_error.get("retryable"))`.

---

## 14. Testing strategy

- **Framework**: pytest (dev only), run as `.venv/bin/python -m pytest -q`. The suite must pass with **no real agy**
  on PATH and must not touch `~/.config/agw` (every test sets `AGW_CONFIG` to a temp file).
- **Fake agy** (`tests/fake_agy/fake_agy.py`): a stdlib Python script invoked through config
  `agy_path = [sys.executable, "<abs path>/fake_agy.py"]`.
  - `--version` prints `1.5.0`, exit 0. `models` prints `gemini-3.8-flash-low\tGemini 3.8 Flash (Low)` and
    `gemini-3.8-flash-high\tGemini 3.8 Flash (High)`, exit 0.
  - With `--output-format json`, it prints only the result object (the envelope, without the
    `{"event":"result","result":…}` wrapper) as one line, and no other events.
  - Otherwise it reads the prompt from stdin, appends `{"argv", "cwd", "stdin", "task_id"}` as one JSON line to
    `$FAKE_AGY_LOG`, and picks a scenario from `$FAKE_AGY_SCENARIOS` (JSON:
    `{"default": <Scenario>, "tasks": {"<id>": <Scenario> | [<Scenario>, …]}}`; for a list, the n-th call for that
    task id uses item n, clamped to the last; call counts live in `$FAKE_AGY_STATE` or `dirname($FAKE_AGY_LOG)`).
  - Scenario keys (all optional): `fixture` (file name in `tests/fixtures/`, default `minimal-success.ndjson`),
    `actions` (a list of `{"write": path, "content": str}`, `{"delete": path}`, `{"shell": cmd}`, `{"sleep": secs}`,
    executed in order in the cwd before the events are emitted), `extra_events` (event objects emitted after the
    fixture's non-result events), `structured_output` (default: the done report below; JSON `null` means omit),
    `result_status` (default `"SUCCESS"`), `error` (default `""`), `finished_reason`, `exit_code` (default `0`),
    `stderr` (text written to stderr), `sleep` (seconds before the result is emitted).
  - The placeholders `__CWD__` (cwd) and `__OUTSIDE__` (`$FAKE_AGY_OUTSIDE`, default `/nonexistent-outside`) are
    substituted in fixture text, action paths, and `extra_events`.
  - The fixture's own `result` event is dropped and replaced by one built from the scenario. `usage` is taken from the
    fixture's result if present, else `{"input_tokens":1000,"output_tokens":100,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":1100}`.
    `response` is the JSON of `structured_output`, or `"DONE\n"`.
  - Timeouts: if `sleep` exceeds the `--print-timeout` value (parsed `<n>s`/`<n>m`), it sleeps for the timeout, emits
    an ERROR result with `finished_reason:"print_timeout"`, writes the `AGY_ERROR` line from the real sample to
    stderr, and exits 3.
  - SIGTERM: it emits `{"event":"result","result":{"status":"ERROR","error":"interrupted",…}}` and exits 1.
- **Fixtures** (`tests/fixtures/*.ndjson`) are derived from `docs/research/samples/` by path substitution (exact
  commands are in the plan), plus a hand-written `minimal-success.ndjson`.
- **Unit tests** per module: config merge and validation, task validation (every rule), brief rendering (golden
  string), NDJSON parsing over every fixture, classification table, flag rules, status table, summary rendering
  (golden), and git operations against real temp repos.
- **Integration tests** (`tests/test_cli_run.py`): `cli.main([...])` in-process against a temp repo with the fake agy:
  an ok edit, an ok read, acceptance failure, read-mode changes, out-of-scope write, main-tree change, timeout, quota
  abort, auth abort, parallelism, the `attempt` limit, `show`/`apply`/`clean`, interrupt. Plus one subprocess test
  that runs `bin/agw --version` and `bin/agw run` end to end.
- **Live e2e** (`tests/test_live_e2e.py`, marker `live`): skipped unless `AGW_LIVE=1`. It uses the real `agy` with
  `AGW_LIVE_MODEL` (default `gemini-3.8-flash-low`) on a throwaway repo: one edit task (fix a bug so a failing test
  passes) and one read task. It asserts edit `ok` with acceptance passing, read `ok` with non-empty findings, the main
  tree unchanged, `agw apply` succeeding with the test then passing in the main tree, and `agw clean --all` leaving no
  `agw/` branches.

---

## 15. Open risks

1. **Shell writes outside the worktree** cannot be prevented, only partially detected (heuristic plus main-tree
   snapshot). This is documented.
2. **agy changes fast** (1.4.12 → 1.5.0 within a day). Event tool names are not a stable API. Mitigations: the generic
   path-parameter fallback, the live e2e test, and `agw doctor --live`.
3. **Quota, auth and rate-limit error shapes are unverified.** They are classified by regex plus `AGY_ERROR.status`;
   the fallback is a generic `error` with the message shown to Claude.
4. `--sandbox` behaviour outside `/tmp` is unverified (moot while it's off by default).
5. **Gemini quality** varies. Acceptance commands are the main defence, so tasks without good `accept` commands are
   weakly verified. The skill tells Claude to prefer tasks that can be checked.
6. On macOS, temp paths go through `/private/var` symlinks. All path comparisons use `os.path.realpath`.
7. Windows is untested: no `os.kill(pid, 0)` liveness check (age-based instead), `shell=True` uses `cmd.exe`, and
   `bin/agw` needs `python` on PATH when the shebang isn't honoured.
8. Each worker costs about 15–50k Gemini input tokens even for tiny tasks (agy system prompt overhead, verified),
   which matters for quota planning.

---

## 16. Repo layout

The repository root is both the marketplace root and the plugin root (`"source": "./"`, validated with
`claude plugin validate`).

```
.claude-plugin/
  plugin.json
  marketplace.json
skills/delegate/SKILL.md
bin/agw
src/agw/
  __init__.py  __main__.py  models.py  config.py  tasks.py  brief.py
  gitops.py  agy.py  verify.py  summary.py  runner.py  cli.py
tests/
  conftest.py
  fake_agy/fake_agy.py
  fixtures/*.ndjson
  test_fake_agy.py  test_config.py  test_tasks.py  test_brief.py  test_gitops.py
  test_agy.py  test_verify.py  test_summary.py  test_runner.py  test_cli_run.py  test_live_e2e.py
docs/research/…  docs/superpowers/specs/…  docs/superpowers/plans/…
pyproject.toml  README.md  LICENSE  .gitignore
```

`plugin.json` (verbatim):

```json
{
  "name": "ag-workers",
  "version": "0.1.0",
  "description": "Delegate well-specified work from Claude Code to cheap Gemini workers via the official Antigravity agy CLI, then verify and integrate it.",
  "author": { "name": "jabezpauls" },
  "license": "MIT",
  "keywords": ["gemini", "antigravity", "agy", "delegation", "workers", "git-worktree"]
}
```

`marketplace.json` (verbatim):

```json
{
  "name": "ag-workers",
  "description": "ag-workers: hand work from Claude Code to Gemini workers through the official agy CLI.",
  "owner": { "name": "jabezpauls" },
  "plugins": [
    {
      "name": "ag-workers",
      "source": "./",
      "description": "Delegate well-specified work to Gemini workers via agy, verify it, and integrate it."
    }
  ]
}
```

---

## 17. README outline

1. **ag-workers**: a one-paragraph pitch. Claude plans and verifies, Gemini executes, and you save Claude usage. It
   uses the official `agy` CLI only.
2. **Account safety**: only the official `agy` CLI is used. Never use OAuth proxies or token-reuse tools with your
   Google account: Google treats that as a ToS violation and bans accounts. agw never touches agy credentials.
3. **Safety model**: the honest version of §12: skip-permissions, worktree ≠ sandbox, `--sandbox` doesn't confine,
   the deny-rule guardrail, detection flags, and "commit your work first".
4. **Requirements**: Claude Code, the Antigravity CLI `agy` (installed and logged in by running `agy` once), Python
   ≥ 3.11, git ≥ 2.31. Linux/macOS supported; Windows best-effort.
5. **Install**: `claude plugin marketplace add OWNER/anti-g-workers` then `claude plugin install ag-workers@ag-workers`
   (with a note to replace OWNER with the GitHub account hosting the repo), or locally:
   `claude plugin marketplace add /path/to/anti-g-workers`. Verify with `agw doctor --live` from a Claude Code session
   (or `bin/agw doctor --live`).
6. **Usage**: `/ag-workers:delegate <task>`, an example session, and what Claude does at each step.
7. **How it works**: the architecture diagram (§3, simplified), worktrees, branches, `agw apply`.
8. **The `agw` CLI**: the commands table and exit codes (§5).
9. **Tasks file**: a short field table plus the example (§6, §11).
10. **Configuration**: the key table, defaults and examples (§8).
11. **Cost notes**: the always-on cost of about 84 Claude tokens; about 15–50k Gemini input tokens per worker.
12. **Troubleshooting**: agy not found, auth, quota, timeouts, missing dependencies in worktrees (`setup`), stale
    worktrees (`agw clean`).
13. **Development**: venv plus pytest, the fake agy, `AGW_LIVE=1` for the live test, and `claude plugin validate .`.
14. **License**: MIT.
