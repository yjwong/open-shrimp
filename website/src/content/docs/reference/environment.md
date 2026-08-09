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

## HCS sandbox overrides

These apply only to contexts on the [HCS sandbox backend](/guides/hcs-sandbox/), and only when an artifact is not where the backend looks for it by default.

| Variable | Description |
|----------|-------------|
| `OPENSHRIMP_HCS_KERNEL` | Kernel the guest boots. Defaults to WSL's, at `C:\Program Files\WSL\tools\kernel` |
| `OPENSHRIMP_HCS_INITRD` | Control initramfs you built yourself, instead of the one downloaded automatically. Unset, a copy staged at `C:\ProgramData\openshrimp\hcs\initrd.img` is used if present, and the released asset is downloaded otherwise |
| `OPENSHRIMP_HCS_RDP_HELPER` | Directory holding an RDP helper bundle you staged yourself, instead of the one downloaded automatically |

## Internal variables

These are used internally by OpenShrimp and generally don't need to be set manually.

| Variable | Description |
|----------|-------------|
| `OPENSHRIMP_RESTART_CHAT_ID` | Chat ID for post-restart confirmation (set by `/restart`) |
| `OPENSHRIMP_RESTART_THREAD_ID` | Thread ID for post-restart confirmation (set by `/restart`) |
