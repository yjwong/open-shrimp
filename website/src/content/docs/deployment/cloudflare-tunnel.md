---
title: Cloudflare Tunnel
description: Expose Mini Apps publicly with a Cloudflare quick tunnel.
sidebar:
  order: 2
---

Mini Apps (Review, Terminal, VNC, Markdown preview) need a public URL so Telegram can load them. The simplest way is a [Cloudflare quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/) — free, no account needed.

## Setup

Add `tunnel: cloudflared` to the `review` section of your config:

```yaml
review:
  port: 8080
  tunnel: cloudflared
```

That's it. On startup, OpenShrimp will:

1. Download the `cloudflared` binary if not already installed
2. Start a quick tunnel pointing to the local HTTP server
3. Set `public_url` automatically from the assigned `trycloudflare.com` URL

The tunnel URL changes each time the bot restarts. This is fine for personal use — Telegram caches Mini App URLs per session.

## Custom domain

If you'd rather use your own domain, set `public_url` directly instead of using the tunnel:

```yaml
review:
  port: 8080
  public_url: "https://shrimp.yourdomain.com"
```

Then point your domain to the HTTP server using a reverse proxy (nginx, Caddy, etc.) or a persistent Cloudflare tunnel.

When `public_url` is set, the `tunnel` option is ignored.

## How it works

The HTTP server hosts several Mini App endpoints:

| Path | Mini App | Description |
|------|----------|-------------|
| `/review/` | Review | Git diff viewer and staging tool |
| `/terminal/` | Terminal | xterm.js viewer for background task output |
| `/vnc/` | VNC | noVNC viewer for computer-use desktops |
| `/preview/` | Markdown Preview | Ephemeral markdown content viewer |

All Mini Apps use token-based authentication via Telegram's `initData` validation. The tunnel simply makes these endpoints reachable from Telegram's servers.

## Troubleshooting

- **Mini App buttons show "Bot domain invalid"** — the tunnel URL may have changed after a restart. Send a new command (e.g. `/review`) to get a fresh URL.
- **Tunnel fails to start** — check that port 8080 (or your configured port) is not already in use. Check logs with `journalctl --user -u open-shrimp -f`.
- **Slow to connect** — quick tunnels can take a few seconds to establish on startup. The bot will log the assigned URL once ready.
