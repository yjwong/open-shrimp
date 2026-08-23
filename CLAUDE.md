# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

OpenShrimp — a Telegram bot for remote coding-agent access. Python 3.11+, fully async, managed with `uv`. Backends: Claude Agent SDK (default) or OpenCode. Telegram is the only UI; Mini Apps (review, terminal, VNC, config, preview) provide richer views.

## Commands

```bash
uv sync                                  # install deps (dev group included)
uv run openshrimp                        # run the bot (setup wizard if no config)
uv run pytest                            # run all tests
uv run pytest tests/test_stream.py       # one file
uv run pytest tests/test_stream.py::test_name -x   # one test
```

- Tests: `pytest` + `pytest-asyncio`; async tests are marked `@pytest.mark.asyncio` individually (no global asyncio_mode). Mock the Agent SDK and Telegram API.
- Web Mini Apps (`web/<app>/`, Node 18+): `npm install && npm run build` per app. `uv sync` needs each `web/<app>/dist` to exist — `mkdir -p web/<app>/dist` as a placeholder if you don't need them.
- Android companion (`android/companion/`): Gradle project, `./gradlew assembleDebug`.
- Version bump: edit the root `VERSION` file — `open-shrimp` and `moonshine-stt` both read it dynamically (hatchling).
- No linter/formatter is configured; match surrounding style.

## Architecture

```
Telegram <-> bot.py / handlers/ <-> client_manager.py <-> backend/ (claude_sdk | opencode)
                                          |                    |
              config.py (YAML) ── db.py (SQLite) ──── sandbox/ (libvirt | lima | hcs)
```

### Core concepts (the vocabulary everything else uses)

- **Context**: a working directory + per-context model, tool policy, and optional sandbox. Defined in config; switched with `/context`.
- **ChatScope**: `(chat_id, thread_id)` — the unit of conversation isolation. Each Telegram forum topic is an independent scope with its own context binding, session, and approval state.
- **Session**: a persistent agent conversation. The backend owns persistence; OpenShrimp maps `(chat_id, thread_id, context_name) -> session_id` in SQLite (`db.py`).
- **Client manager** (`client_manager.py`): keeps one live backend client per ChatScope across messages so the CLI subprocess survives between turns; only the first message after a restart uses resume.
- **dispatch_registry**: the cross-component path for injecting a turn into a scope from outside the normal message handler (scheduled tasks, host_monitor events, event pick-up).

### Backend layer (`backend/`)

Everything downstream of `client_manager` speaks the backend-neutral contract in `backend/protocol.py` + `backend/types.py`. `backend/claude_sdk/` wraps the Claude Agent SDK; `backend/opencode/` drives `opencode serve` over HTTP (models must be provider-qualified like `openai/gpt-5.5`). Per-agent sandbox integration (image bundles, runtime factory, in-guest installers) lives under `backend/<agent>/` — the `sandbox/` package is generic plumbing and must never name an agent.

### Tool approval (`hooks.py`, `handlers/approval.py`, `bash_parse.py`)

Layered: read-only file tools auto-approved inside the context directory; Edit/Write always prompt via inline keyboard (unless the user taps "Accept all edits" for the session); other tools use `allowed_tools` patterns or session-scoped `ApprovalRule(tool_name, pattern)` fnmatch rules; paths outside the context dirs always prompt. Sandboxed contexts auto-approve Bash (the sandbox is the boundary) — but the `is_host_escape` check for `host_bash`/`host_monitor` runs *before* all other checks and always demands a fresh approval; never reorder it. Bash commands are parsed with tree-sitter (`bash_parse.py`) for prefix-pattern approval.

### Sandbox layer (`sandbox/`)

`Sandbox` protocol in `sandbox/base.py` (lifecycle: `ensure_environment -> ensure_running -> provision_workspace -> build_cli_wrapper -> cleanup/stop`), `SandboxManager` factory in `sandbox/manager.py`. One backend per platform: libvirt/QEMU (Linux; supports `persistent_paths` qcow2 volumes, computer use, and Waydroid phone use), Lima (macOS), HCS (Windows; a Linux guest on the Hyper-V Compute Service — `persistent_paths` VHDX volumes, computer use over a weston/RDP desktop, port forwarding; shares are 9p, not virtiofs, and phone use and security-key forwarding are unsupported because the WSL-shipped kernel builds without binder and uhid). The agent CLI runs inside via a generated wrapper script pointed at by the SDK's `cli_path`; all SDK streaming/callback machinery is unchanged.

### Inbound events (`events/`)

External sources deliver into per-source forum topics with **zero LLM processing on receipt** — the sink renders, persists (`inbound_events` table), and posts an inert message with a "Pick up" button. No adapter exposes a provider-facing webhook: Telegram intake long-polls and Lark holds a WebSocket, both outbound. A *passive* adapter instead records `emit` and is fed by a first-party authenticated push — WhatsApp is one (the paired Android companion posts batches to the device-signed `/api/whatsapp/messages` in `whatsapp_api.py`); it declares `SupportsIngest` and is reached through `EventManager.get_adapter_of_type`, never a registry of its own. The invariant is that inbound HTTP is first-party and signed, not that it does not exist. Scheduled tasks (`events/schedule.py`) are built on the same machinery; cron expressions are POSIX crontab (0=Sunday) and must go through `build_cron_trigger`, never raw into APScheduler's `CronTrigger` (which numbers 0=Monday).

**Untrusted-content rule (load-bearing, applies everywhere):** agent prompts never carry untrusted content — not even provider-delivered event text. Prompts reference events by id; the agent fetches content itself via the `read_inbound_event` MCP tool, which returns it wrapped in an `<inbound-event untrusted="true">` envelope as a tool result.

### Control channel (`control/`)

Lets a UI process outside Python supervise the core: `status`, `shutdown`, `restart`, plus pushed `state`/`stopping` events. Newline-delimited JSON over a Windows named pipe or a Unix domain socket, chosen by platform behind one `ControlServer`; the address is derived from the instance name alone (`endpoint_address`) so no discovery file is needed. Authorisation is the OS's — pipe DACL, or a `0600` socket in a `0700` directory — never a token, and the channel must never be mounted on the main Starlette app, which is published through a tunnel. It exists because `request_shutdown()` is in-process only and `os.kill(pid, SIGTERM)` on Windows is an unconditional `TerminateProcess` that skips shutdown and strands the sandbox guest; `SIGBREAK` is bound to the debug dump, not to stopping. Two cores sharing an instance name must refuse to start rather than both serve — asyncio's pipes allow unlimited instances of a name, so the collision is otherwise silent on Windows. Process control only: it carries no conversation or event content.

Front ends that must work while the core is *stopped* use the CLI instead — `openshrimp config write --json -` and `openshrimp models --json`. The config schema lives only in Python; a GUI collects values and pipes JSON in.

## Config

Runtime config: `~/.config/openshrimp/config.yaml` (schema documented inline in `config.example.yaml`). `ANTHROPIC_API_KEY` comes from the environment, not config. Session/container state under `~/.config/openshrimp/`.

## Conventions

- All async, `asyncio` throughout — no blocking calls. Type hints on all signatures. No classes where a function will do.
- SQLite only through `aiosqlite`.
- Errors: catch at the handler level, log, send a friendly Telegram message. Never crash the bot.
- Telegram output is MarkdownV2 via `markdown.py` (GFM conversion); streaming uses `sendMessageDraft` through raw API calls (not yet in python-telegram-bot). Max message length 4096 — split at paragraph/code-block boundaries.
- No backwards-compatibility shims: single-user project deployed in lockstep — rename and drop freely.
- No temporal references in code comments (no plan/tier/date mentions); state the invariant the code enforces instead.
