"""Tests for agw.tasks: parsing and validating the tasks JSON."""
from __future__ import annotations

import io
import json

import pytest

from agw.models import Task, TaskSet
from agw.tasks import TaskFileError, check_attempts, load_taskset, parse_taskset

SKILL_EXAMPLE = {
    "context": ["Python 3.12 project. Tests: python -m pytest -q"],
    "tasks": [
        {
            "id": "add-retry",
            "mode": "edit",
            "title": "Retry GETs in http client",
            "goal": "HttpClient.get retries up to 3 times with backoff on 5xx responses and connection errors.",
            "files": ["src/net/http.py", "tests/test_http.py"],
            "steps": [
                "In src/net/http.py add method _with_retry(self, fn).",
                "Make HttpClient.get call its request through _with_retry.",
                "In tests/test_http.py add tests.",
            ],
            "scope": ["src/net/http.py", "tests/test_http.py"],
            "constraints": ["No new dependencies.", "Keep HttpClient's public signatures unchanged."],
            "accept": ["python -m pytest -q tests/test_http.py"],
        },
        {
            "id": "map-auth",
            "goal": "Explain how an incoming request is authenticated, from middleware to the loaded user object.",
            "report": "Entry points with file:line, token validation steps, where the user is loaded, any caching.",
        },
    ],
}


def _errors(data: object) -> list[str]:
    with pytest.raises(TaskFileError) as exc:
        parse_taskset(data)
    return exc.value.errors


def test_minimal_file_gets_defaults():
    ts = parse_taskset({"tasks": [{"id": "a", "goal": "g"}]})
    assert ts == TaskSet(tasks=[Task(id="a", goal="g")], base="HEAD", snapshot_dirty=False, context=[])
    t = ts.tasks[0]
    assert t.mode == "read" and t.accept is None and t.attempt == 1
    assert t.model is None and t.timeout_seconds is None and t.title == "" and t.report == ""


def test_skill_example_parses():
    ts = parse_taskset(json.loads(json.dumps(SKILL_EXAMPLE)))
    assert ts.context == ["Python 3.12 project. Tests: python -m pytest -q"]
    assert [t.id for t in ts.tasks] == ["add-retry", "map-auth"]
    edit, read = ts.tasks
    assert edit.mode == "edit"
    assert edit.title == "Retry GETs in http client"
    assert edit.files == ["src/net/http.py", "tests/test_http.py"]
    assert len(edit.steps) == 3
    assert edit.scope == ["src/net/http.py", "tests/test_http.py"]
    assert edit.constraints[0] == "No new dependencies."
    assert edit.accept == ["python -m pytest -q tests/test_http.py"]
    assert read.mode == "read"
    assert read.accept is None
    assert read.report.startswith("Entry points")


def test_all_optional_fields_round_trip():
    ts = parse_taskset({
        "version": 1, "base": "main", "snapshot_dirty": False, "context": ["c"],
        "tasks": [{"id": "x1", "mode": "edit", "title": "t", "goal": "g", "context": ["a"], "files": ["f"],
                   "steps": ["s"], "scope": ["src/"], "constraints": ["k"], "accept": ["true"], "report": "r",
                   "model": "m", "timeout_seconds": 60, "attempt": 3}],
    })
    assert ts.base == "main"
    t = ts.tasks[0]
    assert (t.model, t.timeout_seconds, t.attempt, t.report) == ("m", 60, 3, "r")


def test_snapshot_dirty_with_head_ok():
    ts = parse_taskset({"snapshot_dirty": True, "base": "HEAD", "tasks": [{"id": "a", "goal": "g"}]})
    assert ts.snapshot_dirty is True


def test_empty_accept_is_kept_as_empty_list():
    ts = parse_taskset({"tasks": [{"id": "a", "mode": "edit", "goal": "g", "accept": []}]})
    assert ts.tasks[0].accept == []
    assert ts.tasks[0].accept is not None


def _t(**fields: object) -> dict:
    return {"tasks": [{"id": "a", "goal": "g", **fields}]}


@pytest.mark.parametrize("data, expected", [
    ([1], "(root): must be an object"),
    ("x", "(root): must be an object"),
    ({}, "tasks: required"),
    ({"tasks": [{"id": "a", "goal": "g"}], "extra": 1}, "extra: unknown key"),
    ({"tasks": [{"id": "a", "goal": "g", "idd": "b"}]}, "tasks[0].idd: unknown key"),
    ({"version": 2, "tasks": [{"id": "a", "goal": "g"}]}, "version: must be 1"),
    ({"version": True, "tasks": [{"id": "a", "goal": "g"}]}, "version: must be 1"),
    ({"base": "", "tasks": [{"id": "a", "goal": "g"}]}, "base: must be a non-empty string"),
    ({"base": 3, "tasks": [{"id": "a", "goal": "g"}]}, "base: must be a non-empty string"),
    ({"snapshot_dirty": "yes", "tasks": [{"id": "a", "goal": "g"}]}, "snapshot_dirty: must be a boolean"),
    ({"context": "x", "tasks": [{"id": "a", "goal": "g"}]}, "context: must be a list of non-empty strings"),
    ({"context": [""], "tasks": [{"id": "a", "goal": "g"}]}, "context: must be a list of non-empty strings"),
    ({"tasks": []}, "tasks: must contain 1 to 20 tasks"),
    ({"tasks": "x"}, "tasks: must contain 1 to 20 tasks"),
    ({"tasks": [{"id": f"t{i}", "goal": "g"} for i in range(21)]}, "tasks: must contain 1 to 20 tasks"),
    ({"tasks": ["x"]}, "tasks[0]: must be an object"),
    ({"tasks": [{"goal": "g"}]}, "tasks[0].id: required"),
    ({"tasks": [{"id": "a"}]}, "tasks[0].goal: required"),
    (_t(id=5), "tasks[0].id: must be a string"),
    (_t(id="Bad"), "tasks[0].id: must match ^[a-z0-9][a-z0-9-]{0,39}$"),
    (_t(id="-a"), "tasks[0].id: must match ^[a-z0-9][a-z0-9-]{0,39}$"),
    (_t(id="a" * 41), "tasks[0].id: must match ^[a-z0-9][a-z0-9-]{0,39}$"),
    (_t(id="a\n"), "tasks[0].id: must match ^[a-z0-9][a-z0-9-]{0,39}$"),
    (_t(mode="write"), 'tasks[0].mode: must be "read" or "edit"'),
    (_t(mode=1), 'tasks[0].mode: must be "read" or "edit"'),
    (_t(title=3), "tasks[0].title: must be a string"),
    (_t(title="x" * 81), "tasks[0].title: must be at most 80 characters"),
    (_t(goal=""), "tasks[0].goal: must be a non-empty string"),
    (_t(goal=3), "tasks[0].goal: must be a non-empty string"),
    (_t(context="x"), "tasks[0].context: must be a list of non-empty strings"),
    (_t(files=["ok", ""]), "tasks[0].files: must be a list of non-empty strings"),
    (_t(steps=[1]), "tasks[0].steps: must be a list of non-empty strings"),
    (_t(scope="src/"), "tasks[0].scope: must be a list of non-empty strings"),
    (_t(constraints={"a": 1}), "tasks[0].constraints: must be a list of non-empty strings"),
    (_t(accept="pytest"), "tasks[0].accept: must be a list of non-empty strings"),
    (_t(accept=[""]), "tasks[0].accept: must be a list of non-empty strings"),
    (_t(report=1), "tasks[0].report: must be a string"),
    (_t(model=""), "tasks[0].model: must be a non-empty string"),
    (_t(timeout_seconds=59), "tasks[0].timeout_seconds: must be an integer between 60 and 7200"),
    (_t(timeout_seconds=7201), "tasks[0].timeout_seconds: must be an integer between 60 and 7200"),
    (_t(timeout_seconds=True), "tasks[0].timeout_seconds: must be an integer between 60 and 7200"),
    (_t(timeout_seconds=60.5), "tasks[0].timeout_seconds: must be an integer between 60 and 7200"),
    (_t(attempt=0), "tasks[0].attempt: must be an integer >= 1"),
    (_t(attempt=True), "tasks[0].attempt: must be an integer >= 1"),
    (_t(attempt="2"), "tasks[0].attempt: must be an integer >= 1"),
    ({"tasks": [{"id": "a", "goal": "g"}, {"id": "a", "goal": "h"}]}, 'tasks[1].id: duplicate id "a"'),
    ({"base": "main", "snapshot_dirty": True, "tasks": [{"id": "a", "goal": "g"}]},
     '(root): snapshot_dirty requires base "HEAD"'),
])
def test_each_rule_has_its_message(data, expected):
    assert _errors(data) == [expected]


def test_boundaries_are_accepted():
    ts = parse_taskset({"tasks": [{"id": "0" + "-" * 39, "goal": "g", "title": "x" * 80,
                                   "timeout_seconds": 7200, "attempt": 1}]})
    assert len(ts.tasks) == 1
    assert len(parse_taskset({"tasks": [{"id": f"t{i}", "goal": "g"} for i in range(20)]}).tasks) == 20


def test_all_errors_reported_in_document_order():
    errors = _errors({
        "base": "",
        "tasks": [
            {"id": "BAD", "mode": "x", "goal": ""},
            {"id": "ok", "goal": "g", "attempt": 0},
            {"id": "ok", "goal": "g", "colour": "red"},
        ],
        "extra": 1,
    })
    assert errors == [
        "base: must be a non-empty string",
        "tasks[0].id: must match ^[a-z0-9][a-z0-9-]{0,39}$",
        'tasks[0].mode: must be "read" or "edit"',
        "tasks[0].goal: must be a non-empty string",
        "tasks[1].attempt: must be an integer >= 1",
        'tasks[2].id: duplicate id "ok"',
        "tasks[2].colour: unknown key",
        "extra: unknown key",
    ]


def test_missing_fields_reported_after_present_ones():
    errors = _errors({"tasks": [{"title": 3}]})
    assert errors == ["tasks[0].title: must be a string", "tasks[0].id: required", "tasks[0].goal: required"]


def test_error_str_is_newline_joined():
    with pytest.raises(TaskFileError) as exc:
        parse_taskset({"tasks": [{"title": 3}]})
    assert str(exc.value) == "\n".join(exc.value.errors)
    assert isinstance(exc.value, ValueError)


def test_load_from_stdin():
    stdin = io.StringIO(json.dumps({"tasks": [{"id": "a", "goal": "g"}]}))
    assert load_taskset("-", stdin).tasks[0].id == "a"


def test_load_from_file(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"tasks": [{"id": "a", "goal": "café"}]}), encoding="utf-8")
    assert load_taskset(str(p), io.StringIO("")).tasks[0].goal == "café"


def test_load_tolerates_bom(tmp_path):
    p = tmp_path / "t.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"tasks": [{"id": "a", "goal": "g"}]}).encode())
    assert load_taskset(str(p), io.StringIO("")).tasks[0].id == "a"
    assert load_taskset("-", io.StringIO("﻿" + '{"tasks":[{"id":"a","goal":"g"}]}')).tasks[0].id == "a"


def test_invalid_json_reports_label_line_and_col(tmp_path):
    with pytest.raises(TaskFileError) as exc:
        load_taskset("-", io.StringIO('{\n  "tasks": [,]\n}'))
    assert exc.value.errors == ["stdin: invalid JSON: Expecting value (line 2 col 13)"]
    p = tmp_path / "bad.json"
    p.write_text("{", encoding="utf-8")
    with pytest.raises(TaskFileError) as exc:
        load_taskset(str(p), io.StringIO(""))
    assert exc.value.errors[0].startswith(f"{p}: invalid JSON: ")
    assert "(line 1 col 2)" in exc.value.errors[0]


def test_missing_file_and_directory(tmp_path):
    missing = str(tmp_path / "nope.json")
    with pytest.raises(TaskFileError) as exc:
        load_taskset(missing, io.StringIO(""))
    assert exc.value.errors == [f"{missing}: file not found"]
    with pytest.raises(TaskFileError) as exc:
        load_taskset(str(tmp_path), io.StringIO(""))
    assert exc.value.errors[0].startswith(f"{tmp_path}: cannot read")


def test_non_utf8_file(tmp_path):
    p = tmp_path / "t.json"
    p.write_bytes(b'{"tasks": "\xff"}')
    with pytest.raises(TaskFileError) as exc:
        load_taskset(str(p), io.StringIO(""))
    assert exc.value.errors == [f"{p}: not valid UTF-8"]


def test_empty_stdin_is_invalid_json():
    with pytest.raises(TaskFileError) as exc:
        load_taskset("-", io.StringIO(""))
    assert exc.value.errors == ["stdin: invalid JSON: Expecting value (line 1 col 1)"]


def test_load_validation_errors_propagate():
    with pytest.raises(TaskFileError) as exc:
        load_taskset("-", io.StringIO('{"tasks": []}'))
    assert exc.value.errors == ["tasks: must contain 1 to 20 tasks"]


def test_check_attempts():
    ts = parse_taskset({"tasks": [
        {"id": "a", "goal": "g", "attempt": 3},
        {"id": "b", "goal": "g", "attempt": 4},
        {"id": "c", "goal": "g"},
    ]})
    assert check_attempts(ts, 2) == [
        "tasks[1].attempt: 4 exceeds 1 + retry_limit (2); take the task over instead of retrying"
    ]
    assert check_attempts(ts, 3) == []
    assert len(check_attempts(ts, 0)) == 2
