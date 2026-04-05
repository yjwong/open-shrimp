---
title: Mini Apps
description: Built-in Telegram Mini Apps for reviewing diffs, viewing terminal output, watching VNC, and previewing markdown.
sidebar:
  order: 8
---

OpenShrimp includes several Telegram Mini Apps — lightweight web interfaces that open directly inside Telegram. They require the `review` section in your config.

## Setup

```yaml
review:
  host: "127.0.0.1"
  port: 8080
  tunnel: cloudflared  # auto-start a public tunnel
```

The `tunnel: cloudflared` option starts a free [Cloudflare quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/) — no account needed. The `cloudflared` binary is auto-downloaded if not installed.

Alternatively, set `public_url` if you're behind a reverse proxy:

```yaml
review:
  host: "127.0.0.1"
  port: 8080
  public_url: "https://your-domain.com"
```

:::note
Mini Apps need a public URL to work in Telegram. Use either `tunnel: cloudflared` or `public_url` — not both. If `public_url` is set, the tunnel is not started.
:::

## Review App

The Review App is a web-based diff viewer and staging tool. Open it with:

```
/review
```

It shows the `git diff` for the current context's working directory. You can:

- Browse changed files with syntax-highlighted diffs
- Stage and unstage individual hunks
- Commit changes with a message

If your context has `additional_directories`, you'll see one button per directory.

## Terminal App

The Terminal App shows output from background Bash tasks. When Claude runs a Bash command with `run_in_background`, the tool result message includes a "View output" button that opens the terminal viewer.

The viewer:

- Loads existing output from the task log
- Streams new output in real time via Server-Sent Events (SSE)
- Uses xterm.js for proper terminal rendering (colors, cursor positioning, etc.)

Task output files are stored under `/tmp/claude-<uid>/` and discovered automatically.

## VNC App

The VNC App provides a live view of the desktop in [computer-use](/guides/computer-use/) contexts. Open it with:

```
/vnc
```

It uses noVNC to connect through a WebSocket-to-TCP proxy to the sandbox's VNC server. You can watch Claude interact with the desktop in real time, or take over and interact manually.

Only available when the context has `computer_use: true` and the sandbox is running.

## Markdown Preview App

The Markdown Preview App renders markdown content in a formatted view. When Claude sends a file via the `send_file` MCP tool and the file is a Markdown file, a "Preview" button is attached that opens the rendered preview.

This uses an ephemeral content store — previews are temporary and not persisted.

## Authentication

Mini Apps use token-based authentication. Use `/login` to authenticate if you encounter auth issues. The auth token is validated against your Telegram user identity.
