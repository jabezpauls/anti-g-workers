# ag-workers

Claude Code plans and verifies; Gemini workers execute. ag-workers is a Claude Code plugin plus a small stdlib-only Python CLI (`agw`) that hands well-specified coding and research tasks to Gemini models in Google Antigravity, so routine work stops eating your Claude usage. It drives the official `agy` CLI in headless mode and nothing else.

It runs only on demand: type `/ag-workers:delegate <task>`. Nothing else is loaded or executed until you do.

## Account safety

ag-workers only drives the official `agy` CLI. Never use OAuth proxies or token-reuse tools with your Google account: Google treats that as a Terms of Service violation and bans accounts for it.

`agw` only invokes the `agy` binary that you installed and logged into. It never reads, copies or proxies agy's credentials or OAuth tokens, and it never calls Google APIs itself.

## Safety model

Read this before using it. ag-workers reduces accidents; it is not a sandbox.

What the tool does for you:

- **A deny-rule guardrail (real but partial).** Each worker's agy session gets a `--settings` deny rule on the `write_file` tool (which also covers `edit_file`) for your main repo root, the git common dir if it is outside the root, `~/.ssh`, `~/.gnupg`, `~/.aws` and any `deny_write` paths. Read-mode workers are also denied their own worktree. This was verified to hold under `--dangerously-skip-permissions`.
- **Detection after the fact.** `agw` flags file-tool writes outside the worktree, suspicious shell commands, any change to your main tree during the run, out-of-scope edits, and worker reports that do not match what changed.
- **Review before integration.** Nothing reaches your working tree until `agw apply`, and `agw` never commits on your branches.

What does NOT protect you:

- Workers run with `--dangerously-skip-permissions`. They can run any shell command as your user. A command like `echo x > ~/somefile` succeeds (verified); only heuristics or the main-tree snapshot may catch it.
- A git worktree is not a security boundary.
- `agy --sandbox` does not confine writes under `--dangerously-skip-permissions` (verified), so it is off by default (`sandbox = false`). It only adds failed first attempts.
- The deny-rule guardrail covers file tools only. Shell commands can write anywhere your user can.
- Network access is unrestricted: workers can fetch from and push to the internet. `git push` is flagged, not prevented.
- Detection is best-effort. A determined or confused model can do damage that no flag reports.
- Commit or stash your work first. Run `agw` only on repositories and machines where you would accept an unattended junior developer running commands as you.

## Requirements

- Claude Code
- The Antigravity CLI `agy`, installed and logged in (run `agy` once to log in)
- Python 3.11 or newer (standard library only; no packages to install)
- git 2.31 or newer
- Linux and macOS are supported. Windows is best-effort and untested.

## Install

```bash
claude plugin marketplace add jabezpauls/anti-g-workers
claude plugin install ag-workers@ag-workers
```

If you host a fork, replace `jabezpauls` with the GitHub account that hosts it. To install from a local checkout instead:

```bash
claude plugin marketplace add /path/to/anti-g-workers
claude plugin install ag-workers@ag-workers
```

Verify the setup from a Claude Code session with `agw doctor --live` (or run `bin/agw doctor --live` from the checkout). `--live` makes one real, tiny `agy` call to confirm that login and the model work.

## Usage

```
/ag-workers:delegate <what you want done>
```

For example: `/ag-workers:delegate add retry with backoff to HttpClient.get and write tests, and explain how request authentication works`.

What Claude does:

1. Runs `agw doctor` once per session and stops with a clear message if something is missing.
2. Decides what is worth delegating: tasks with a precise goal, a known scope and a way to check the result. Ambiguous, security-sensitive or tiny changes stay with Claude.
3. Writes structured tasks (goal, context, files, steps, scope, constraints, accept). Each task is `read` (explore and report; file-writing tools are disabled) or `edit` (change code). `agw` renders them into one fixed, unambiguous brief for Gemini.
4. Starts `agw run` in the background and gets one compact summary back when it finishes.
5. Verifies cheapest first: trusts clean `[ok]` results with passing acceptance commands, and investigates anything failed or flagged with `agw show`.
6. Integrates good edit tasks with `agw apply` (uncommitted, one task at a time), then runs your tests.
7. Retries a failed task with a refined brief (at most `retry_limit` times) or takes it over, then runs `agw clean <run>` and reports back.

## How it works

```
/ag-workers:delegate <task>
        |
        v
Claude writes tasks JSON ---> agw run (background)
                                  |
                                  |  per task, in parallel (max_parallel):
                                  |   1. git worktree at the base commit
                                  |   2. setup commands (config)
                                  |   3. fixed brief on stdin to: agy --output-format stream-json ...
                                  |   4. parse events; collect changes; run acceptance commands
                                  |   5. flags + status; remove the worktree
                                  v
                          compact summary on stdout
                                  |
                                  v
Claude: verify by tiers --> agw show --> agw apply --> run tests --> agw clean
```

- Every worker runs in its own git worktree, created outside your repository under `<system temp>/agw-<user>/` (configurable with `worktree_dir`).
- For edit tasks, the result is committed on the branch `agw/<run>/<task>` and saved as `diff.patch`. The worktree is removed as soon as the task finishes; the branch survives.
- Read tasks run in a detached worktree. Any changes a read worker makes are discarded and flagged.
- `agw apply RUN TASK` applies the task's patch to your working tree (`git apply`, falling back to `git apply --3way`) and does not commit.
- Run state lives in `<git common dir>/agw/runs/<run_id>/`, inside `.git`, so it never shows up in `git status`.
- A quota error stops new tasks from starting (they become `skipped`); an auth error aborts the run with exit code 3.
- agw retries transient agy errors once on its own. Refined retries are driven by Claude, and `agw` enforces the `attempt` limit.

## The agw CLI

The plugin puts `agw` on the Bash tool's PATH while it is enabled. You can also run `bin/agw` directly.

| command | purpose |
|---|---|
| `agw --version` | print `agw 0.1.0` |
| `agw run [TASKS] [--model M] [--parallel N] [--progress]` | run a tasks file (`TASKS` is a path or `-` for stdin; default `-`) and print the compact summary. `--parallel` is 1-8. `--progress` prints one line per task start and finish on stderr. |
| `agw show [RUN [TASK] [--diff\|--log\|--brief\|--events\|--report]]` | with no arguments, list the 20 newest runs; with `RUN`, print its summary; with `RUN TASK`, print full task detail or one of the artifacts |
| `agw apply RUN TASK` | apply an edit task's patch to the working tree without committing |
| `agw clean [RUN] [--all]` | with no arguments, mark dead runs abandoned and remove stale worktrees; with `RUN` or `--all`, also remove the run's branches and records |
| `agw doctor [--live]` | check python, git, agy, config, repo, worktree dir and the write-deny guardrail; `--live` also makes one real agy call. Exit 0 unless a check FAILs (then 3) |
| `agw config` | print the effective config with the source of each value |

`RUN` may be the literal `last`, meaning the newest run in this repository. `agw run` must be started from inside the git repository.

Exit codes of `agw run`:

| exit | meaning |
|---|---|
| 0 | every task `ok`, and no warn-severity flag on any task or on the run |
| 1 | at least one task not `ok`, or at least one warn flag |
| 2 | usage error: invalid arguments, invalid tasks file, invalid config, or `attempt` over the limit |
| 3 | environment error: not a git repo, no commits, agy not found, Python too old, or an agy auth failure |
| 130 | interrupted (SIGINT/SIGTERM). Running workers are stopped and a summary is still printed |

`agw doctor` exits 0 if no check FAILs, otherwise 3.

### Reading the summary

Each task prints `[status] id mode "title" duration attempt a/max`, followed by the diff size and branch, the acceptance result, the commands the worker ran, any `flags:` and the worker's report. Statuses are `ok`, `failed`, `error`, `timeout` and `skipped`. Flags to look at first:

| flag | meaning |
|---|---|
| `OUT_OF_SCOPE_WRITE` | a file tool wrote outside the task's worktree |
| `SUSPICIOUS_COMMAND` | a shell command that looks like a git state change or a write outside the worktree |
| `MAIN_TREE_CHANGED` | your main tree changed during the run (may have been you or Claude) |
| `READ_MODE_CHANGES` | a read worker changed files; the changes were discarded |
| `SCOPE_VIOLATION` | an edit touched a path outside the task's `scope` |
| `REPORT_MISMATCH`, `NO_REPORT` | the worker's report is wrong or missing; rely on the diff and acceptance results |
| `BLOCKED_ACTIONS` | the worker reported actions it could not perform |
| `OVERLAP` | two edit tasks changed the same path; expect conflicts on apply |

Informational flags: `NO_CHANGES`, `LARGE_DIFF`, `DELETED_FILES`, `DIRTY_BASE`, `DENY_RULE_DROPPED`.

## Tasks file

`agw run` reads UTF-8 JSON. Every validation error is reported at once as `<json path>: <message>`, and unknown keys are errors.

Top-level keys: `tasks` (required, 1 to 20 items), `version` (optional, must be `1`), `base` (git ref to start from, default `HEAD`), `snapshot_dirty` (default `false`; if true, workers start from a snapshot of uncommitted changes to tracked files, never untracked files; requires `base` `HEAD`) and `context` (shared paragraphs prepended to every task).

Task fields:

| field | type | notes |
|---|---|---|
| `id` | string | required; unique; matches `^[a-z0-9][a-z0-9-]{0,39}$` |
| `goal` | string | required; the outcome, in 1-2 sentences |
| `mode` | `"read"` or `"edit"` | default `"read"` |
| `title` | string | up to 80 characters |
| `context` | list of strings | facts the worker cannot cheaply discover |
| `files` | list of strings | files to read first |
| `steps` | list of strings | ordered, concrete actions |
| `scope` | list of strings | edit mode: globs the worker may change; empty means unrestricted |
| `constraints` | list of strings | must / must-not rules |
| `accept` | list of strings | shell commands that must exit 0, run in the worktree. Absent: edit tasks use the config `accept`, read tasks use none. `[]` means none. |
| `report` | string | what `findings` must cover |
| `model` | string | overrides `--model` and the config model |
| `timeout_seconds` | integer | 60-7200 |
| `attempt` | integer | default 1; must be at most `1 + retry_limit` |

Example:

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

Tasks in one run execute in parallel, so they must be independent. Put dependent work in a later run.

## Configuration

Config is merged in this order, and later wins (lists are replaced, not merged):

1. built-in defaults
2. the user file: `$AGW_CONFIG` if set; otherwise on Windows `%APPDATA%\agw\config.toml`; otherwise `$XDG_CONFIG_HOME/agw/config.toml`; otherwise `~/.config/agw/config.toml`
3. the project file `<repo root>/.agw.toml`
4. CLI overrides (`--model`, `--parallel`)

A missing file is fine. The environment variable `AGW_AGY`, if set, replaces `agy_path`. Unknown keys or wrong types are errors (exit 2). Run `agw config` to see the effective values and where each came from.

| key | type | default | rules |
|---|---|---|---|
| `model` | string | `"gemini-3.8-flash-high"` | non-empty |
| `max_parallel` | int | `3` | 1-8 |
| `timeout_seconds` | int | `1200` | 60-7200; per worker (`--print-timeout`) |
| `sandbox` | bool | `false` | passes `--sandbox` when true; see the safety model |
| `retry_limit` | int | `2` | 0-5; refined retries allowed after the first attempt |
| `accept` | list of strings | `[]` | default acceptance commands for edit tasks without `accept` |
| `accept_timeout_seconds` | int | `600` | 10-7200; per acceptance command |
| `setup` | list of strings | `[]` | shell commands run in each new worktree before the worker starts |
| `deny_write` | list of strings | `["~/.ssh", "~/.gnupg", "~/.aws"]` | extra paths that file tools may not write (`~` expanded); added to the built-in main-root deny |
| `agy_path` | string or list of strings | `"agy"` | executable, or argv prefix |
| `worktree_dir` | string | `""` | `""` means `<system temp dir>/agw-<user>`; `~` expanded |

Setup and acceptance commands run through the shell in the worktree and get the extra environment variables `AGW_MAIN_ROOT`, `AGW_WORKTREE`, `AGW_RUN_ID` and `AGW_TASK_ID`.

User config, `~/.config/agw/config.toml`:

```toml
model = "gemini-3.8-flash-high"
max_parallel = 3
timeout_seconds = 1200
retry_limit = 2
```

Project config, `.agw.toml` (Node project):

```toml
accept = ["npm test --silent"]
setup = ["ln -s \"$AGW_MAIN_ROOT/node_modules\" node_modules"]
```

## Cost notes

How it saves money: Claude spends tokens on planning one structured task file, reading one compact summary and checking the risky parts. Gemini workers spend the tokens on the bulk reading, editing and test-running. Several things keep Claude's side small:

- Always-on cost is just the skill description, about 84 Claude tokens. The skill body loads only when you invoke `/ag-workers:delegate`, and there is no MCP server whose tool definitions would sit in every session.
- One Bash call starts a run, and one compact summary comes back. Details (`agw show`) are read only when needed.
- Claude writes structured fields and `agw` renders the full brief, so Claude writes less.
- Acceptance commands run inside `agw`, so Claude does not spend turns running them.

Where it costs: each worker uses roughly 15-50k Gemini input tokens even for tiny tasks, because of agy's own system prompt overhead. That matters for quota planning, and it is why tiny changes are better done by Claude directly.

## Troubleshooting

- **`agy not found`**: install the Antigravity CLI, make sure `agy` is on PATH (or set `agy_path`), and run `agy` once to log in. Check with `agw doctor`.
- **Auth failure (exit 3)**: run `agy` and log in again, then rerun.
- **Quota or rate limit**: the failing task is an error and the unstarted tasks are `skipped`. Wait, then rerun the affected tasks. The quota and auth error shapes are matched heuristically because they could not be triggered during testing.
- **Timeouts**: split the task, or set a larger `timeout_seconds` on the task or in config. Edit changes from a timed-out worker are still committed to its branch for inspection.
- **Missing dependencies in worktrees** (`node_modules`, `.venv`, and so on): worktrees contain only tracked files. Use `setup` in config to symlink or install them, for example the `.agw.toml` shown above.
- **Stale worktrees or branches**: `agw clean` removes worktrees of dead runs; `agw clean <run>` or `agw clean --all` also removes branches `agw/<run>/*` and run records.
- **Invalid model**: agy reports `invalid model selection`; set a valid `model` (list them with `agy models`).
- **Config or tasks errors (exit 2)**: every error is listed, one per line.

## Development

```bash
python3 -m venv .venv && .venv/bin/python -m pip install 'pytest>=8'
.venv/bin/python -m pytest -q
```

The suite uses a fake `agy` (`tests/fake_agy/fake_agy.py`) that replays recorded fixtures, so no real `agy`, network or account is needed, and it never touches your real config.

An opt-in end-to-end test uses the real `agy` on a throwaway repository (it uses your quota; `AGW_LIVE_MODEL` selects the model, default `gemini-3.8-flash-low`):

```bash
AGW_LIVE=1 .venv/bin/python -m pytest -q -m live tests/test_live_e2e.py
```

Validate the plugin and inspect it:

```bash
claude plugin validate .
claude --plugin-dir . plugin details ag-workers
```

Design documents are in `docs/superpowers/specs/` and the verified behaviour of `agy` is in `docs/research/agy-cli-findings.md`.

## License

MIT. See [LICENSE](LICENSE).
