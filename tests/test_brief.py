"""Tests for brief rendering: golden strings written by hand from spec section 10."""
from __future__ import annotations

import re

import pytest

from agw.brief import effective_accept, render_brief
from agw.models import Config, Task

RULES_AND_REPORT_HEAD = """\
## Rules
1. Stay inside the working directory. Never write outside it, except throwaway files in the system temp directory.
2. Never ask questions; nobody will answer. If something is unclear, choose the most reasonable interpretation and note it in `concerns`.
3. Do not run git commands that change state (commit, push, pull, reset, checkout, switch, rebase, merge, stash, clean, branch, tag). Your changes are collected automatically.
4. Do not install packages or edit dependency manifests or lockfiles unless a step tells you to.
5. If a tool call is denied or fails and you cannot work around it within these rules, record it in `blocked_actions` and continue with the rest of the task.
6. Never claim success you have not verified. If the goal is not fully met, use status "partial" or "blocked" and say why in `summary`.
7. Finish with the JSON report described below and nothing else.

## Report
Return a JSON object with these fields:
- status: "done" if the goal is fully met and verified, "partial" if only part is done, "blocked" if you could not make progress.
- summary: 1-3 sentences on what you did and the result.
"""

REPORT_TAIL = """\
- files_changed: paths (relative to the working directory) you created, modified or deleted. Use [] if none.
- blocked_actions: each denied or failed action you could not work around. Use [] if none.
- concerns: risks, assumptions and anything the reviewer should double-check. Use [] if none.
"""

INTRO = """\
# Worker brief

Task ID: {id}
Mode: {mode}
Working directory: {wt}

You are a worker agent. A senior engineer wrote this brief and will review your work. Follow it exactly.

## Goal
{goal}

"""


def test_golden_edit_task_all_fields() -> None:
    task = Task(
        id="add-retry", mode="edit", title="ignored in brief", goal="Add retry to HttpClient.get.",
        context=["Task paragraph."], files=["src/http.py", "tests/test_http.py"],
        steps=["Add _with_retry.", "Use it in get."], scope=["src/http.py", "tests/"],
        constraints=["No new dependencies."], report="the design choice", attempt=2,
    )
    got = render_brief(task, ["Shared paragraph."], "/wt/run/add-retry", ["pytest -q", "ruff check"])
    expected = (
        INTRO.format(id="add-retry", mode="EDIT", wt="/wt/run/add-retry", goal="Add retry to HttpClient.get.")
        + """\
## Context
Shared paragraph.

Task paragraph.

## Read these files first
- src/http.py
- tests/test_http.py

## Steps
1. Add _with_retry.
2. Use it in get.

## Scope
Create, modify or delete ONLY files matching these paths (relative to the working directory):
- src/http.py
- tests/
Do not touch any other file. If the goal needs a change outside this scope, do not make it; describe it in `concerns`.

## Constraints
- No new dependencies.

## Acceptance checks
After you finish, these commands will be run in the working directory. Each must exit with code 0:
- `pytest -q`
- `ruff check`
Run them yourself before you finish. If one fails, fix the cause within scope and run it again.

"""
        + RULES_AND_REPORT_HEAD
        + '- findings: notes the reviewer needs (design decisions, assumptions, follow-ups). '
          'At most 120 words. Use "" if none. Cover: the design choice\n'
        + REPORT_TAIL
    )
    assert got == expected


def test_golden_read_task_minimal() -> None:
    task = Task(id="map-auth", goal="Explain authentication.")
    got = render_brief(task, [], "/wt/map-auth", [])
    expected = (
        INTRO.format(id="map-auth", mode="READ-ONLY", wt="/wt/map-auth", goal="Explain authentication.")
        + """\
## Scope
READ-ONLY. Do not create, modify or delete any file, and do not run commands that write files (formatters, code generators, package installs, builds that write into the project). Reading files and running read-only commands or tests is allowed. File-writing tools are disabled for this task.

"""
        + RULES_AND_REPORT_HEAD
        + "- findings: your answer to the goal. Be specific: file paths, function or class names, "
          "line numbers, short code excerpts where they help. No filler. At most 400 words.\n"
        + REPORT_TAIL
    )
    assert got == expected
    for heading in ("## Context", "## Read these files first", "## Steps", "## Constraints",
                    "## Acceptance checks"):
        assert heading not in got


def test_edit_without_scope_uses_minimal_change_text_and_report_cover() -> None:
    task = Task(id="t", mode="edit", goal="Do it.", report="what changed")
    got = render_brief(task, [], "/wt", [])
    assert ("## Scope\nChange only the files the goal requires. Keep the change minimal: "
            "no unrelated refactors, reformatting or renames.\n\n## Rules") in got
    assert "ONLY files matching" not in got
    assert "Use \"\" if none. Cover: what changed\n" in got


def test_read_report_cover_suffix() -> None:
    task = Task(id="t", goal="Q?", report="entry points")
    got = render_brief(task, [], "/wt", [])
    assert "At most 400 words. Cover: entry points\n" in got


def test_shared_context_alone_and_task_context_alone() -> None:
    only_shared = render_brief(Task(id="t", goal="g"), ["S1", "S2"], "/w", [])
    assert "## Context\nS1\n\nS2\n\n## Scope" in only_shared
    only_task = render_brief(Task(id="t", goal="g", context=["T1"]), [], "/w", [])
    assert "## Context\nT1\n\n## Scope" in only_task


@pytest.mark.parametrize("mode,task_accept,cfg_accept,expected", [
    ("edit", None, ["cfg"], ["cfg"]),
    ("edit", ["own"], ["cfg"], ["own"]),
    ("read", None, ["cfg"], []),
    ("read", ["own"], ["cfg"], ["own"]),
    ("edit", [], ["cfg"], []),
    ("read", [], ["cfg"], []),
])
def test_effective_accept(mode: str, task_accept: list[str] | None, cfg_accept: list[str],
                          expected: list[str]) -> None:
    task = Task(id="t", goal="g", mode=mode, accept=task_accept)
    assert effective_accept(task, Config(accept=cfg_accept)) == expected


def test_effective_accept_returns_copy() -> None:
    cfg = Config(accept=["x"])
    result = effective_accept(Task(id="t", goal="g", mode="edit"), cfg)
    result.append("y")
    assert cfg.accept == ["x"]


@pytest.mark.parametrize("mode", ["read", "edit"])
def test_task_id_line_single_trailing_newline_no_markers(mode: str) -> None:
    task = Task(id="abc-1", goal="g", mode=mode, scope=["x"] if mode == "edit" else [])
    got = render_brief(task, ["c"], "/w", ["a"])
    assert re.search(r"^Task ID: (\S+)$", got, re.M).group(1) == "abc-1"
    assert got.endswith("\n") and not got.endswith("\n\n")
    assert "⟨" not in got and "⟩" not in got
    assert "\n\n\n" not in got
