"""Shared pytest fixtures: temporary git repos and an isolated agw environment wired to the fake agy."""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FAKE_AGY = REPO_ROOT / "tests" / "fake_agy" / "fake_agy.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def toml_dumps(data: dict) -> str:
    """Serialize str / int / bool / list[str] values as TOML `key = value` lines."""
    lines = []
    for key, value in data.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        elif isinstance(value, (str, list)):
            rendered = json.dumps(value)      # JSON strings and arrays of strings are valid TOML
        else:
            raise TypeError(f"unsupported TOML value for {key!r}: {value!r}")
        lines.append(f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def run_git(args: list[str], cwd: pathlib.Path) -> str:
    """Run git in cwd (check=True) and return stdout."""
    proc = subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return proc.stdout


@pytest.fixture
def git_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A small committed git repo at tmp_path/'repo' (branch main). Returns its realpath."""
    repo = tmp_path / "repo"
    repo.mkdir()
    repo = pathlib.Path(os.path.realpath(repo))
    run_git(["init", "-q", "-b", "main"], repo)
    run_git(["config", "user.name", "Test"], repo)
    run_git(["config", "user.email", "test@example.com"], repo)
    run_git(["config", "commit.gpgsign", "false"], repo)
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    run_git(["add", "-A"], repo)
    run_git(["commit", "-q", "-m", "init"], repo)
    return repo


@dataclasses.dataclass
class AgwEnv:
    root: pathlib.Path
    repo: pathlib.Path
    config_path: pathlib.Path
    worktree_dir: pathlib.Path
    outside: pathlib.Path
    log_path: pathlib.Path
    scenarios_path: pathlib.Path
    state_dir: pathlib.Path

    def write_config(self, **overrides: object) -> None:
        """Write the user config file (AGW_CONFIG) pointing agw at the fake agy; overrides win."""
        base: dict[str, object] = {
            "agy_path": [sys.executable, str(FAKE_AGY)],
            "worktree_dir": str(self.worktree_dir),
            "deny_write": [],
        }
        base.update(overrides)
        self.config_path.write_text(toml_dumps(base), encoding="utf-8")

    def set_scenarios(self, default: dict | None = None, tasks: dict | None = None) -> None:
        """Write the fake agy scenario file and reset its per-task call counters."""
        self.scenarios_path.write_text(
            json.dumps({"default": default or {}, "tasks": tasks or {}}), encoding="utf-8")
        for counter in self.state_dir.glob("*.count"):
            counter.unlink()

    def calls(self) -> list[dict]:
        """Parsed records the fake agy logged, oldest first ([] if it was never called)."""
        if not self.log_path.exists():
            return []
        text = self.log_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.fixture
def agw_env(tmp_path: pathlib.Path, git_repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> AgwEnv:
    """Isolated environment: temp repo, fake agy config, scenario/log files, and a private 'system temp'."""
    real = os.path.realpath
    root = pathlib.Path(real(tmp_path))
    outside = root / "outside"
    worktree_dir = root / "wt"
    state_dir = root / "fake-state"
    systmp = root / "systmp"
    for d in (outside, worktree_dir, state_dir, systmp):
        d.mkdir()
    env = AgwEnv(
        root=root, repo=git_repo, config_path=root / "config.toml",
        worktree_dir=pathlib.Path(real(worktree_dir)), outside=pathlib.Path(real(outside)),
        log_path=root / "fake-agy.log.jsonl", scenarios_path=root / "scenarios.json",
        state_dir=pathlib.Path(real(state_dir)),
    )
    env.write_config()
    env.set_scenarios()
    monkeypatch.setenv("AGW_CONFIG", str(env.config_path))
    monkeypatch.setenv("FAKE_AGY_LOG", str(env.log_path))
    monkeypatch.setenv("FAKE_AGY_SCENARIOS", str(env.scenarios_path))
    monkeypatch.setenv("FAKE_AGY_OUTSIDE", str(env.outside))
    monkeypatch.setenv("FAKE_AGY_STATE", str(env.state_dir))
    monkeypatch.delenv("AGW_AGY", raising=False)
    monkeypatch.chdir(git_repo)
    # Keep the "system temp dir" that agw sees disjoint from outside/ and wt/, otherwise the
    # temp-dir ignore rules would hide the out-of-scope writes the tests want to detect.
    monkeypatch.setattr(tempfile, "tempdir", str(pathlib.Path(real(systmp))))
    return env
