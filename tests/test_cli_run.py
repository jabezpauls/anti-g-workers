"""Integration tests for the agw CLI: cli.main() in-process against a temp repo and the fake agy."""
from __future__ import annotations

import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest

from agw import cli, runner
from agw.gitops import list_branches, repo_info

from conftest import REPO_ROOT, AgwEnv, run_git

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX only")

REPORT_DONE = {"status": "done", "summary": "did it", "findings": "", "files_changed": ["app.py"],
               "blocked_actions": [], "concerns": []}
EDIT_OK = {"actions": [{"write": "app.py", "content": "def add(a, b):\n    return a + b  # edited\n"}],
           "structured_output": REPORT_DONE}
READ_REPORT = dict(REPORT_DONE, findings="app.py defines add()", files_changed=[])


def tasks_json(*tasks: dict, **top: object) -> str:
    return json.dumps({"tasks": list(tasks), **top})


EDIT = {"id": "fix", "mode": "edit", "title": "Fix app", "goal": "Edit app.py", "accept": []}
READ = {"id": "look", "goal": "Explain app.py"}


@pytest.fixture
def cli_env(agw_env: AgwEnv, monkeypatch: pytest.MonkeyPatch):
    agw_env.set_scenarios(tasks={"fix": EDIT_OK, "look": {"structured_output": READ_REPORT}})

    def call(*args: str, stdin: str | None = None) -> int:
        if stdin is not None:
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        return cli.main(list(args))

    agw_env.call = call            # type: ignore[attr-defined]
    return agw_env


def out_err(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    captured = capsys.readouterr()
    return captured.out, captured.err


def do_run(env, capsys, *tasks: dict, args: tuple[str, ...] = (), **top: object) -> tuple[int, str, str]:
    code = env.call("run", "-", *args, stdin=tasks_json(*tasks, **top))
    out, err = out_err(capsys)
    return code, out, err


# ---- basics ---------------------------------------------------------------------------------------------------------

def test_version(capsys) -> None:
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == "agw 0.1.0\n"


def test_help_and_usage_errors(capsys) -> None:
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for word in ("run", "show", "apply", "clean", "doctor", "config"):
        assert word in out
    assert cli.main([]) == 2
    assert "agw: error:" in capsys.readouterr().err
    assert cli.main(["frobnicate"]) == 2
    assert cli.main(["run", "--parallel", "many"]) == 2
    assert cli.main(["show", "r", "t", "--diff", "--log"]) == 2


# ---- run ------------------------------------------------------------------------------------------------------------

def test_run_ok(cli_env, capsys) -> None:
    code, out, err = do_run(cli_env, capsys, EDIT, READ)
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0].startswith("agw ") and "| 2 task(s): 2 ok |" in lines[0]
    assert "[ok] fix edit \"Fix app\"" in out and "[ok] look read" in out
    assert "findings:\n    app.py defines add()" in out
    assert out.rstrip("\n").splitlines()[-1].startswith("next: agw show ")
    assert out.count("\n\n") == 3        # header, 2 task blocks, footer are separated by single blank lines
    run_id = lines[0].split()[1]
    assert (runner.runs_root(repo_info(str(cli_env.repo))) / run_id / "summary.txt").read_text(encoding="utf-8") == out


def test_run_reads_tasks_from_a_file(cli_env, capsys, tmp_path) -> None:
    path = tmp_path / "tasks.json"
    path.write_text(tasks_json(READ), encoding="utf-8")
    assert cli_env.call("run", str(path)) == 0
    assert "[ok] look" in out_err(capsys)[0]
    assert cli_env.call("run", str(tmp_path / "missing.json")) == 2
    assert "file not found" in out_err(capsys)[1]


def test_run_exit_one_when_a_task_fails(cli_env, capsys) -> None:
    cli_env.set_scenarios(default={"structured_output": dict(READ_REPORT, status="partial")})
    code, out, _ = do_run(cli_env, capsys, READ)
    assert code == 1 and "[failed] look" in out


def test_run_progress_goes_to_stderr(cli_env, capsys) -> None:
    code, out, err = do_run(cli_env, capsys, READ, args=("--progress",))
    assert code == 0 and "[agw]" not in out
    assert err.splitlines()[0] == "[agw] look started" and err.splitlines()[1].startswith("[agw] look ok (")


def test_run_model_and_parallel_overrides(cli_env, capsys) -> None:
    code, _, _ = do_run(cli_env, capsys, READ, args=("--model", "gemini-9", "--parallel", "2"))
    assert code == 0
    argv = cli_env.calls()[0]["argv"]
    assert argv[argv.index("--model") + 1] == "gemini-9"
    assert cli_env.call("run", "-", "--parallel", "9", stdin=tasks_json(READ)) == 2
    assert "max_parallel" in out_err(capsys)[1]
    assert cli_env.call("run", "-", "--model", "", stdin=tasks_json(READ)) == 2


def test_invalid_tasks_json_lists_every_error(cli_env, capsys) -> None:
    code = cli_env.call("run", "-", stdin=json.dumps({"tasks": [{"id": "A b", "goal": ""}, {"mode": "x"}], "bogus": 1}))
    err = out_err(capsys)[1]
    assert code == 2
    assert "agw: error: bogus: unknown key" in err and "tasks[0].id" in err and "tasks[1].mode" in err
    assert err.count("agw: error:") >= 4
    assert cli_env.call("run", "-", stdin="{not json") == 2
    assert "invalid JSON" in out_err(capsys)[1]
    assert cli_env.calls() == []


def test_attempt_over_limit_is_a_usage_error(cli_env, capsys) -> None:
    code, _, err = do_run(cli_env, capsys, dict(READ, attempt=4))
    assert code == 2 and "tasks[0].attempt: 4 exceeds 1 + retry_limit (2)" in err
    assert cli_env.calls() == []
    code, _, _ = do_run(cli_env, capsys, dict(READ, attempt=3))
    assert code == 0


def test_invalid_config_is_a_usage_error(cli_env, capsys) -> None:
    cli_env.config_path.write_text('max_parallel = "many"\n', encoding="utf-8")
    code, _, err = do_run(cli_env, capsys, READ)
    assert code == 2 and "max_parallel" in err and err.startswith("agw: error: ")


def test_unknown_base_is_a_usage_error(cli_env, capsys) -> None:
    code, _, err = do_run(cli_env, capsys, READ, base="no-such-ref")
    assert code == 2 and "no-such-ref" in err


def test_not_a_git_repo_and_no_commits(cli_env, capsys, tmp_path, monkeypatch) -> None:
    elsewhere = tmp_path / "plain"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    code, _, err = do_run(cli_env, capsys, READ)
    assert code == 3 and "not a git repository; agw needs git (git init && git commit first)" in err
    run_git(["init", "-q"], elsewhere)
    code, _, err = do_run(cli_env, capsys, READ)
    assert code == 3 and "repository has no commits yet" in err


def test_agy_not_found(cli_env, capsys, monkeypatch) -> None:
    monkeypatch.setenv("AGW_AGY", "/nonexistent/agy")
    code, out, err = do_run(cli_env, capsys, READ)
    assert code == 3 and out == "" and "agy not found" in err and "Install the Antigravity CLI" in err
    assert not runner.runs_root(repo_info(str(cli_env.repo))).exists()


def test_works_from_a_subdirectory_and_a_linked_worktree(cli_env, capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(cli_env.repo / "tests")
    code, _, _ = do_run(cli_env, capsys, READ)
    assert code == 0
    linked = tmp_path / "linked"
    run_git(["worktree", "add", "-q", "--detach", str(linked)], cli_env.repo)
    monkeypatch.chdir(linked)
    code, out, _ = do_run(cli_env, capsys, EDIT)
    assert code == 0 and "[ok] fix" in out
    # the run record lives in the shared common dir, so it is visible from the main checkout too
    monkeypatch.chdir(cli_env.repo)
    assert cli_env.call("show") == 0
    assert out_err(capsys)[0].count("task(s)") == 2


# ---- show -----------------------------------------------------------------------------------------------------------

@pytest.fixture
def ran(cli_env, capsys):
    code, out, _ = do_run(cli_env, capsys, EDIT, READ)
    assert code == 0
    cli_env.run_id = out.split()[1]
    cli_env.summary = out
    return cli_env


def test_show_lists_and_prints_summary(ran, capsys) -> None:
    assert ran.call("show") == 0
    listing = out_err(capsys)[0]
    assert listing.startswith(ran.run_id) and "done" in listing and "2 task(s): 2 ok" in listing
    assert ran.call("show", "last") == 0
    assert out_err(capsys)[0] == ran.summary
    assert ran.call("show", ran.run_id) == 0
    assert out_err(capsys)[0] == ran.summary


def test_show_empty_and_unknown(cli_env, capsys) -> None:
    assert cli_env.call("show") == 0
    assert out_err(capsys)[0] == "no runs\n"
    assert cli_env.call("show", "last") == 2
    assert cli_env.call("show", "20990101-000000-ffff") == 2
    assert "agw: error: " in out_err(capsys)[1]


def test_show_run_without_summary(ran, capsys) -> None:
    run_dir = runner.runs_root(repo_info(str(ran.repo))) / ran.run_id
    (run_dir / "summary.txt").unlink()
    run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_json["status"] = "running"
    run_json["pid"] = os.getpid()
    (run_dir / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    assert ran.call("show", ran.run_id) == 0
    assert out_err(capsys)[0] == f"run {ran.run_id}: running (no summary yet)\n"


def test_show_task_views(ran, capsys) -> None:
    rid = ran.run_id
    assert ran.call("show", rid, "fix") == 0
    detail = out_err(capsys)[0]
    assert detail.startswith("task fix (edit) status ok") and "M app.py" in detail
    assert ran.call("show", "last", "fix", "--diff") == 0
    diff = out_err(capsys)[0]
    assert diff.startswith("diff --git a/app.py b/app.py") and "+    return a + b  # edited" in diff
    assert ran.call("show", rid, "look", "--diff") == 0
    assert out_err(capsys)[0] == "(no diff)\n"
    assert ran.call("show", rid, "fix", "--brief") == 0
    brief = out_err(capsys)[0]
    assert brief.startswith("# Worker brief\n\nTask ID: fix\n") and brief == next(c for c in ran.calls() if c["task_id"] == "fix")["stdin"]
    assert ran.call("show", rid, "fix", "--events") == 0
    assert out_err(capsys)[0] == "(no tool calls)\n"
    assert ran.call("show", rid, "look", "--report") == 0
    assert json.loads(out_err(capsys)[0])["findings"] == "app.py defines add()"
    assert ran.call("show", rid, "fix", "--log") == 0
    assert out_err(capsys)[0] == "(no acceptance log)\n"


@posix_only
def test_show_log_and_events_with_data(cli_env, capsys) -> None:
    cli_env.set_scenarios(default={"fixture": "write-and-run.ndjson", **EDIT_OK})
    code, out, _ = do_run(cli_env, capsys, dict(EDIT, accept=["echo hello; exit 0"]))
    assert code == 0
    assert cli_env.call("show", "last", "fix", "--log") == 0
    log = out_err(capsys)[0]
    assert "$ echo hello; exit 0\nhello\n[exit 0]" in log
    assert cli_env.call("show", "last", "fix", "--events") == 0
    events = out_err(capsys)[0]
    assert "write_file" in events and "run_command ls -la [0]" in events and "python3 a.py [0]" in events


def test_show_unknown_task_and_misuse(ran, capsys) -> None:
    assert ran.call("show", ran.run_id, "nope") == 2
    assert ran.call("show", ran.run_id, "../fix") == 2
    assert ran.call("show", ran.run_id, "--diff") == 2          # flags need a task
    err = out_err(capsys)[1]
    assert err.count("agw: error: ") >= 2


# ---- apply ----------------------------------------------------------------------------------------------------------

def test_apply_edit_task(ran, capsys) -> None:
    assert ran.call("apply", "last", "fix") == 0
    out, _ = out_err(capsys)
    assert out == "applied fix (1 file) to working tree; not committed\n"
    assert "+    return a + b  # edited" in run_git(["diff"], ran.repo)
    assert run_git(["log", "--oneline"], ran.repo).count("\n") == 1          # nothing committed
    assert ran.call("apply", "last", "fix") == 1                              # already applied: fails
    out, err = out_err(capsys)
    assert out.startswith("conflict applying fix:\n  ") or err.startswith("conflict applying fix:\n  ")


def test_apply_conflict_reports_git_output_and_marker_hint(ran, capsys) -> None:
    (ran.repo / "app.py").write_text("def add(a, b):\n    return a * b\n", encoding="utf-8")
    run_git(["commit", "-qam", "conflicting change"], ran.repo)
    assert ran.call("apply", "last", "fix") == 1
    text = "".join(out_err(capsys))
    assert text.startswith("conflict applying fix:\n  ")
    assert "git status" in text and "conflict markers" in text
    assert "<<<<<<<" in (ran.repo / "app.py").read_text(encoding="utf-8")


def test_apply_three_way_message(ran, capsys) -> None:
    # An unrelated committed change next to the edited line makes the plain apply fail but the 3-way succeed.
    cfg = ran.repo / "app.py"
    cfg.write_text("# header\ndef add(a, b):\n    return a + b\n", encoding="utf-8")
    run_git(["commit", "-qam", "add header"], ran.repo)
    code = ran.call("apply", "last", "fix")
    out, _ = out_err(capsys)
    assert code == 0 and out in ("applied fix (1 file) to working tree; not committed\n",
                                 "applied fix with 3-way merge; changes are staged; not committed\n")


def test_apply_errors(ran, capsys) -> None:
    assert ran.call("apply", "last", "look") == 2                 # read task
    assert "read task" in out_err(capsys)[1]
    assert ran.call("apply", "last", "nope") == 2
    assert ran.call("apply", "nope", "fix") == 2
    run_dir = runner.runs_root(repo_info(str(ran.repo))) / ran.run_id
    (run_dir / "fix" / "diff.patch").unlink()
    assert ran.call("apply", "last", "fix") == 2
    assert "diff.patch" in out_err(capsys)[1]


def test_apply_goes_to_the_current_checkout(ran, capsys, monkeypatch, tmp_path) -> None:
    linked = tmp_path / "linked"
    run_git(["worktree", "add", "-q", "--detach", str(linked)], ran.repo)
    monkeypatch.chdir(linked / "tests")
    assert ran.call("apply", "last", "fix") == 0
    assert "edited" in (linked / "app.py").read_text(encoding="utf-8")
    assert "edited" not in (ran.repo / "app.py").read_text(encoding="utf-8")


# ---- clean ----------------------------------------------------------------------------------------------------------

def test_clean_run_and_all(ran, capsys) -> None:
    repo = repo_info(str(ran.repo))
    assert list_branches(repo) == [f"agw/{ran.run_id}/fix"]
    assert ran.call("clean", "last") == 0
    assert out_err(capsys)[0] == f"removed run {ran.run_id}: 1 branch(es)\n"
    assert list_branches(repo) == [] and runner.list_runs(repo) == []
    assert ran.call("clean", "--all") == 0
    assert out_err(capsys)[0] == "removed 0 run(s), 0 branch(es)\n"
    assert ran.call("clean") == 0
    assert out_err(capsys)[0] == "cleaned: 0 worktree dir(s), 0 abandoned run(s)\n"


def test_clean_all_and_errors(ran, capsys) -> None:
    do_run(ran, capsys, EDIT)
    assert ran.call("clean", "--all") == 0
    assert out_err(capsys)[0] == "removed 2 run(s), 2 branch(es)\n"
    assert ran.call("clean", "nope") == 2
    assert ran.call("clean", "last") == 2
    assert ran.call("clean", ran.run_id, "--all") == 2


def test_clean_refuses_a_running_run(ran, capsys) -> None:
    run_dir = runner.runs_root(repo_info(str(ran.repo))) / ran.run_id
    data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    data.update(status="running", pid=os.getpid())
    (run_dir / "run.json").write_text(json.dumps(data), encoding="utf-8")
    assert ran.call("clean", ran.run_id) == 2
    assert "still running" in out_err(capsys)[1] and run_dir.exists()


# ---- config / doctor ------------------------------------------------------------------------------------------------

def test_config_prints_eleven_lines(cli_env, capsys) -> None:
    assert cli_env.call("config") == 0
    out = out_err(capsys)[0]
    lines = out.splitlines()
    assert len(lines) == 11 and lines[0].startswith("model = ") and lines[0].endswith("# default")
    assert any(line.startswith("agy_path = ") and str(cli_env.config_path) in line for line in lines)


def test_config_invalid_and_outside_repo(cli_env, capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli_env.call("config") == 0
    assert len(out_err(capsys)[0].splitlines()) == 11
    cli_env.config_path.write_text("bogus = 1\n", encoding="utf-8")
    assert cli_env.call("config") == 2
    assert "bogus: unknown key" in out_err(capsys)[1]


def test_project_config_is_used(cli_env, capsys) -> None:
    (cli_env.repo / ".agw.toml").write_text('timeout_seconds = 300\n', encoding="utf-8")
    assert cli_env.call("config") == 0
    assert f"timeout_seconds = 300  # {cli_env.repo}/.agw.toml" in out_err(capsys)[0]


def test_doctor(cli_env, capsys) -> None:
    assert cli_env.call("doctor") == 0
    lines = out_err(capsys)[0].splitlines()
    assert [ln.split(":")[0] for ln in lines] == [
        "ok   python", "ok   git", "ok   agy", "ok   config", "ok   repo", "ok   worktree_dir", "ok   deny"]
    assert "1.5.0" in lines[2] and str(cli_env.config_path) in lines[3] and str(cli_env.repo) in lines[4]


def test_doctor_live_with_fake(cli_env, capsys) -> None:
    assert cli_env.call("doctor", "--live") == 0
    lines = out_err(capsys)[0].splitlines()
    assert lines[-1].startswith("ok   live: ") and len(lines) == 8


def test_doctor_failures(cli_env, capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGW_AGY", "/nonexistent/agy")
    assert cli_env.call("doctor") == 3
    out = out_err(capsys)[0]
    assert "FAIL agy: agy not found" in out
    monkeypatch.delenv("AGW_AGY")
    cli_env.config_path.write_text("model = 5\n", encoding="utf-8")
    assert cli_env.call("doctor") == 3
    assert "FAIL config: " in out_err(capsys)[0]


def test_doctor_outside_a_repo_only_warns(cli_env, capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli_env.call("doctor") == 0
    out = out_err(capsys)[0]
    assert "warn repo: not inside a git repository" in out


def test_doctor_warns_when_worktrees_live_inside_the_repo(cli_env, capsys) -> None:
    cli_env.write_config(worktree_dir=str(cli_env.repo / ".wt"))
    assert cli_env.call("doctor") == 0
    assert "warn deny: " in out_err(capsys)[0]


def test_doctor_live_failure_reports_the_kind(cli_env, capsys) -> None:
    cli_env.set_scenarios(default={"exit_code": 1, "result_status": "ERROR", "error": "Quota exceeded",
                                   "structured_output": None})
    assert cli_env.call("doctor", "--live") == 3
    assert "FAIL live: quota: Quota exceeded" in out_err(capsys)[0]


# ---- real subprocess ------------------------------------------------------------------------------------------------

AGW = [sys.executable, str(REPO_ROOT / "bin" / "agw")]


def test_bin_agw_version_and_run(cli_env) -> None:
    proc = subprocess.run([*AGW, "--version"], capture_output=True, text=True, timeout=60)
    assert (proc.returncode, proc.stdout) == (0, "agw 0.1.0\n")
    proc = subprocess.run([*AGW, "run", "-"], input=tasks_json(EDIT, READ), capture_output=True, text=True,
                          cwd=cli_env.repo, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("agw ") and "[ok] fix" in proc.stdout and "[ok] look" in proc.stdout
    proc = subprocess.run([*AGW, "show", "last", "fix", "--diff"], capture_output=True, text=True,
                          cwd=cli_env.repo, timeout=60)
    assert proc.returncode == 0 and proc.stdout.startswith("diff --git")
    proc = subprocess.run([sys.executable, "-m", "agw", "--version"], capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}, timeout=60)
    assert proc.stdout == "agw 0.1.0\n"


def test_bin_agw_usage_error_exit_code(cli_env) -> None:
    proc = subprocess.run([*AGW, "run", "-"], input="{", capture_output=True, text=True, cwd=cli_env.repo, timeout=60)
    assert proc.returncode == 2 and "invalid JSON" in proc.stderr and proc.stdout == ""


def fake_agy_processes(env: AgwEnv) -> list[int]:
    """Pids of running fake agy processes whose cwd is below this test's worktree dir."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cwd = os.readlink(f"/proc/{entry}/cwd")
            cmd = pathlib.Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue
        if b"fake_agy.py" in cmd and cwd.startswith(str(env.worktree_dir)):
            found.append(int(entry))
    return found


@posix_only
@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_signal_stops_workers_cleans_up_and_prints_summary(cli_env, sig) -> None:
    cli_env.set_scenarios(default={"sleep": 60, "actions": EDIT_OK["actions"]})
    proc = subprocess.Popen([*AGW, "run", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=cli_env.repo)
    try:
        proc.stdin.write(tasks_json(EDIT, dict(EDIT, id="fix2"), READ, dict(READ, id="look2")))
        proc.stdin.close()
        proc.stdin = None      # Python 3.11's communicate() flushes a closed stdin
        deadline = time.monotonic() + 30
        while len(cli_env.calls()) < 3 and time.monotonic() < deadline:    # max_parallel is 3
            time.sleep(0.1)
        assert len(cli_env.calls()) >= 3, "workers did not start"
        assert fake_agy_processes(cli_env)
        proc.send_signal(sig)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130, err
    assert out.startswith("agw ") and "interrupted" in out
    assert out.count("[error]") + out.count("[skipped]") == 4
    assert "not started: interrupted" in out
    assert fake_agy_processes(cli_env) == []
    assert list(cli_env.worktree_dir.glob("*/*")) == []
    assert run_git(["worktree", "list"], cli_env.repo).count("\n") == 1
    repo = repo_info(str(cli_env.repo))
    run_json = json.loads((runner.runs_root(repo) / out.split()[1] / "run.json").read_text(encoding="utf-8"))
    assert run_json["status"] == "interrupted" and run_json["exit_code"] == 130
    assert run_git(["status", "--porcelain"], cli_env.repo) == ""


@posix_only
def test_sigint_while_waiting_for_stdin_exits_130(cli_env) -> None:
    proc = subprocess.Popen([*AGW, "run", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=cli_env.repo)
    time.sleep(1.0)
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 130 and out == ""
    assert cli_env.calls() == []


@posix_only
def test_two_concurrent_agw_processes_on_one_repo(cli_env) -> None:
    cli_env.set_scenarios(default={"sleep": 1, "actions": EDIT_OK["actions"], "structured_output": REPORT_DONE})
    procs = [subprocess.Popen([*AGW, "run", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, cwd=cli_env.repo) for _ in range(3)]
    for i, proc in enumerate(procs):
        proc.stdin.write(tasks_json(dict(EDIT, id=f"a{i}"), dict(EDIT, id=f"b{i}")))
        proc.stdin.close()
        proc.stdin = None      # Python 3.11's communicate() flushes a closed stdin
    outs = [proc.communicate(timeout=120) for proc in procs]
    run_ids = set()
    for proc, (out, err) in zip(procs, outs):
        assert proc.returncode == 1, err               # exit 1: the two tasks of a run touch the same file (OVERLAP)
        assert "! OVERLAP: app.py" in out and "MAIN_TREE_CHANGED" not in out and out.count("[ok]") == 2
        run_ids.add(out.split()[1])
    assert len(run_ids) == 3
    repo = repo_info(str(cli_env.repo))
    assert len(list_branches(repo)) == 6 and list(cli_env.worktree_dir.glob("*/*")) == []
    assert run_git(["worktree", "list"], cli_env.repo).count("\n") == 1
    assert cli_env.call("clean", "--all") == 0
    assert list_branches(repo) == []


@posix_only
@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
def test_sigkilled_run_is_abandoned_and_cleaned_up_later(cli_env, capsys) -> None:
    cli_env.set_scenarios(default={"sleep": 4, "actions": EDIT_OK["actions"]})
    proc = subprocess.Popen([*AGW, "run", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=cli_env.repo)
    proc.stdin.write(tasks_json(EDIT))
    proc.stdin.close()
    proc.stdin = None      # Python 3.11's communicate() flushes a closed stdin
    deadline = time.monotonic() + 30
    while not cli_env.calls() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert cli_env.calls()
    proc.kill()
    proc.communicate(timeout=30)
    repo = repo_info(str(cli_env.repo))
    assert list(cli_env.worktree_dir.glob("*/*"))                           # the dead run left its worktree behind
    assert [r["status"] for r in runner.list_runs(repo)] == ["abandoned"]    # ... and is shown as abandoned
    assert cli_env.call("clean") == 0
    assert out_err(capsys)[0] == "cleaned: 1 worktree dir(s), 1 abandoned run(s)\n"
    assert list(cli_env.worktree_dir.glob("*/*")) == []
    assert run_git(["worktree", "list"], cli_env.repo).count("\n") == 1
    deadline = time.monotonic() + 15                                         # the orphaned fake agy ends by itself
    while fake_agy_processes(cli_env) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert cli_env.call("clean", "--all") == 0
    assert list_branches(repo) == []


# ---- review fixes ---------------------------------------------------------------------------------------------------

@posix_only
def test_show_log_includes_setup_and_agy_stderr(cli_env, capsys) -> None:
    cli_env.write_config(setup=["echo setup-ran"])
    cli_env.set_scenarios(default={**EDIT_OK, "stderr": "warning: model chatter\n"})
    code, _, _ = do_run(cli_env, capsys, dict(EDIT, accept=["echo accepted"]))
    assert code == 0
    assert cli_env.call("show", "last", "fix", "--log") == 0
    log = out_err(capsys)[0]
    assert "== setup ==\n$ echo setup-ran\nsetup-ran\n" in log
    assert "== acceptance ==\n$ echo accepted" in log
    assert "== agy stderr ==\nwarning: model chatter\n" in log
    assert log.index("== setup ==") < log.index("== acceptance ==") < log.index("== agy stderr ==")


def test_show_log_truncates_long_logs_and_skips_empty_ones(cli_env, capsys) -> None:
    code, _, _ = do_run(cli_env, capsys, EDIT)
    assert code == 0
    task_dir = runner.runs_root(repo_info(str(cli_env.repo))) / os.listdir(
        runner.runs_root(repo_info(str(cli_env.repo))))[0] / "fix"
    (task_dir / "agy.stderr.txt").write_text("".join(f"line {i}\n" for i in range(1000)), encoding="utf-8")
    assert cli_env.call("show", "last", "fix", "--log") == 0
    log = out_err(capsys)[0]
    assert log.startswith("== agy stderr ==\n[... 800 earlier lines omitted ...]\n") and "line 999\n" in log
    assert "== setup ==" not in log and "line 0\n" not in log


def test_apply_pluralizes_file_counts(cli_env, capsys) -> None:
    two = {"actions": [{"write": "app.py", "content": "x = 1\n"}, {"write": "b.py", "content": "y = 2\n"}],
           "structured_output": REPORT_DONE}
    cli_env.set_scenarios(tasks={"fix": two})
    do_run(cli_env, capsys, EDIT)
    assert cli_env.call("apply", "last", "fix") == 0
    assert out_err(capsys)[0] == "applied fix (2 files) to working tree; not committed\n"


def test_clean_force_flag(ran, capsys) -> None:
    run_dir = runner.runs_root(repo_info(str(ran.repo))) / ran.run_id
    data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    data.update(status="running", pid=os.getpid())
    (run_dir / "run.json").write_text(json.dumps(data), encoding="utf-8")
    assert ran.call("clean", ran.run_id) == 2
    out_err(capsys)
    assert ran.call("clean", ran.run_id, "--force") == 0
    out, err = out_err(capsys)
    assert out.startswith(f"removed run {ran.run_id}") and "--force" in err and not run_dir.exists()


def test_doctor_fails_on_an_insecure_worktree_dir(cli_env, capsys) -> None:
    cli_env.worktree_dir.chmod(0o755)
    assert cli_env.call("doctor") == 3
    assert "FAIL worktree_dir: " in out_err(capsys)[0]


def test_run_refuses_an_insecure_worktree_dir(cli_env, capsys) -> None:
    cli_env.worktree_dir.chmod(0o777)
    code, _, err = do_run(cli_env, capsys, READ)
    assert code == 2 and "agw: error: worktree_dir is not safe to use" in err


def test_bin_agw_is_executable_in_the_working_tree_and_in_git() -> None:
    assert os.access(REPO_ROOT / "bin" / "agw", os.X_OK)
    listing = subprocess.run(["git", "ls-files", "-s", "bin/agw"], cwd=REPO_ROOT, capture_output=True, text=True)
    assert listing.stdout.startswith("100755")
