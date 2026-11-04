"""Tests for agw.models: serialization round-trip, flag levels and flag-code bookkeeping."""
from __future__ import annotations

import json

import agw.models as m
from agw.models import (AcceptResult, DiffStat, Flag, Report, RunResult, TaskResult, ToolCall,
                        run_result_from_dict, task_result_from_dict, to_jsonable)

ALL_FLAG_CODES = {
    m.OUT_OF_SCOPE_WRITE, m.SUSPICIOUS_COMMAND, m.READ_MODE_CHANGES, m.SCOPE_VIOLATION, m.BLOCKED_ACTIONS,
    m.NO_REPORT, m.REPORT_MISMATCH, m.NO_CHANGES, m.LARGE_DIFF, m.DELETED_FILES, m.MAIN_TREE_CHANGED,
    m.DIRTY_BASE, m.OVERLAP, m.DENY_RULE_DROPPED,
}


def make_run() -> RunResult:
    task = TaskResult(
        task_id="add-retry", mode="edit", title="Retry GETs", status="ok", reason="", attempt=2, max_attempts=3,
        duration_seconds=130.5, model="gemini-3.8-flash-high", branch="agw/r1/add-retry",
        base_sha="a" * 40, head_sha="b" * 40,
        diff=DiffStat(files=[("M", "app.py"), ("A", "new.py")], insertions=58, deletions=4),
        accept=[AcceptResult(command="pytest -q", exit_code=0, tail=["1 passed"], duration_seconds=1.5)],
        commands=[ToolCall(step_index=4, name="run_command", params={"CommandLine": "ls"},
                           output="Command exited with code 0.", exit_code=0)],
        flags=[Flag("LARGE_DIFF", "62 lines changed"), Flag("OVERLAP", "app.py (a, b)")],
        report=Report(status="done", summary="did it", findings="n", files_changed=["app.py"],
                      blocked_actions=[], concerns=["c"]),
        usage={"input_tokens": 10, "output_tokens": 2}, task_dir="/x/y", worker_ran=True,
    )
    read_task = TaskResult(task_id="r", mode="read", title="", status="skipped", reason="not started: quota exhausted")
    return RunResult(run_id="20261105-143201-a1b2", base_sha="c" * 40, base_ref="main", main_root="/repo",
                     started="2026-11-05T14:32:01", duration_seconds=372.0, tasks=[task, read_task],
                     flags=[Flag("DIRTY_BASE", "2 uncommitted path(s) not visible to workers")],
                     exit_code=1, status="done", pid=123, retry_limit=2)


def test_run_result_round_trip_through_json():
    run = make_run()
    data = json.loads(json.dumps(to_jsonable(run)))
    back = run_result_from_dict(data)
    assert back == run
    assert back.tasks[0].diff is not None
    assert all(isinstance(f, tuple) for f in back.tasks[0].diff.files)
    assert back.tasks[1].diff is None
    assert isinstance(back.tasks[0].flags[0], Flag)


def test_flag_levels():
    assert Flag("OVERLAP", "x").level == "warn"
    assert Flag("LARGE_DIFF", "x").level == "info"


def test_flag_sets_are_disjoint_and_complete():
    assert m.WARN_FLAGS.isdisjoint(m.INFO_FLAGS)
    assert m.WARN_FLAGS | m.INFO_FLAGS == ALL_FLAG_CODES
    assert len(ALL_FLAG_CODES) == 14


def test_task_result_from_dict_ignores_unknown_keys():
    data = to_jsonable(make_run().tasks[0])
    data["bogus"] = 1
    data["report"]["bogus"] = 2
    data["flags"][0]["bogus"] = 3
    assert task_result_from_dict(data) == make_run().tasks[0]


def test_report_schema_requires_all_properties():
    assert set(m.REPORT_SCHEMA["required"]) == set(m.REPORT_SCHEMA["properties"])
    assert m.REPORT_SCHEMA["properties"]["status"]["enum"] == list(m.REPORT_STATUSES)
