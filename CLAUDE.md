# OpenUdang

Telegram bot for remote Claude access via the Agent SDK. A personal, self-hosted alternative to OpenClaw.

## Project Overview

- **PRD**: `docs/prd.md` - full requirements, architecture, feasibility assessment
- **Language**: Python 3.11+, managed with `uv`
- **Key deps**: `claude-agent-sdk`, `python-telegram-bot[httpx,job-queue]`, `aiosqlite`, `pyyaml`, `tree-sitter`, `tree-sitter-bash`

## Architecture

```
Telegram <-> OpenUdang (Python, async) <-> Claude Agent SDK
                |
                +-- Config (YAML: contexts, ACL)
                +-- Session store (SQLite: chat/thread -> session_id mapping)
                +-- PreToolUse hooks (tool approval)
```

### Core Concepts

- **Context**: A working directory + CLAUDE.md. Switch with `/context <name>`. Each context has its own model, auto-approve list, and tools.
- **ChatScope**: A `(chat_id, thread_id)` pair that identifies a unique conversation scope. In private/group chats, `thread_id` is `None`. In forum topics, each topic thread has its own `thread_id`, so multiple topics in the same chat get independent contexts and sessions.
- **Session**: A persistent Claude conversation. The Agent SDK handles persistence as `.jsonl` files under `~/.claude/projects/<encoded-cwd>/`. OpenUdang maps `(chat_id, thread_id, context_name) -> session_id` in SQLite.
- **Tool approval**: Uses the SDK's `allowedTools` for auto-approved tools (patterns like `Bash(git *)`) and a `canUseTool` callback for everything else. Read-only file tools (Read, Glob, Grep) are auto-approved within the context working directory. Mutating tools (Edit, Write) always require explicit approval via Telegram inline keyboard, even within cwd, unless the user opts into "accept all edits" for the session. Non-path tools use pattern-based session-scoped approval rules: for Bash, the user can approve by command prefix (e.g. "Accept all git" creates a `git *` pattern) or blanket-approve the entire tool. Approval rules are `ApprovalRule(tool_name, pattern)` with `fnmatch` glob matching.
- **Containerization**: Optional per-context Docker isolation via the `container:` config key. The Claude CLI runs inside a container with only the project directory bind-mounted (at the same host path). Session storage is isolated under `~/.config/openudang/containers/<context>/`, mounted as `~/.claude` inside the container. The container runs as the host user's uid/gid to avoid root-owned files. The SDK's `cli_path` is pointed at a generated wrapper script that invokes `docker run`; all other SDK machinery (stdin/stdout streaming, canUseTool callbacks, MCP) works unchanged. Containerized contexts auto-approve all Bash commands since Docker provides the safety boundary. Custom Dockerfiles can be specified per-context via `container.dockerfile` to install dev tools (Go, Node, Rust, etc.); images are tagged `openudang-claude:<context-name>` and built lazily.
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

Both `open-udang` and `moonshine-stt` share a single version from the `VERSION` file at the repo root. Both `pyproject.toml` files use hatchling's dynamic version source to read from it. To bump the version, edit `VERSION` — both projects pick it up automatically.

## Project Structure

```
VERSION                   # Single source of truth for version (shared)
src/open_udang/
    __init__.py
    main.py          # Entry point, arg parsing, config loading
    bot.py            # Telegram bot setup, handlers, long polling
    agent.py          # Claude Agent SDK wrapper, session management
    hooks.py          # canUseTool callback, tool approval logic
    bash_parse.py     # tree-sitter bash command parsing and security checks
    stream.py         # Stream bridge: SDK messages -> sendMessageDraft
    config.py         # Config loading and validation (YAML)
    container.py      # Docker container wrapper for isolated CLI execution
    tools.py          # MCP tool registration (edit_topic, scheduling tools)
    db.py             # SQLite persistence: sessions, contexts, scheduled tasks
    scheduler.py      # Scheduled task execution, JobQueue integration
    markdown.py       # GFM -> Telegram MarkdownV2 conversion
    stt.py            # Speech-to-text: download/invoke moonshine-stt binary
    service.py        # install/uninstall as systemd/launchd service
    terminal/
        api.py       # SSE tail + read endpoints for background task output
web/terminal-app/         # Terminal Mini App frontend (TypeScript, xterm.js)
    src/main.ts      # Entry point: xterm.js terminal, SSE streaming
moonshine-stt/            # Subproject: standalone STT binary (packaged via PyApp)
    pyproject.toml
    src/moonshine_stt/
        main.py      # CLI entry point (transcribe / download subcommands)
        audio.py     # PyAV: OGG/Opus/any -> 16kHz mono float32 PCM
        model.py     # ONNX Runtime: Moonshine V1 four-file inference
        tokenizer.py # tokens.txt -> decoded text
        download.py  # Auto-download models from sherpa-onnx releases
```

## Config

Config lives at `~/.config/openudang/config.yaml`. See `config.example.yaml` for schema.

Key fields:
- `telegram.token` - Bot token from @BotFather
- `allowed_users` - List of Telegram user IDs (integers)
- `contexts` - Map of context name -> {directory, description, model, allowed_tools, default_for_chats, container}
- `default_context` - Context name to use when none is specified

`ANTHROPIC_API_KEY` is read from the environment, not the config file.

## Telegram API Notes

- **Streaming**: Use `sendMessageDraft` (Bot API 9.5). Not natively supported in `python-telegram-bot` v22.6 yet. Use raw API via `bot.do_api_request("sendMessageDraft", ...)` or direct `httpx` POST.
- **Long messages**: Telegram max is 4096 chars. Auto-split at paragraph/code block boundaries. Finalize current draft, start new one.
- **Group chats**: Only respond to @mentions and replies. Check `message.entities` for bot mention or `message.reply_to_message`.
- **Inline keyboards**: Use `InlineKeyboardMarkup` for tool approval buttons. Handle via `CallbackQueryHandler`.
- **Forum topics**: Full support for Telegram forum (threaded) chats. Each forum topic gets its own independent ChatScope — separate context, session, and state. The bot responds to all messages in forum topics (no @mention required). In forum topics, an `edit_topic` MCP tool is auto-registered so Claude can set descriptive topic titles with optional emoji icons.
- **Terminal Mini App**: When a Bash tool runs with `run_in_background`, the tool result message includes a "View output" `web_app` button that opens an xterm.js-based terminal viewer. The viewer loads existing output via `/api/terminal/read`, then streams new output via SSE at `/api/terminal/tail`. Task output files are discovered under `/tmp/claude-<uid>/`. Served at `/terminal/`.
- **Parse mode**: Use `MarkdownV2` parse mode. Escape special characters: `_*[]()~>#+-=|{}.!`

## Commands

| Command | Handler | Description |
|---------|---------|-------------|
| `/context` | `context_handler` | List or switch contexts |
| `/clear` | `clear_handler` | Fresh session in current context |
| `/status` | `status_handler` | Current context, session, running state |
| `/cancel` | `cancel_handler` | Abort running Claude invocation |
| `/model` | `model_handler` | Show or override the model for this chat |
| `/resume` | `resume_handler` | List and resume a previous session |
| `/review` | `review_handler` | Open the review Mini App for the current context |
| `/mcp` | `mcp_handler` | List and manage MCP servers (reset/enable/disable) |
| `/schedule` | `schedule_handler` | List and manage scheduled tasks |
| `/tasks` | `tasks_handler` | List or stop background tasks |

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
- `uv run openudang` as the ExecStart command.
- Environment: `ANTHROPIC_API_KEY` in systemd unit or `.env` file loaded by the service.
- Restart the service: `systemctl --user restart open-udang`
