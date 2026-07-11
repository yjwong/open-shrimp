# OpenShrimp

Telegram bot for remote Claude access via the Agent SDK. A personal, self-hosted alternative to OpenClaw.

## Project Overview

- **PRD**: `docs/prd.md` - full requirements, architecture, feasibility assessment
- **Language**: Python 3.11+, managed with `uv`
- **Key deps**: `claude-agent-sdk`, `python-telegram-bot[httpx,job-queue]`, `aiosqlite`, `pyyaml`, `mistune`, `starlette`, `uvicorn`, `tree-sitter`, `tree-sitter-bash`, `platformdirs`, `watchfiles`
- **Optional deps**: `rumps` (macOS menu bar app), `libvirt-python` (Libvirt/QEMU VM sandbox), `lark-oapi` (Lark inbound-events adapter)

## Architecture

```
Telegram <-> OpenShrimp (Python, async) <-> Agent backend (Claude SDK | OpenCode)
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
  - **Lima** (`backend: lima`): Full VM isolation via Lima (Apple Virtualization.framework), for macOS hosts. VirtioFS mounts for host directories. Config changes (mounts, CPU, memory) trigger a full VM rebuild. Runs under a dedicated `LIMA_HOME` isolated from the user's personal Lima instances.
  The SDK's `cli_path` is pointed at a generated wrapper script; all other SDK machinery (stdin/stdout streaming, canUseTool callbacks, MCP) works unchanged. Sandboxed contexts auto-approve all Bash commands since the sandbox provides the safety boundary. The `Sandbox` protocol (`sandbox/base.py`) defines the lifecycle: `ensure_environment()` -> `ensure_running()` -> `provision_workspace()` -> `build_cli_wrapper()` -> `cleanup()` -> `stop()`. A `SandboxManager` (`sandbox/manager.py`) provides the factory and global lifecycle.
  - **Persistent volumes** (libvirt only): The `persistent_paths` config key lists guest paths (e.g. `/var/lib/docker`) that get dedicated qcow2 disk images surviving VM rebuilds (only the overlay is deleted). Thin-provisioned and auto-formatted on first use; space is reclaimed when files are deleted.
- **Sudo mode** (`sandbox.allow_host_escape: true`): Opt-in per-context escape hatch that registers two HOST-escape MCP tools, both with `cwd` set to the context's source directory. `host_bash` runs a one-shot shell command on the HOST (outside the sandbox) and returns stdout/stderr/exit code once. `host_monitor` is its streaming sibling: it runs a long-running host command and delivers each stdout line as an event into the session via `dispatch_registry.dispatch()` (mid-turn, at the next tool-call boundary — the same path as an injected user message), with a token-bucket throttle that coalesces bursts within a window, a suppression counter that folds over-budget lines into a note, and a mandatory flood auto-stop that kills a runaway `tail -f` and tells the model to re-arm with a tighter `grep --line-buffered`/`awk` filter. Its Telegram presentation mirrors the SDK Monitor: events use the SDK's `<task-notification>` envelope, the full unthrottled stream is teed to a host-side output file (`transient_task_output_path("host_monitor", <id>)`) tailed by the Terminal Mini App via a "📺 View output" button, and a `⏳ <description>` status message is edited in place to ✅/⏱️/🛑/⏹️ when the monitor ends. Monitors register as transient tasks (`task_type: host_monitor`), so they appear exactly once in `/tasks` alongside CLI background tasks. A monitor runs until its `timeout_ms` (default 300000, max 3600000) or, when `persistent: true`, until stopped. Host processes are invisible to the CLI's `TaskStop` registry, so `host_monitor.py` owns the lifecycle; stop a monitor with the auto-approved `host_monitor_stop(monitor_id)` tool or `/tasks stop <id>`, and all scope monitors are killed on `close_session` (covers `/clear`, context switch, shutdown). Both arm tools need a fresh, intentional approval — no pattern rules or session blanket approvals; an unanswered prompt auto-denies with a "timed out" message (distinct from explicit denial) so the agent can retry or fall back to sandboxed `Bash`. The `is_host_escape` check in `hooks.py` runs before all other approval checks, so the sandbox auto-approve path for Bash never bypasses it. All outcomes (arm + stop) are audited to `sudo.log` (mode 0600) under the data directory.
- **Computer use**: Optional per-context GUI interaction via `sandbox.computer_use: true`. Runs a headless Wayland desktop inside the sandbox with Chromium and a terminal. Claude interacts via `computer_*` MCP tools (screenshot, click, type, key, scroll, window management); screenshots are sent to Telegram for observability. A VNC server is exposed for live viewing via the `/vnc` Mini App. The computer-use image extends the matching base image — a separate tag per backend (`openshrimp-computer-use` for Claude, `openshrimp-opencode-computer-use` for OpenCode).
- **Phone use** (`sandbox.phone_use: true`, **libvirt only**): Sibling of computer-use for driving Android. Runs Waydroid (Android in an LXC container on the guest kernel — not a nested VM) inside the libvirt guest. Implies `computer_use` (reuses the labwc desktop + VNC) and auto-enables `virgl` (Android gets hardware GLES 3.2 via virglrenderer → host GPU; `android.gpu: software` opts into the slow llvmpipe fallback). Control plane is `sudo waydroid shell -- <cmd>` over SSH (root `lxc-attach`, no ADB/auth) exposed via three MCP tools: `phone_shell` (the workhorse — `input`/`uiautomator`/`pm`/`am`/`wm`; uiautomator-first playbook in its description), `phone_screenshot` (native-resolution `screencap`, sent to Telegram with a "View phone" → `/vnc` button), and `phone_install_apk` (guest-side `waydroid app install`, unreachable via `phone_shell`). All three are auto-approved (sandbox is the safety boundary) and lazily call `ensure_phone_running()` (self-healing session start + boot-wait + desync reset). Requires the guest kernel's binder module (Ubuntu ships it in `linux-modules-extra`, installed at provision time) and a headless PulseAudio socket (Waydroid's LXC bind-mounts it non-optionally). The ~2.4 GB Android images download once on first boot via `waydroid init` and live on `persistent_paths` (`/var/lib/waydroid`, `~/.local/share/waydroid`) so they survive VM rebuilds. `android` sub-config: `image_type` (VANILLA/GAPPS), `resolution` (`WxH` → `wm size`), `dpi` (→ `wm density`), `gpu`. Watch live with `/phone` (a `/vnc` alias); a labwc rule maximizes the Waydroid window. Text entry caveat: `input text` is ASCII-only (space → `%s`, no Unicode). See `docs/waydroid-phone-use.md` for the full design.
- **Client manager**: Persistent `ClaudeSDKClient` instances keyed by ChatScope (`client_manager.py`). Keeps the CLI subprocess alive across multiple messages in the same conversation, avoiding the "Continue from where you left off" injection on session resume. Only the first message uses `--resume`; subsequent messages call `client.query()` on the already-connected client.
- **Scheduled tasks**: Cron-like recurring and one-shot Claude prompts. Users describe schedules in natural language; Claude calls MCP tools (`create_schedule`, `list_schedules`, `delete_schedule`) to manage them. Tasks run in isolated sessions with read-only tools only (no approval UI needed). Persistence via SQLite `scheduled_tasks` table, scheduling via `python-telegram-bot` JobQueue (APScheduler). Safety: 5-minute minimum interval, max 20 tasks per chat, max 3 concurrent executions (global semaphore), per-task timeout (default 10 minutes). One-shot tasks auto-delete after execution.
- **Inbound events** (`events:` config key, `src/open_shrimp/events/`): External sources deliver events into dedicated forum topics — one topic per source (e.g. `📥 lark`, with a 📰 topic icon), auto-created on first event and persisted in the `event_topics` table. Zero LLM processing on receipt: the sink (`events/sink.py`) renders each event (text via `gfm_to_telegram`, else pretty-printed JSON fallback), persists the provider-delivered content in the `inbound_events` table (pruned to the newest 500 per source), and posts it as an inert message with a `▶️ Pick up` inline button (attached to the last chunk only for multi-message events; `pickup: false` per source disables the button); it never calls `dispatch_registry`. The source topic is a pure inbox. **Pick-up handoff** (`events/pickup.py`): tapping `▶️ Pick up` opens an inline context picker (the source's optional `context:` — else `default_context` — is starred and listed first); choosing a context atomically claims the event (`UPDATE … WHERE picked_up=0` is the double-tap race gate), creates a dedicated forum topic (`↩️ <source> · <snippet>`), binds the chosen context to it *before* dispatching the first turn, injects a trusted first turn that references the event **by id only** (instructing the agent to fetch it via `read_inbound_event`), and rewrites the inbox button into a `✅ Picked up → open` deep link to the new topic. The event content is shown to the human via the placeholder message — display-only, never in the agent's context. Each picked-up event gets its own session/context, so concurrent events never interleave. **Untrusted-content rule**: agent prompts NEVER carry untrusted content, not even provider-delivered event text. Prompts reference events by id; the agent fetches the content itself with the auto-approved read-only `read_inbound_event` MCP tool (`tools.py`), which returns the persisted provider content wrapped in an `<inbound-event source="…" untrusted="true">` envelope as a *tool result*. The reply path follows the same rule: replying to a posted event prepends only a trusted reference line naming the event id; replies to any other bot message inject nothing. The envelope builder + closing-tag neutralizer is `event_envelope` in `events/pickup.py`. **Reply-back** (`reply_inbound_event`): a pick-up topic is bound to exactly one event (`inbound_events.pickup_thread_id`, set before the first-turn dispatch), and that binding gates a scope-bound `reply_inbound_event(text)` MCP tool — no event-id argument, so it can only reach the spawning event and is auto-approved by construction; it is registered only in pick-up topics whose event carries `reply_ref` (adapter-extracted routing JSON persisted at ingest, e.g. the Lark parent `message_id`). Delivery goes through the source adapter's optional `SupportsReply.reply(reply_ref, text)` capability (`events/base.py`; Lark replies in-thread via `im.v1.message.reply`, telegram intake replies via its own bot), resolved through the live `EventManager` (`get_active_manager()`), and every sent reply is echoed into the pick-up topic (`↪️ Replied to <source>`) so the human sees exactly what left without opening the source app. Ingestion is via outbound source adapters only (no inbound HTTP): `telegram` (a second bot token, long-polling, allowlisted chat ids; optional per-source `require_mention` drops group messages that don't address the bot — `@mention`, `/cmd@bot`, or text-mention — while DMs to the bot always pass) and `lark` (WebSocket long connection, `lark-oapi` optional dep, `uv sync --extra lark`; per-source `domain: lark|feishu` selects the international `open.larksuite.com` vs China `open.feishu.cn` endpoint, default `feishu`). Adapters implement the `EventSourceAdapter` protocol (`events/base.py`); `EventManager` (`events/manager.py`) starts/stops them with the bot. Delivery is best-effort (log and drop on failure) with an in-memory dedup LRU.

### Backends

The top-level `backend:` config key selects the agent runtime. Everything downstream — `client_manager`, tool serving, options, permissions — speaks the backend-neutral contract in `backend/protocol.py` and the shared message/permission types in `backend/types.py`. Two backends ship:

- **`claude_sdk`** (default; `backend/claude_sdk/`): the Claude Agent SDK. Sandboxed launch generates a wrapper script and points the SDK's `cli_path` at it.
- **`opencode`** (`backend/opencode/`): OpenCode (`sst/opencode`) driven over its HTTP `serve` API. Every context's `model:` must be provider-qualified (`provider/model`, e.g. `openai/gpt-5.5`); the host must be pre-authenticated (`opencode auth login`); and the `opencode` binary must be discoverable. Non-sandboxed contexts spawn a host-local `opencode serve`; sandboxed contexts run it inside the sandbox and reach it over a tunnel or published port. Docker contexts use a separate `openshrimp-opencode:latest` image (built lazily from `Dockerfile.opencode`); libvirt and lima sandboxes auto-install the host's `opencode` binary into the guest when present, otherwise the operator-provided base image or `provision:` script must supply it.

Per-agent sandbox-integration code lives under `backend/<agent>/`: the `ImageBundle` constructor (`sandbox_bundle.py`), the runtime factory (`runtime.py`), and the in-guest binary installers (`libvirt_install.py`, `lima_install.py`). The `sandbox/` package owns only generic Docker/libvirt/Lima plumbing and the `ImageBundle` / `AgentRuntime` / `ServedEndpoint` / `WrappedCLI` dataclasses — it never names an agent.

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
    events/                    # Inbound events: sources -> per-source forum topics
        types.py               # Event dataclass
        base.py                # EventSourceAdapter protocol
        sink.py                # Render + persist + post into per-source topic; dedup; topic lifecycle
        pickup.py              # Pick-up handoff: context picker, claim, topic spawn, first-turn injection
        manager.py             # Start/stop adapters with the bot lifecycle
        telegram_intake.py     # Second-bot-token adapter (long polling)
        lark.py                # Lark WebSocket adapter (optional dep)
    backend/                   # Pluggable agent-runtime layer (see "### Backends" above)
        protocol.py            # Backend/BackendClient protocols, BackendOptions contract
        types.py               # Backend-neutral message/content/permission types
        factory.py             # Config-driven backend selection (get_backend)
        claude_sdk/            # Claude Agent SDK backend
        opencode/              # OpenCode HTTP-serve backend
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
- `contexts` - Map of context name -> {directory, description, model, effort, allowed_tools, default_for_chats, locked_for_chats, additional_directories, sandbox}
- `default_context` - Context name to use when none is specified
- `review` - Optional: `host`, `port`, `public_url`, `tunnel` (cloudflared) for Mini App HTTP server
- `events` - Optional: `chat_id` (forum chat for 📥 topics) + `sources` (list of `telegram`/`lark` source configs)

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
| `/effort` | `effort_handler` | Show or override the thinking effort level (low/medium/high/xhigh/max) |
| `/add_dir` | `add_dir_handler` | Add a working directory to the current context |
| `/resume` | `resume_handler` | List and resume a previous session |
| `/review` | `review_handler` | Open the review Mini App for the current context |
| `/mcp` | `mcp_handler` | List and manage MCP servers (reset/enable/disable) |
| `/schedule` | `schedule_handler` | List and manage scheduled tasks |
| `/tasks` | `tasks_handler` | List or stop background tasks |
| `/usage` | `usage_handler` | Show Claude quota/usage statistics |
| `/vnc` | `vnc_handler` | Open VNC viewer for computer-use contexts |
| `/phone` | `phone_handler` | Open VNC viewer for phone-use (Android) contexts |
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
