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

Workers see the base commit (HEAD), not uncommitted edits. If tasks depend on uncommitted tracked changes, set top-level `"snapshot_dirty": true` in the tasks file (untracked files are never included), or commit first.

Worktrees contain ONLY tracked files: no `.venv`, `node_modules` or build outputs. Check `agw config` for `setup`. If there is none, write `accept` commands that use the main tree's tools via `$AGW_MAIN_ROOT` (exported to agy, setup and acceptance commands), e.g. `"$AGW_MAIN_ROOT/.venv/bin/python" -m pytest -q tests/test_x.py`.

## 3. Write the tasks
Gemini performs well only with explicit, structured instructions. agw turns your fields into a fixed brief.
- `goal`: 1-2 sentences describing the outcome: what is true when done.
- `context`: facts the worker needs but can't cheaply discover: conventions, interfaces, decisions already made. Don't paste whole files; list them in `files`.
- `files`: files to read first, most important first.
- `steps`: ordered, concrete, imperative, one action each. Name the exact files, functions and signatures.
- `scope` (edit): globs of the files it may change. Keep it tight.
- `constraints`: must / must-not rules (no new dependencies, keep the public API, style).
- `accept` (edit): fast, targeted commands that prove it works (for example one test file). If omitted, the configured defaults apply. With none, the edit is unverified (`accept: none`, flag `NO_ACCEPT`).
- `report` (optional): what the findings must cover. For read tasks, the exact questions.
- `model` (optional): default `gemini-3.8-flash-high`; cheaper `gemini-3.8-flash-low` for trivial tasks; stronger `gemini-4-argon-high` for hard ones. `agy models` lists them; never invent names.
Never use vague verbs ("improve", "clean up", "handle properly"); say exactly what to change.

Start the run in the background (Bash with `run_in_background: true`). Claude Code caps background commands at 2 h, so keep each run well under it: estimate ceil(tasks / parallel) × timeout (config `max_parallel`, `timeout_seconds`) and split big task sets into several runs. Pass JSON on stdin:

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
- `[ok]`, no `flags:` line, and `accept: pass`: trust the acceptance result. `accept: none` / `NO_ACCEPT` on an edit task means it is unverified: read the diff. For other edit tasks, read the diff (`agw show <run> <task> --diff`) only if the area is risky: security, data migrations, concurrency, public API, or deleted files.
- `[failed]`, `[error]`, `[timeout]`, or any `flags:` line: investigate before acting, with `agw show <run> <task>` (details), `--log` (acceptance output, plus setup and agy stderr) and `--diff`.
- Read tasks: the `findings` are the answer. Spot-check one or two concrete claims (open the cited file:line) before relying on them.
- `OUT_OF_SCOPE_WRITE`, `SUSPICIOUS_COMMAND`, `MAIN_TREE_CHANGED`: check what changed outside the worktree (`git status`, plus the listed paths) and tell the user.
- `SCOPE_VIOLATION`: read the out-of-scope hunks. `BRANCH_SWITCHED` (worker switched branches in its worktree): review the diff before applying. `REPORT_MISMATCH` / `NO_REPORT`: don't trust the report; rely on the diff and the acceptance results. `READ_MODE_CHANGES`: the changes were discarded; usually harmless. `OVERLAP`: expect conflicts when applying.

## 5. Integrate
For each good edit task, run `agw apply <run> <task>`. This applies the patch to the working tree without committing. Apply tasks one at a time, and resolve any reported conflicts yourself. Then run the project's tests in the main tree. Don't commit unless the user asked you to.

## 6. Retry or take over
For a failed task, find the cause: missing context, an ambiguous step, a wrong acceptance command, or a task that is too hard. If a better brief would fix it, rerun only that task with the fix, the same `id`, and `"attempt": <previous attempt + 1>`. Fix the brief; don't just repeat it. agw refuses attempts beyond its retry limit. After that, or when the task is harder than expected, do it yourself (the worker's attempt is on its branch, shown in the summary) or report back to the user.
Error kinds: `quota` (Gemini quota or rate limit; tell the user, don't retry right away), `auth` (the user must run `agy` and log in), `model` (bad model name), `timeout` (split the task, or set a larger `timeout_seconds`).

## 7. Finish
After integrating a run, run `agw clean <run>` (it removes that run's branches and records). Report to the user: what the workers did, what you verified, what you applied, and anything flagged.
