"""Tests for the fake agy harness (tests/fake_agy/fake_agy.py), run as a real subprocess."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from conftest import FAKE_AGY

DEFAULT_REPORT = {"status": "done", "summary": "fake worker done", "findings": "",
                  "files_changed": [], "blocked_actions": [], "concerns": []}


def run_fake(args, cwd, *, stdin="Task ID: abc\n", scenarios=None, tmp_path=None, outside=None, timeout=30):
    env = dict(os.environ)
    for k in ("FAKE_AGY_LOG", "FAKE_AGY_SCENARIOS", "FAKE_AGY_STATE", "FAKE_AGY_OUTSIDE"):
        env.pop(k, None)
    if tmp_path is not None:
        env["FAKE_AGY_LOG"] = str(tmp_path / "log.jsonl")
        env["FAKE_AGY_STATE"] = str(tmp_path / "state")
        if scenarios is not None:
            (tmp_path / "scenarios.json").write_text(json.dumps(scenarios), encoding="utf-8")
            env["FAKE_AGY_SCENARIOS"] = str(tmp_path / "scenarios.json")
    if outside is not None:
        env["FAKE_AGY_OUTSIDE"] = str(outside)
    return subprocess.run([sys.executable, str(FAKE_AGY), *args], input=stdin, cwd=str(cwd), env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=timeout)


def events(proc):
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


@pytest.fixture
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


def test_version_and_models(work):
    v = run_fake(["--version"], work)
    assert v.returncode == 0 and v.stdout.strip() == "1.5.0"
    models = run_fake(["models"], work)
    assert models.returncode == 0
    lines = models.stdout.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("gemini-3.8-flash-low\t")
    assert lines[1].startswith("gemini-3.8-flash-high\t")


def test_default_scenario_streams_ndjson_and_logs(work, tmp_path):
    p = run_fake(["--output-format", "stream-json", "--model", "m"], work, tmp_path=tmp_path)
    assert p.returncode == 0
    evts = events(p)
    assert evts[0]["event"] == "init"
    assert evts[-1]["event"] == "result"
    res = evts[-1]["result"]
    assert res["status"] == "SUCCESS"
    assert res["structured_output"] == DEFAULT_REPORT
    assert json.loads(res["response"]) == DEFAULT_REPORT
    assert res["usage"]["input_tokens"] == 1000
    logs = [json.loads(x) for x in (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(logs) == 1
    assert logs[0]["task_id"] == "abc"
    assert logs[0]["stdin"] == "Task ID: abc\n"
    assert logs[0]["argv"] == ["--output-format", "stream-json", "--model", "m"]
    assert os.path.realpath(logs[0]["cwd"]) == os.path.realpath(work)


def test_fixture_placeholders_and_result_replacement(work, tmp_path):
    p = run_fake(["--output-format", "stream-json"], work, tmp_path=tmp_path,
                 scenarios={"default": {"fixture": "edit-existing-file.ndjson"}})
    assert p.returncode == 0
    evts = events(p)
    assert "__CWD__" not in p.stdout
    assert evts[0]["init"]["cwd"] == str(work)
    tool_paths = [e["step_update"]["tool_info"]["parameters"]["Path"] for e in evts
                  if e["event"] == "step_update" and e["step_update"].get("step_type") == "tool"
                  and "Path" in e["step_update"].get("tool_info", {}).get("parameters", {})]
    assert tool_paths and all(tp.startswith(str(work)) for tp in tool_paths)
    results = [e for e in evts if e["event"] == "result"]
    assert len(results) == 1 and evts[-1]["event"] == "result"
    assert json.loads(results[0]["result"]["response"]) == DEFAULT_REPORT


def test_actions_write_in_cwd_and_outside(work, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    scen = {"default": {"actions": [
        {"write": "sub/dir/a.txt", "content": "hello"},
        {"write": "__OUTSIDE__/o.txt", "content": "out"},
        {"write": "gone.txt", "content": "x"},
        {"delete": "gone.txt"},
        {"shell": "echo shelled > shell.txt"},
    ]}}
    p = run_fake(["--output-format", "stream-json"], work, tmp_path=tmp_path, scenarios=scen, outside=outside)
    assert p.returncode == 0
    assert (work / "sub" / "dir" / "a.txt").read_text(encoding="utf-8") == "hello"
    assert (outside / "o.txt").read_text(encoding="utf-8") == "out"
    assert not (work / "gone.txt").exists()
    assert (work / "shell.txt").read_text(encoding="utf-8").strip() == "shelled"


def test_per_task_list_scenario_uses_nth_item(work, tmp_path):
    scen = {"default": {}, "tasks": {"abc": [{"exit_code": 1, "result_status": "ERROR", "error": "boom"},
                                              {"exit_code": 0}]}}
    first = run_fake(["--output-format", "stream-json"], work, tmp_path=tmp_path, scenarios=scen)
    second = run_fake(["--output-format", "stream-json"], work, tmp_path=tmp_path, scenarios=scen)
    third = run_fake(["--output-format", "stream-json"], work, tmp_path=tmp_path, scenarios=scen)
    assert first.returncode == 1
    assert events(first)[-1]["result"]["status"] == "ERROR"
    assert events(first)[-1]["result"]["error"] == "boom"
    assert second.returncode == 0 and third.returncode == 0   # clamped to the last item
    assert events(second)[-1]["result"]["status"] == "SUCCESS"


def test_print_timeout(work, tmp_path):
    started = time.monotonic()
    p = run_fake(["--output-format", "stream-json", "--print-timeout", "1s"], work, tmp_path=tmp_path,
                 scenarios={"default": {"sleep": 5}})
    assert time.monotonic() - started < 4
    assert p.returncode == 3
    res = events(p)[-1]["result"]
    assert res["status"] == "ERROR"
    assert res["finished_reason"] == "print_timeout"
    assert "print timeout after 1s" in res["error"]
    assert "AGY_ERROR: " in p.stderr and '"reason":"print_timeout"' in p.stderr
    assert "5s" not in p.stderr


def test_structured_output_null_is_omitted(work, tmp_path):
    p = run_fake(["--output-format", "stream-json"], work, tmp_path=tmp_path,
                 scenarios={"default": {"structured_output": None}})
    res = events(p)[-1]["result"]
    assert "structured_output" not in res
    assert res["response"] == "DONE\n"


@pytest.mark.skipif(os.name != "posix", reason="SIGTERM semantics are POSIX-only")
def test_sigterm_emits_interrupted_result(work, tmp_path):
    scen = tmp_path / "scenarios.json"
    scen.write_text(json.dumps({"default": {"sleep": 30}}), encoding="utf-8")
    env = dict(os.environ, FAKE_AGY_SCENARIOS=str(scen))
    env.pop("FAKE_AGY_LOG", None)
    proc = subprocess.Popen([sys.executable, str(FAKE_AGY), "--output-format", "stream-json"], cwd=str(work),
                            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8")
    proc.stdin.write("Task ID: abc\n")
    proc.stdin.close()
    first = proc.stdout.readline()          # the init event proves the SIGTERM handler is installed
    assert json.loads(first)["event"] == "init"
    time.sleep(0.3)
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 1
    last = json.loads(out.strip().splitlines()[-1])
    assert last["event"] == "result"
    assert last["result"]["status"] == "ERROR"
    assert last["result"]["error"] == "interrupted"
    assert "interrupted" in err


def test_json_output_format_prints_only_the_envelope(work, tmp_path):
    p = run_fake(["--output-format", "json"], work, tmp_path=tmp_path)
    assert p.returncode == 0
    lines = [line for line in p.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["status"] == "SUCCESS"
    assert "event" not in obj


def test_conftest_fixtures_wire_the_fake_agy(agw_env):
    import tempfile
    import tomllib

    cfg = tomllib.loads(agw_env.config_path.read_text(encoding="utf-8"))
    assert cfg["agy_path"] == [sys.executable, str(FAKE_AGY)]
    assert cfg["deny_write"] == [] and cfg["worktree_dir"] == str(agw_env.worktree_dir)
    assert os.getcwd() == str(agw_env.repo)
    assert not tempfile.gettempdir().startswith(str(agw_env.outside))
    assert agw_env.calls() == []
    agw_env.set_scenarios(tasks={"t1": {"actions": [{"write": "f.txt", "content": "x"}]}})
    proc = subprocess.run([sys.executable, str(FAKE_AGY), "--output-format", "stream-json"], input="Task ID: t1\n",
                          capture_output=True, text=True, cwd=str(agw_env.repo))
    assert proc.returncode == 0
    assert (agw_env.repo / "f.txt").read_text(encoding="utf-8") == "x"
    assert [c["task_id"] for c in agw_env.calls()] == ["t1"]
