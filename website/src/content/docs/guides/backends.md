---
title: Agent Backends
description: Choose the agent runtime — the Claude Agent SDK or OpenCode — globally or per context.
sidebar:
  order: 1.5
---

An *agent backend* is the runtime that actually drives the coding agent behind OpenShrimp. The top-level `backend:` key selects it for the whole instance, and any context can override it with its own `backend:` key. Everything else — contexts, tool approval, sandboxes, sessions — works the same regardless of which backend is active.

Two backends ship:

```yaml
backend: claude_sdk   # global default; can be overridden per context
```

## Comparison

| | `claude_sdk` | `opencode` |
|---|---|---|
| Default? | Yes | No |
| Runtime | Claude Agent SDK (bundled Claude Code CLI) | [`sst/opencode`](https://github.com/sst/opencode) over its HTTP serve API |
| Models | Anthropic models (`sonnet`, `opus`, `haiku`, or a full model ID) | OpenAI, Anthropic, and Google models — **must** be provider-qualified (`provider/model`) |
| Auth | `ANTHROPIC_API_KEY` or `/login` OAuth | `opencode auth login` (out-of-band, on the host) |
| Extra binary needed? | No (bundled) | No — downloaded on the first turn that needs it |

:::caution[Two unrelated `backend` keys]
There are **two** completely separate `backend:` settings. Don't conflate them:

- **`backend:`** (top-level or per-context) — selects the **agent runtime**: `claude_sdk` or `opencode`.
- **`sandbox.backend:`** (inside a context's `sandbox:` block) — selects the **sandbox type**: `libvirt`, `lima`, or `hcs`.

A context can set both at once, e.g. the `opencode` agent runtime running inside a `libvirt` sandbox.
:::

## OpenCode setup

Two preconditions on the host:

1. **Provider-qualified models.** Every OpenCode context's `model:` must be written as `provider/model`. OpenCode has no implicit default provider, so an unqualified model fails fast at startup. Examples:
   - `openai/gpt-5.5`
   - `anthropic/claude-opus-4-7`
   - `google/gemini-2.5-pro`
2. **Pre-authenticate out-of-band.** Run `opencode auth login` on the host. This writes credentials to `~/.local/share/opencode/auth.json`, which OpenShrimp reuses.

You do not install the CLI. The first turn on an OpenCode context downloads a pinned build (about 60 MB) and reports the transfer in the chat; every turn after that finds it already there. Bumping the pin re-downloads on the next start.

### The binary OpenShrimp runs

Only the copy OpenShrimp downloaded, under its own data directory. An `opencode` on your `PATH` or at `~/.opencode/bin/opencode` is ignored — it carries a version and an update policy OpenShrimp does not control, and the pin exists so host and every guest run the same build.

To run a different one, name it:

```bash
export OPENCODE_BIN=/path/to/your/opencode
```

`$OPENCODE_BIN` is taken as-is: OpenShrimp neither checks its version nor replaces it.

### Minimal example

```yaml
backend: opencode
contexts:
  my-project:
    directory: /home/you/projects/my-project
    model: openai/gpt-5.5   # provider/model REQUIRED
```

## Interaction with sandboxes

OpenCode works inside every sandbox backend, and the guest gets the same pinned version as the host. A libvirt guest is handed the host's binary over ssh; a Lima or HCS guest downloads the Linux archive itself and checks it against the same sha256 the host would. A guest running an older build is upgraded on the next sandbox start.

See the [VM Sandbox](/guides/vm-sandbox/), [Lima Sandbox](/guides/lima-sandbox/), and [HCS Sandbox](/guides/hcs-sandbox/) guides for sandbox setup, and the [Configuration Reference](/reference/config/) for all fields.
