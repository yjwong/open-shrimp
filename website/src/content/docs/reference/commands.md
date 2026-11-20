---
title: Commands
description: All bot commands with syntax and examples.
---

## Session commands

### `/context [name]`

List available contexts or switch to a different one.

- **No arguments** — shows a paginated list of contexts. The current context is marked with a bullet.
- **With name** — switches to the named context. If a session exists for that context, you're prompted to resume or start fresh.

```
/context            # list contexts
/context myproject  # switch to myproject
```

:::note
If a context is locked to a chat via `locked_for_chats`, switching is not allowed in that chat.
:::

### `/clear`

Start a completely fresh session. Clears all state: session history, model overrides, edit approvals, and cancels any running tasks.

```
/clear
```

### `/model [name|reset]`

Show or change the model for the current session.

- **No arguments** — shows the current model and any active override.
- **With name** — sets a session-scoped model override. The current session is closed; the next message starts a new session with the new model.
- **`reset`** — clears the override and reverts to the context's default model.

```
/model          # show current model
/model opus     # switch to Opus
/model reset    # revert to default
```

Short names are supported: `sonnet`, `opus`, `haiku`. You can also use a full model ID.

### `/backend [name|reset]`

Show or change which agent backend serves this topic, independently of every other topic on the same project.

- **No arguments** — shows the backend in effect, the context default, and a picker.
- **With name** — pins this topic to `claude_sdk` or `opencode`.
- **`reset`** — clears the pin and reverts to the context's `backend:` key.

```
/backend            # show current backend
/backend opencode   # run this topic on OpenCode
/backend reset      # revert to the context default
```

The two backends keep separate conversation histories, so switching closes the current session and the next message starts a fresh one. Any `/model` override goes with it — model names are backend-specific.

### `/resume [session_id]`

List recent sessions or resume a specific one.

- **No arguments** — shows a paginated list of recent sessions for the current context, with metadata (title, summary, creation time, size).
- **With ID or prefix** — resumes the specified session.

```
/resume                      # list recent sessions
/resume abc123               # resume by ID prefix
```

### `/fork`

Copy this topic's session into a new topic, so you can try a different direction without losing the current one.

The new topic is named after the session's summary with `(fork)` appended. It runs on the same project and working directory, and for a sandboxed context it uses the same sandbox. It keeps this topic's `/model`, `/effort` and `/backend` overrides, its `/add_dir` directories, and every approval you gave for the session, including "Accept all edits". Its first message repeats your last prompt and the last reply, and the conversation continues from there when you send a message.

The two topics are independent after the fork. Both edit the same files, so keep track of which one changes what.

If the agent is still working when you fork, the copy ends at the last finished turn, and the running turn stays in this topic. If no turn has finished yet, wait for the first one to end.

```
/fork
```

:::note
`/fork` needs a chat that can have topics: a private chat with the bot, or a group with topics turned on.
:::

### `/share`

Send this topic's session as a single HTML file you can forward, to WhatsApp for example. It opens in any browser, offline, with no account.

The page has every prompt, reply and tool call, with tool calls and the agent's reasoning folded until tapped. File edits show as coloured diffs, and screenshots and images you sent are embedded. Subagent runs appear inside the call that started them. The header names the model and the agent, when the session started and ended, and each turn carries its time. Times are in the bot host's time zone.

Tool output goes in verbatim, with nothing redacted, so a session that printed a key, a token or a private path carries it in the file. Read the page before forwarding it. Only the system prompt, `CLAUDE.md`-style instruction files, hook output and compaction summaries are left out.

```
/share
```

Telegram caps what a bot can send at 50 MB. A session past that, usually from many screenshots, gets an error instead of a file.

## Task commands

### `/cancel`

Abort the currently running Claude task. Also clears any queued setup messages.

```
/cancel
```

### `/tasks`

List active background tasks with their elapsed time and description.

```
/tasks
```

### `/schedule`

List and manage scheduled tasks. Scheduled tasks are cron-like recurring or one-shot Claude prompts that run automatically.

```
/schedule
```

:::tip
To create a scheduled task, just describe it in natural language to the bot:

> Check the deployment status every 30 minutes and tell me if anything is failing.

Claude will use the scheduling MCP tools to set it up.
:::

## Information commands

### `/status`

Show current state: context name and directory, active model, session ID, running status, and any active background tasks.

```
/status
```

### `/usage`

Show Claude API quota and usage statistics for your account, including tier information and usage bars.

```
/usage
```

## Mini App commands

### `/review`

Open the Review Mini App — a web-based diff viewer for the current context's working directory. Shows one button per directory if the context has additional directories.

Requires `review.public_url` or `review.tunnel` to be configured.

```
/review
```

### `/vnc`

Open the VNC viewer Mini App for computer-use contexts. Only available when the context has `computer_use: true` and the sandbox is running with a desktop environment.

```
/vnc
```

### `/login`

Open the authentication Mini App for token-based login. Used to re-authenticate Claude Code access for Mini Apps.

```
/login
```

## Server commands

### `/mcp [subcommand] [server-name]`

List and manage MCP (Model Context Protocol) servers.

- **No arguments** — lists all MCP servers with their connection status, tool counts, and version info.
- **`reset <name>`** — reconnect a failed MCP server.
- **`enable <name>`** — enable a disabled MCP server.
- **`disable <name>`** — disable an MCP server.

```
/mcp                # list servers
/mcp reset github   # reconnect the github server
/mcp disable slack  # disable the slack server
```

:::note
MCP-server management works regardless of the active [agent backend](/guides/backends/) — both `claude_sdk` and `opencode`.
:::

### `/restart`

Restart the OpenShrimp process. The bot sends a confirmation message after it comes back online.

```
/restart
```
