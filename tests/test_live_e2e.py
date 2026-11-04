"""Opt-in end-to-end test against the REAL agy CLI. Skipped unless AGW_LIVE=1 (run with `-s` to see the summary).

    AGW_LIVE=1 .venv/bin/python -m pytest -q -s -m live tests/test_live_e2e.py

It spends a little Gemini quota: one edit worker and one read worker on a throwaway repository.
AGW_LIVE_MODEL picks the model (default gemini-3.8-flash-low).
"""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess
import sys

import pytest

from conftest import REPO_ROOT, run_git, toml_dumps

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("AGW_LIVE") != "1", reason="set AGW_LIVE=1 to run against real agy"),
]

AGW = [sys.executable, str(REPO_ROOT / "bin" / "agw")]


def agw(args: list[str], cwd: pathlib.Path, env: dict[str, str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([*AGW, *args], input=stdin, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=1500)


def test_live_edit_and_read_task(tmp_path: pathlib.Path) -> None:
    repo = pathlib.Path(os.path.realpath(tmp_path)) / "repo"
    repo.mkdir()
    run_git(["init", "-q", "-b", "main"], repo)
    run_git(["config", "user.name", "Live Test"], repo)
    run_git(["config", "user.email", "live@example.com"], repo)
    run_git(["config", "commit.gpgsign", "false"], repo)
    (repo / "calc.py").write_text("def mul(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "tests").mkdir()
    test_cmd = f"{shlex.quote(sys.executable)} -m pytest -q tests/test_calc.py"
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 12\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    run_git(["add", "-A"], repo)
    run_git(["commit", "-q", "-m", "init"], repo)

    config = tmp_path / "config.toml"
    config.write_text(toml_dumps({
        "model": os.environ.get("AGW_LIVE_MODEL", "gemini-3.8-flash-low"),
        "timeout_seconds": 600,
        "deny_write": [],
        "worktree_dir": str(tmp_path / "wt"),
    }), encoding="utf-8")
    env = {**os.environ, "AGW_CONFIG": str(config)}
    env.pop("AGW_AGY", None)

    tasks = {"tasks": [
        {"id": "fix-mul", "mode": "edit", "title": "Fix mul",
         "goal": "Fix calc.py so that mul(a, b) returns the product of a and b.",
         "files": ["calc.py", "tests/test_calc.py"], "scope": ["calc.py"], "accept": [test_cmd]},
        {"id": "explain", "goal": "Describe what calc.py contains and what the test checks."},
    ]}
    proc = agw(["run", "-"], repo, env, stdin=json.dumps(tasks))
    print(proc.stdout)
    print(proc.stderr, file=sys.stderr)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[ok] fix-mul edit" in proc.stdout and "accept: pass (1/1)" in proc.stdout
    assert "[ok] explain read" in proc.stdout
    explain = proc.stdout.split("[ok] explain read", 1)[1]
    assert explain.split("findings:", 1)[1].split("next:", 1)[0].strip(), "the read task returned no findings"

    # The worker must not have touched the main tree.
    assert run_git(["status", "--porcelain"], repo) == ""
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def mul(a, b):\n    return a + b\n"

    applied = agw(["apply", "last", "fix-mul"], repo, env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert applied.stdout.startswith("applied fix-mul")
    test = subprocess.run(shlex.split(test_cmd), cwd=repo, capture_output=True, text=True)
    assert test.returncode == 0, test.stdout + test.stderr

    cleaned = agw(["clean", "--all"], repo, env)
    assert cleaned.returncode == 0, cleaned.stdout + cleaned.stderr
    assert run_git(["branch", "--list", "agw/*"], repo).strip() == ""
    assert not list((tmp_path / "wt").glob("*/*"))
