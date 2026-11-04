"""Render the fixed worker brief (spec section 10) from a task. Pure: no I/O."""
from __future__ import annotations

from agw.models import Config, Task

_INTRO = "You are a worker agent. A senior engineer wrote this brief and will review your work. Follow it exactly."

_SCOPE_EDIT_LIMITED_HEAD = (
    "Create, modify or delete ONLY files matching these paths (relative to the working directory):"
)
_SCOPE_EDIT_LIMITED_TAIL = (
    "Do not touch any other file. If the goal needs a change outside this scope, do not make it; "
    "describe it in `concerns`."
)
_SCOPE_EDIT_FREE = (
    "Change only the files the goal requires. Keep the change minimal: "
    "no unrelated refactors, reformatting or renames."
)
_SCOPE_READ = (
    "READ-ONLY. Do not create, modify or delete any file, and do not run commands that write files "
    "(formatters, code generators, package installs, builds that write into the project). "
    "Reading files and running read-only commands or tests is allowed. "
    "File-writing tools are disabled for this task."
)

_ACCEPT_HEAD = (
    "After you finish, these commands will be run in the working directory. Each must exit with code 0:"
)
_ACCEPT_TAIL = "Run them yourself before you finish. If one fails, fix the cause within scope and run it again."

_RULES = """\
## Rules
1. Stay inside the working directory. Never write outside it, except throwaway files in the system temp directory.
2. Never ask questions; nobody will answer. If something is unclear, choose the most reasonable interpretation and note it in `concerns`.
3. Do not run git commands that change state (commit, push, pull, reset, checkout, switch, rebase, merge, stash, clean, branch, tag). Your changes are collected automatically.
4. Do not install packages or edit dependency manifests or lockfiles unless a step tells you to.
5. If a tool call is denied or fails and you cannot work around it within these rules, record it in `blocked_actions` and continue with the rest of the task.
6. Never claim success you have not verified. If the goal is not fully met, use status "partial" or "blocked" and say why in `summary`.
7. Finish with the JSON report described below and nothing else."""

_FINDINGS_READ = (
    "your answer to the goal. Be specific: file paths, function or class names, line numbers, "
    "short code excerpts where they help. No filler. At most 400 words."
)
_FINDINGS_EDIT = (
    'notes the reviewer needs (design decisions, assumptions, follow-ups). At most 120 words. Use "" if none.'
)


def effective_accept(task: Task, config: Config) -> list[str]:
    """Acceptance commands for a task: its own list if given, else config.accept for edit tasks, else none."""
    if task.accept is not None:
        return list(task.accept)
    if task.mode == "edit":
        return list(config.accept)
    return []


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def render_brief(task: Task, shared_context: list[str], worktree: str, accept: list[str]) -> str:
    """Return the worker brief for `task`, exactly as specified in spec section 10 (ends with one newline)."""
    edit = task.mode == "edit"
    sections: list[str] = [
        f"# Worker brief\n\nTask ID: {task.id}\nMode: {'EDIT' if edit else 'READ-ONLY'}\n"
        f"Working directory: {worktree}",
        _INTRO,
        f"## Goal\n{task.goal.rstrip()}",
    ]

    paragraphs = [p.rstrip() for p in [*shared_context, *task.context]]
    if paragraphs:
        sections.append("## Context\n" + "\n\n".join(paragraphs))

    if task.files:
        sections.append("## Read these files first\n" + _bullets(task.files))

    if task.steps:
        numbered = "\n".join(f"{i}. {step}" for i, step in enumerate(task.steps, start=1))
        sections.append("## Steps\n" + numbered)

    if edit and task.scope:
        sections.append(
            f"## Scope\n{_SCOPE_EDIT_LIMITED_HEAD}\n{_bullets(task.scope)}\n{_SCOPE_EDIT_LIMITED_TAIL}")
    elif edit:
        sections.append(f"## Scope\n{_SCOPE_EDIT_FREE}")
    else:
        sections.append(f"## Scope\n{_SCOPE_READ}")

    if task.constraints:
        sections.append("## Constraints\n" + _bullets(task.constraints))

    if accept:
        commands = "\n".join(f"- `{command}`" for command in accept)
        sections.append(f"## Acceptance checks\n{_ACCEPT_HEAD}\n{commands}\n{_ACCEPT_TAIL}")

    sections.append(_RULES)

    findings = _FINDINGS_EDIT if edit else _FINDINGS_READ
    if task.report:
        findings += f" Cover: {task.report}"
    sections.append(
        "## Report\n"
        "Return a JSON object with these fields:\n"
        '- status: "done" if the goal is fully met and verified, "partial" if only part is done, '
        '"blocked" if you could not make progress.\n'
        "- summary: 1-3 sentences on what you did and the result.\n"
        f"- findings: {findings}\n"
        "- files_changed: paths (relative to the working directory) you created, modified or deleted. "
        "Use [] if none.\n"
        "- blocked_actions: each denied or failed action you could not work around. Use [] if none.\n"
        "- concerns: risks, assumptions and anything the reviewer should double-check. Use [] if none."
    )
    return "\n\n".join(sections) + "\n"
