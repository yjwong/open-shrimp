# OpenShrimp

Telegram bot for remote Claude access via the Agent SDK. A personal, self-hosted alternative to OpenClaw.

## Project Overview

- **PRD**: `docs/prd.md` - full requirements, architecture, feasibility assessment
- **Language**: Python 3.11+, managed with `uv`
- **Key deps**: `claude-agent-sdk`, `python-telegram-bot[httpx,job-queue]`, `aiosqlite`, `pyyaml`, `mistune`, `starlette`, `uvicorn`, `tree-sitter`, `tree-sitter-bash`, `platformdirs`, `watchfiles`
- **Optional deps**: `rumps` (macOS menu bar app), `libvirt-python` (Libvirt/QEMU VM sandbox)

## Architecture

```
Telegram <-> OpenShrimp (Python, async) <-> Claude Agent SDK
                |
                +-- Config (YAML: contexts, ACL)
                +-- Session store (SQLite: chat/thread -> session_id mapping)
                +-- PreToolUse hooks (tool approval)
```

### Core Concepts

- **Context**: A working directory + CLAUDE.md. Switch with `/context <name>`. Each context has its own model, auto-approve list, and tools.
- **ChatScope**: A `(chat_id, thread_id)` pair that identifies a unique conversation scope. In private/group chats, `thread_id` is `None`. In forum topics, each topic thread has its own `thread_id`, so multiple topics in the same chat get independent contexts and sessions.
- **Session**: A persistent Claude conversation. The Agent SDK handles persistence as `.jsonl` files under `~/.claude/projects/<encoded-cwd>/`. OpenShrimp maps `(chat_id, thread_id, context_name) -> session_id` in SQLite.
- **Tool approval**: Uses the SDK's `allowedTools` for auto-approved tools (patterns like `Bash(git *)`) and a `canUseTool` callback for everything else. Read-only file tools (Read, Glob, Grep) are auto-approved within the context working directory. Mutating tools (Edit, Write) always require explicit approval via Telegram inline keyboard, even within cwd, unless the user opts into "accept all edits" for the session. Non-path tools use pattern-based session-scoped approval rules: for Bash, the user can approve by command prefix (e.g. "Accept all git" creates a `git *` pattern) or blanket-approve the entire tool. Approval rules are `ApprovalRule(tool_name, pattern)` with `fnmatch` glob matching.
- **Sandbox**: Optional per-context isolated execution via the `sandbox:` config key (the older `container:` key is a backwards-compatible alias for `sandbox.backend: docker`). Multiple backends are supported:
  - **Docker** (`backend: docker`): Linux containers with the project directory bind-mounted. Session storage isolated under `~/.config/openshrimp/containers/<context>/`. Runs as host uid/gid. Custom Dockerfiles supported; images tagged `openshrimp-claude:<context-name>`, built lazily. `docker_in_docker: true` enables rootless Docker inside the container.
  - **Libvirt/QEMU** (`backend: libvirt`): Full VM isolation via libvirt. Requires `libvirt-python` optional dep. Supports `base_image` and `provision` (shell script for first boot) config fields.
  - **Lima** (`backend: lima`): Full VM isolation via Lima (Apple Virtualization.framework). Designed for macOS hosts. VirtioFS mounts for host directories. Config changes (mounts, CPU, memory) are detected via YAML fingerprinting (SHA-256) and trigger a full VM rebuild (`limactl delete` + `create`). Uses `LIMA_HOME=~/.openshrimp/lima` for isolation from user's personal Lima instances. State stored under `~/.local/share/openshrimp/lima/<context>/`.
  The SDK's `cli_path` is pointed at a generated wrapper script; all other SDK machinery (stdin/stdout streaming, canUseTool callbacks, MCP) works unchanged. Sandboxed contexts auto-approve all Bash commands since the sandbox provides the safety boundary. The `Sandbox` protocol (`sandbox/base.py`) defines the lifecycle: `ensure_environment()` -> `ensure_running()` -> `provision_workspace()` -> `build_cli_wrapper()` -> `cleanup()` -> `stop()`. A `SandboxManager` (`sandbox/manager.py`) provides the factory and global lifecycle.
- **Computer use**: Optional per-context GUI interaction via `sandbox.computer_use: true`. Runs a headless Wayland desktop (labwc compositor, 1280x720) inside the sandbox with Chromium and a foot terminal. Claude interacts via MCP tools: `computer_screenshot` (grim), `computer_click`/`computer_type`/`computer_key`/`computer_scroll` (wlrctl), and `computer_toplevel` for window management. Screenshots are saved to a bind-mounted directory and sent to Telegram for user observability. A VNC server (wayvnc) is exposed on a dynamic port for live viewing; the `/vnc` command opens a noVNC Mini App. The computer-use image (`openshrimp-computer-use:latest`) extends the base image with `Dockerfile.computer-use`.
- **Client manager**: Persistent `ClaudeSDKClient` instances keyed by ChatScope (`client_manager.py`). Keeps the CLI subprocess alive across multiple messages in the same conversation, avoiding the "Continue from where you left off" injection on session resume. Only the first message uses `--resume`; subsequent messages call `client.query()` on the already-connected client.
- **Scheduled tasks**: Cron-like recurring and one-shot Claude prompts. Users describe schedules in natural language; Claude calls MCP tools (`create_schedule`, `list_schedules`, `delete_schedule`) to manage them. Tasks run in isolated sessions with read-only tools only (no approval UI needed). Persistence via SQLite `scheduled_tasks` table, scheduling via `python-telegram-bot` JobQueue (APScheduler). Safety: 5-minute minimum interval, max 20 tasks per chat, max 3 concurrent executions (global semaphore), per-task timeout (default 10 minutes). One-shot tasks auto-delete after execution.

### Key SDK Patterns

```python
# Multi-turn session
async with ClaudeSDKClient(options=options) as client:
    await client.query("prompt here")
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            # stream text to Telegram
        elif isinstance(message, ResultMessage):
            session_id = message.session_id  # save for resume

# Resume across restarts
options = ClaudeAgentOptions(resume=session_id, cwd="/path/to/context")

# canUseTool callback for tool approval (tools not in allowedTools)
async def can_use_tool(tool_name, tool_input, context):
    # Send Telegram inline keyboard, await callback
    approved = await wait_for_telegram_approval(tool_name, tool_input)
    if approved:
        return PermissionResultAllow()
    else:
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
    agent.py                   # Claude Agent SDK wrapper, session management
    client_manager.py          # Persistent ClaudeSDKClient lifecycle across messages
    hooks.py                   # canUseTool callback, tool approval logic
    bash_parse.py              # tree-sitter bash command parsing and security checks
    stream.py                  # Stream bridge: SDK messages -> sendMessageDraft
    config.py                  # Config loading and validation (YAML)
    tools.py                   # MCP tool registration (edit_topic, scheduling tools)
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
        questions.py           # Question/interaction handlers
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
        api.py                 # SSE tail + read endpoints for background task output
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
- `contexts` - Map of context name -> {directory, description, model, allowed_tools, default_for_chats, locked_for_chats, additional_directories, sandbox}
- `default_context` - Context name to use when none is specified
- `review` - Optional: `host`, `port`, `public_url`, `tunnel` (cloudflared) for Mini App HTTP server

`ANTHROPIC_API_KEY` is read from the environment, not the config file.

## Telegram API Notes

- **Streaming**: Use `sendMessageDraft` (Bot API 9.5). Not natively supported in `python-telegram-bot` v22.6 yet. Use raw API via `bot.do_api_request("sendMessageDraft", ...)` or direct `httpx` POST.
- **Long messages**: Telegram max is 4096 chars. Auto-split at paragraph/code block boundaries. Finalize current draft, start new one.
- **Group chats**: Only respond to @mentions and replies. Check `message.entities` for bot mention or `message.reply_to_message`.
- **Inline keyboards**: Use `InlineKeyboardMarkup` for tool approval buttons. Handle via `CallbackQueryHandler`.
- **Forum topics**: Full support for Telegram forum (threaded) chats. Each forum topic gets its own independent ChatScope — separate context, session, and state. The bot responds to all messages in forum topics (no @mention required). In forum topics, an `edit_topic` MCP tool is auto-registered so Claude can set descriptive topic titles with optional emoji icons.
- **Terminal Mini App**: When a Bash tool runs with `run_in_background`, the tool result message includes a "View output" `web_app` button that opens an xterm.js-based terminal viewer. The viewer loads existing output via `/api/terminal/read`, then streams new output via SSE at `/api/terminal/tail`. Task output files are discovered under `/tmp/claude-<uid>/`. Served at `/terminal/`.
- **VNC Mini App**: For computer-use contexts, the `/vnc` command opens a noVNC-based viewer. The backend proxies WebSocket connections to the sandbox's VNC server via a WebSocket-to-TCP bridge. Served at `/vnc/`.
- **Markdown Preview Mini App**: Ephemeral content store for rendering markdown previews. Served at `/preview/`.
- **Tunnel support**: When `review.tunnel: cloudflared` is set, a cloudflared quick tunnel is auto-started to expose Mini Apps publicly (for group chats). The binary is auto-downloaded if not installed.
- **Parse mode**: Use `MarkdownV2` parse mode. Escape special characters: `_*[]()~>#+-=|{}.!`

## Commands

| Command | Handler | Description |
|---------|---------|-------------|
| `/context` | `context_handler` | List or switch contexts |
| `/clear` | `clear_handler` | Fresh session in current context |
| `/status` | `status_handler` | Current context, session, running state |
| `/cancel` | `cancel_handler` | Abort running Claude invocation |
| `/model` | `model_handler` | Show or override the model for this chat |
| `/add_dir` | `add_dir_handler` | Add a working directory to the current context |
| `/resume` | `resume_handler` | List and resume a previous session |
| `/review` | `review_handler` | Open the review Mini App for the current context |
| `/mcp` | `mcp_handler` | List and manage MCP servers (reset/enable/disable) |
| `/schedule` | `schedule_handler` | List and manage scheduled tasks |
| `/tasks` | `tasks_handler` | List or stop background tasks |
| `/usage` | `usage_handler` | Show Claude quota/usage statistics |
| `/vnc` | `vnc_handler` | Open VNC viewer for computer-use contexts |
| `/login` | `login_handler` | Token-based authentication for Mini Apps |

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
- Mock the Agent SDK and Telegram API for unit tests.
- Integration test: real bot token + real Agent SDK against a test context directory.

## Deployment

- Run as a systemd service on the home server.
- `uv run openshrimp` as the ExecStart command.
- Environment: `ANTHROPIC_API_KEY` in systemd unit or `.env` file loaded by the service.
- Restart the service: `systemctl --user restart open-shrimp`
