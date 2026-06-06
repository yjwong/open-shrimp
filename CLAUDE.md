# OpenShrimp

Telegram bot for remote OpenCode-backed coding agent access. A personal, self-hosted alternative to OpenClaw.

## Project Overview

- **PRD**: `docs/prd.md` - full requirements, architecture, feasibility assessment
- **Language**: Python 3.11+, managed with `uv`
- **Runtime engine**: OpenCode, driven through `opencode serve` by `open_shrimp.opencode_client`
- **Key deps**: `python-telegram-bot[httpx,job-queue]`, `httpx`, `aiosqlite`, `pyyaml`, `mistune`, `starlette`, `uvicorn`, `tree-sitter`, `tree-sitter-bash`, `platformdirs`, `watchfiles`
- **Optional deps**: `rumps` (macOS menu bar app), `libvirt-python` (Libvirt/QEMU VM sandbox)

## Architecture

```
Telegram <-> OpenShrimp (Python, async) <-> opencode_client <-> opencode serve
                |                         |                  |
                |                         |                  +-- HTTP session/prompt API
                |                         |                  +-- SSE event stream
                |                         |                  +-- permission/question events
                |                         |
                |                         +-- Permission bridge -> Telegram approvals
                |                         +-- MCP proxy -> OpenShrimp tools and user MCP servers
                |
                +-- Config (YAML: contexts, ACL, providers, sandbox, MCP)
                +-- Session store (SQLite: chat/thread/context -> OpenCode session_id)
```

### Core Concepts

- **Context**: A working directory plus project instructions. Switch with `/context <name>`. Each context has its own model, effort, allowed tools, MCP servers, sandbox settings, and optional additional directories. Legacy `CLAUDE.md` project instruction files remain supported as compatibility instructions, but the product should be described as OpenCode-backed.
- **ChatScope**: A `(chat_id, thread_id)` pair that identifies a unique conversation scope. In private/group chats, `thread_id` is `None`. In forum topics, each topic thread has its own `thread_id`, so multiple topics in the same chat get independent contexts and sessions.
- **Session**: A persistent OpenCode session. OpenShrimp maps `(chat_id, thread_id, context_name) -> session_id` in SQLite and resumes through OpenCode's session API. Host sessions use the host OpenCode data store. Sandboxed contexts use per-context OpenCode homes managed by the sandbox backend.
- **OpenCode client**: `open_shrimp.opencode_client` starts or connects to `opencode serve`, creates/resumes sessions, sends prompts via HTTP, subscribes to SSE events, and adapts OpenCode events into the message classes consumed by `stream.py`.
- **Tool approval**: OpenCode session permission rules provide initial allow/ask behavior from `allowed_tools`, built-in OpenShrimp rules, and `additional_directories`. The `PermissionBridge` listens for `permission.asked` SSE events, reconstructs the tool name/input from in-flight tool parts or message fetches, calls the approval callback in `hooks.py`, then replies to OpenCode through `/permission/{id}/reply`. Read-only file tools (`read`, `glob`, `grep`) are auto-approved within allowed directories. Mutating tools (`edit`, `write`, `apply_patch`) always require explicit Telegram approval unless the user opts into accept-all-edits for the session. Non-path tools use pattern-based session approval rules where applicable, such as `bash(git *)`.
- **Questions**: OpenCode `question.asked` events are routed through `handlers/questions.py` to Telegram inline keyboards and free-form responses.
- **MCP proxy**: `mcp_proxy/` exposes OpenShrimp-owned MCP tools and merges user MCP servers from OpenCode global config plus per-context `mcp:` config. The proxy preserves scope-specific callbacks for tools such as scheduling, topic editing, nested agents, file sending, host escape, and computer use.
- **Sandbox**: Optional per-context isolated execution via the `sandbox:` config key. The older `container:` key is a backwards-compatible alias for Docker sandboxing. Multiple backends are supported:
  - **Docker** (`backend: docker`): Linux containers with the project directory bind-mounted. OpenCode runs inside the container, with per-context OpenCode/session storage. Runs as host uid/gid. Custom Dockerfiles are supported; images are tagged for OpenShrimp/OpenCode and built lazily. `docker_in_docker: true` enables rootless Docker inside the container.
  - **Libvirt/QEMU** (`backend: libvirt`): Full VM isolation via libvirt. Requires `libvirt-python` optional dep. Supports `base_image`, `provision`, and persistent guest volumes.
  - **Lima** (`backend: lima`): Full VM isolation via Lima (Apple Virtualization.framework). Designed for macOS hosts. VirtioFS mounts host directories. Config changes are detected via YAML fingerprinting and trigger a VM rebuild. Uses OpenShrimp-managed Lima state rather than the user's personal Lima instances.
  - The `Sandbox` protocol (`sandbox/base.py`) defines lifecycle operations for preparing the environment, running the sandbox, provisioning the workspace, starting OpenCode, cleanup, and stop. `SandboxManager` (`sandbox/manager.py`) provides backend factories and global lifecycle.
- **Persistent volumes** (libvirt only): The `persistent_paths` config key lists guest paths such as `/var/lib/docker` that get dedicated qcow2 disk images. These volumes survive VM rebuilds. Each volume is 100 GB sparse. Auto-formatted ext4 on first use, mounted via systemd units with `LABEL=` for device-order independence. `discard=unmap` enables space reclamation.
- **Sudo mode** (`sandbox.allow_host_escape: true`): Opt-in per-context escape hatch that registers an `openshrimp_host_bash`/`host_bash` MCP tool path. The tool runs shell commands on the host with `cwd` set to the context source directory. Every invocation routes through a dedicated Telegram approval flow with a 10-second auto-deny timeout. No pattern rules or blanket approvals are available. All outcomes are appended to `sudo.log` under the OpenShrimp data directory resolved by `paths.data_dir()`.
- **Computer use**: Optional per-context GUI interaction via `sandbox.computer_use: true`. Runs a headless Wayland desktop (labwc compositor, 1280x720) inside the sandbox with Chromium and a foot terminal. The agent interacts via MCP tools such as screenshot, click, type, key, scroll, and window management. Screenshots are saved to a bind-mounted directory and sent to Telegram for observability. A VNC server is exposed on a dynamic port; `/vnc` opens a noVNC Mini App.
- **Client manager**: Persistent `OpenCodeClient` instances keyed by ChatScope (`client_manager.py`). Keeps session handles, OpenCode endpoint ownership, and SSE event buses alive across messages in the same conversation.
- **Scheduled tasks**: Cron-like recurring and one-shot agent prompts. Users describe schedules in natural language; the agent calls MCP tools (`openshrimp_create_schedule`, `openshrimp_list_schedules`, `openshrimp_delete_schedule`) to manage them. Tasks run in isolated sessions with read-only tools only. Persistence via SQLite `scheduled_tasks` table, scheduling via `python-telegram-bot` JobQueue (APScheduler). Safety: 5-minute minimum interval, max 20 tasks per chat, max 3 concurrent executions, per-task timeout (default 10 minutes). One-shot tasks auto-delete after execution.

### Key OpenCode Patterns

```python
# Multi-turn session
options = OpenCodeOptions(
    cwd="/path/to/context",
    provider="openai",
    model="gpt-5.5",
    resume=session_id,
    allowed_tools=["LSP", "bash(git *)"],
    can_use_tool=can_use_tool,
)

async with OpenCodeClient(options=options) as client:
    await client.query("prompt here")
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            # stream text/tool updates to Telegram
            ...
        elif isinstance(message, ResultMessage):
            session_id = message.session_id  # save for resume

# Permission callback shape retained for approval code compatibility.
async def can_use_tool(tool_name, tool_input, context):
    approved = await wait_for_telegram_approval(tool_name, tool_input)
    if approved:
        return PermissionResultAllow()
    return PermissionResultDeny(message="User denied tool use.")
```

## Versioning

Both `open-shrimp` and `moonshine-stt` share a single version from the `VERSION` file at the repo root. Both `pyproject.toml` files use hatchling's dynamic version source to read from it. To bump the version, edit `VERSION` — both projects pick it up automatically.

## Project Structure

```
VERSION                        # Single source of truth for version (shared)
src/open_shrimp/
    __init__.py
    main.py                    # Entry point, arg parsing, config loading
    setup.py                   # Interactive setup wizard for first-time config
    bot.py                     # Telegram bot setup, handlers, long polling
    agent.py                   # Agent dispatch wrapper around OpenCode client flow
    client_manager.py          # Persistent OpenCodeClient lifecycle across messages
    opencode_client/           # OpenCode HTTP/SSE/session/permission wrapper
        client.py              # OpenCodeClient and session prompt flow
        events.py              # SDK-compatible message/result dataclasses
        options.py             # OpenCodeOptions and provider/model parsing
        permission.py          # permission.asked -> approval callback bridge
        process.py             # opencode serve process management
        sessions.py            # session listing/resume helpers
        sse.py                 # shared SSE event bus
        tool_names.py          # OpenCode permission/category name mapping
    mcp_proxy/                 # Scope-aware MCP proxy and OpenCode config reader
    hooks.py                   # Tool approval logic and path-scoped checks
    bash_parse.py              # tree-sitter bash command parsing and security checks
    stream.py                  # OpenCode messages -> Telegram drafts/messages
    config.py                  # Config loading and validation (YAML)
    tools.py                   # MCP tool registration (topic, scheduling, nested agents)
    db.py                      # SQLite persistence: sessions, contexts, scheduled tasks
    scheduler.py               # Scheduled task execution, JobQueue integration
    markdown.py                # GFM -> Telegram MarkdownV2 conversion
    stt.py                     # Speech-to-text: download/invoke moonshine-stt binary
    service.py                 # install/uninstall as systemd/launchd service
    tunnel.py                  # Cloudflared tunnel management for public URLs
    dispatch_registry.py       # Cross-component dispatch callback (API -> agent)
    web_app_button.py          # Helper for Mini App buttons (private vs group chats)
    handlers/
        __init__.py
        commands.py            # /context, /clear, /status, etc. command handlers
        messages.py            # Message handler logic
        approval.py            # Tool approval handling (inline keyboards)
        questions.py           # OpenCode question handling
        state.py               # State management helpers
        utils.py               # Handler utilities
    sandbox/
        __init__.py
        base.py                # Sandbox protocol (lifecycle interface)
        manager.py             # SandboxManager factory and global lifecycle
        docker.py              # Docker container backend
        docker_helpers.py      # Docker utility functions
        libvirt.py             # Libvirt/QEMU VM backend
        libvirt_helpers.py     # Libvirt utility functions
        lima.py                # Lima VM backend (macOS host)
        lima_helpers.py        # Lima utility functions
    terminal/
        __init__.py
        api.py                 # Terminal Mini App, task output, provider connect PTY
        log_source.py          # Task output log source discovery
        jsonl_render.py        # JSONL rendering for terminal output
    review/
        __init__.py
        api.py                 # Review Mini App HTTP endpoints
        auth.py                # Mini App authentication
        git_diff.py            # Git diff hunk parsing
        git_stage.py           # Git staging operations
    preview/
        __init__.py
        api.py                 # Markdown preview Mini App (ephemeral content store)
    vnc/
        __init__.py
        api.py                 # WebSocket-to-TCP proxy for noVNC, VNC Mini App routes
    platform/
        macos/
            app.py             # macOS menu bar application (optional, requires rumps)
            app_setup.py       # Native macOS setup wizard
            resources/         # App resources (icons, etc.)
web/
    review-app/                # Review Mini App frontend
    terminal-app/              # Terminal Mini App frontend (xterm.js)
    markdown-app/              # Markdown preview Mini App frontend
    vnc-app/                   # VNC viewer Mini App frontend (noVNC)
moonshine-stt/                 # Subproject: standalone STT binary (packaged via PyApp)
    pyproject.toml
    src/moonshine_stt/
        main.py                # CLI entry point (transcribe / download subcommands)
        audio.py               # PyAV: OGG/Opus/any -> 16kHz mono float32 PCM
        model.py               # ONNX Runtime: Moonshine V1 four-file inference
        tokenizer.py           # tokens.txt -> decoded text
        download.py            # Auto-download models from sherpa-onnx releases
```

## Config

Config lives at `~/.config/openshrimp/config.yaml`. See `config.example.yaml` for schema.

Key fields:
- `telegram.token` - Bot token from @BotFather
- `allowed_users` - List of Telegram user IDs (integers)
- `contexts` - Map of context name -> {directory, description, model, effort, allowed_tools, default_for_chats, locked_for_chats, additional_directories, mcp, sandbox}
- `contexts.<name>.model` - OpenCode provider/model id, for example `openai/gpt-5.5`
- `contexts.<name>.mcp` - Optional per-context MCP servers managed by OpenShrimp
- `default_context` - Context name to use when none is specified
- `review` - Optional: `host`, `port`, `public_url`, `tunnel` (cloudflared) for Mini App HTTP server
- `instance_name` - Optional namespace for running multiple OpenShrimp instances on one host

Provider credentials are managed by OpenCode. Users normally connect providers through `/connect`, which opens an OpenCode auth/login PTY in the Terminal Mini App. Provider-specific environment variables may still be supplied by the service environment when a provider requires them.

## Telegram API Notes

- **Streaming**: Use `sendMessageDraft` (Bot API 9.5). Not natively supported in `python-telegram-bot` v22.6 yet. Use raw API via `bot.do_api_request("sendMessageDraft", ...)` or direct `httpx` POST.
- **Long messages**: Telegram max is 4096 chars. Auto-split at paragraph/code block boundaries. Finalize current draft, start new one.
- **Group chats**: Only respond to @mentions and replies. Check `message.entities` for bot mention or `message.reply_to_message`.
- **Inline keyboards**: Use `InlineKeyboardMarkup` for tool approvals, context/resume choices, OpenCode questions, and prompt suggestions. Handle via `CallbackQueryHandler`.
- **Forum topics**: Full support for Telegram forum chats. Each topic gets its own ChatScope, context/session state, and optional auto-title via the topic-edit MCP tool.
- **Terminal Mini App**: When a long-running command starts in the background, the tool result can include a "View output" `web_app` button that opens an xterm.js terminal viewer. The viewer loads existing output via `/api/terminal/read` and streams new output via SSE at `/api/terminal/tail`. Served at `/terminal/`.
- **VNC Mini App**: For computer-use contexts, `/vnc` opens a noVNC-based viewer. The backend proxies WebSocket connections to the sandbox's VNC server. Served at `/vnc/`.
- **Markdown Preview Mini App**: Ephemeral content store for rendering markdown previews. Served at `/preview/`.
- **Tunnel support**: When `review.tunnel: cloudflared` is set, a cloudflared quick tunnel is auto-started to expose Mini Apps publicly. The binary is auto-downloaded if not installed.
- **Parse mode**: Use `MarkdownV2` parse mode. Escape special characters: `_*[]()~>#+-=|{}.!`

## Commands

| Command | Handler | Description |
|---------|---------|-------------|
| `/context` | `context_handler` | List or switch contexts |
| `/clear` | `clear_handler` | Fresh session in current context |
| `/status` | `status_handler` | Current context, session, running state |
| `/cancel` | `cancel_handler` | Abort running agent invocation |
| `/model` | `model_handler` | Show or override the model for this chat |
| `/effort` | `effort_handler` | Show or override the thinking effort level (low/medium/high/xhigh/max) |
| `/add_dir` | `add_dir_handler` | Add a working directory to the current context |
| `/resume` | `resume_handler` | List and resume a previous session |
| `/review` | `review_handler` | Open the review Mini App for the current context |
| `/mcp` | `mcp_handler` | List and manage MCP servers |
| `/schedule` | `schedule_handler` | List and manage scheduled tasks |
| `/tasks` | `tasks_handler` | List or stop background tasks |
| `/vnc` | `vnc_handler` | Open VNC viewer for computer-use contexts |
| `/connect` | `connect_handler` | Connect model providers |
| `/config` | `config_handler` | Edit bot configuration |
| `/restart` | `restart_handler` | Restart the bot process |

## Conventions

- All async. Use `asyncio` throughout, no blocking calls.
- Type hints on all function signatures.
- Structured logging via `logging` module to stderr.
- No classes where a function will do. Keep it simple.
- Config is loaded once at startup, passed as a dict/dataclass.
- SQLite access through `aiosqlite` only.
- Error handling: catch at the handler level, log, send user-friendly error message to Telegram. Never crash the bot.

## Testing

- Use `pytest` + `pytest-asyncio`.
- Mock OpenCode HTTP/SSE behavior, MCP proxy interactions, and Telegram API for unit tests.
- Integration tests should use a real bot token plus a test context directory and either a local OpenCode server or the bundled OpenCode runtime.
- Sandbox tests should avoid host mutation unless explicitly testing `allow_host_escape`, and host escape must always exercise the approval/audit path.

## Deployment

- Run as a systemd service on the home server.
- `uv run openshrimp` as the ExecStart command.
- Provider auth is handled by OpenCode. Use `/connect` after the bot starts, or put provider-specific environment variables in the systemd unit or `.env` file if required.
- Restart the service: `systemctl --user restart open-shrimp`
