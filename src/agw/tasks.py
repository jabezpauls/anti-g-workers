"""Parse and validate the tasks JSON (spec section 6) into a TaskSet, collecting every error."""
from __future__ import annotations

import json
import re
from typing import Any, TextIO

from agw.models import MODES, Task, TaskSet

MAX_TASKS = 20
MAX_TITLE_CHARS = 80
_ID_PATTERN = "^[a-z0-9][a-z0-9-]{0,39}$"
_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")

_TOP_KEYS = ("version", "base", "snapshot_dirty", "context", "tasks")
_TASK_KEYS = ("id", "mode", "title", "goal", "context", "files", "steps", "scope", "constraints",
              "accept", "report", "model", "timeout_seconds", "attempt")
_LIST_KEYS = ("context", "files", "steps", "scope", "constraints", "accept")


class TaskFileError(ValueError):
    """An invalid tasks file. `errors` lists every problem; str() joins them with newlines."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and len(value) > 0


def _str_list(value: object) -> bool:
    return isinstance(value, list) and all(_nonempty_str(x) for x in value)


def _check_task(index: int, raw: object, seen_ids: set[str], errors: list[str]) -> Task | None:
    """Validate one task object; append errors; return a Task only when it is fully valid."""
    path = f"tasks[{index}]"
    if not isinstance(raw, dict):
        errors.append(f"{path}: must be an object")
        return None
    start = len(errors)
    fields: dict[str, Any] = {}
    for key, value in raw.items():
        where = f"{path}.{key}"
        if key not in _TASK_KEYS:
            errors.append(f"{where}: unknown key")
        elif key == "id":
            if not isinstance(value, str):
                errors.append(f"{where}: must be a string")
            elif not _ID_RE.fullmatch(value):
                errors.append(f"{where}: must match {_ID_PATTERN}")
            elif value in seen_ids:
                errors.append(f'{where}: duplicate id "{value}"')
            else:
                seen_ids.add(value)
                fields[key] = value
        elif key == "mode":
            if value in MODES and isinstance(value, str):
                fields[key] = value
            else:
                errors.append(f'{where}: must be "read" or "edit"')
        elif key == "title":
            if not isinstance(value, str):
                errors.append(f"{where}: must be a string")
            elif len(value) > MAX_TITLE_CHARS:
                errors.append(f"{where}: must be at most {MAX_TITLE_CHARS} characters")
            else:
                fields[key] = value
        elif key in ("goal", "model"):
            if _nonempty_str(value):
                fields[key] = value
            else:
                errors.append(f"{where}: must be a non-empty string")
        elif key in _LIST_KEYS:
            if _str_list(value):
                fields[key] = list(value)  # type: ignore[call-overload]
            else:
                errors.append(f"{where}: must be a list of non-empty strings")
        elif key == "report":
            if isinstance(value, str):
                fields[key] = value
            else:
                errors.append(f"{where}: must be a string")
        elif key == "timeout_seconds":
            if _is_int(value) and 60 <= value <= 7200:
                fields[key] = value
            else:
                errors.append(f"{where}: must be an integer between 60 and 7200")
        elif key == "attempt":
            if _is_int(value) and value >= 1:
                fields[key] = value
            else:
                errors.append(f"{where}: must be an integer >= 1")
    for required in ("id", "goal"):
        if required not in raw:
            errors.append(f"{path}.{required}: required")
    if len(errors) != start:
        return None
    return Task(**fields)


def parse_taskset(data: object) -> TaskSet:
    """Validate decoded JSON and build a TaskSet. Raises TaskFileError listing ALL errors."""
    if not isinstance(data, dict):
        raise TaskFileError(["(root): must be an object"])
    errors: list[str] = []
    base: Any = "HEAD"
    snapshot_dirty: Any = False
    context: list[str] = []
    tasks: list[Task] = []
    seen_ids: set[str] = set()

    for key, value in data.items():
        if key not in _TOP_KEYS:
            errors.append(f"{key}: unknown key")
        elif key == "version":
            if not (_is_int(value) and value == 1):
                errors.append("version: must be 1")
        elif key == "base":
            if _nonempty_str(value):
                base = value
            else:
                errors.append("base: must be a non-empty string")
        elif key == "snapshot_dirty":
            if isinstance(value, bool):
                snapshot_dirty = value
            else:
                errors.append("snapshot_dirty: must be a boolean")
        elif key == "context":
            if _str_list(value):
                context = list(value)
            else:
                errors.append("context: must be a list of non-empty strings")
        elif key == "tasks":
            if not isinstance(value, list) or not 1 <= len(value) <= MAX_TASKS:
                errors.append(f"tasks: must contain 1 to {MAX_TASKS} tasks")
            if isinstance(value, list):
                for i, raw in enumerate(value):
                    task = _check_task(i, raw, seen_ids, errors)
                    if task is not None:
                        tasks.append(task)
    if "tasks" not in data:
        errors.append("tasks: required")
    if snapshot_dirty is True and base != "HEAD":
        errors.append('(root): snapshot_dirty requires base "HEAD"')
    if errors:
        raise TaskFileError(errors)
    return TaskSet(tasks=tasks, base=base, snapshot_dirty=snapshot_dirty, context=context)


def load_taskset(source: str, stdin: TextIO) -> TaskSet:
    """Read the tasks JSON from `source` ('-' = stdin, else a UTF-8 file) and validate it."""
    if source == "-":
        label = "stdin"
        text = stdin.read()
    else:
        label = source
        try:
            with open(source, encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            raise TaskFileError([f"{source}: file not found"]) from None
        except UnicodeDecodeError:
            raise TaskFileError([f"{source}: not valid UTF-8"]) from None
        except OSError as exc:
            raise TaskFileError([f"{source}: cannot read: {exc.strerror or exc}"]) from None
    if text.startswith("﻿"):
        text = text[1:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TaskFileError([f"{label}: invalid JSON: {exc.msg} (line {exc.lineno} col {exc.colno})"]) from None
    except RecursionError:
        raise TaskFileError([f"{label}: invalid JSON: nesting too deep"]) from None
    return parse_taskset(data)


def check_attempts(taskset: TaskSet, retry_limit: int) -> list[str]:
    """Errors for tasks whose `attempt` exceeds 1 + retry_limit (a run-time usage error)."""
    return [
        f"tasks[{i}].attempt: {task.attempt} exceeds 1 + retry_limit ({retry_limit}); "
        "take the task over instead of retrying"
        for i, task in enumerate(taskset.tasks)
        if task.attempt > 1 + retry_limit
    ]
