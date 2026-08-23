---
title: Computer Use
description: Enable GUI interaction — screenshots, clicking, typing, and web browsing inside the sandbox.
sidebar:
  order: 6
---

Computer use gives Claude a headless desktop environment inside the sandbox. Claude can take screenshots, click, type, scroll, and browse the web — all through MCP tools. You can watch live via VNC.

## Requirements

- A sandbox with `computer_use: true` — supported on every sandbox backend: Libvirt, Lima, and [HCS](/guides/hcs-sandbox/)
- The `review` section configured for Mini Apps (needed for VNC viewer)

## Setup

### Libvirt VM

```yaml
contexts:
  browser-tasks:
    directory: /home/you/Documents/browser-project
    description: "Browser automation"
    allowed_tools:
      - LSP
      - AskUserQuestion
    sandbox:
      backend: libvirt
      computer_use: true
```

## The desktop environment

The sandbox runs a headless 1280x720 Wayland desktop with:

- **labwc** — lightweight Wayland compositor
- **Chromium** — web browser
- **foot** — terminal emulator
- **wayvnc** — VNC server for live viewing

## MCP tools

When computer use is enabled, these MCP tools are registered automatically:

| Tool | Description |
|------|-------------|
| `computer_screenshot` | Take a PNG screenshot (1280x720). Sent to Telegram and returned for Claude to analyze. |
| `computer_click` | Click at (x, y) coordinates. Supports left, right, and middle buttons. |
| `computer_type` | Type text character by character. |
| `computer_key` | Press a key or key combo (e.g. `ctrl+a`, `alt+F4`, `super+d`). |
| `computer_scroll` | Scroll at (x, y) in a direction (up/down/left/right). |
| `computer_toplevel` | Focus a window by name (case-insensitive substring match). |

### How Claude uses them

Claude follows a screenshot-act loop:

1. Take a screenshot to see the current state
2. Decide what to do (click a button, type text, etc.)
3. Perform the action
4. Take another screenshot to verify the result

Screenshots are automatically sent to your Telegram chat so you can see what Claude sees.

## Watching live via VNC

Use the `/vnc` command to open the VNC viewer Mini App in Telegram. This gives you a live view of the desktop as Claude interacts with it.

```
/vnc
```

The VNC viewer uses noVNC and connects through a WebSocket proxy to the sandbox's VNC server.

:::note
The `/vnc` command requires the `review` section to be configured with either `public_url` or `tunnel`.
:::

## Implementation differences by backend

### Libvirt VM

- Screenshots via the libvirt domain screenshot API
- Input via QMP (QEMU Machine Protocol) — mouse events, key presses
- Window focus not directly supported (use `Alt+Tab` or similar key combos)
- VNC port auto-assigned from QEMU's VNC server

### HCS (Windows)

- The guest runs weston with its RDP backend; the host drives it over RDP and serves the VNC Mini App from that surface
- Screenshots and input travel over the RDP connection; clipboard goes over the guest exec channel via `wl-copy`/`wl-paste`
- Window focus not directly supported (use `Alt+Tab` or similar key combos)
- The desktop guest image downloads automatically on first boot — see the [HCS sandbox guide](/guides/hcs-sandbox/)

## Tips

- Claude works best when you describe what you want it to do on the screen rather than giving pixel coordinates
- For web tasks, you can ask Claude to open Chromium and navigate to a URL
- Screenshots are 1280x720 — this is the desktop resolution Claude interacts with
- If Claude gets stuck, you can connect via VNC and interact manually
