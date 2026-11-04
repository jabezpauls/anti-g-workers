# Antigravity CLI (`agy`) — verified behaviour

Probed against `agy` 1.4.12 on Linux, 2026-11-05. Everything below was observed, not assumed.
Sanitized raw output lives in `samples/`.

## Invocation

- Headless: `agy -p "<prompt>"` (aliases `--print`, `--prompt`). Runs one turn and exits.
- Workspace = the process's current working directory (`init.cwd` in stream-json). `--add-dir` adds more.
- Model: `--model <slug>`. `agy models` lists slugs. Available on the probing account:
  `gemini-3.8-flash-{low,medium,high}`, `gemini-4-argon-{low,medium,high}`,
  `claude-opus-5-5-*`, `claude-sonnet-5-5-*`, `gpt-oss-120b-medium`.
  The effort level is baked into the slug; `--effort` (low|medium|high|xhigh|max) also exists.
- `--print-timeout 10m` caps the run (`0s` = unlimited, the default).
- `--dangerously-skip-permissions` auto-approves every tool call.
- `--sandbox` "terminal restrictions" — **does NOT confine file writes to the workspace** (see below).
- `--settings '<json or path>'` — per-session settings, never saved.
- `--disable-slash-commands` — disable slash command / skill expansion in print mode.
- `--mode accept-edits|auto|plan`.
- Diagnostics go to stderr; the response goes to stdout.

## Output formats

### `--output-format json` (single envelope)

```json
{"conversation_id":"…","status":"SUCCESS","response":"PONG\n","duration_seconds":3.55,
 "num_turns":1,"usage":{"input_tokens":7242,"output_tokens":24,"thinking_tokens":22,
 "cache_read_tokens":0,"total_tokens":7266}}
```

On error: `{"conversation_id":"","status":"ERROR","response":"","error":"invalid model selection …"}`,
non-zero exit code, and the error text is also printed to stderr.

### `--json-schema '<schema json or path>'`

The envelope gains `"structured_output": {...}` (parsed object conforming to the schema) and echoes
`"json_schema"`. `response` holds the same object as a JSON string. With stream-json, the schema applies only to
the final result.

### `--output-format stream-json` (NDJSON, one event per line)

- `{"event":"init","conversation_id":…,"init":{"model":…,"cwd":…,"tools":[…]}}`
- `{"event":"step_update","step_update":{"step_index":N,"state":"ACTIVE|DONE","step_type":"user_input|agent_response|tool", …}}`
  - tool steps: `"tool_name":"write_file"`, `"tool_info":{"name":…,"parameters":{"Path":"/abs/path"}}`
  - `"tool_name":"run_command"`, `"tool_info":{"parameters":{"CommandLine":"ls -la"},"output":"Command exited with code 0.\n\nCommand output:\n…"}` (output only on the DONE event)
  - agent_response steps carry `text_delta` and per-step `usage`.
  - Each tool step emits an ACTIVE event and then a DONE event — dedupe by `step_index`.
- `{"event":"result","result":{"status":"SUCCESS",…same fields as the json envelope…}}`

The tool list includes `ask_question`, `ask_permission`, and `ask_custom_permission`. In headless mode a worker
must be told never to ask questions.

## Sandbox does not isolate the filesystem

With `--dangerously-skip-permissions --sandbox` and the cwd set to a git repo, the worker successfully:

1. wrote a file **outside** the cwd via `write_file`, and
2. wrote a file **outside** the cwd via `run_command` (`echo Y > /outside/path`).

So a git worktree is **not** a security boundary. agw must:

- detect out-of-scope writes: any `write_file`-family tool `Path` outside the worktree, and
- detect changes to the user's main working tree (snapshot `git status --porcelain` plus HEAD before and after), and
- flag either loudly in the summary, and document this honestly in the README.

Whether `--settings` permission deny rules still apply under `--dangerously-skip-permissions` was UNVERIFIED in
round 1. Round 2 (below) verified that they DO apply.

---

## Round 2 probes: `agy` 1.5.0, Linux, 2026-11-05

`agy` auto-updated from 1.4.12 to **1.5.0** between rounds. Every probe below used `--model gemini-3.8-flash-low`
in throwaway git repos under a scratch dir in `/tmp`. Sanitized outputs are in `samples/` (named per probe below).
Everything here was observed, not assumed. Anything still unknown is marked UNVERIFIED.

### Tool names in events (`edit-existing-file.ndjson`)

- `tool_name` in `step_update` events is a **normalized name that differs from the `init.tools` list**. The init list
  says `view_file`, `write_to_file`, `replace_file_content`, `multi_replace_file_content`, `sed_file` and
  `notebook_edit`, but the events said:
  - `read_file` (`Path`), with output like `"2 lines, 22 bytes"`
  - `edit_file` (`Path`): editing an EXISTING file
  - `write_file` (`Path`): creating a file
  - `run_command` (`CommandLine`): deleting files was done via `rm`; no delete tool was used.
- Treat any tool event that is not a known read tool and carries a path parameter as a potential write.
- `tool_info.parameters` does NOT show every parameter: a `run_command` that the model retried with
  `BypassSandbox: true` shows only `CommandLine` (see the sandbox section).

### `--settings` permission deny rules DO apply under `--dangerously-skip-permissions` (`deny-rule-write.ndjson`)

- Rule grammar (from the binary): `^(command|read_file|write_file|read_url|mcp|execute_url|unsandboxed)\s*\(.*\)$`.
  `unsandboxed(...)` is deprecated in favour of `command(...)`. Settings shape: `{"permissions":{"allow":[…],"deny":[…]}}`.
- `deny: ["write_file(/abs/dir)"]` with `--dangerously-skip-permissions` blocked `write_file` of `/abs/dir/w.txt`. The
  model received `Permission denied for write_file(<path>). Matches user-configured deny rule.`
- The same `write_file(<dir>)` deny rule also blocked **`edit_file`** of an existing file under `<dir>`
  (`worktree-structured-output.ndjson`). So `write_file(...)` covers edits.
- Matching is by **path component**, not raw string prefix: denying `…/main` did NOT block `…/main2/z.txt`.
- **A denied tool call emits an ACTIVE event followed by an ERROR event** (CORRECTED in round 3; round 2 wrongly
  said "no `step_update` at all"). In `deny-rule-write.ndjson` step 2 is `write_file` ACTIVE, then `write_file` ERROR
  with `tool_info.error = {"type":"TOOL_ERROR","message":"permission check failed for write_file \"<path>\":
  Permission denied for write_file(<path>). Matches user-configured deny rule."}`. The same holds for a denied
  `edit_file` (`worktree-structured-output.ndjson`, step 6). Re-verified live on agy 1.5.0 (see round 3). Consumers
  must ignore tool steps whose latest state is ERROR; the model's own `blocked_actions` report is the second signal.
- **Deny beats allow.** `deny:["write_file(/)"]` plus `allow:["write_file(<repo>)"]` blocked writes inside `<repo>` too.
- `allow` rules WITHOUT `--dangerously-skip-permissions` do NOT confine writes: `allow:["write_file(<repo>)", …]`
  (no skip flag) still let `write_file` create a file outside `<repo>`.
- `deny:["command(*)"]` blocks **every** shell command, sandboxed or not, with or without `--sandbox`. That is useless
  for workers, which must run tests.
- `{"allowNonWorkspaceAccess": false}` as a top-level `--settings` key had **no observable effect**: writes outside the
  workspace still succeeded via both the file tool and the shell.

=> A cheap, real (partial) guardrail: `deny: ["write_file(<main repo root>)", …]`. It only works if the worker's
worktree is NOT inside the main repo root, because deny beats allow. It does not stop shell commands.

### `--sandbox` is not a boundary under `--dangerously-skip-permissions` (`sandbox-bypass.ndjson`)

- With `--sandbox` and a `/tmp` workspace, a sandboxed `echo A > inside_a.txt` **inside the workspace** failed with
  `Read-only file system`. The tool output then tells the model it can "run the command again with `BypassSandbox` set
  to true to request explicit user permission".
- With `--dangerously-skip-permissions`, that permission request is auto-approved. The model retried, and the command
  plus a write OUTSIDE the workspace both succeeded. The bypass is not visible in `tool_info.parameters`.
- Net effect: `--sandbox` adds failed first attempts (time and tokens) and no confinement.
- UNVERIFIED: whether in-workspace writes also fail under the sandbox when the workspace is outside `/tmp`. Not
  probed, because probes may not run in `$HOME`.

### Exit codes and errors (`invalid-model.*`, `print-timeout.*`)

| Situation | exit | `result.status` | other signals |
|---|---|---|---|
| success | 0 | `SUCCESS` | |
| invalid `--model` | 1 | `ERROR` | `conversation_id:""`; the stream has ONLY a `result` event; the error text is also on stderr as `error: …` |
| `--print-timeout` hit | **3** | `ERROR` | `finished_reason:"print_timeout"`; stderr line `AGY_ERROR: {"short_error":…,"status":"DEADLINE_EXCEEDED","retryable":false,"reason":"print_timeout"}` |
| SIGTERM to the process group | 1 | `ERROR` | `error:"interrupted"`, exits within ~0.3s, and the running child shell command is killed (its follow-up write never happened) |

- The `AGY_ERROR: {json}` stderr line is machine-readable (`status`, `retryable`, `reason`). It appeared for the timeout
  but not for the invalid model.
- UNVERIFIED: the quota/rate-limit error shape (could not trigger it cheaply). Expect `status:"ERROR"`, probably with
  `AGY_ERROR` `status:"RESOURCE_EXHAUSTED"` and/or `retryable:true`.
- UNVERIFIED: the not-logged-in behaviour. Running with `HOME` set to an empty dir was **still authenticated**, so the
  credentials are not under `$HOME/.gemini` (probably the OS keyring, or resolved via the passwd entry). It could not be
  simulated without logging the real account out.

### Headless `ask_question` does not hang (`ask-question-headless.ndjson`)

Told to call `ask_question` and wait, the run finished normally. The question step shows up as `step_type:"unknown"`
(DONE after ~0.1s), and the model said "You skipped answering the question". No hang occurred, but the worker wastes a
turn, so the brief should still say "never ask questions".

### Other verified behaviour

- **The prompt can come from stdin**: `echo "…" | agy --output-format json …` with no `-p` works. stdin also combines
  with `--output-format stream-json --json-schema <file> --disable-slash-commands`. This avoids argv length limits
  (Windows: 32K) and quoting problems for long briefs.
- **`--disable-slash-commands` matters**: the prompt `/help` without it was handled locally as a slash command (it
  printed the command list, `conversation_id:""`, and made no model call). With it, the text went to the model.
- **The cwd can be a `git worktree`** (`.git` is a file there). `init.cwd` is the worktree path and edits land in the
  worktree. The main tree was untouched.
- `--json-schema <path>` with stream-json: the final `result` event carries `structured_output` (parsed object) plus
  `json_schema`. Enums were respected (`"status":"partial"` when part of the task was denied).
- Parallel runs: 3–4 concurrent `agy` processes on the same account ran fine.
- agy did NOT add probe dirs to `trustedWorkspaces` in its settings.json (the count was unchanged before and after), and
  it wrote nothing extra into the workspace (`git status` showed only the requested changes).
- agy keeps per-conversation state under `~/.gemini/antigravity-cli/` (`brain/<conversation-id>/`, `conversations/`).
- Usage per tiny run: ~7–9k input tokens per model turn (system prompt overhead). Even trivial workers cost ~15–50k
  input tokens.

---

## Round 3: final verification, `agy` 1.5.0 (`agy --version` = 1.5.0), Linux, 2026-11-05

Observed through `agw` in throwaway git repos under a scratch dir in `/tmp`, model `gemini-3.8-flash-low`.

- **Denied file-tool writes (re-verified).** An edit worker told to write four files with its file tool: the write into
  the main repo root (covered by `write_file(<main root>)`) produced `ACTIVE` then `ERROR` in `events.ndjson`, while
  writes to the worktree, to another scratch dir and to `/var/tmp` succeeded. A read worker, whose own worktree is
  also denied, got `ACTIVE`+`ERROR` for both `edit_file` and `write_file`; the worktree stayed unchanged.
- **The structured report arrives as a `finish` tool step.** The final `step_update` events have `tool_name:"finish"`
  with the report fields (`status`, `summary`, `blocked_actions`, ...) as `tool_info.parameters`, in addition to the
  `structured_output` in the `result` event. A worker that was denied listed the denied path in `blocked_actions`.
- **Deny rules do not cover the shell.** A read worker's `run_command` `echo hi > newfile.txt` (inside its worktree)
  and `echo leak > <main repo>/shell_leak.txt` both exited 0 and wrote the files. Only the main-tree snapshot catches
  the second one.
- **SIGINT to agw**: both agy processes were terminated, the shell child `sleep 100` did not survive, agw exited 130
  within about 0.5 s and the summary was printed (`error: interrupted: interrupted`).
- **Token use** of tiny workers on `gemini-3.8-flash-low`: 16k-62k input tokens per run, 0-1k output.
