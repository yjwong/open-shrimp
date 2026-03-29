<p align="center">
  <img src="assets/logo.svg" alt="OpenUdang" width="480">
</p>

<p align="center">
  <strong>Claude Code in your pocket. No laptop required.</strong>
</p>

---

OpenUdang puts a full Claude coding agent in Telegram — complete with file editing, tool use, and project awareness. It's the prawn 🦐 to [OpenClaw](https://openclaw.ai/)'s lobster.

*Udang* is Malay for "prawn" — small, personal, gets the job done.

<p align="center">
  <a href="#quick-start">Quick Start</a> · <a href="#commands">Commands</a> · <a href="#code-review">Code Review</a> · <a href="#scheduled-tasks">Scheduled Tasks</a> · <a href="#voice-notes">Voice Notes</a> · <a href="#macos-app">macOS App</a> · <a href="#deployment">Deployment</a>
</p>

<div align="center">
<table>
<tr>
<td align="center"><strong>Agent</strong></td>
<td align="center"><strong>Code Review</strong></td>
</tr>
<tr>
<td>

https://github.com/user-attachments/assets/2eaedb5a-cdff-4088-82c2-5cb4d6eee23a

</td>
<td>

https://github.com/user-attachments/assets/b8971e87-2003-4956-a449-8f8ca09a043f

</td>
</tr>
</table>
</div>

---

## OpenUdang vs OpenClaw

Both are self-hosted and open source. They solve different problems.

| | **OpenUdang** | **OpenClaw** |
|---|---|---|
| **Focus** | Code agent — reads, edits, and writes files in your projects | General-purpose assistant — browsing, memory, smart home, 50+ integrations |
| **Platform** | Telegram | WhatsApp, Telegram, Discord, Slack, Signal, iMessage |
| **AI model** | Claude only (via Agent SDK) | Claude, GPT, local models |
| **Tool approval** | Interactive — inline keyboard approve/deny per tool call | Autonomous by default |
| **Project awareness** | Full — CLAUDE.md, working directories, path-scoped permissions | Limited — general shell access |

**TL;DR:** OpenClaw is a Swiss Army knife for daily life. OpenUdang is a scalpel for code — it does one thing and does it well.

## OpenUdang vs Claude Code Remote Control

Both let you use Claude Code from your phone. They take very different approaches.

| | **OpenUdang** | **Claude Code Remote Control** |
|---|---|---|
| **How it works** | Standalone bot — talks to Agent SDK directly | Remote view into a running Claude Code terminal session |
| **Interface** | Telegram — no extra app needed | claude.ai/code or Claude mobile app |
| **Always on** | Yes — runs as a systemd service, message it anytime | No — requires a Claude Code session to be started first |
| **Code review** | Mobile-first review UI — swipe through hunks to stage or skip | No dedicated review UI |
| **Stability** | Simple architecture, fewer moving parts | Research preview — still buggy |

**TL;DR:** Remote Control mirrors a terminal session to your phone. OpenUdang *is* the session — always on, in Telegram, with a proper code review flow. No extra app required.

## Why OpenUdang?

You're away from your desk but need Claude to fix a bug, review a diff, or scaffold something quick. OpenUdang gives you a proper Claude Code session from any Telegram chat — on your phone, your tablet, wherever.

- **Real agent, not a chatbot.** Claude can read, edit, and write files in your actual project directories. Full tool use, not just text completion.
- **You stay in control.** Every file mutation requires your explicit approval via inline keyboard buttons. One tap to approve, one tap to deny. Or hit "Accept all edits" when you trust the flow. When you're ready to commit, `/review` opens a swipe-based UI to stage exactly the hunks you want.
- **Talk to it.** Send a voice note and it gets transcribed automatically as a prompt — no typing needed. Great for quick instructions when you're on the go.
- **Multiple projects, one bot.** Switch between project contexts on the fly with `/context`. Each context has its own working directory, CLAUDE.md, model, and tool permissions.
- **Persistent sessions.** Pick up where you left off. Sessions survive restarts, and you can `/resume` any previous conversation.
- **Forum topic support.** Use Telegram forum channels to organize conversations — each topic thread gets its own independent Claude session. Run parallel tasks in the same chat without them stepping on each other. Claude auto-titles each topic for easy navigation.
- **Container isolation.** Run each context inside a Docker container with only the project directory mounted. On macOS, native `sandbox-exec` isolation — no Docker required.
- **Computer use.** Enable a headless desktop inside the container — Claude can launch Chromium, click around, take screenshots, and interact with GUIs. Watch live via VNC.
- **Group chat ready.** Add the bot to a team chat. It responds to @mentions and replies, so it stays out of the way until you need it.
- **Schedule recurring tasks.** Tell Claude to check your repo every morning, monitor a CI pipeline, or run a one-shot task later — all via natural language. Tasks run in isolated sessions automatically.
- **Watch background tasks.** When Claude runs a long command in the background, tap "View output" to open a live terminal viewer right in Telegram.
- **Locked down by default.** User allowlist, path-scoped file access, and granular tool approval. The agent can't silently read your `~/.ssh` or write outside your project.

## Code Review

OpenUdang includes a mobile-first code review UI built as a Telegram Mini App. Send `/review` to open it.

It works like Tinder for diffs — each hunk is a card. Swipe right to stage, left to skip, down to undo. You review at the hunk level, not the file level, so you can cherry-pick exactly the changes you want — like `git add -p`, but designed for your phone.

## Voice Notes

Send a voice message instead of typing. OpenUdang automatically transcribes it using [Moonshine](https://github.com/usefulsensors/moonshine) — a fast, lightweight speech-to-text model that runs locally. The transcribed text is sent to Claude as a prompt, prefixed with `[Transcribed from voice note]` so it knows the input came from speech.

The `moonshine-stt` binary is auto-downloaded on first use. No setup required.

## Scheduled Tasks

Set up recurring or one-shot tasks that Claude runs automatically. Just describe what you want in natural language — "check for broken tests every morning at 9am", "summarize the git log every Friday", or "run this migration in 30 minutes".

Claude manages schedules via built-in tools. Use `/schedule` to see what's active or remove tasks. Scheduled tasks run in isolated sessions with read-only access, so they can report but not modify your code without a follow-up conversation.

## Container Isolation

You can run each context inside a Docker container by adding a `container:` block to your context config. The Claude CLI runs inside the container with only the project directory bind-mounted — so it can't touch anything else on the host.

Session state is stored separately per context under `~/.config/openudang/containers/`, so containerized contexts don't interfere with each other or your host `~/.claude`.

On Linux, this uses Docker. On macOS, it uses Apple's `sandbox-exec` for native binary-level isolation — no Docker required.

## macOS App

On macOS, OpenUdang is also available as a menu bar app. Download the `.dmg` from [Releases](https://github.com/yjwong/open-udang/releases), drag to Applications, and launch — no terminal needed.

- Lives in the menu bar (shrimp icon) with no Dock icon
- First-run setup wizard walks you through configuration with native macOS dialogs
- Start/stop the bot, open config, view logs — all from the menu bar
- "Start at Login" toggle for automatic launch

> **Note:** The macOS app is currently unsigned. On first launch, macOS will block it — right-click the app and choose "Open" to bypass Gatekeeper, or go to System Settings → Privacy & Security and click "Open Anyway".

## Quick Start

### Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed and authenticated (via `claude` login or an [Anthropic API key](https://console.anthropic.com/))
- A Telegram bot token from [@BotFather](https://t.me/BotFather) — we strongly recommend enabling **Threaded Mode** (Settings → Bot Settings → Threads Settings → Threaded Mode). This lets each conversation run in its own forum topic with an independent Claude session.

### Option 1: Download Binary (recommended)

Grab the latest binary from [Releases](https://github.com/yjwong/open-udang/releases). No Python or package manager required — just download, configure, and run.

> **Note:** The Linux binaries require glibc ≥ 2.39 (Ubuntu 24.04+, Debian 13+, Fedora 40+). On older distros, use the [from-source](#option-2-from-source) install instead.

```bash
# Linux x86_64
curl -fsSL https://github.com/yjwong/open-udang/releases/latest/download/openudang-linux-x86_64 -o openudang
# Linux ARM64
curl -fsSL https://github.com/yjwong/open-udang/releases/latest/download/openudang-linux-aarch64 -o openudang
# macOS Apple Silicon
curl -fsSL https://github.com/yjwong/open-udang/releases/latest/download/openudang-macos-aarch64 -o openudang

chmod +x openudang
```

On first run, the binary will automatically set up an isolated Python environment and install dependencies. If no config file exists, an interactive setup wizard walks you through creating one. Subsequent runs start instantly.

### Option 2: From Source

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/yjwong/open-udang.git
cd open-udang

# Build the web apps (requires Node.js 18+)
for app in review-app terminal-app markdown-app vnc-app; do
  (cd "web/$app" && npm install && npm run build)
done

# If you don't need the web features, create empty placeholders instead:
# for app in review-app terminal-app markdown-app vnc-app; do mkdir -p "web/$app/dist"; done

uv sync
```

Just like the binary, running `uv run openudang` without a config file will launch the interactive setup wizard. Or configure manually:

<details>
<summary>Manual config setup</summary>

```bash
cp config.example.yaml ~/.config/openudang/config.yaml
```

Edit `~/.config/openudang/config.yaml` with your bot token, Telegram user IDs, and project directories:

```yaml
telegram:
  token: "YOUR_BOT_TOKEN"

allowed_users:
  - 123456789  # Your Telegram user ID

contexts:
  my-project:
    directory: /home/you/projects/my-project
    description: "My awesome project"
    model: claude-sonnet-4-6

default_context: my-project
```

</details>

### Run

```bash
# Binary
./openudang

# From source
uv run openudang

# If using an API key instead of Claude Code login
ANTHROPIC_API_KEY=sk-ant-... ./openudang
```

If no config file exists, OpenUdang starts an interactive setup wizard that walks you through creating one — no need to copy or edit YAML manually.

Or deploy as a systemd service for always-on access — see [Deployment](#deployment).

## Commands

| Command | Description |
|---------|-------------|
| `/context [name]` | List available contexts or switch to one |
| `/clear` | Start a fresh session in the current context |
| `/status` | Show current context, session, and running state |
| `/cancel` | Abort a running Claude invocation |
| `/model [name]` | Show or override the model for this chat |
| `/resume` | List and resume a previous session |
| `/review` | Open the mobile code review UI |
| `/mcp` | List and manage MCP servers |
| `/schedule` | List and manage scheduled tasks |
| `/tasks` | List or stop background tasks |
| `/vnc` | View the computer-use desktop |

## How Tool Approval Works

OpenUdang enforces a layered permission model:

- **Read-only tools** (Read, Glob, Grep) — auto-approved within the context directory
- **Write tools** (Edit, Write) — always require manual approval via Telegram inline buttons
- **Bash and other tools** — configurable per-context via `allowed_tools` patterns (e.g., `Bash(git *)`)
- **Paths outside the context directory** — always require manual approval, regardless of tool type

When a tool needs approval, you get three options: **Allow** (once), **Accept all [tool]** (auto-approve that tool for the session), or **Deny**. Edit/Write get the familiar **"Accept all edits"** button instead. All session-level approvals reset on `/clear` or context switch.

## Deployment

The easiest way to deploy is with the built-in install command:

```bash
# Install as a systemd user service (Linux) or launchd agent (macOS)
openudang install

# Remove the service
openudang uninstall
```

This auto-detects your platform, finds the executable path, and sets everything up — including enabling lingering on Linux so the service runs without an active login session.

<details>
<summary>Manual setup</summary>

If you prefer to set up the service manually:

```ini
# ~/.config/systemd/user/open-udang.service
[Unit]
Description=OpenUdang Telegram Bot

[Service]
ExecStart=/path/to/uv run openudang
# Only needed if not using Claude Code login:
# Environment=ANTHROPIC_API_KEY=sk-ant-...
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now open-udang
```

</details>

## License

MIT
