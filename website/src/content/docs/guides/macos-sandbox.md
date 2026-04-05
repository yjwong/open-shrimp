---
title: macOS Sandbox
description: Lightweight sandboxing on macOS using Apple's sandbox-exec.
sidebar:
  order: 5
---

The macOS sandbox uses Apple's built-in `sandbox-exec` to restrict the Claude CLI's filesystem access. It's lighter than Docker or a VM — no images to build, no boot time — but provides less isolation.

## Setup

```yaml
contexts:
  myproject:
    directory: /Users/you/Documents/myproject
    description: "My project"
    allowed_tools:
      - LSP
      - AskUserQuestion
    sandbox:
      backend: macos
```

## What it restricts

The sandbox profile allows access to:

- The project directory and any `additional_directories`
- Claude's session storage
- Network access (for API calls)

Everything else on the filesystem is blocked. The agent can't read your home directory, SSH keys, or other projects.

## Differences from Docker/VM sandboxes

| Feature | macOS | Docker | VM |
|---------|-------|--------|----|
| Boot time | None | Fast | ~13s |
| Image build | None | Required | Required |
| Filesystem isolation | Process-level | Container-level | Full VM |
| Network isolation | No | Partial | Full |
| Computer use | Not supported | Supported | Supported |
| Tool auto-approval | Yes | Yes | Yes |

Like all sandboxes, enabling the macOS backend auto-approves all Bash commands and path-scoped tools since the sandbox provides the safety boundary.

## Limitations

- **macOS only** — obviously, this only works on macOS
- **No computer use** — GUI interaction is not supported with the macOS sandbox
- **Process-level isolation** — relies on Apple's sandbox profiles rather than a full container or VM boundary
- **Shared host environment** — the agent runs in your host environment, so installed tools and environment variables are accessible

## When to use it

The macOS sandbox is a good fit when:

- You're running OpenShrimp on your Mac and want basic filesystem isolation
- You don't need Docker or VM overhead
- You don't need computer use capabilities
- You want the convenience of auto-approved tools with some safety guardrails
