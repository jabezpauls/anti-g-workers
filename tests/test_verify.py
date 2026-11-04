"""Tests for acceptance running, report normalization, path/scope helpers, flags and status decisions."""
from __future__ import annotations

import os
import sys

import pytest

from agw import models as m
from agw.models import (AcceptResult, AgyOutcome, DiffStat, Report, Task, TaskResult, ToolCall)
from agw.verify import (decide_status, is_suspicious_command, matches_scope, normalize_rel,
                        normalize_report, overlap_flags, path_params, path_within, run_acceptance,
                        task_flags)

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX shell syntax")


# ---- run_acceptance -----------------------------------------------------------------------------

@posix_only
def test_run_acceptance_codes_tail_and_log(tmp_path) -> None:
    log = tmp_path / "accept.log"
    results = run_acceptance(["exit 0", "echo hi; exit 3"], str(tmp_path), 30, str(log), dict(os.environ))
    assert [r.exit_code for r in results] == [0, 3]
    assert results[1].tail == ["hi"]
    assert results[1].command == "echo hi; exit 3"
    text = log.read_text(encoding="utf-8")
    assert "$ exit 0\n" in text and "$ echo hi; exit 3\nhi\n[exit 3] (" in text
    assert text.count("[exit ") == 2 and text.endswith("\n\n")


@posix_only
def test_run_acceptance_runs_all_commands_even_after_failure(tmp_path) -> None:
    results = run_acceptance(["exit 1", "echo second"], str(tmp_path), 30, str(tmp_path / "l"), dict(os.environ))
    assert [r.exit_code for r in results] == [1, 0]
    assert results[1].tail == ["second"]


@posix_only
def test_run_acceptance_timeout_kills_children_and_logs(tmp_path) -> None:
    log = tmp_path / "accept.log"
    import time
    t0 = time.monotonic()
    # A background child keeps stdout open; the whole process group must still be reaped promptly.
    results = run_acceptance(["echo before; sleep 30 & sleep 30"], str(tmp_path), 1, str(log), dict(os.environ))
    assert time.monotonic() - t0 < 10
    assert results[0].exit_code == -1
    assert results[0].tail[-1] == "(timed out after 1s)"
    assert "before" in results[0].tail
    assert "(timed out after 1s)" in log.read_text(encoding="utf-8")


@posix_only
def test_run_acceptance_truncates_huge_output(tmp_path) -> None:
    log = tmp_path / "accept.log"
    cmd = f"{sys.executable} -c \"print('x' * 100 + chr(10)) ; [print('line', i) for i in range(200000)]\""
    results = run_acceptance([cmd], str(tmp_path), 60, str(log), dict(os.environ))
    assert results[0].exit_code == 0
    assert results[0].tail[-1] == "line 199999"
    assert len(results[0].tail) == m.TAIL_LINES
    assert log.stat().st_size < 600_000
    assert "truncated" in log.read_text(encoding="utf-8")


@posix_only
def test_run_acceptance_passes_env_and_cwd(tmp_path) -> None:
    env = dict(os.environ, AGW_TASK_ID="tid")
    results = run_acceptance(['echo "$AGW_TASK_ID"; pwd'], str(tmp_path), 30, str(tmp_path / "l"), env)
    assert results[0].tail[0] == "tid"
    assert os.path.realpath(results[0].tail[1]) == os.path.realpath(str(tmp_path))


@posix_only
def test_run_acceptance_stdin_is_closed(tmp_path) -> None:
    results = run_acceptance(["cat; echo done"], str(tmp_path), 5, str(tmp_path / "l"), dict(os.environ))
    assert results[0].exit_code == 0 and results[0].tail == ["done"]


def test_run_acceptance_empty_list(tmp_path) -> None:
    assert run_acceptance([], str(tmp_path), 5, str(tmp_path / "l"), {}) == []


# ---- normalize_report ---------------------------------------------------------------------------

def test_normalize_report_valid() -> None:
    rep = normalize_report({"status": "done", "summary": "s", "findings": "f", "files_changed": ["a"],
                            "blocked_actions": ["b"], "concerns": ["c"]}, "ignored")
    assert rep == Report("done", "s", "f", ["a"], ["b"], ["c"])


def test_normalize_report_coerces_fields() -> None:
    rep = normalize_report({"status": "partial", "summary": 5, "findings": ["x"], "files_changed": "a.py",
                            "blocked_actions": [1, 2], "concerns": None}, "")
    assert rep.status == "partial"
    assert rep.summary == "5" and rep.findings == "['x']"
    assert rep.files_changed == [] and rep.blocked_actions == ["1", "2"] and rep.concerns == []


def test_normalize_report_missing_fields_default_empty() -> None:
    rep = normalize_report({"status": "blocked"}, "")
    assert rep == Report("blocked", "", "", [], [], [])


@pytest.mark.parametrize("structured", [None, "text", [1], 5, {"status": "weird"}, {"summary": "no status"}])
def test_normalize_report_missing_keeps_response_as_findings(structured: object) -> None:
    rep = normalize_report(structured, "  the raw answer \n")
    assert rep == Report("missing", "", "the raw answer", [], [], [])


def test_normalize_report_none_value_becomes_empty_string() -> None:
    assert normalize_report({"status": "done", "summary": None}, "").summary == ""


# ---- normalize_rel / path_within ----------------------------------------------------------------

def test_normalize_rel(tmp_path) -> None:
    wt = str(tmp_path / "wt")
    assert normalize_rel(os.path.join(wt, "src", "a.py"), wt) == "src/a.py"
    assert normalize_rel("./x.py", wt) == "x.py"
    assert normalize_rel(".//./x.py", wt) == "x.py"
    assert normalize_rel("src\\b.py", wt) == "src/b.py"
    outside = os.path.join(str(tmp_path), "elsewhere", "c.py")
    assert normalize_rel(outside, wt) == outside.replace("\\", "/")
    assert normalize_rel("plain.txt", wt) == "plain.txt"


def test_path_within(tmp_path) -> None:
    main = str(tmp_path / "main")
    assert path_within(main, main)
    assert path_within(main + "/sub/f", main)
    assert not path_within(str(tmp_path / "main2" / "z"), main)
    assert not path_within(str(tmp_path), main)
    assert not path_within(main + "/../main2", main)


def test_path_within_resolves_symlinks(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert path_within(str(link / "f"), str(real))
    assert path_within(str(real / "f"), str(link))


# ---- matches_scope ------------------------------------------------------------------------------

def test_matches_scope_rules(tmp_path) -> None:
    assert matches_scope("src/a/b.py", ["src/"])
    assert not matches_scope("srcx/a.py", ["src/"])
    assert matches_scope("src/a/b.py", ["src/**/*.py"])
    assert matches_scope("src/b.py", ["src/**/*.py"])
    assert not matches_scope("src/b.txt", ["src/**/*.py"])
    assert matches_scope("README.md", ["*.md"])
    assert matches_scope("docs/deep/README.md", ["*.md"])         # fnmatch: * crosses /
    assert matches_scope("a/b.py", ["x.py", "a/b.py"])
    assert not matches_scope("a/c.py", ["a/b.py"])
    assert matches_scope("src/x/y.py", ["src/**"])
    assert not matches_scope("anything", [])
    assert matches_scope("./src/a.py", ["./src/a.py"])


def test_matches_scope_existing_directory_without_slash(tmp_path) -> None:
    (tmp_path / "pkg").mkdir()
    assert matches_scope("pkg/mod.py", ["pkg"], str(tmp_path))
    assert not matches_scope("pkg/mod.py", ["pkg"])                 # no worktree: plain fnmatch
    assert not matches_scope("pkgx/mod.py", ["pkg"], str(tmp_path))
    assert matches_scope("pkg", ["pkg"], str(tmp_path))


# ---- is_suspicious_command ----------------------------------------------------------------------

WT = "/tmp/agwtest/wt/run/task"
TEMP = "/tmp/agwtest/systmp"


@pytest.mark.parametrize("cmd", [
    "echo x > /etc/passwd",
    "echo x>/etc/passwd",
    "rm -rf ~/foo",
    "cp a ../../outside.txt",
    "git push origin main",
    "git -C /other commit -m x",
    "sed -i s/a/b/ /home/u/.bashrc",
    "git reset --hard",
    "cd /tmp/x && git checkout main",
    "tee /var/log/x < in.txt",
])
def test_suspicious_true(cmd: str) -> None:
    assert is_suspicious_command(cmd, WT, TEMP)


@pytest.mark.parametrize("cmd", [
    "pytest -q",
    "ls -la /usr/lib",
    "cat /etc/hosts",
    "python3 a.py 2>/dev/null",
    "echo hi > out.txt",
    "rm build/x.o",
    f"cp a.txt {TEMP}/x",
    f"echo hi > {WT}/sub/f.txt",
    "git status",
    "git diff HEAD~1",
    "git log --oneline",
    "mkdir -p sub/dir && touch sub/dir/x",
    "echo 'unterminated > out.txt",          # shlex ValueError falls back to split()
])
def test_suspicious_false(cmd: str) -> None:
    assert not is_suspicious_command(cmd, WT, TEMP)


def test_suspicious_handles_unbalanced_quote_with_outside_path() -> None:
    assert is_suspicious_command("echo 'oops > /etc/passwd", WT, TEMP)


# ---- path_params --------------------------------------------------------------------------------

def test_path_params() -> None:
    call = ToolCall(1, "write_file", {"Path": "/a", "TargetFile": "/b", "Other": "/c", "File": 5, "content": "x"})
    assert sorted(path_params(call)) == ["/a", "/b"]
    assert path_params(ToolCall(2, "x", {})) == []


# ---- task_flags ---------------------------------------------------------------------------------

def _flags(**over):
    base = dict(
        task=Task(id="t", goal="g", mode="edit"), worktree="/w/base/t", worktree_base="/w/base",
        temp_root="/sys/tmp", brain_dir="/home/u/.gemini/antigravity-cli/brain", changes=["a.py"],
        tool_calls=[], report=Report("done", "s", files_changed=["a.py"]),
        diff=DiffStat([("M", "a.py")], 1, 1), worker_ran_ok=True)
    base.update(over)
    return task_flags(**base)


def _codes(flags) -> list[str]:
    return [f.code for f in flags]


def test_clean_edit_has_no_flags() -> None:
    assert _flags() == []


def test_out_of_scope_write_detail_and_cap() -> None:
    calls = [ToolCall(i, "write_file", {"Path": f"/home/u/f{i}"}) for i in range(5)]
    flags = _flags(tool_calls=calls)
    assert _codes(flags) == [m.OUT_OF_SCOPE_WRITE]
    assert flags[0].detail == "/home/u/f0, /home/u/f1, /home/u/f2 +2 more"


def test_out_of_scope_write_variants() -> None:
    inside = [ToolCall(1, "write_file", {"Path": "/w/base/t/src/x.py"}),
              ToolCall(2, "edit_file", {"TargetFile": "rel/y.py"}),
              ToolCall(3, "read_file", {"Path": "/etc/hosts"}),
              ToolCall(4, "grep_search", {"SearchPath": "/etc"}),
              ToolCall(5, "run_command", {"CommandLine": "ls", "Cwd": "/etc", "Path": "/etc"}),
              ToolCall(6, "write_file", {"Path": "/home/u/.gemini/antigravity-cli/brain/c1/plan.md"}),
              ToolCall(7, "write_file", {"Path": "/sys/tmp/scratch.txt"})]
    assert _flags(tool_calls=inside) == []


def test_out_of_scope_write_other_workers_tree_under_temp_is_flagged() -> None:
    for worktree_base in ("/sys/tmp/agw-u/repo/run", "/sys/tmp/agw-u"):
        flags = _flags(worktree=worktree_base + "/mine", worktree_base=worktree_base, temp_root="/sys/tmp",
                       tool_calls=[ToolCall(1, "write_file", {"Path": worktree_base + "/other-task/x"})])
        assert _codes(flags) == [m.OUT_OF_SCOPE_WRITE]


def test_out_of_scope_write_unknown_tool_with_path_counts_as_write() -> None:
    flags = _flags(tool_calls=[ToolCall(1, "brand_new_tool", {"file_path": "/home/u/z"})])
    assert _codes(flags) == [m.OUT_OF_SCOPE_WRITE]


def test_out_of_scope_write_main_tree_path() -> None:
    flags = _flags(tool_calls=[ToolCall(1, "edit_file", {"Path": "/home/u/proj/app.py"})])
    assert flags[0].code == m.OUT_OF_SCOPE_WRITE


def test_suspicious_command_detail() -> None:
    calls = [ToolCall(i, "run_command", {"CommandLine": c}) for i, c in
             enumerate(["pytest", "git push origin main", "x " * 60 + "&& git commit -m y", "git reset --hard"])]
    flags = _flags(tool_calls=calls)
    assert _codes(flags) == [m.SUSPICIOUS_COMMAND]
    parts = flags[0].detail.split(" +")[0].split(", ")
    assert parts[0] == "git push origin main"
    assert len(parts[1]) == 72 and parts[1].endswith("...")
    assert flags[0].detail.endswith(" +1 more")


def test_suspicious_command_multiline_is_one_line() -> None:
    flags = _flags(tool_calls=[ToolCall(1, "run_command", {"CommandLine": "git push\norigin main"})])
    assert "\n" not in flags[0].detail


def test_read_mode_changes() -> None:
    flags = _flags(task=Task(id="t", goal="g"), changes=[f"p{i}" for i in range(7)], diff=None,
                   report=Report("done", "s"))
    assert _codes(flags) == [m.READ_MODE_CHANGES]
    assert flags[0].detail == "p0, p1, p2, p3, p4 +2 more"


def test_scope_violation() -> None:
    task = Task(id="t", goal="g", mode="edit", scope=["src/"])
    flags = _flags(task=task, changes=["src/a.py", "setup.cfg", "docs/x.md"],
                   report=Report("done", "s", files_changed=["src/a.py"]), diff=DiffStat([("M", "x")], 1, 1))
    assert _codes(flags) == [m.SCOPE_VIOLATION]
    assert flags[0].detail == "setup.cfg, docs/x.md"


def test_scope_violation_none_when_scope_empty() -> None:
    assert m.SCOPE_VIOLATION not in _codes(_flags(changes=["anything.txt"],
                                                  report=Report("done", "s", files_changed=["anything.txt"])))


def test_blocked_actions() -> None:
    flags = _flags(report=Report("done", "s", files_changed=["a.py"], blocked_actions=["x", "y"]))
    assert [(f.code, f.detail) for f in flags] == [(m.BLOCKED_ACTIONS, "2 action(s)")]


def test_no_report_only_when_worker_ran_ok() -> None:
    flags = _flags(report=Report("missing"))
    assert [(f.code, f.detail) for f in flags] == [(m.NO_REPORT, "worker returned no structured report")]
    assert _flags(report=Report("missing"), worker_ran_ok=False) == []


def test_report_mismatch() -> None:
    flags = _flags(report=Report("done", "s", files_changed=["a.py", "./ghost.py", "/w/base/t/ghost2.py"]))
    assert [(f.code, f.detail) for f in flags] == [
        (m.REPORT_MISMATCH, "claimed but unchanged: ghost.py, ghost2.py")]


def test_report_mismatch_only_for_done_edit() -> None:
    partial = Report("partial", "s", files_changed=["ghost.py"])
    assert _flags(report=partial) == []
    read = _flags(task=Task(id="t", goal="g"), changes=[], diff=None, report=Report("done", "s", files_changed=["g"]))
    assert m.REPORT_MISMATCH not in _codes(read)


def test_no_changes_info() -> None:
    flags = _flags(changes=[], diff=DiffStat(), report=Report("done", "s"))
    assert [(f.code, f.detail) for f in flags] == [(m.NO_CHANGES, "edit task produced no changes")]
    assert flags[0].level == "info"
    assert _flags(changes=[], diff=DiffStat(), report=Report("blocked", "s")) == []
    assert _flags(changes=[], diff=DiffStat(), report=Report("done", "s"), worker_ran_ok=False) == []


def test_large_diff_threshold() -> None:
    at = _flags(diff=DiffStat([("M", "a.py")], 300, 100))
    assert at == []
    over = _flags(diff=DiffStat([("M", "a.py")], 300, 101))
    assert [(f.code, f.detail) for f in over] == [(m.LARGE_DIFF, "401 lines changed")]


def test_deleted_files() -> None:
    files = [("D", f"d{i}.py") for i in range(6)] + [("M", "a.py")]
    flags = _flags(diff=DiffStat(files, 0, 10), changes=[p for _, p in files],
                   report=Report("done", "s", files_changed=[p for _, p in files]))
    assert [(f.code, f.detail) for f in flags] == [(m.DELETED_FILES, "d0.py, d1.py, d2.py, d3.py, d4.py +1 more")]


def test_flag_order() -> None:
    task = Task(id="t", goal="g", mode="edit", scope=["src/"])
    files = [("D", "x.py")]
    flags = task_flags(
        task=task, worktree="/w/base/t", worktree_base="/w/base", temp_root="/sys/tmp", brain_dir="/b",
        changes=["x.py"],
        tool_calls=[ToolCall(1, "run_command", {"CommandLine": "git push"}),
                    ToolCall(2, "write_file", {"Path": "/home/u/q"})],
        report=Report("done", "s", files_changed=["ghost"], blocked_actions=["b"]),
        diff=DiffStat(files, 300, 200), worker_ran_ok=True)
    assert _codes(flags) == [m.OUT_OF_SCOPE_WRITE, m.SUSPICIOUS_COMMAND, m.SCOPE_VIOLATION, m.BLOCKED_ACTIONS,
                             m.REPORT_MISMATCH, m.LARGE_DIFF, m.DELETED_FILES]


# ---- overlap_flags ------------------------------------------------------------------------------

def _tr(tid: str, mode: str, paths: list[str]) -> TaskResult:
    diff = DiffStat([("M", p) for p in paths], 1, 1) if paths else None
    return TaskResult(task_id=tid, mode=mode, title="", status="ok", diff=diff)


def test_overlap_flags() -> None:
    flags = overlap_flags([_tr("t1", "edit", ["a.py", "b.py"]), _tr("t2", "edit", ["a.py", "c.py"]),
                           _tr("t3", "read", ["a.py"]), _tr("t4", "edit", [])])
    assert [(f.code, f.detail) for f in flags] == [(m.OVERLAP, "a.py (t1, t2)")]
    assert flags[0].level == "warn"


def test_overlap_none_and_cap() -> None:
    assert overlap_flags([_tr("t1", "edit", ["a"]), _tr("t2", "edit", ["b"])]) == []
    paths = [f"f{i}.py" for i in range(7)]
    flags = overlap_flags([_tr("t1", "edit", paths), _tr("t2", "edit", paths)])
    assert len(flags) == 1
    assert flags[0].detail.startswith("f0.py (t1, t2); f1.py (t1, t2)")
    assert flags[0].detail.endswith("f4.py (t1, t2) +2 more")


# ---- decide_status ------------------------------------------------------------------------------

def _outcome(**over) -> AgyOutcome:
    base = dict(exit_code=0, status="SUCCESS", kind="ok")
    base.update(over)
    return AgyOutcome(**base)


def _decide(**over):
    base = dict(mode="edit", not_started_reason="", setup_failure="", outcome=_outcome(), timeout_seconds=1200,
                report=Report("done", "s"), changes=["a.py"], accept=[AcceptResult("t", 0)])
    base.update(over)
    return decide_status(**base)


def test_status_ok() -> None:
    assert _decide() == ("ok", "")


def test_status_rule1_skipped() -> None:
    assert _decide(not_started_reason="quota exhausted", outcome=None) == ("skipped", "not started: quota exhausted")


def test_status_rule2_setup_failed() -> None:
    assert _decide(setup_failure="`npm ci` exit 1", outcome=None) == ("error", "setup failed: `npm ci` exit 1")


def test_status_rule3_timeout() -> None:
    assert _decide(outcome=_outcome(kind="timeout", status="ERROR")) == ("timeout", "worker timed out after 1200s")


@pytest.mark.parametrize("kind", ["quota", "auth", "model", "interrupted", "error"])
def test_status_rule4_error_kinds(kind: str) -> None:
    out = _outcome(kind=kind, status="ERROR", error="first line\nsecond line")
    assert _decide(outcome=out) == ("error", f"{kind}: first line")


def test_status_rule4_long_error_is_capped() -> None:
    status, reason = _decide(outcome=_outcome(kind="error", status="ERROR", error="x" * 500))
    assert status == "error" and reason.startswith("error: ") and len(reason) - len("error: ") == 160
    assert reason.endswith("...")


def test_status_rule4_empty_error_still_has_text() -> None:
    assert _decide(outcome=_outcome(kind="auth", status="ERROR", error=""))[1] == "auth: no details"


def test_status_missing_outcome_is_error() -> None:
    status, reason = _decide(outcome=None)
    assert status == "error" and reason


def test_status_rule5_blocked() -> None:
    assert _decide(report=Report("blocked", "s")) == ("failed", "worker blocked")


def test_status_rule6_no_changes_edit_only() -> None:
    assert _decide(changes=[]) == ("failed", "no changes")
    assert _decide(mode="read", changes=[]) == ("ok", "")


def test_status_rule7_acceptance() -> None:
    assert _decide(accept=[AcceptResult("a", 0), AcceptResult("b", 2)]) == ("failed", "acceptance failed")
    assert _decide(accept=[AcceptResult("a", -1)]) == ("failed", "acceptance failed")


def test_status_rule8_partial() -> None:
    assert _decide(report=Report("partial", "s")) == ("failed", "worker reported partial")


def test_status_rule9_read_no_report() -> None:
    assert _decide(mode="read", report=Report("missing"), changes=[], accept=[]) == ("failed", "no report")
    assert _decide(mode="read", report=Report("missing", findings="answer"), changes=[], accept=[]) == ("ok", "")
    assert _decide(mode="read", report=Report("missing", findings="  \n"), changes=[], accept=[]) == (
        "failed", "no report")
    assert _decide(mode="edit", report=Report("missing")) == ("ok", "")


def test_status_precedence() -> None:
    timeout = _outcome(kind="timeout", status="ERROR")
    assert _decide(outcome=timeout, accept=[AcceptResult("a", 1)]) == ("timeout", "worker timed out after 1200s")
    assert _decide(report=Report("blocked", "s"), changes=[]) == ("failed", "worker blocked")
    assert _decide(changes=[], accept=[AcceptResult("a", 1)]) == ("failed", "no changes")
    assert _decide(report=Report("partial", "s"), accept=[AcceptResult("a", 1)]) == ("failed", "acceptance failed")
    assert _decide(not_started_reason="auth failure", setup_failure="x", outcome=timeout)[0] == "skipped"
    assert _decide(setup_failure="x", outcome=timeout)[0] == "error"


# ---- run_acceptance cancellation (added in T7 so Ctrl-C does not wait for a long command) --------------

@posix_only
def test_run_acceptance_cancel_stops_running_command_and_skips_the_rest(tmp_path) -> None:
    import threading
    import time
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    log = tmp_path / "accept.log"
    t0 = time.monotonic()
    results = run_acceptance(["echo before; sleep 30 & sleep 30", "echo never"], str(tmp_path), 60, str(log),
                             dict(os.environ), cancel=cancel)
    assert time.monotonic() - t0 < 10
    assert [r.exit_code for r in results] == [-1]          # the second command never started
    assert results[0].tail[-1] == "(interrupted)"
    assert "(interrupted)" in log.read_text(encoding="utf-8")


@posix_only
def test_run_acceptance_already_cancelled_runs_nothing(tmp_path) -> None:
    import threading
    cancel = threading.Event()
    cancel.set()
    assert run_acceptance(["echo hi"], str(tmp_path), 30, str(tmp_path / "l"), dict(os.environ), cancel=cancel) == []
