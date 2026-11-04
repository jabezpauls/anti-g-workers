#!/usr/bin/env python3
"""Fake `agy` for tests: replays NDJSON fixtures under scripted scenarios (stdlib only, no network).

Behaviour is specified in docs/superpowers/specs/2026-11-05-ag-workers-design.md section 14.
Environment variables:
  FAKE_AGY_LOG        append one JSON line per run: {"argv", "cwd", "stdin", "task_id"}
  FAKE_AGY_SCENARIOS  JSON: {"default": <Scenario>, "tasks": {"<task id>": <Scenario> | [<Scenario>, ...]}}
  FAKE_AGY_STATE      directory for per-task call counters (default: dirname of FAKE_AGY_LOG)
  FAKE_AGY_OUTSIDE   value substituted for __OUTSIDE__ (default /nonexistent-outside)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, os.pardir, "fixtures")

DEFAULT_USAGE = {"input_tokens": 1000, "output_tokens": 100, "thinking_tokens": 0,
                 "cache_read_tokens": 0, "total_tokens": 1100}
DEFAULT_STRUCTURED = {"status": "done", "summary": "fake worker done", "findings": "",
                      "files_changed": [], "blocked_actions": [], "concerns": []}

# Mutable run state so the SIGTERM handler can emit a well-formed result.
_STATE: dict[str, Any] = {"json_mode": False, "conversation_id": "fake", "usage": DEFAULT_USAGE}


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj), flush=True)


def parse_duration(text: str) -> float | None:
    """Parse '120s', '2m', '1h30m', '0s' into seconds. Returns None when unparsable."""
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?", text.strip())
    if not m or not any(m.groups()):
        return None
    h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + s


def flag_value(args: list[str], name: str) -> str | None:
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def subst_text(text: str, cwd: str, outside: str, *, json_escaped: bool) -> str:
    """Replace the __CWD__ / __OUTSIDE__ placeholders; json_escaped makes the values safe inside JSON strings."""
    def enc(v: str) -> str:
        return json.dumps(v)[1:-1] if json_escaped else v
    return text.replace("__CWD__", enc(cwd)).replace("__OUTSIDE__", enc(outside))


def subst_obj(obj: Any, cwd: str, outside: str) -> Any:
    return json.loads(subst_text(json.dumps(obj), cwd, outside, json_escaped=True))


def pick_scenario(task_id: str) -> dict[str, Any]:
    path = os.environ.get("FAKE_AGY_SCENARIOS")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    scen = (data.get("tasks") or {}).get(task_id, data.get("default") or {})
    if isinstance(scen, list):
        log = os.environ.get("FAKE_AGY_LOG")
        state = os.environ.get("FAKE_AGY_STATE") or (os.path.dirname(os.path.abspath(log)) if log else tempfile.gettempdir())
        os.makedirs(state, exist_ok=True)
        counter = os.path.join(state, (task_id or "_default") + ".count")
        n = 0
        if os.path.exists(counter):
            with open(counter, encoding="utf-8") as fh:
                n = int(fh.read().strip() or 0)
        with open(counter, "w", encoding="utf-8") as fh:
            fh.write(str(n + 1))
        if not scen:
            return {}
        scen = scen[min(n, len(scen) - 1)]
    return scen or {}


def run_actions(actions: list[dict[str, Any]], cwd: str, outside: str) -> None:
    for act in actions:
        if "write" in act:
            p = subst_text(act["write"], cwd, outside, json_escaped=False)
            p = p if os.path.isabs(p) else os.path.join(cwd, p)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write(act.get("content", ""))
        elif "delete" in act:
            p = subst_text(act["delete"], cwd, outside, json_escaped=False)
            p = p if os.path.isabs(p) else os.path.join(cwd, p)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.lexists(p):
                os.remove(p)
        elif "shell" in act:
            cmd = subst_text(act["shell"], cwd, outside, json_escaped=False)
            subprocess.run(cmd, shell=True, cwd=cwd)
        elif "sleep" in act:
            time.sleep(float(act["sleep"]))


def load_fixture(name: str, cwd: str, outside: str) -> list[dict[str, Any]]:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        text = subst_text(fh.read(), cwd, outside, json_escaped=True)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def build_result(scen: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "conversation_id": _STATE["conversation_id"],
        "status": scen.get("result_status", "SUCCESS"),
        "response": "DONE\n",
        "duration_seconds": 0.1,
        "num_turns": 1,
        "usage": _STATE["usage"],
    }
    structured = scen["structured_output"] if "structured_output" in scen else DEFAULT_STRUCTURED
    if structured is not None:
        result["response"] = json.dumps(structured)
        result["structured_output"] = structured
        result["json_schema"] = {"type": "object"}
    if scen.get("error"):
        result["error"] = scen["error"]
    if scen.get("finished_reason"):
        result["finished_reason"] = scen["finished_reason"]
    return result


def emit_result(result: dict[str, Any]) -> None:
    emit(result if _STATE["json_mode"] else {"event": "result", "result": result})


def on_sigterm(signum: int, frame: Any) -> None:
    result = {
        "conversation_id": _STATE["conversation_id"], "status": "ERROR", "response": "", "error": "interrupted",
        "duration_seconds": 0.1, "num_turns": 1, "usage": _STATE["usage"], "finished_reason": "interrupted",
    }
    emit_result(result)
    sys.stderr.write("error: interrupted\n")
    sys.stderr.flush()
    sys.stdout.flush()
    os._exit(1)


def timeout_stderr(seconds: str) -> str:
    with open(os.path.join(FIXTURES, "print-timeout.stderr.txt"), encoding="utf-8") as fh:
        return fh.read().replace("5s", seconds + "s")


def main(argv: list[str]) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    if argv and argv[0] == "--version":
        print("1.5.0")
        return 0
    if argv and argv[0] == "models":
        print("gemini-3.8-flash-low\tGemini 3.8 Flash (Low)")
        print("gemini-3.8-flash-high\tGemini 3.8 Flash (High)")
        return 0

    timeout_raw = flag_value(argv, "--print-timeout")
    timeout = parse_duration(timeout_raw) if timeout_raw else None
    _STATE["json_mode"] = flag_value(argv, "--output-format") == "json"
    if hasattr(signal, "SIGTERM") and os.name == "posix":
        signal.signal(signal.SIGTERM, on_sigterm)

    prompt = sys.stdin.read()
    m = re.search(r"^Task ID: (\S+)$", prompt, re.M)
    task_id = m.group(1) if m else ""
    cwd = os.getcwd()
    outside = os.environ.get("FAKE_AGY_OUTSIDE", "/nonexistent-outside")

    log = os.environ.get("FAKE_AGY_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": argv, "cwd": cwd, "stdin": prompt, "task_id": task_id}) + "\n")

    scen = pick_scenario(task_id)
    events = load_fixture(scen.get("fixture", "minimal-success.ndjson"), cwd, outside)

    fixture_result: dict[str, Any] | None = None
    for evt in events:
        if evt.get("event") == "init":
            _STATE["conversation_id"] = evt.get("conversation_id") or "fake"
        elif evt.get("event") == "result":
            fixture_result = evt.get("result") or {}
    if fixture_result and fixture_result.get("usage"):
        _STATE["usage"] = fixture_result["usage"]

    run_actions(scen.get("actions", []), cwd, outside)

    if not _STATE["json_mode"]:
        for evt in events:
            if evt.get("event") != "result":
                emit(evt)
        for evt in scen.get("extra_events", []):
            emit(subst_obj(evt, cwd, outside))

    sleep = float(scen.get("sleep", 0) or 0)
    if sleep:
        if timeout and sleep > timeout:
            time.sleep(timeout)
            secs = str(int(timeout)) if float(timeout).is_integer() else str(timeout)
            result = build_result({"result_status": "ERROR", "finished_reason": "print_timeout",
                                   "structured_output": None,
                                   "error": f"print timeout after {secs}s with the turn still in progress; the response is partial"})
            result["response"] = ""
            sys.stderr.write(timeout_stderr(secs))
            sys.stderr.flush()
            emit_result(result)
            return 3
        time.sleep(sleep)

    if scen.get("stderr"):
        sys.stderr.write(scen["stderr"])
        sys.stderr.flush()
    emit_result(build_result(scen))
    return int(scen.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
