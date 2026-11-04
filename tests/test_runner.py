"""Tests for runner.py: execute_run end to end against the fake agy, plus run bookkeeping and cleanup."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

from agw import runner
from agw.config import load_config
from agw.gitops import list_branches, repo_info
from agw.models import EXIT_ENV, EXIT_INTERRUPTED, EXIT_OK, EXIT_TASKS_FAILED, Task, TaskSet
from agw.summary import render_summary

from conftest import AgwEnv, run_git

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX shell syntax")

REPORT_DONE = {"status": "done", "summary": "did it", "findings": "", "files_changed": ["app.py"],
               "blocked_actions": [], "concerns": []}
WRITE_APP = {"write": "app.py", "content": "def add(a, b):\n    return a + b  # edited\n"}
EDIT_OK = {"actions": [WRITE_APP], "structured_output": REPORT_DONE}


def setup(env: AgwEnv, **cfg: object):
    env.write_config(**cfg)
    repo = repo_info(str(env.repo))
    return repo, load_config(repo.main_root, None)


def edit_task(tid: str = "t1", **kw: object) -> Task:
    base = dict(id=tid, goal="Edit app.py", mode="edit", title="Edit it", accept=[])
    base.update(kw)
    return Task(**base)


def read_task(tid: str = "r1", **kw: object) -> Task:
    return Task(**{"id": tid, "goal": "Explain app.py", **kw})


def go(env: AgwEnv, tasks: list[Task], *, taskset_kw: dict | None = None, cfg: dict | None = None, **kw):
    repo, config = setup(env, **(cfg or {}))
    ts = TaskSet(tasks=tasks, **(taskset_kw or {}))
    kw.setdefault("retry_delay", 0)
    return runner.execute_run(ts, config, repo, **kw), repo, config


def worktree_dirs(env: AgwEnv) -> list[str]:
    """Run or task directories left below <worktree_dir>/<repo slug>/ (the slug directory itself may stay)."""
    return [str(p) for p in env.worktree_dir.glob("*/*")]


# ---- 1. single ok edit task -----------------------------------------------------------------------

def test_single_ok_edit_task(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default=EDIT_OK)
    result, repo, _ = go(agw_env, [edit_task()])
    t = result.tasks[0]
    assert (t.status, t.reason, result.exit_code) == ("ok", "", EXIT_OK)
    assert t.branch == f"agw/{result.run_id}/t1"
    assert list_branches(repo) == [t.branch]
    log = run_git(["log", "--format=%s", f"{t.base_sha}..{t.branch}"], agw_env.repo).splitlines()
    assert log == [f"agw {result.run_id}/t1: Edit it"]
    assert run_git(["diff", "--name-only", t.base_sha, t.branch], agw_env.repo).split() == ["app.py"]
    run_dir = runner.runs_root(repo) / result.run_id
    assert "app.py" in (run_dir / "t1" / "diff.patch").read_text(encoding="utf-8")
    assert worktree_dirs(agw_env) == []
    run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run_json["status"] == "done" and run_json["run_id"] == result.run_id
    assert (run_dir / "summary.txt").read_text(encoding="utf-8") == render_summary(result)
    assert json.loads((run_dir / "t1" / "result.json").read_text(encoding="utf-8"))["status"] == "ok"
    assert json.loads((run_dir / "tasks.json").read_text(encoding="utf-8"))["tasks"][0]["id"] == "t1"
    # the main tree is untouched and nothing was committed on the user's branch
    assert run_git(["status", "--porcelain"], agw_env.repo) == ""
    assert (agw_env.repo / "app.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"


# ---- 2. how agy is invoked --------------------------------------------------------------------------

def test_agy_invocation_flags_settings_and_stdin(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default=EDIT_OK)
    result, repo, _ = go(agw_env, [edit_task(), read_task()])
    calls = {c["task_id"]: c for c in agw_env.calls()}
    run_dir = runner.runs_root(repo) / result.run_id
    for tid in ("t1", "r1"):
        argv = calls[tid]["argv"]
        assert "--disable-slash-commands" in argv and "--dangerously-skip-permissions" in argv
        assert argv[argv.index("--print-timeout") + 1] == "1200s"
        assert "--sandbox" not in argv
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert argv[argv.index("--json-schema") + 1] == str(run_dir / "report.schema.json")
        assert calls[tid]["stdin"] == (run_dir / tid / "brief.md").read_text(encoding="utf-8")
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert (run_dir / tid / "agy-settings.json").read_text(encoding="utf-8") == argv[argv.index("--settings") + 1]
        deny = settings["permissions"]["deny"]
        assert f"write_file({agw_env.repo})" in deny
        wt = calls[tid]["cwd"]
        assert wt.startswith(str(agw_env.worktree_dir) + os.sep) and os.path.basename(wt) == tid
        assert (f"write_file({wt})" in deny) == (tid == "r1")      # only read workers deny their own tree
    assert json.loads((run_dir / "report.schema.json").read_text(encoding="utf-8"))["required"][0] == "status"


def test_sandbox_option_is_passed_when_configured(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default=EDIT_OK)
    go(agw_env, [read_task()], cfg={"sandbox": True, "timeout_seconds": 90})
    argv = agw_env.calls()[0]["argv"]
    assert "--sandbox" in argv and argv[argv.index("--print-timeout") + 1] == "90s"


def test_task_model_and_timeout_override_config(agw_env: AgwEnv) -> None:
    go(agw_env, [read_task(model="gemini-x", timeout_seconds=75), read_task("r2")],
       model_override="gemini-cli")
    calls = {c["task_id"]: c["argv"] for c in agw_env.calls()}
    assert calls["r1"][calls["r1"].index("--model") + 1] == "gemini-x"
    assert calls["r1"][calls["r1"].index("--print-timeout") + 1] == "75s"
    assert calls["r2"][calls["r2"].index("--model") + 1] == "gemini-cli"


# ---- 3 / 4. read tasks -------------------------------------------------------------------------------

def test_read_ok_uses_report_findings_and_leaves_no_branch(agw_env: AgwEnv) -> None:
    rep = dict(REPORT_DONE, findings="app.py defines add()", files_changed=[])
    agw_env.set_scenarios(default={"structured_output": rep})
    result, repo, _ = go(agw_env, [read_task()])
    t = result.tasks[0]
    assert t.status == "ok" and t.diff is None and t.branch == ""
    assert t.report.findings == "app.py defines add()"
    assert list_branches(repo) == []
    assert t.accept == [] and t.worker_ran and t.model == "gemini-3.8-flash-high"
    assert t.usage["input_tokens"] == 1000
    assert worktree_dirs(agw_env) == []
    assert result.tasks[0].max_attempts == 3 and result.retry_limit == 2


def test_read_mode_changes_are_flagged_and_discarded(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default=EDIT_OK)
    result, repo, _ = go(agw_env, [read_task()])
    t = result.tasks[0]
    assert [f.code for f in t.flags] == ["READ_MODE_CHANGES"] and "app.py" in t.flags[0].detail
    assert result.exit_code == EXIT_TASKS_FAILED
    assert list_branches(repo) == []
    assert run_git(["status", "--porcelain"], agw_env.repo) == ""
    assert (agw_env.repo / "app.py").read_text(encoding="utf-8").endswith("a + b\n")


# ---- 5 / 6. acceptance, out-of-scope -----------------------------------------------------------------

@posix_only
def test_acceptance_failure(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default=EDIT_OK)
    cmd = "python3 -c 'import sys; print(\"boom\"); sys.exit(1)'"
    result, repo, _ = go(agw_env, [edit_task(accept=[cmd])])
    t = result.tasks[0]
    assert (t.status, t.reason) == ("failed", "acceptance failed")
    assert t.accept[0].exit_code == 1 and t.accept[0].tail == ["boom"]
    assert t.branch and t.diff.files == [("M", "app.py")]          # the branch is kept for inspection
    assert "boom" in (runner.runs_root(repo) / result.run_id / "t1" / "accept.log").read_text(encoding="utf-8")
    assert result.exit_code == EXIT_TASKS_FAILED


@posix_only
def test_acceptance_pass_and_config_default_accept(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default=EDIT_OK)
    result, _, _ = go(agw_env, [edit_task(accept=None)], cfg={"accept": ["test -f app.py", "true"]})
    t = result.tasks[0]
    assert t.status == "ok" and [a.exit_code for a in t.accept] == [0, 0]


@posix_only
def test_acceptance_runs_in_worktree_with_env(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default=EDIT_OK)
    cmd = 'test "$AGW_TASK_ID" = t1 && test "$AGW_MAIN_ROOT" = "%s" && test "$(pwd -P)" = "$AGW_WORKTREE" && grep -q edited app.py' % agw_env.repo
    result, _, _ = go(agw_env, [edit_task(accept=[cmd])])
    assert result.tasks[0].status == "ok", result.tasks[0].accept


def test_out_of_scope_write_is_flagged(agw_env: AgwEnv) -> None:
    ev = {"event": "step_update", "step_update": {"step_index": 5, "state": "DONE", "step_type": "tool",
          "tool_name": "write_file", "tool_info": {"name": "write_file", "parameters": {"Path": "__OUTSIDE__/x"}}}}
    agw_env.set_scenarios(default={**EDIT_OK, "extra_events": [ev]})
    result, _, _ = go(agw_env, [edit_task()])
    t = result.tasks[0]
    assert [f.code for f in t.flags] == ["OUT_OF_SCOPE_WRITE"]
    assert str(agw_env.outside) in t.flags[0].detail
    assert result.exit_code == EXIT_TASKS_FAILED


def test_commands_come_from_run_command_events(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default={"fixture": "write-and-run.ndjson", "structured_output": REPORT_DONE})
    result, repo, _ = go(agw_env, [read_task()])
    t = result.tasks[0]
    assert [(c.name, c.exit_code) for c in t.commands] == [("run_command", 0), ("run_command", 0)]
    assert [c.params["CommandLine"] for c in t.commands] == ["ls -la", "python3 a.py"]
    assert t.usage["input_tokens"] == 35222


# ---- 7. main tree changes ---------------------------------------------------------------------------------

@posix_only
def test_main_tree_change_during_run(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default={"actions": [{"shell": f"echo x >> '{agw_env.repo}/README.md'"}],
                                   "structured_output": dict(REPORT_DONE, files_changed=[])})
    result, _, _ = go(agw_env, [read_task()])
    flags = {f.code: f for f in result.flags}
    assert "MAIN_TREE_CHANGED" in flags and flags["MAIN_TREE_CHANGED"].detail.startswith("README.md (changed during run")
    assert flags["MAIN_TREE_CHANGED"].detail.endswith("; may be you or Claude - check)")
    assert result.exit_code == EXIT_TASKS_FAILED
    assert "! MAIN_TREE_CHANGED: README.md" in render_summary(result)


# ---- 8. timeout --------------------------------------------------------------------------------------------

def test_timeout_is_reported_without_acceptance(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default={
        "exit_code": 3, "result_status": "ERROR", "finished_reason": "print_timeout", "structured_output": None,
        "error": "print timeout after 60s with the turn still in progress; the response is partial",
        "actions": [WRITE_APP]})
    result, repo, _ = go(agw_env, [edit_task(timeout_seconds=60, accept=["true"])])
    t = result.tasks[0]
    assert (t.status, t.reason) == ("timeout", "worker timed out after 60s")
    assert not (runner.runs_root(repo) / result.run_id / "t1" / "accept.log").exists()
    assert t.accept == [] and t.branch      # partial work is still committed for inspection
    assert result.exit_code == EXIT_TASKS_FAILED


# ---- 9 / 10. quota and auth ---------------------------------------------------------------------------------

def test_quota_stops_new_tasks(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default={"exit_code": 1, "result_status": "ERROR", "error": "Quota exceeded",
                                   "structured_output": None})
    result, _, _ = go(agw_env, [read_task("r1"), read_task("r2")], cfg={"max_parallel": 1})
    a, b = result.tasks
    assert a.status == "error" and a.reason.startswith("quota: Quota exceeded")
    assert (b.status, b.reason) == ("skipped", "not started: quota exhausted") and not b.worker_ran
    assert [c["task_id"] for c in agw_env.calls()] == ["r1"]
    assert result.exit_code == EXIT_TASKS_FAILED
    assert (runner.runs_root(repo_info(str(agw_env.repo))) / result.run_id / "r2" / "result.json").exists()


def test_auth_failure_aborts_with_env_exit(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default={"exit_code": 1, "result_status": "ERROR", "error": "not logged in",
                                   "structured_output": None})
    result, _, _ = go(agw_env, [read_task("r1"), read_task("r2")], cfg={"max_parallel": 1})
    assert result.tasks[0].reason.startswith("auth: not logged in")
    assert result.tasks[1].reason == "not started: auth failure"
    assert result.exit_code == EXIT_ENV


def test_skipped_edit_task_leaves_no_branch(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default={"exit_code": 1, "result_status": "ERROR", "error": "Quota exceeded",
                                   "structured_output": None})
    result, repo, _ = go(agw_env, [read_task("r1"), edit_task("t2")], cfg={"max_parallel": 1})
    assert result.tasks[1].status == "skipped"
    assert list_branches(repo) == [] and worktree_dirs(agw_env) == []


# ---- 11. transient retry ------------------------------------------------------------------------------------

def test_transient_error_is_retried_once(agw_env: AgwEnv) -> None:
    flaky = {"exit_code": 1, "result_status": "ERROR", "error": "backend hiccup", "structured_output": None,
             "stderr": 'AGY_ERROR: {"short_error":"x","status":"UNAVAILABLE","retryable":true}\n'}
    agw_env.set_scenarios(tasks={"r1": [flaky, {}]})
    result, _, _ = go(agw_env, [read_task()])
    assert result.tasks[0].status == "ok"
    assert [c["task_id"] for c in agw_env.calls()] == ["r1", "r1"]


def test_transient_error_is_retried_only_once(agw_env: AgwEnv) -> None:
    flaky = {"exit_code": 1, "result_status": "ERROR", "error": "backend hiccup", "structured_output": None,
             "stderr": 'AGY_ERROR: {"status":"UNAVAILABLE","retryable":true}\n'}
    agw_env.set_scenarios(default=flaky)
    result, _, _ = go(agw_env, [read_task()])
    assert result.tasks[0].status == "error" and len(agw_env.calls()) == 2


# ---- 12. parallelism ----------------------------------------------------------------------------------------

def test_tasks_run_in_parallel(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default={"sleep": 1})
    t0 = time.monotonic()
    result, _, _ = go(agw_env, [read_task("a"), read_task("b"), read_task("c")], cfg={"max_parallel": 3})
    assert time.monotonic() - t0 < 3
    assert [t.status for t in result.tasks] == ["ok"] * 3
    assert [t.task_id for t in result.tasks] == ["a", "b", "c"]          # file order, not completion order


def test_max_parallel_one_is_sequential(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default={"sleep": 0.5})
    t0 = time.monotonic()
    go(agw_env, [read_task("a"), read_task("b"), read_task("c")], cfg={"max_parallel": 1})
    assert time.monotonic() - t0 >= 1.5


# ---- 13. setup ------------------------------------------------------------------------------------------------

@posix_only
def test_setup_files_are_excluded_from_the_diff(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default=EDIT_OK)
    result, repo, _ = go(agw_env, [edit_task()], cfg={"setup": ["echo dep > dep.txt"]})
    t = result.tasks[0]
    assert t.status == "ok" and t.diff.files == [("M", "app.py")]
    assert run_git(["diff", "--name-only", t.base_sha, t.branch], agw_env.repo).split() == ["app.py"]


@posix_only
def test_setup_failure_does_not_start_the_worker(agw_env: AgwEnv) -> None:
    result, repo, _ = go(agw_env, [edit_task()], cfg={"setup": ["exit 7", "echo never"]})
    t = result.tasks[0]
    assert (t.status, t.reason) == ("error", "setup failed: `exit 7` exit 7")
    assert agw_env.calls() == [] and not t.worker_ran
    assert list_branches(repo) == [] and worktree_dirs(agw_env) == []
    assert result.exit_code == EXIT_TASKS_FAILED
    setup_log = (runner.runs_root(repo) / result.run_id / "t1" / "setup.log").read_text(encoding="utf-8")
    assert "$ exit 7" in setup_log and "never" not in setup_log


@posix_only
def test_setup_sees_environment(agw_env: AgwEnv) -> None:
    result, repo, _ = go(agw_env, [read_task()], cfg={
        "setup": ['test "$AGW_TASK_ID" = r1 && test "$AGW_MAIN_ROOT" = "%s" && test "$(pwd -P)" = "$AGW_WORKTREE" '
                  '&& test -n "$AGW_RUN_ID"' % agw_env.repo]})
    assert result.tasks[0].status == "ok"


# ---- 14 / 15. flags --------------------------------------------------------------------------------------------

def test_overlap_flag(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default=EDIT_OK)
    result, _, _ = go(agw_env, [edit_task("a"), edit_task("b")])
    assert [f.code for f in result.flags] == ["OVERLAP"] and result.flags[0].detail == "app.py (a, b)"
    assert result.exit_code == EXIT_TASKS_FAILED


@posix_only
def test_dirty_base_and_snapshot_dirty(agw_env: AgwEnv) -> None:
    (agw_env.repo / "README.md").write_text("modified readme\n", encoding="utf-8")
    scen = {"actions": [{"shell": "cat README.md > seen.txt"}], "structured_output": dict(REPORT_DONE, files_changed=["seen.txt"])}
    agw_env.set_scenarios(default=scen)
    result, _, _ = go(agw_env, [edit_task()])
    assert [(f.code, f.detail) for f in result.flags] == [("DIRTY_BASE", "1 uncommitted path(s) not visible to workers")]
    assert result.exit_code == EXIT_OK          # info flags do not fail a run
    # the worker saw the committed README, not the dirty one
    patch = (runner.runs_root(repo_info(str(agw_env.repo))) / result.run_id / "t1" / "diff.patch").read_text(encoding="utf-8")
    assert "+demo" in patch and "modified readme" not in patch

    agw_env.set_scenarios(default=scen)
    result2, _, _ = go(agw_env, [edit_task()], taskset_kw={"snapshot_dirty": True})
    assert result2.flags == [] and result2.base_ref == "snapshot of uncommitted changes"
    patch2 = (runner.runs_root(repo_info(str(agw_env.repo))) / result2.run_id / "t1" / "diff.patch").read_text(encoding="utf-8")
    assert "+modified readme" in patch2
    assert (agw_env.repo / "README.md").read_text(encoding="utf-8") == "modified readme\n"     # main tree untouched


def test_deny_rule_dropped_when_worktrees_live_inside_the_repo(agw_env: AgwEnv) -> None:
    inside = agw_env.repo / ".agw-worktrees"
    result, _, _ = go(agw_env, [read_task()], cfg={"worktree_dir": str(inside)})
    flags = {f.code: f.detail for f in result.flags}
    assert flags["DENY_RULE_DROPPED"] == str(agw_env.repo)
    argv = agw_env.calls()[0]["argv"]
    deny = json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]
    assert f"write_file({agw_env.repo})" not in deny
    assert result.exit_code == EXIT_OK and result.tasks[0].status == "ok"


def test_extra_deny_write_paths(agw_env: AgwEnv) -> None:
    go(agw_env, [read_task()], cfg={"deny_write": [str(agw_env.outside)]})
    argv = agw_env.calls()[0]["argv"]
    deny = json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]
    assert f"write_file({agw_env.outside})" in deny and f"write_file({agw_env.repo})" in deny


def test_unknown_base_is_a_usage_error_and_leaves_no_state(agw_env: AgwEnv) -> None:
    repo, config = setup(agw_env)
    with pytest.raises(runner.RunError) as err:
        runner.execute_run(TaskSet(tasks=[read_task()], base="no-such-ref"), config, repo)
    assert err.value.exit_code == 2 and "no-such-ref" in str(err.value)
    assert not runner.runs_root(repo).exists() or list(runner.runs_root(repo).iterdir()) == []


@posix_only
def test_worker_that_commits_itself_is_not_lost(agw_env: AgwEnv) -> None:
    sh = ("echo changed > app.py && git add -A && git -c user.name=w -c user.email=w@x commit -qm 'worker commit'")
    agw_env.set_scenarios(default={"actions": [{"shell": sh}], "structured_output": REPORT_DONE})
    result, _, _ = go(agw_env, [edit_task()])
    t = result.tasks[0]
    assert t.status == "ok" and t.diff.files == [("M", "app.py")] and t.branch
    assert run_git(["show", f"{t.branch}:app.py"], agw_env.repo) == "changed\n"
    assert "SUSPICIOUS_COMMAND" not in [f.code for f in t.flags]       # no run_command event in the fake stream


def test_edit_task_without_changes_has_no_branch(agw_env: AgwEnv) -> None:
    result, repo, _ = go(agw_env, [edit_task()])
    t = result.tasks[0]
    assert (t.status, t.reason, t.branch) == ("failed", "no changes", "")
    assert t.diff.files == [] and list_branches(repo) == []
    assert "NO_CHANGES" in [f.code for f in t.flags]


# ---- 16. cancel ------------------------------------------------------------------------------------------------

@posix_only
def test_cancel_interrupts_worker_and_skips_the_rest(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default={"sleep": 30, "actions": [WRITE_APP]})
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    t0 = time.monotonic()
    result, repo, _ = go(agw_env, [edit_task("a"), edit_task("b")], cfg={"max_parallel": 1}, cancel=cancel)
    assert time.monotonic() - t0 < 15
    a, b = result.tasks
    assert a.status == "error" and a.reason.startswith("interrupted:")
    assert (b.status, b.reason) == ("skipped", "not started: interrupted")
    assert result.exit_code == EXIT_INTERRUPTED and result.status == "interrupted"
    assert worktree_dirs(agw_env) == []
    run_json = json.loads((runner.runs_root(repo) / result.run_id / "run.json").read_text(encoding="utf-8"))
    assert run_json["status"] == "interrupted"
    assert list_branches(repo) == [f"agw/{result.run_id}/a"]            # a's partial work stays for inspection
    assert render_summary(result).startswith("agw ")


@posix_only
def test_cancel_during_acceptance_stops_the_command(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default=EDIT_OK)
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    t0 = time.monotonic()
    result, _, _ = go(agw_env, [edit_task(accept=["sleep 60"])], cancel=cancel)
    assert time.monotonic() - t0 < 20
    t = result.tasks[0]
    assert t.status == "error" and t.reason.startswith("interrupted:")
    assert result.exit_code == EXIT_INTERRUPTED and worktree_dirs(agw_env) == []


@posix_only
def test_cancel_during_setup_stops_the_command(agw_env: AgwEnv) -> None:
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    t0 = time.monotonic()
    result, _, _ = go(agw_env, [read_task()], cfg={"setup": ["sleep 60"]}, cancel=cancel)
    assert time.monotonic() - t0 < 20
    assert result.tasks[0].status == "error" and result.tasks[0].reason.startswith("interrupted:")
    assert agw_env.calls() == [] and worktree_dirs(agw_env) == []


def test_already_cancelled_run_starts_nothing(agw_env: AgwEnv) -> None:
    cancel = threading.Event()
    cancel.set()
    result, repo, _ = go(agw_env, [read_task("a"), edit_task("b")], cancel=cancel)
    assert [t.status for t in result.tasks] == ["skipped", "skipped"]
    assert agw_env.calls() == [] and list_branches(repo) == [] and worktree_dirs(agw_env) == []
    assert result.exit_code == EXIT_INTERRUPTED


def test_progress_lines(agw_env: AgwEnv) -> None:
    import io
    buf = io.StringIO()
    go(agw_env, [read_task("a")], progress=buf)
    lines = buf.getvalue().splitlines()
    assert lines[0] == "[agw] a started" and lines[1].startswith("[agw] a ok (") and lines[1].endswith("s)")


def test_unexpected_exception_becomes_a_task_error_and_cleans_up(agw_env: AgwEnv, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(runner, "build_settings", boom)
    result, repo, _ = go(agw_env, [edit_task("a"), read_task("b")])
    for t in result.tasks:
        assert t.status == "error" and t.reason == "internal error: RuntimeError: kaboom"
        assert "kaboom" in (runner.runs_root(repo) / result.run_id / t.task_id / "internal-error.txt").read_text(encoding="utf-8")
    assert list_branches(repo) == [] and worktree_dirs(agw_env) == []


def test_git_failure_creating_a_worktree_is_a_task_error(agw_env: AgwEnv, monkeypatch) -> None:
    from agw.gitops import GitError

    def fail(*args, **kwargs):
        raise GitError(["worktree", "add"], 128, "fatal: no space left\nmore")
    monkeypatch.setattr(runner, "add_worktree", fail)
    result, _, _ = go(agw_env, [read_task()])
    assert (result.tasks[0].status, result.tasks[0].reason) == ("error", "git: fatal: no space left")


# ---- run bookkeeping -------------------------------------------------------------------------------------------

def test_run_id_and_slug_shapes() -> None:
    import datetime
    rid = runner.new_run_id(datetime.datetime(2026, 11, 5, 14, 32, 1))
    assert rid.startswith("20261105-143201-") and len(rid) == len("20261105-143201-a1b2")
    slug = runner.repo_slug("/home/u/proj")
    assert slug.startswith("proj-") and len(slug) == len("proj-") + 8


def test_resolve_list_and_load_runs(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(tasks={"bad": {"exit_code": 1, "result_status": "ERROR", "error": "x", "structured_output": None}})
    r1, repo, config = go(agw_env, [read_task("a")])
    r2, _, _ = go(agw_env, [read_task("a"), read_task("bad")])
    runs = runner.list_runs(repo)
    assert [r["run_id"] for r in runs] == sorted([r1.run_id, r2.run_id], reverse=True)
    by_id = {r["run_id"]: r for r in runs}
    assert by_id[r1.run_id]["counts"] == "1 ok" and by_id[r1.run_id]["n_tasks"] == 1
    assert by_id[r2.run_id]["counts"] == "1 ok, 1 error" and by_id[r2.run_id]["n_tasks"] == 2
    assert by_id[r1.run_id]["status"] == "done" and by_id[r1.run_id]["started"] == r1.started
    assert runner.resolve_run_id(repo, "last") == runs[0]["run_id"]
    assert runner.resolve_run_id(repo, r1.run_id) == r1.run_id
    assert runner.load_run(repo, r1.run_id)["run_id"] == r1.run_id
    for bad in ("nope", "../x", "", ".."):
        with pytest.raises(KeyError):
            runner.resolve_run_id(repo, bad)
    with pytest.raises(KeyError):
        runner.load_run(repo, "nope")


def test_list_runs_is_limited_and_empty_repo_has_none(agw_env: AgwEnv) -> None:
    repo, _ = setup(agw_env)
    assert runner.list_runs(repo) == []
    with pytest.raises(KeyError):
        runner.resolve_run_id(repo, "last")
    root = runner.runs_root(repo)
    for i in range(25):
        d = root / f"20260101-0000{i:02d}-aaaa"
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({"run_id": d.name, "status": "done", "started": "s"}), encoding="utf-8")
    runs = runner.list_runs(repo)
    assert len(runs) == 20 and runs[0]["run_id"] == "20260101-000024-aaaa"
    assert runs[0]["counts"] == "-" and runs[0]["n_tasks"] == 0


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_is_run_alive() -> None:
    assert runner.is_run_alive({"status": "running", "pid": os.getpid()})
    assert not runner.is_run_alive({"status": "done", "pid": os.getpid()})
    assert not runner.is_run_alive({"status": "running", "pid": dead_pid()}) or os.name == "nt"
    for bad in (0, -1, None, "x", True):
        assert not runner.is_run_alive({"status": "running", "pid": bad})
    assert not runner.is_run_alive({})


def fake_stale_run(repo, run_id: str, worktree_root: pathlib.Path | None = None, pid: int | None = None) -> pathlib.Path:
    d = runner.runs_root(repo) / run_id
    d.mkdir(parents=True)
    data = {"run_id": run_id, "pid": dead_pid() if pid is None else pid, "status": "running",
            "started": "2026-01-01T00:00:00"}
    if worktree_root is not None:
        data["worktree_root"] = str(worktree_root)
    (d / "run.json").write_text(json.dumps(data), encoding="utf-8")
    return d


# ---- 17. clean ---------------------------------------------------------------------------------------------------

def test_clean_stale_run_and_remove(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default=EDIT_OK)
    result, repo, config = go(agw_env, [edit_task()])
    run_id = result.run_id
    assert list_branches(repo) == [f"agw/{run_id}/t1"]

    stale_id = "20260101-000000-dead"
    stale_dir = fake_stale_run(repo, stale_id)
    # a leftover worktree dir of the stale run, registered with git
    stale_wt_root = pathlib.Path(config.worktree_dir) / runner.repo_slug(repo.main_root) / stale_id
    from agw.gitops import add_worktree
    add_worktree(repo, str(stale_wt_root / "x"), result.base_sha, f"agw/{stale_id}/x")
    assert (stale_wt_root / "x" / "app.py").exists()

    msg = runner.clean(repo, config)
    assert msg == "cleaned: 1 worktree dir(s), 1 abandoned run(s)"
    assert json.loads((stale_dir / "run.json").read_text(encoding="utf-8"))["status"] == "abandoned"
    assert not stale_wt_root.exists()
    assert sorted(list_branches(repo)) == sorted([f"agw/{run_id}/t1", f"agw/{stale_id}/x"])   # branches are kept
    assert len(run_git(["worktree", "list"], agw_env.repo).splitlines()) == 1
    assert runner.clean(repo, config) == "cleaned: 0 worktree dir(s), 0 abandoned run(s)"

    assert runner.clean(repo, config, stale_id) == f"removed run {stale_id}: 1 branch(es)"
    assert not stale_dir.exists() and f"agw/{stale_id}/x" not in list_branches(repo)

    assert runner.clean(repo, config, all_runs=True) == "removed 1 run(s), 1 branch(es)"
    assert list_branches(repo) == [] and runner.list_runs(repo) == []


def test_clean_refuses_alive_run_and_all_skips_it(agw_env: AgwEnv) -> None:
    repo, config = setup(agw_env)
    alive = fake_stale_run(repo, "20260101-000001-aaaa", pid=os.getpid())
    dead = fake_stale_run(repo, "20260101-000002-bbbb")
    with pytest.raises(ValueError, match="still running"):
        runner.clean(repo, config, "20260101-000001-aaaa")
    assert runner.clean(repo, config, all_runs=True) == "removed 1 run(s), 0 branch(es)"
    assert alive.exists() and not dead.exists()
    with pytest.raises(KeyError):
        runner.clean(repo, config, "nope")


def test_clean_removes_orphan_worktree_dirs_but_spares_fresh_ones(agw_env: AgwEnv) -> None:
    repo, config = setup(agw_env)
    base = pathlib.Path(config.worktree_dir) / runner.repo_slug(repo.main_root)
    old = base / "20250101-000000-aaaa" / "t"
    old.mkdir(parents=True)
    (old / "f").write_text("x", encoding="utf-8")
    os.utime(base / "20250101-000000-aaaa", (1, 1))
    fresh = base / "20261105-000000-bbbb"
    fresh.mkdir()                              # looks like a run that is just being created
    assert runner.clean(repo, config) == "cleaned: 1 worktree dir(s), 0 abandoned run(s)"
    assert not (base / "20250101-000000-aaaa").exists() and fresh.exists()


def test_clean_never_touches_a_live_runs_worktrees(agw_env: AgwEnv) -> None:
    repo, config = setup(agw_env)
    live_id = "20260101-000009-cccc"
    root = pathlib.Path(config.worktree_dir) / runner.repo_slug(repo.main_root) / live_id
    (root / "t").mkdir(parents=True)
    os.utime(root, (1, 1))
    fake_stale_run(repo, live_id, worktree_root=root, pid=os.getpid())
    assert runner.clean(repo, config) == "cleaned: 0 worktree dir(s), 0 abandoned run(s)"
    assert (root / "t").exists()


def test_start_of_run_cleans_stale_runs(agw_env: AgwEnv) -> None:
    repo, config = setup(agw_env)
    stale = fake_stale_run(repo, "20260101-000000-dead")
    go(agw_env, [read_task()])
    assert json.loads((stale / "run.json").read_text(encoding="utf-8"))["status"] == "abandoned"


def test_concurrent_runs_in_threads_do_not_interfere(agw_env: AgwEnv) -> None:
    agw_env.set_scenarios(default={"sleep": 0.5, "actions": [WRITE_APP], "structured_output": REPORT_DONE})
    repo, config = setup(agw_env)
    results: list = []

    def one(i: int) -> None:
        ts = TaskSet(tasks=[edit_task("t"), edit_task("u")])
        results.append(runner.execute_run(ts, config, repo, retry_delay=0))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(results) == 3 and len({r.run_id for r in results}) == 3
    for r in results:
        assert [t.status for t in r.tasks] == ["ok", "ok"], [t.reason for t in r.tasks]
        assert "MAIN_TREE_CHANGED" not in [f.code for f in r.flags]
    assert len(list_branches(repo)) == 6 and worktree_dirs(agw_env) == []
    assert len(run_git(["worktree", "list"], agw_env.repo).splitlines()) == 1


@posix_only
def test_worker_commit_and_revert_leaves_no_branch(agw_env: AgwEnv) -> None:
    sh = ("git -c user.name=w -c user.email=w@x commit -qam wip --allow-empty >/dev/null; echo x > app.py; "
          "git add -A; git -c user.name=w -c user.email=w@x commit -qm change; "
          "git -c user.name=w -c user.email=w@x revert --no-edit HEAD >/dev/null")
    agw_env.set_scenarios(default={"actions": [{"shell": sh}], "structured_output": REPORT_DONE})
    result, repo, _ = go(agw_env, [edit_task()])
    t = result.tasks[0]
    assert (t.status, t.reason, t.branch) == ("failed", "no changes", "")
    assert list_branches(repo) == [] and worktree_dirs(agw_env) == []


def test_unwritable_worktree_dir_is_a_task_error(agw_env: AgwEnv, tmp_path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    result, repo, _ = go(agw_env, [edit_task(), read_task()], cfg={"worktree_dir": str(blocker / "wt")})
    for t in result.tasks:
        assert t.status == "error" and t.reason.startswith("cannot create the worktree directory:")
    assert agw_env.calls() == [] and list_branches(repo) == []
    assert result.exit_code == EXIT_TASKS_FAILED
