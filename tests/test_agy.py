"""Tests for agw.agy: argv building, process control, NDJSON parsing, classification, availability checks."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import time

import pytest

from agw import agy
from agw.models import AgyOutcome
from conftest import FAKE_AGY, FIXTURES

FAKE = [sys.executable, str(FAKE_AGY)]


# ---- helpers ---------------------------------------------------------------------------------
def stage(tmp_path: pathlib.Path, fixture: str | None, stderr: str | None = None, *, text: str | None = None) -> tuple[str, str]:
    """Copy a fixture (placeholders substituted) and write a stderr file; return (events_path, stderr_path)."""
    events = tmp_path / "events.ndjson"
    if text is None:
        text = (FIXTURES / fixture).read_text(encoding="utf-8") if fixture else ""
        text = text.replace("__CWD__", "/w").replace("__OUTSIDE__", "/o")
    events.write_text(text, encoding="utf-8")
    err = tmp_path / "stderr.txt"
    if stderr and stderr.endswith(".stderr.txt"):
        stderr = (FIXTURES / stderr).read_text(encoding="utf-8")
    err.write_text(stderr or "", encoding="utf-8")
    return str(events), str(err)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:                                   # a zombie that nobody reaped is not running
        state = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except OSError:
        return True


def wait_dead(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


def run(agw_env, tmp_path, brief="Task ID: t1\nhello", *, hard=60.0, cancel=None, argv=None):
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    events, err = str(tmp_path / "ev.ndjson"), str(tmp_path / "err.txt")
    result = agy.run_agy(argv or FAKE, brief, str(cwd), events, err, hard, cancel or threading.Event())
    return result, events, err, cwd


# ---- 1, 2: settings and argv -------------------------------------------------------------------
def test_build_settings_exact():
    assert agy.build_settings(["/a", "/b c"]) == '{"permissions":{"deny":["write_file(/a)","write_file(/b c)"]}}'
    assert agy.build_settings([]) == '{"permissions":{"deny":[]}}'


def test_build_argv_exact():
    base = ["agy", "--model", "m", "--output-format", "stream-json", "--json-schema", "/s.json",
            "--settings", "{}", "--disable-slash-commands", "--dangerously-skip-permissions",
            "--print-timeout", "90s"]
    assert agy.build_argv(["agy"], "m", "/s.json", "{}", 90, False) == base
    assert agy.build_argv(["agy"], "m", "/s.json", "{}", 90, True) == base + ["--sandbox"]
    assert agy.build_argv(["py", "fake.py"], "m", "/s.json", "{}", 90, False)[:2] == ["py", "fake.py"]


# ---- 3: parse_stream over fixtures ---------------------------------------------------------------
def test_parse_edit_existing_file(tmp_path):
    o = agy.parse_stream(*stage(tmp_path, "edit-existing-file.ndjson"), exit_code=0)
    assert [t.name for t in o.tool_calls] == ["read_file", "edit_file", "write_file", "run_command", "run_command"]
    assert [t.step_index for t in o.tool_calls] == [2, 4, 6, 8, 10]
    assert [t.exit_code for t in o.tool_calls if t.name == "run_command"] == [1, 0]
    assert [t.exit_code for t in o.tool_calls if t.name != "run_command"] == [None, None, None]
    assert o.tool_calls[0].params == {"Path": "/w/a.py"}
    assert o.tool_calls[0].output == "2 lines, 22 bytes"
    assert o.status == "SUCCESS" and o.kind == "ok" and o.bad_lines == 0
    assert o.usage["input_tokens"] == 52339
    assert o.model == "gemini-3.8-flash-low"
    assert o.conversation_id == "237848eb-2a4e-42ab-92eb-5550cfea8190"
    assert o.duration_seconds == pytest.approx(20.038641231)
    assert o.structured is None and o.response == "DONE\n"


def test_parse_deny_rule_emits_no_tool_call(tmp_path):
    o = agy.parse_stream(*stage(tmp_path, "deny-rule-write.ndjson"), exit_code=0)
    cmds = [t for t in o.tool_calls if t.name == "run_command"]
    assert len(cmds) == 1 and cmds[0].step_index == 3 and cmds[0].exit_code == 1
    assert not any(t.name == "write_file" for t in o.tool_calls)
    assert 2 not in [t.step_index for t in o.tool_calls]       # the denied step (ACTIVE then ERROR) is dropped


def _step(i, name, state, **info):
    return json.dumps({"event": "step_update", "step_update": {
        "step_index": i, "state": state, "step_type": "tool", "tool_name": name,
        "tool_info": {"parameters": {"Path": "/x", "CommandLine": "ls"}, **info}}})


def test_parse_errored_tool_steps(tmp_path):
    text = "\n".join([
        _step(1, "write_file", "ACTIVE"), _step(1, "write_file", "ERROR", error={"message": "denied"}),
        _step(2, "run_command", "ACTIVE"), _step(2, "run_command", "ERROR", error={"message": "denied"}),
        _step(3, "edit_file", "ACTIVE"), _step(3, "edit_file", "DONE"),
        _step(4, "write_file", "ERROR"), _step(4, "write_file", "DONE")])     # a later DONE wins
    o = agy.parse_stream(*stage(tmp_path, None, "", text=text), exit_code=0)
    assert [(t.step_index, t.name) for t in o.tool_calls] == [(2, "run_command"), (3, "edit_file"), (4, "write_file")]


def test_parse_structured_output(tmp_path):
    o = agy.parse_stream(*stage(tmp_path, "worktree-structured-output.ndjson"), exit_code=0)
    assert o.structured is not None
    assert o.structured["status"] == "partial"
    assert len(o.structured["blocked_actions"]) == 1


def test_parse_print_timeout(tmp_path):
    o = agy.parse_stream(*stage(tmp_path, "print-timeout.ndjson", "print-timeout.stderr.txt"), exit_code=3)
    assert o.kind == "timeout"
    assert o.agy_error is not None and o.agy_error["reason"] == "print_timeout"
    assert o.retryable is False
    assert o.finished_reason == "print_timeout"
    assert o.status == "ERROR"
    assert o.stderr_tail.splitlines()[-1].startswith("AGY_ERROR: ")
    assert [t.name for t in o.tool_calls] == ["run_command"]
    assert o.tool_calls[0].exit_code is None and o.tool_calls[0].output is None   # ACTIVE only, never finished


def test_parse_invalid_model(tmp_path):
    o = agy.parse_stream(*stage(tmp_path, "invalid-model.ndjson", "invalid-model.stderr.txt"), exit_code=1)
    assert o.status == "ERROR" and o.kind == "model"
    assert "invalid model selection" in o.error
    assert o.agy_error is None and o.conversation_id == ""


def test_parse_ask_question_counts_unknown_step(tmp_path):
    o = agy.parse_stream(*stage(tmp_path, "ask-question-headless.ndjson"), exit_code=0)
    assert o.unknown_steps == 1
    assert o.tool_calls == [] and o.kind == "ok"


def test_parse_empty_events_is_missing(tmp_path):
    o = agy.parse_stream(*stage(tmp_path, None, "boom\n"), exit_code=1)
    assert o.status == "MISSING" and o.error == "boom" and o.kind == "error"
    assert o.stderr_tail == "boom"


def test_parse_missing_without_stderr_uses_exit_code(tmp_path):
    o = agy.parse_stream(*stage(tmp_path, None, ""), exit_code=7)
    assert o.status == "MISSING" and o.error == "agy exited 7 without a result" and o.kind == "error"


def test_parse_missing_files_do_not_raise(tmp_path):
    o = agy.parse_stream(str(tmp_path / "nope.ndjson"), str(tmp_path / "nope.txt"), 127)
    assert o.status == "MISSING" and o.kind == "error" and o.error == "agy exited 127 without a result"


def test_parse_garbage_line_is_counted_and_skipped(tmp_path):
    minimal = (FIXTURES / "minimal-success.ndjson").read_text().replace("__CWD__", "/w")
    o = agy.parse_stream(*stage(tmp_path, None, "", text="this is not json\n" + minimal), exit_code=0)
    assert o.bad_lines == 1
    assert o.status == "SUCCESS" and o.kind == "ok" and o.usage["input_tokens"] == 1000


def test_parse_robust_to_odd_lines(tmp_path):
    good_result = '{"event":"result","result":{"status":"SUCCESS","response":"x","usage":{"input_tokens":5}}}'
    lines = [
        "", "   ", "[1,2,3]", "42", '"str"', "null",             # blank lines are ignored; non-objects are bad
        '{"event":"step_update","step_update":"oops"}',            # malformed step_update
        '{"event":"step_update","step_update":{"state":"DONE","step_type":"tool"}}',   # no step_index
        '{"event":"mystery","x":1}',                               # unknown event types are ignored silently
        '{"event":"result","result":"oops"}',                      # malformed result
        '{"event":"step_update","step_update":{"step_index":1,"step_type":"tool","tool_name":"x"',  # truncated JSON
        good_result,
    ]
    o = agy.parse_stream(*stage(tmp_path, None, "", text="\n".join(lines)), exit_code=0)
    # 4 non-objects + 3 malformed events + 1 truncated line; blank lines and unknown event types are not counted
    assert o.bad_lines == 8
    assert o.status == "SUCCESS"


def test_parse_truncated_last_line_without_newline(tmp_path):
    text = ('{"event":"step_update","step_update":{"step_index":2,"state":"ACTIVE","step_type":"tool",'
            '"tool_name":"run_command","tool_info":{"parameters":{"CommandLine":"ls"}}}}\n'
            '{"event":"step_update","step_update":{"step_index":3,"state":"DO')
    o = agy.parse_stream(*stage(tmp_path, None, "", text=text), exit_code=-9, hard_killed=True)
    assert o.bad_lines == 1
    assert [t.name for t in o.tool_calls] == ["run_command"]
    assert o.status == "MISSING" and o.kind == "timeout"


def test_parse_dedupes_active_and_done_by_step_index(tmp_path):
    def ev(state, output=None, params=True):
        info = {"name": "run_command"}
        if params:
            info["parameters"] = {"CommandLine": "pytest"}
        if output is not None:
            info["output"] = output
        return json.dumps({"event": "step_update", "step_update": {
            "step_index": 4, "state": state, "step_type": "tool", "tool_name": "run_command", "tool_info": info}})
    text = "\n".join([ev("ACTIVE"), ev("DONE", "Command exited with code -2.\n\nCommand output:\n"),
                      ev("ACTIVE"),                       # a stale ACTIVE arriving late must not erase the output
                      ev("DONE", None, params=False)])    # nor may a DONE that omits parameters
    o = agy.parse_stream(*stage(tmp_path, None, "", text=text), exit_code=0)
    assert len(o.tool_calls) == 1
    call = o.tool_calls[0]
    assert call.params == {"CommandLine": "pytest"} and call.exit_code == -2
    assert call.output.startswith("Command exited with code -2")


def test_parse_orders_tool_calls_by_step_index(tmp_path):
    def ev(i, name):
        return json.dumps({"event": "step_update", "step_update": {
            "step_index": i, "state": "DONE", "step_type": "tool", "tool_name": name, "tool_info": {"parameters": {}}}})
    o = agy.parse_stream(*stage(tmp_path, None, "", text="\n".join([ev(9, "c"), ev(3, "a"), ev(5, "b")])), exit_code=0)
    assert [t.name for t in o.tool_calls] == ["a", "b", "c"]


def test_parse_unknown_steps_counted_once_per_index(tmp_path):
    def ev(i):
        return json.dumps({"event": "step_update", "step_update": {"step_index": i, "state": "ACTIVE", "step_type": "unknown"}})
    o = agy.parse_stream(*stage(tmp_path, None, "", text="\n".join([ev(2), ev(2), ev(5)])), exit_code=0)
    assert o.unknown_steps == 2


def test_parse_tool_name_falls_back_to_tool_info(tmp_path):
    text = json.dumps({"event": "step_update", "step_update": {
        "step_index": 1, "state": "DONE", "step_type": "tool", "tool_info": {"name": "write_file", "parameters": {"Path": "/x"}}}})
    o = agy.parse_stream(*stage(tmp_path, None, "", text=text), exit_code=0)
    assert o.tool_calls[0].name == "write_file"


def test_parse_command_without_exit_code_line(tmp_path):
    text = json.dumps({"event": "step_update", "step_update": {
        "step_index": 1, "state": "DONE", "step_type": "tool", "tool_name": "run_command",
        "tool_info": {"parameters": {"CommandLine": "x"}, "output": "weird output"}}})
    o = agy.parse_stream(*stage(tmp_path, None, "", text=text), exit_code=0)
    assert o.tool_calls[0].exit_code is None


def test_parse_non_run_command_output_is_not_scanned_for_exit_code(tmp_path):
    text = json.dumps({"event": "step_update", "step_update": {
        "step_index": 1, "state": "DONE", "step_type": "tool", "tool_name": "read_file",
        "tool_info": {"parameters": {"Path": "/x"}, "output": "Command exited with code 5."}}})
    o = agy.parse_stream(*stage(tmp_path, None, "", text=text), exit_code=0)
    assert o.tool_calls[0].exit_code is None


def test_parse_result_field_types_are_coerced(tmp_path):
    res = {"event": "result", "result": {"status": "SUCCESS", "response": None, "error": 5,
                                         "structured_output": ["not", "a", "dict"], "duration_seconds": "x",
                                         "usage": {"input_tokens": 7, "output_tokens": "9", "x": True, "y": 2.0}}}
    o = agy.parse_stream(*stage(tmp_path, None, "", text=json.dumps(res)), exit_code=0)
    assert o.structured is None and o.response == "" and o.error == "5"
    assert o.usage == {"input_tokens": 7, "y": 2} and o.duration_seconds == 0.0


def test_parse_result_without_status_is_error(tmp_path):
    o = agy.parse_stream(*stage(tmp_path, None, "", text='{"event":"result","result":{"response":"x"}}'), exit_code=0)
    assert o.status == "ERROR" and o.kind == "error"


def test_parse_last_result_wins(tmp_path):
    text = ('{"event":"result","result":{"status":"ERROR","error":"first"}}\n'
            '{"event":"result","result":{"status":"SUCCESS","response":"second"}}\n')
    o = agy.parse_stream(*stage(tmp_path, None, "", text=text), exit_code=0)
    assert o.status == "SUCCESS" and o.response == "second"


def test_parse_missing_result_salvages_step_usage(tmp_path):
    def ev(i, n):
        return json.dumps({"event": "step_update", "step_update": {
            "step_index": i, "state": "DONE", "step_type": "agent_response", "usage": {"input_tokens": n, "output_tokens": 1}}})
    o = agy.parse_stream(*stage(tmp_path, None, "", text="\n".join([ev(1, 100), ev(1, 100), ev(3, 50)])), exit_code=1)
    assert o.status == "MISSING" and o.usage == {"input_tokens": 150, "output_tokens": 2}


def test_parse_invalid_utf8_does_not_raise(tmp_path):
    events = tmp_path / "e.ndjson"
    events.write_bytes(b'\xff\xfe garbage\n{"event":"result","result":{"status":"SUCCESS"}}\n')
    err = tmp_path / "s.txt"
    err.write_bytes(b"bad \xff bytes\n")
    o = agy.parse_stream(str(events), str(err), 0)
    assert o.status == "SUCCESS" and o.bad_lines == 1 and "bad" in o.stderr_tail


def test_stderr_tail_keeps_last_20_lines_and_drops_trailing_blanks(tmp_path):
    err = "".join(f"line {i}\n" for i in range(30)) + "\n\n"
    o = agy.parse_stream(*stage(tmp_path, None, err), exit_code=1)
    assert o.stderr_tail.splitlines() == [f"line {i}" for i in range(10, 30)]
    assert o.error == "line 29"


def test_agy_error_uses_last_line_and_tolerates_garbage(tmp_path):
    first = 'AGY_ERROR: {"status":"RESOURCE_EXHAUSTED","retryable":true}\n'
    o = agy.parse_stream(*stage(tmp_path, None, first), exit_code=1)
    assert o.agy_error == {"status": "RESOURCE_EXHAUSTED", "retryable": True} and o.retryable is True
    assert o.kind == "quota"
    both = first + 'AGY_ERROR: {"status":"X","retryable":false}\n'
    o = agy.parse_stream(*stage(tmp_path, None, both), exit_code=1)
    assert o.agy_error == {"status": "X", "retryable": False} and o.retryable is False
    o = agy.parse_stream(*stage(tmp_path, None, first + "AGY_ERROR: {not json\n"), exit_code=1)
    assert o.agy_error is None and o.retryable is False
    o = agy.parse_stream(*stage(tmp_path, None, "AGY_ERROR: [1]\n"), exit_code=1)
    assert o.agy_error is None
    o = agy.parse_stream(*stage(tmp_path, None, 'AGY_ERROR: {"retryable":"yes"}\n'), exit_code=1)
    assert o.retryable is True


def test_parse_flags_pass_through_to_classify(tmp_path):
    ev = stage(tmp_path, "minimal-success.ndjson")
    assert agy.parse_stream(*ev, exit_code=0).kind == "ok"
    assert agy.parse_stream(*ev, exit_code=-15, cancelled=True).kind == "interrupted"
    assert agy.parse_stream(*ev, exit_code=-9, hard_killed=True).kind == "timeout"


# ---- 4: classify -----------------------------------------------------------------------------------
def O(**kw):
    kw.setdefault("exit_code", 1)
    kw.setdefault("status", "ERROR")
    return AgyOutcome(**kw)


CASES = [
    ("success", O(exit_code=0, status="SUCCESS"), "", {}, "ok"),
    ("success beats stderr noise", O(exit_code=0, status="SUCCESS"), "quota warning", {}, "ok"),
    ("hard killed", O(exit_code=-9), "", {"hard_killed": True}, "timeout"),
    ("exit 3", O(exit_code=3), "", {}, "timeout"),
    ("finished_reason", O(finished_reason="print_timeout"), "", {}, "timeout"),
    ("agy_error reason", O(agy_error={"reason": "print_timeout"}), "", {}, "timeout"),
    ("interrupted error", O(error="interrupted"), "", {}, "interrupted"),
    ("cancelled", O(exit_code=-15), "", {"cancelled": True}, "interrupted"),
    ("quota status", O(agy_error={"status": "RESOURCE_EXHAUSTED"}), "", {}, "quota"),
    ("quota text", O(error="Quota exceeded for model"), "", {}, "quota"),
    ("quota 429", O(error="429 Too Many Requests"), "", {}, "quota"),
    ("quota in stderr", O(), "error: rate limit hit", {}, "quota"),
    ("quota credits", O(error="You are out of credits"), "", {}, "quota"),
    ("auth text", O(error="not logged in"), "", {}, "auth"),
    ("auth tos", O(error="Your account is blocked due to a Terms of Service violation."), "", {}, "auth"),
    ("auth status", O(agy_error={"status": "UNAUTHENTICATED"}), "", {}, "auth"),
    ("auth permission", O(agy_error={"status": "PERMISSION_DENIED"}), "", {}, "auth"),
    ("auth please log in", O(), "Please sign in to continue", {}, "auth"),
    ("model", O(error="invalid model selection (--model x)"), "", {}, "model"),
    ("other", O(error="kaboom"), "", {}, "error"),
    ("missing", O(status="MISSING", error="agy exited 1 without a result"), "", {}, "error"),
    ("no number false positive", O(error="failed at step 14290"), "", {}, "error"),
    ("exit 0 but ERROR status", O(exit_code=0, status="ERROR", error="x"), "", {}, "error"),
    ("timeout beats interrupted", O(exit_code=3, error="interrupted"), "", {}, "timeout"),
    ("interrupted beats quota", O(error="interrupted"), "quota", {}, "interrupted"),
    ("quota beats auth", O(error="quota exceeded, not logged in"), "", {}, "quota"),
    ("auth beats model", O(error="unauthenticated: invalid model selection"), "", {}, "auth"),
]


@pytest.mark.parametrize("name,outcome,stderr,flags,expected", CASES, ids=[c[0] for c in CASES])
def test_classify_table(name, outcome, stderr, flags, expected):
    assert agy.classify(outcome, stderr, **flags) == expected


def test_classify_reads_agy_error_from_stderr_text_when_outcome_has_none():
    line = 'AGY_ERROR: {"status":"RESOURCE_EXHAUSTED","retryable":true}\n'
    assert agy.classify(O(), line) == "quota"


# ---- 5: run_agy with the fake ------------------------------------------------------------------------
def test_run_agy_normal(agw_env, tmp_path):
    brief = "Task ID: t1\nhello wörld\n"
    (code, hard, cancelled), events, err, cwd = run(agw_env, tmp_path, brief)
    assert (code, hard, cancelled) == (0, False, False)
    lines = [json.loads(x) for x in pathlib.Path(events).read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["event"] == "result" and lines[-1]["result"]["status"] == "SUCCESS"
    call = agw_env.calls()[0]
    assert call["stdin"] == brief and call["task_id"] == "t1" and call["cwd"] == str(cwd.resolve())


def test_run_agy_large_brief_does_not_deadlock(agw_env, tmp_path):
    brief = "Task ID: big\n" + ("x" * 99 + "\n") * 50_000        # 5 MB, far larger than any pipe buffer
    (code, hard, cancelled), _, _, _ = run(agw_env, tmp_path, brief, hard=60)
    assert (code, hard, cancelled) == (0, False, False)
    assert agw_env.calls()[0]["stdin"] == brief


def test_run_agy_child_that_never_reads_stdin_is_still_bounded(tmp_path):
    cwd = tmp_path
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    big = "y" * 5_000_000                                        # the pipe fills; the writer thread must not block us
    started = time.monotonic()
    code, hard, cancelled = agy.run_agy(argv, big, str(cwd), str(tmp_path / "e"), str(tmp_path / "s"), 1, threading.Event())
    assert hard is True and cancelled is False
    assert time.monotonic() - started < 20


def test_run_agy_hard_timeout_kills_process_and_children(agw_env, tmp_path):
    agw_env.set_scenarios(default={"sleep": 30, "actions": [
        {"shell": "echo $PPID > fakepid.txt; sleep 300 & echo $! > childpid.txt"}]})
    started = time.monotonic()
    (code, hard, cancelled), events, err, cwd = run(agw_env, tmp_path, hard=1)
    assert time.monotonic() - started < 15
    assert hard is True and cancelled is False
    fake_pid = int((cwd / "fakepid.txt").read_text())
    child_pid = int((cwd / "childpid.txt").read_text())
    assert wait_dead(fake_pid) and wait_dead(child_pid)        # nothing orphaned


def test_run_agy_kills_process_that_ignores_sigterm(agw_env, tmp_path, monkeypatch):
    monkeypatch.setattr(agy, "TERM_WAIT_SECONDS", 1)
    script = tmp_path / "stubborn.py"
    script.write_text("import signal, time, os\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                      "open('pid.txt','w').write(str(os.getpid()))\ntime.sleep(300)\n")
    started = time.monotonic()
    (code, hard, cancelled), _, _, cwd = run(agw_env, tmp_path, hard=0.5, argv=[sys.executable, str(script)])
    assert hard is True and time.monotonic() - started < 10
    assert code == -9                                            # SIGKILL
    assert wait_dead(int((cwd / "pid.txt").read_text()))


def test_run_agy_normal_exit_reaps_leftover_background_children(agw_env, tmp_path):
    agw_env.set_scenarios(default={"actions": [{"shell": "sleep 300 & echo $! > childpid.txt"}]})
    (code, hard, cancelled), _, _, cwd = run(agw_env, tmp_path)
    assert (code, hard, cancelled) == (0, False, False)
    assert wait_dead(int((cwd / "childpid.txt").read_text()))


def test_run_agy_cancel(agw_env, tmp_path):
    agw_env.set_scenarios(default={"sleep": 30, "actions": [{"shell": "echo $PPID > fakepid.txt"}]})
    cancel = threading.Event()
    threading.Timer(0.8, cancel.set).start()
    started = time.monotonic()
    (code, hard, cancelled), events, err, cwd = run(agw_env, tmp_path, hard=120, cancel=cancel)
    assert cancelled is True and hard is False
    assert time.monotonic() - started < 15
    if os.name == "posix":
        assert code == 1                                         # the fake handles SIGTERM and exits 1
    assert wait_dead(int((cwd / "fakepid.txt").read_text()))
    outcome = agy.parse_stream(events, err, code, hard, cancelled)
    assert outcome.kind == "interrupted" and outcome.error == "interrupted"


def test_run_agy_already_cancelled_never_starts(agw_env, tmp_path):
    cancel = threading.Event()
    cancel.set()
    result, events, err, _ = run(agw_env, tmp_path, cancel=cancel)
    assert result[1:] == (False, True)
    assert agw_env.calls() == []
    assert pathlib.Path(events).exists() and pathlib.Path(err).exists()


def test_run_agy_finished_process_is_not_reported_cancelled(agw_env, tmp_path):
    cancel = threading.Event()
    (code, hard, cancelled), _, _, _ = run(agw_env, tmp_path, cancel=cancel)
    cancel.set()
    assert (code, hard, cancelled) == (0, False, False)


def test_run_agy_nonexistent_executable(tmp_path):
    cwd = tmp_path
    result = agy.run_agy(["/nonexistent/agy-xyz", "--x"], "brief", str(cwd), str(tmp_path / "e"), str(tmp_path / "s"),
                         5, threading.Event())
    assert result == (127, False, False)
    assert (tmp_path / "s").read_text() == "agw: cannot execute /nonexistent/agy-xyz\n"
    assert (tmp_path / "e").read_text() == ""


def test_run_agy_missing_cwd_and_non_executable(tmp_path):
    ev, er = str(tmp_path / "e"), str(tmp_path / "s")
    assert agy.run_agy(FAKE, "b", str(tmp_path / "nodir"), ev, er, 5, threading.Event()) == (127, False, False)
    assert "working directory does not exist" in pathlib.Path(er).read_text()
    plain = tmp_path / "plain.txt"
    plain.write_text("not executable")
    assert agy.run_agy([str(plain)], "b", str(tmp_path), ev, er, 5, threading.Event()) == (127, False, False)
    assert "cannot execute" in pathlib.Path(er).read_text()


def test_run_agy_child_dying_early_with_unread_stdin(tmp_path):
    argv = [sys.executable, "-c", "import sys; sys.exit(4)"]
    code, hard, cancelled = agy.run_agy(argv, "z" * 3_000_000, str(tmp_path), str(tmp_path / "e"), str(tmp_path / "s"),
                                        30, threading.Event())
    assert (code, hard, cancelled) == (4, False, False)


def test_run_agy_heavy_stdout_and_stderr_do_not_deadlock(tmp_path):
    code_src = ("import sys\nfor i in range(20000):\n    sys.stdout.write('o'*100+'\\n'); sys.stderr.write('e'*100+'\\n')\n")
    code, hard, cancelled = agy.run_agy([sys.executable, "-c", code_src], "", str(tmp_path),
                                        str(tmp_path / "e"), str(tmp_path / "s"), 60, threading.Event())
    assert (code, hard, cancelled) == (0, False, False)
    assert (tmp_path / "e").stat().st_size == 20000 * 101 and (tmp_path / "s").stat().st_size == 20000 * 101


def test_run_agy_print_timeout_end_to_end(agw_env, tmp_path):
    agw_env.set_scenarios(default={"sleep": 30, "fixture": "print-timeout.ndjson"})
    argv = agy.build_argv(FAKE, "m", "/s.json", "{}", 1, False)
    (code, hard, cancelled), events, err, _ = run(agw_env, tmp_path, argv=argv)
    assert code == 3 and not hard and not cancelled
    o = agy.parse_stream(events, err, code, hard, cancelled)
    assert o.kind == "timeout" and o.agy_error["reason"] == "print_timeout"


def test_run_agy_passes_env(tmp_path):
    out = tmp_path / "out.txt"
    argv = [sys.executable, "-c", f"import os; open({str(out)!r},'w').write(os.environ.get('AGW_T',''))"]
    agy.run_agy(argv, "", str(tmp_path), str(tmp_path / "e"), str(tmp_path / "s"), 30, threading.Event(),
                env={**os.environ, "AGW_T": "yes"})
    assert out.read_text() == "yes"


# ---- 6: check_available ------------------------------------------------------------------------------
def test_check_available_with_fake():
    assert agy.check_available(FAKE) == (True, "1.5.0")


def test_check_available_not_found():
    ok, detail = agy.check_available(["/nonexistent/agy"])
    assert ok is False
    assert detail == ("agy not found (agy_path=['/nonexistent/agy']). "
                      "Install the Antigravity CLI and run 'agy' once to log in.")
    assert agy.check_available([])[0] is False
    assert agy.check_available(["definitely-not-a-binary-xyz"])[0] is False


def test_check_available_version_failure(tmp_path):
    script = tmp_path / "bad.py"
    script.write_text("import sys\nsys.stderr.write('\\nboom happened\\nmore\\n')\nsys.exit(2)\n")
    ok, detail = agy.check_available([sys.executable, str(script)])
    assert ok is False and detail == f"'{sys.executable} {script} --version' failed: boom happened"


def test_check_available_non_executable_file(tmp_path):
    plain = tmp_path / "plain"
    plain.write_text("x")
    ok, detail = agy.check_available([str(plain)])
    assert ok is False and "failed" in detail


# ---- 7: live_check -------------------------------------------------------------------------------------
def test_live_check_ok(agw_env):
    kind, detail = agy.live_check(FAKE, "gemini-3.8-flash-low", timeout_seconds=30)
    assert (kind, detail) == ("ok", "model gemini-3.8-flash-low replied")
    argv = agw_env.calls()[0]["argv"]
    assert "--output-format" in argv and "json" in argv and "--disable-slash-commands" in argv
    assert agw_env.calls()[0]["stdin"] == "Reply with exactly: PONG"
    assert agw_env.calls()[0]["cwd"] != str(agw_env.repo)         # ran in a fresh temp dir


def test_live_check_auth_failure(agw_env):
    agw_env.set_scenarios(default={"result_status": "ERROR", "error": "not logged in", "exit_code": 1})
    assert agy.live_check(FAKE, "m", timeout_seconds=30) == ("auth", "not logged in")


def test_live_check_other_kinds(agw_env):
    agw_env.set_scenarios(default={"result_status": "ERROR", "error": "Quota exceeded\nsecond line", "exit_code": 1})
    assert agy.live_check(FAKE, "m", 30) == ("quota", "Quota exceeded")
    agw_env.set_scenarios(default={"result_status": "ERROR", "error": "invalid model selection x", "exit_code": 1})
    assert agy.live_check(FAKE, "m", 30)[0] == "model"
    agw_env.set_scenarios(default={"sleep": 30})
    kind, detail = agy.live_check(FAKE, "m", timeout_seconds=1)
    assert kind == "timeout"


def test_live_check_no_envelope(tmp_path):
    script = tmp_path / "noisy.py"
    script.write_text("import sys\nprint('not json')\nsys.stderr.write('crashed hard\\n')\nsys.exit(1)\n")
    assert agy.live_check([sys.executable, str(script)], "m", 30) == ("error", "crashed hard")


def test_live_check_unlaunchable():
    kind, detail = agy.live_check(["/nonexistent/agy"], "m", 30)
    assert kind == "error" and "/nonexistent/agy" in detail
