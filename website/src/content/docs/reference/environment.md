---
title: Environment Variables
description: Environment variables used by OpenShrimp.
---

OpenShrimp reads the following environment variables at runtime.

## `ANTHROPIC_API_KEY`

Your Anthropic API key. Passed through to the Claude CLI for authentication.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

This is optional if you have authenticated the Claude CLI via OAuth (`claude login`). When both are available, the API key takes precedence.

:::tip
For systemd services, set the API key in the unit file's `Environment=` directive or use `EnvironmentFile=` to load it from a file. See [systemd deployment](/deployment/systemd/) for details.
:::

## Internal variables

These are used internally by OpenShrimp and generally don't need to be set manually.

| Variable | Description |
|----------|-------------|
| `OPENSHRIMP_RESTART_CHAT_ID` | Chat ID for post-restart confirmation (set by `/restart`) |
| `OPENSHRIMP_RESTART_THREAD_ID` | Thread ID for post-restart confirmation (set by `/restart`) |
