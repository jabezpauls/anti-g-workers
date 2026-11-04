"""Tests for summary rendering. The golden text is copied from spec section 9."""
from __future__ import annotations

import json

import pytest

from agw.models import (AcceptResult, DiffStat, Flag, Report, RunResult, TaskResult, ToolCall, to_jsonable)
from agw.summary import (fmt_duration, render_events, render_runs_list, render_summary, render_task_detail)

RUN_ID = "20261105-143201-a1b2"

GOLDEN = """\
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
"""


def _cmd(i: int, command: str, code: int | None) -> ToolCall:
    return ToolCall(i, "run_command", {"CommandLine": command}, None, code)


def spec_run() -> RunResult:
    t1 = TaskResult(
        task_id="add-retry", mode="edit", title="Retry GETs in http client", status="ok", attempt=1,
        max_attempts=3, duration_seconds=130, branch=f"agw/{RUN_ID}/add-retry", worker_ran=True,
        diff=DiffStat([("M", "src/net/http.py"), ("M", "tests/test_http.py")], 58, 4),
        accept=[AcceptResult("python -m pytest -q tests/test_http.py", 0)],
        commands=[_cmd(1, "python -m pytest -q tests/test_http.py", 1),
                  _cmd(2, "python -m pytest -q tests/test_http.py", 0)],
        report=Report("done", "Added _with_retry with 0.5s/1s backoff; HttpClient.get uses it; 3 tests added."),
        usage={"input_tokens": 200_000, "output_tokens": 4_000})
    t2 = TaskResult(
        task_id="fix-parser", mode="edit", title="", status="failed", reason="acceptance failed", attempt=1,
        duration_seconds=241, branch=f"agw/{RUN_ID}/fix-parser", worker_ran=True,
        diff=DiffStat([("M", "src/parser.py")], 3, 1),
        accept=[AcceptResult("python -m pytest -q tests/test_parser.py", 1,
                             ["E       AssertionError: assert ['a', 'b'] == ['a', 'b', '']",
                              "1 failed, 11 passed in 0.31s"])],
        flags=[Flag("SCOPE_VIOLATION", "setup.cfg")],
        report=Report("partial", "Fixed trailing delimiter handling; empty-field case still failing."),
        usage={"input_tokens": 150_000, "output_tokens": 3_000})
    t3 = TaskResult(
        task_id="map-auth", mode="read", title="", status="timeout", reason="worker timed out after 1200s",
        duration_seconds=1200, worker_ran=True, usage={"input_tokens": 62_000, "output_tokens": 2_000})
    return RunResult(
        run_id=RUN_ID, base_sha="3f2a1c9" + "0" * 33, base_ref="main", main_root="/r", started="2026-11-05T14:32:01",
        duration_seconds=372, tasks=[t1, t2, t3], flags=[Flag("DIRTY_BASE", "2 uncommitted path(s) not visible to workers")])


def run_with(*tasks: TaskResult, flags: list[Flag] | None = None, retry_limit: int = 2) -> RunResult:
    return RunResult(run_id="r1", base_sha="abcdef1234", base_ref="main", main_root="/r", started="s",
                     duration_seconds=5, tasks=list(tasks), flags=flags or [], retry_limit=retry_limit)


def task(tid: str = "t", **kw) -> TaskResult:
    base = dict(task_id=tid, mode="read", title="", status="ok", worker_ran=True, report=Report("done", "s"))
    base.update(kw)
    return TaskResult(**base)


# ---- fmt_duration -------------------------------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"), (0.4, "0s"), (45, "45s"), (59.4, "59s"), (59.6, "1m00s"), (60, "1m00s"), (125, "2m05s"),
    (3599.4, "59m59s"), (3599.6, "1h00m"), (3600, "1h00m"), (3780, "1h03m"), (7322, "2h02m"), (-3, "0s"),
])
def test_fmt_duration(seconds: float, expected: str) -> None:
    assert fmt_duration(seconds) == expected


# ---- render_summary -----------------------------------------------------------------------------

def test_golden_spec_example() -> None:
    assert render_summary(spec_run()) == GOLDEN


def test_summary_is_deterministic_ascii_frame() -> None:
    assert render_summary(spec_run()) == render_summary(spec_run())
    assert GOLDEN.isascii()


def test_header_counts_omit_zero_categories_and_round_tokens() -> None:
    tasks = [task("a", usage={"input_tokens": 1499, "output_tokens": 500}),
             task("b", status="error", reason="x", usage={"input_tokens": 0, "output_tokens": 499}),
             task("c", status="skipped", reason="not started: quota exhausted", worker_ran=False),
             task("d", status="skipped", reason="not started: quota exhausted", worker_ran=False)]
    first = render_summary(run_with(*tasks)).splitlines()[0]
    assert first == "agw r1 | base abcdef1 (main) | 4 task(s): 1 ok, 1 error, 2 skipped | 5s | gemini 1k in / 1k out"
    only_failed = render_summary(run_with(task(status="failed", reason="r"))).splitlines()[0]
    assert "1 task(s): 0 ok, 1 failed |" in only_failed


def test_run_flags_warn_and_info_prefix_and_order() -> None:
    flags = [Flag("MAIN_TREE_CHANGED", "a.py (changed during run; may be you or Claude - check)"),
             Flag("OVERLAP", "x.py (t1, t2)"), Flag("DENY_RULE_DROPPED", "/p")]
    lines = render_summary(run_with(task(), flags=flags)).splitlines()
    assert lines[1] == "! MAIN_TREE_CHANGED: a.py (changed during run; may be you or Claude - check)"
    assert lines[2] == "! OVERLAP: x.py (t1, t2)"
    assert lines[3] == "- DENY_RULE_DROPPED: /p"
    assert lines[4] == ""


def test_attempt_denominator_comes_from_retry_limit() -> None:
    out = render_summary(run_with(task(attempt=2), retry_limit=1))
    assert "attempt 2/2" in out


def test_read_findings_block_is_indented_by_four() -> None:
    t = task(report=Report("done", "s", findings="line one\n\nline three\n"))
    out = render_summary(run_with(t))
    assert "  findings:\n    line one\n    \n    line three\n\n" in out
    assert "notes:" not in out


def test_edit_notes_collapse_newlines() -> None:
    t = task(mode="edit", diff=DiffStat([("M", "a")], 1, 1), branch="b",
             report=Report("done", "s", findings="first\nsecond\r\n  third"))
    out = render_summary(run_with(t))
    assert "  notes: first second third\n" in out
    assert "findings:" not in out


def test_commands_more_truncate_and_unknown_exit() -> None:
    cmds = [_cmd(i, f"cmd{i}", 0) for i in range(8)] + [_cmd(9, "x", None), _cmd(10, "y", 0), _cmd(11, "z", 0)]
    out = render_summary(run_with(task(commands=cmds)))
    ran = [ln for ln in out.splitlines() if ln.startswith("  ran:")][0]
    assert ran == ("  ran: cmd0 [0]; cmd1 [0]; cmd2 [0]; cmd3 [0]; cmd4 [0]; cmd5 [0]; cmd6 [0]; cmd7 [0]"
                   " (+3 more)")
    long = "echo " + "a" * 95
    out = render_summary(run_with(task(commands=[_cmd(1, long, 2), _cmd(2, "ls", None)])))
    ran = [ln for ln in out.splitlines() if ln.startswith("  ran:")][0]
    cut = long[:69] + "..."
    assert len(cut) == 72
    assert ran == f"  ran: {cut} [2]; ls [?]"


def test_exactly_eight_commands_has_no_more_suffix() -> None:
    out = render_summary(run_with(task(commands=[_cmd(i, f"c{i}", 0) for i in range(8)])))
    assert "more)" not in out


def test_skipped_task_shows_only_header_and_error() -> None:
    t = task("s1", status="skipped", reason="not started: quota exhausted", worker_ran=False, mode="edit",
             report=Report("missing"))
    block = render_summary(run_with(t)).split("\n\n")[1]
    assert block == '[skipped] s1 edit 0s attempt 1/3\n  error: not started: quota exhausted'


def test_edit_diff_none_and_accept_none_and_missing_report() -> None:
    t = task(mode="edit", status="failed", reason="no changes", report=Report("missing"), diff=None)
    block = render_summary(run_with(t)).split("\n\n")[1].splitlines()
    assert block[1:] == ["  diff: none", "  accept: none", "  report: missing"]


def test_error_status_with_worker_ran_has_diff_but_no_accept_or_report() -> None:
    t = task(mode="edit", status="error", reason="quota: Quota exceeded", diff=DiffStat([("A", "n.py")], 5, 0),
             branch="agw/r1/t", report=Report("missing"))
    block = render_summary(run_with(t)).split("\n\n")[1].splitlines()
    assert block == ["[error] t edit 0s attempt 1/3", "  error: quota: Quota exceeded",
                     "  diff: 1 file(s) +5 -0 | branch agw/r1/t"]


def test_accept_fail_tail_is_capped_to_fifteen_and_counts_passes() -> None:
    tail = [f"l{i}" for i in range(30)]
    acc = [AcceptResult("ok-cmd", 0), AcceptResult("bad `cmd`", -1, tail), AcceptResult("later", 1, ["zzz"])]
    out = render_summary(run_with(task(status="failed", reason="acceptance failed", accept=acc)))
    lines = out.splitlines()
    idx = lines.index("  accept: FAIL 1/3 passed; first failure `bad `cmd`` exit -1")
    assert lines[idx + 1] == "    | l15" and lines[idx + 15] == "    | l29" and lines[idx + 16] == "  report: done - s"
    assert "zzz" not in out


def test_flags_blocked_concerns_lines_and_every_flag_shown() -> None:
    flags = [Flag("OUT_OF_SCOPE_WRITE", "/x"), Flag("NO_CHANGES", "edit task produced no changes"),
             Flag("LARGE_DIFF", "500 lines changed")]
    t = task(flags=flags, report=Report("partial", "sum\nmary", blocked_actions=["a", "b"], concerns=["c1", "c2"]))
    out = render_summary(run_with(t))
    assert ("  flags: OUT_OF_SCOPE_WRITE /x; NO_CHANGES edit task produced no changes; "
            "LARGE_DIFF 500 lines changed\n") in out
    assert "  report: partial - sum mary\n  blocked: a; b\n  concerns: c1; c2\n" in out


def test_report_line_without_summary() -> None:
    assert "  report: done\n" in render_summary(run_with(task(report=Report("done", ""))))


def test_oversized_worker_text_is_capped_but_flags_survive() -> None:
    big = "x" * 100_000
    t = task(flags=[Flag("SUSPICIOUS_COMMAND", "git push")],
             report=Report("done", big, findings=big, concerns=[big]))
    out = render_summary(run_with(t))
    assert len(out) < 25_000
    assert "  flags: SUSPICIOUS_COMMAND git push\n" in out
    assert "truncated" in out and "agw show r1 t --report" in out


def test_title_with_newline_stays_on_one_line() -> None:
    out = render_summary(run_with(task(title="a\nb")))
    assert '[ok] t read "a b" 0s attempt 1/3' in out


def test_footer_is_last_line() -> None:
    out = render_summary(run_with(task()))
    assert out.endswith("\n\nnext: agw show r1 <task> [--diff|--log|--events]; agw apply r1 <task>; agw clean r1\n")


def test_zero_tasks_does_not_crash() -> None:
    out = render_summary(run_with())
    assert out.splitlines()[0].startswith("agw r1 | base abcdef1 (main) | 0 task(s): 0 ok |")


def test_base_ref_snapshot_label() -> None:
    run = run_with(task())
    run.base_ref = "snapshot of uncommitted changes"
    assert "base abcdef1 (snapshot of uncommitted changes)" in render_summary(run)


# ---- render_task_detail -------------------------------------------------------------------------

def test_task_detail_full() -> None:
    tr = spec_run().tasks[1]
    tr.model = "gemini-3.8-flash-high"
    tr.base_sha, tr.head_sha = "1234567890", "abcdef0123"
    tr.task_dir = "/runs/r/fix-parser"
    tr.commands = [_cmd(1, "pytest -q", 1), _cmd(2, "ls\n-la", None)]
    tr.accept[0] = AcceptResult("python -m pytest -q tests/test_parser.py", 1, ["x"], 1.26)
    tr.diff = DiffStat([("M", "src/parser.py"), ("A", "new.py")], 3, 1)
    out = render_task_detail(tr)
    report_json = json.dumps(to_jsonable(tr.report), indent=2, ensure_ascii=False)
    expected = (
        "task fix-parser (edit) status failed: acceptance failed\n"
        "title: -\n"
        "attempt 1/3 | model gemini-3.8-flash-high | 4m01s | gemini 150k in / 3k out\n"
        "branch: agw/20261105-143201-a1b2/fix-parser | base 1234567 | head abcdef0\n"
        "diff: 2 file(s) +3 -1\n"
        "  M src/parser.py\n"
        "  A new.py\n"
        "accept:\n"
        "  $ python -m pytest -q tests/test_parser.py [exit 1] (1.3s)\n"
        "flags:\n"
        "  SCOPE_VIOLATION: setup.cfg\n"
        "commands (2):\n"
        "  [1] pytest -q\n"
        "  [?] ls\\n-la\n"
        "report:\n"
        + "\n".join("  " + ln for ln in report_json.splitlines()) + "\n"
        "files: /runs/r/fix-parser\n"
    )
    assert out == expected


def test_task_detail_empty_values() -> None:
    tr = TaskResult(task_id="r", mode="read", title="T", status="skipped", reason="not started: interrupted")
    out = render_task_detail(tr)
    assert out.startswith("task r (read) status skipped: not started: interrupted\ntitle: T\n")
    assert "attempt 1/3 | model - | 0s | gemini 0k in / 0k out\n" in out
    assert "branch: - | base - | head -\n" in out
    assert "diff: none\naccept: none\nflags: none\ncommands (0):\nreport:\n" in out
    assert out.endswith("files: -\n")
    ok = render_task_detail(task(reason=""))
    assert ok.splitlines()[0] == "task t (read) status ok"


def test_task_detail_accept_timeout_and_all_flags() -> None:
    tr = task(accept=[AcceptResult("sleep 9", -1, ["(timed out after 1s)"], 1.0)],
              flags=[Flag("A", "1"), Flag("LARGE_DIFF", "2")])
    out = render_task_detail(tr)
    assert "  $ sleep 9 [exit -1] (1.0s)\n" in out
    assert "  A: 1\n  LARGE_DIFF: 2\n" in out


# ---- render_runs_list ---------------------------------------------------------------------------

def test_runs_list() -> None:
    assert render_runs_list([]) == "no runs\n"
    runs = [{"run_id": "20261105-1-a", "status": "done", "started": "2026-11-05T14:32:01", "counts": "2 ok, 1 failed",
             "n_tasks": 3},
            {"run_id": "20261104-9-b", "status": "abandoned", "started": "2026-11-04T09:00:00", "counts": "-",
             "n_tasks": 1}]
    assert render_runs_list(runs) == (
        "20261105-1-a  done        2026-11-05T14:32:01  3 task(s): 2 ok, 1 failed\n"
        "20261104-9-b  abandoned   2026-11-04T09:00:00  1 task(s): -\n")


# ---- render_events ------------------------------------------------------------------------------

def test_events() -> None:
    calls = [ToolCall(2, "write_file", {"Path": "/w/a.py", "Content": "x"}),
             _cmd(12, "pytest -q", 1), _cmd(13, "ls", None),
             ToolCall(5, "weird", {"k": "v" * 300}), ToolCall(6, "empty", {})]
    out = render_events(calls).splitlines()
    assert out[0] == "   2 write_file /w/a.py"
    assert out[1] == "  12 run_command pytest -q [1]"
    assert out[2] == "  13 run_command ls [?]"
    assert out[3].startswith("   5 weird {\"k\": \"v") and len(out[3]) == len("   5 weird ") + 120
    assert out[4] == "   6 empty {}"
    assert render_events([]) == "(no tool calls)\n"
