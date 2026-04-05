---
title: MCP Servers
description: Manage Model Context Protocol servers that extend Claude's capabilities.
sidebar:
  order: 11
---

MCP (Model Context Protocol) servers extend Claude's capabilities with additional tools. OpenShrimp inherits MCP server configuration from your Claude CLI settings and provides commands to manage them.

## How MCP servers work

MCP servers are external processes that provide tools to Claude via the Model Context Protocol. Examples include:

- GitHub integration (create PRs, read issues)
- Slack messaging
- Database access
- Custom project-specific tools

Claude discovers available tools from connected MCP servers and can call them during conversations.

## Viewing MCP servers

List all configured MCP servers and their status:

```
/mcp
```

Each server shows:

- **Name** — the server identifier
- **Status** — connection state with emoji indicators:
  - Connected and operational
  - Warning (partial issues)
  - Disconnected or failed
- **Tool count** — number of tools the server provides
- **Version** — server version info

## Managing servers

### Reset a server

If a server has disconnected or is in an error state, reconnect it:

```
/mcp reset github
```

This terminates the existing connection and starts a fresh one.

### Disable a server

Temporarily disable a server without removing its configuration:

```
/mcp disable slack
```

Disabled servers don't start on new sessions.

### Enable a server

Re-enable a previously disabled server:

```
/mcp enable slack
```

## Configuration

MCP servers are configured in your Claude CLI settings, not in OpenShrimp's config. They're typically defined in:

- `~/.claude/settings.json` — global settings
- `<project>/.claude/settings.json` — per-project settings

OpenShrimp respects both global and project-level MCP server configurations. The servers available depend on which context you're in.

## Built-in MCP tools

OpenShrimp registers its own MCP server (`openshrimp`) that provides:

| Tool | Description |
|------|-------------|
| `send_file` | Send files to Telegram (photos, documents) |
| `edit_topic` | Set forum topic title and icon (forum topics only) |
| `create_schedule` | Create a scheduled task |
| `list_schedules` | List scheduled tasks |
| `delete_schedule` | Delete a scheduled task |
| `computer_screenshot` | Take a screenshot (computer-use contexts only) |
| `computer_click` | Click at coordinates (computer-use contexts only) |
| `computer_type` | Type text (computer-use contexts only) |
| `computer_key` | Press keys (computer-use contexts only) |
| `computer_scroll` | Scroll (computer-use contexts only) |
| `computer_toplevel` | Focus a window (computer-use contexts only) |

These tools are registered automatically based on your context configuration.

## Troubleshooting

### Server won't connect

1. Check that the server process is available in the PATH
2. Verify the server configuration in Claude CLI settings
3. Try `/mcp reset <name>` to force a reconnection
4. Check OpenShrimp logs for error details

### Tools not appearing

MCP tools are discovered when a session starts. If you added a new server, use `/clear` to start a fresh session and pick up the new tools.
