"""Render the compact run summary, task detail, run list and condensed event list. Pure: no I/O."""
from __future__ import annotations

import json
import re

from agw.models import (COMMAND_TRUNCATE, MAX_COMMANDS_SHOWN, PATH_PARAM_KEYS, TAIL_LINES, DiffStat,
                        RunResult, TaskResult, ToolCall, to_jsonable)

# Worker-controlled text is clipped so one chatty worker cannot flood Claude's context.
# Flags, statuses and counts are never clipped.
MAX_SUMMARY_CHARS = 400
MAX_LIST_LINE_CHARS = 600
MAX_NOTES_CHARS = 600
MAX_REASON_CHARS = 300
MAX_TAIL_LINE_CHARS = 200
MAX_FINDINGS_CHARS = 12_000
MAX_EVENT_COMMAND_CHARS = 200
MAX_EVENT_PARAMS_CHARS = 120

_STATUS_WITH_ERROR_LINE = ("error", "timeout", "skipped")
_STATUS_WITH_RESULT_CHECKS = ("ok", "failed")


# ---- helpers -------------------------------------------------------------------------------------

def fmt_duration(seconds: float) -> str:
    """45s, 2m05s, 1h03m (rounded to the nearest second first)."""
    total = int(max(seconds, 0) + 0.5)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def _one_line(text: str) -> str:
    """Collapse line breaks (and surrounding blanks) into single spaces."""
    return re.sub(r"\s*[\r\n]+\s*", " ", str(text)).strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _text(text: str, limit: int) -> str:
    return _clip(_one_line(text), limit)


def _kilo(value: int) -> int:
    return (int(value) + 500) // 1000


def _tokens(tasks: list[TaskResult]) -> tuple[int, int]:
    def total(key: str) -> int:
        return sum(int(t.usage.get(key, 0) or 0) for t in tasks)
    return total("input_tokens"), total("output_tokens")


def _tokens_text(tasks: list[TaskResult]) -> str:
    tin, tout = _tokens(tasks)
    return f"gemini {_kilo(tin)}k in / {_kilo(tout)}k out"


def _sha7(sha: str) -> str:
    return sha[:7] if sha else "-"


def _command_of(call: ToolCall) -> str:
    for key, value in call.params.items():
        if isinstance(key, str) and key.lower() in ("commandline", "command") and isinstance(value, str):
            return value
    return ""


def _exit_text(call: ToolCall) -> str:
    return "?" if call.exit_code is None else str(call.exit_code)


def _diff_text(diff: DiffStat | None) -> str:
    if diff is None or not diff.files:
        return "diff: none"
    return f"diff: {len(diff.files)} file(s) +{diff.insertions} -{diff.deletions}"


def _counts(tasks: list[TaskResult]) -> str:
    parts = [f"{sum(1 for t in tasks if t.status == 'ok')} ok"]
    for status in ("failed", "error", "timeout", "skipped"):
        n = sum(1 for t in tasks if t.status == status)
        if n:
            parts.append(f"{n} {status}")
    return ", ".join(parts)


# ---- summary -------------------------------------------------------------------------------------

def _accept_lines(tr: TaskResult) -> list[str]:
    if not tr.accept:
        return ["accept: none"]
    passed = sum(1 for a in tr.accept if a.exit_code == 0)
    total = len(tr.accept)
    first_bad = next((a for a in tr.accept if a.exit_code != 0), None)
    if first_bad is None:
        return [f"accept: pass ({passed}/{total})"]
    lines = [f"accept: FAIL {passed}/{total} passed; first failure `{_one_line(first_bad.command)}` "
             f"exit {first_bad.exit_code}"]
    lines += [f"  | {_clip(line.rstrip(), MAX_TAIL_LINE_CHARS)}" for line in first_bad.tail[-TAIL_LINES:]]
    return lines


def _ran_line(tr: TaskResult) -> str | None:
    if not tr.commands:
        return None
    shown = tr.commands[:MAX_COMMANDS_SHOWN]
    parts = [f"{_clip(_one_line(_command_of(c)) or '(unknown)', COMMAND_TRUNCATE)} [{_exit_text(c)}]"
             for c in shown]
    line = "ran: " + "; ".join(parts)
    extra = len(tr.commands) - len(shown)
    if extra > 0:
        line += f" (+{extra} more)"
    return line


def _findings_block(text: str, run_id: str, task_id: str) -> list[str]:
    clipped = text.rstrip()
    truncated = len(clipped) > MAX_FINDINGS_CHARS
    if truncated:
        clipped = clipped[:MAX_FINDINGS_CHARS]
    lines = ["findings:"] + [f"  {ln}" for ln in clipped.strip("\n").splitlines()]
    if truncated:
        lines.append(f"  [... truncated; full text: agw show {run_id} {task_id} --report]")
    return lines


def _task_block(tr: TaskResult, run: RunResult) -> list[str]:
    head = f"[{tr.status}] {tr.task_id} {tr.mode}"
    if tr.title:
        head += f' "{_one_line(tr.title)}"'
    head += f" {fmt_duration(tr.duration_seconds)} attempt {tr.attempt}/{1 + run.retry_limit}"
    body: list[str] = []
    if tr.status in _STATUS_WITH_ERROR_LINE:
        body.append(f"error: {_text(tr.reason, MAX_REASON_CHARS) or 'unknown'}")
    if tr.mode == "edit" and tr.worker_ran:
        text = _diff_text(tr.diff)
        if text != "diff: none" and tr.branch:
            text += f" | branch {tr.branch}"
        body.append(text)
    ran_ok = tr.worker_ran and tr.status in _STATUS_WITH_RESULT_CHECKS
    if ran_ok:
        body.extend(_accept_lines(tr))
    ran = _ran_line(tr)
    if ran:
        body.append(ran)
    if tr.flags:
        body.append("flags: " + "; ".join(f"{f.code} {_one_line(f.detail)}" for f in tr.flags))
    rep = tr.report
    if ran_ok:
        if rep.status == "missing":
            body.append("report: missing")
        else:
            line = f"report: {rep.status}"
            summary = _text(rep.summary, MAX_SUMMARY_CHARS)
            body.append(f"{line} - {summary}" if summary else line)
    if rep.blocked_actions:
        body.append("blocked: " + _text("; ".join(rep.blocked_actions), MAX_LIST_LINE_CHARS))
    if rep.concerns:
        body.append("concerns: " + _text("; ".join(rep.concerns), MAX_LIST_LINE_CHARS))
    if rep.findings.strip():
        if tr.mode == "edit":
            body.append("notes: " + _text(rep.findings, MAX_NOTES_CHARS))
        else:
            body.extend(_findings_block(rep.findings, run.run_id, tr.task_id))
    out = [head]
    out.extend("  " + line for line in body)
    return out


def render_summary(run: RunResult) -> str:
    """The compact run summary of spec section 9 (ends with a newline)."""
    lines = [
        f"agw {run.run_id} | base {run.base_sha[:7]} ({run.base_ref}) | {len(run.tasks)} task(s): "
        f"{_counts(run.tasks)} | {fmt_duration(run.duration_seconds)} | {_tokens_text(run.tasks)}"
    ]
    for flag in run.flags:
        marker = "!" if flag.level == "warn" else "-"
        lines.append(f"{marker} {flag.code}: {_one_line(flag.detail)}")
    for tr in run.tasks:
        lines.append("")
        lines.extend(_task_block(tr, run))
    lines.append("")
    rid = run.run_id
    lines.append(f"next: agw show {rid} <task> [--diff|--log|--events]; agw apply {rid} <task>; agw clean {rid}")
    return "\n".join(lines) + "\n"


# ---- detail, list, events --------------------------------------------------------------------------

def render_task_detail(tr: TaskResult) -> str:
    """Everything known about one task: commands, flags, accept results and the full report."""
    head = f"task {tr.task_id} ({tr.mode}) status {tr.status}"
    if tr.reason:
        head += f": {_one_line(tr.reason)}"
    lines = [
        head,
        f"title: {_one_line(tr.title) or '-'}",
        f"attempt {tr.attempt}/{tr.max_attempts} | model {tr.model or '-'} | "
        f"{fmt_duration(tr.duration_seconds)} | {_tokens_text([tr])}",
        f"branch: {tr.branch or '-'} | base {_sha7(tr.base_sha)} | head {_sha7(tr.head_sha)}",
        _diff_text(tr.diff),
    ]
    if tr.diff is not None:
        lines += [f"  {letter} {path}" for letter, path in tr.diff.files]
    if tr.accept:
        lines.append("accept:")
        lines += [f"  $ {_one_line(a.command)} [exit {a.exit_code}] ({a.duration_seconds:.1f}s)" for a in tr.accept]
    else:
        lines.append("accept: none")
    if tr.flags:
        lines.append("flags:")
        lines += [f"  {f.code}: {_one_line(f.detail)}" for f in tr.flags]
    else:
        lines.append("flags: none")
    lines.append(f"commands ({len(tr.commands)}):")
    for call in tr.commands:
        escaped = _command_of(call).replace("\r", "").replace("\n", "\\n")
        lines.append(f"  [{_exit_text(call)}] {escaped}")
    lines.append("report:")
    report_json = json.dumps(to_jsonable(tr.report), indent=2, ensure_ascii=False)
    lines += ["  " + line for line in report_json.splitlines()]
    lines.append(f"files: {tr.task_dir or '-'}")
    return "\n".join(lines) + "\n"


def render_runs_list(runs: list[dict]) -> str:
    """One line per run (newest first as given); 'no runs' when empty."""
    if not runs:
        return "no runs\n"
    return "".join(
        f"{r.get('run_id', '')}  {str(r.get('status', '')):<11} {r.get('started', '')}  "
        f"{r.get('n_tasks', 0)} task(s): {r.get('counts', '-')}\n"
        for r in runs)


def render_events(tool_calls: list[ToolCall]) -> str:
    """Condensed list of tool calls, one line each."""
    if not tool_calls:
        return "(no tool calls)\n"
    lines = []
    for call in tool_calls:
        if call.name == "run_command":
            detail = f"{_clip(_one_line(_command_of(call)), MAX_EVENT_COMMAND_CHARS)} [{_exit_text(call)}]"
        else:
            path = next((v for k, v in call.params.items()
                         if isinstance(k, str) and k.lower() in PATH_PARAM_KEYS and isinstance(v, str) and v), None)
            detail = _one_line(path) if path is not None else json.dumps(call.params)[:MAX_EVENT_PARAMS_CHARS]
        lines.append(f"{call.step_index:>4} {call.name} {detail}")
    return "\n".join(lines) + "\n"

