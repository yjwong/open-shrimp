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
| Extra binary needed? | No (bundled) | Yes — the `opencode` binary must be discoverable |

:::caution[Two unrelated `backend` keys]
There are **two** completely separate `backend:` settings. Don't conflate them:

- **`backend:`** (top-level or per-context) — selects the **agent runtime**: `claude_sdk` or `opencode`.
- **`sandbox.backend:`** (inside a context's `sandbox:` block) — selects the **sandbox type**: `docker`, `libvirt`, or `lima`.

A context can set both at once, e.g. the `opencode` agent runtime running inside a `docker` sandbox.
:::

## OpenCode setup

OpenCode is not bundled. Before selecting it, satisfy three preconditions on the host:

1. **Provider-qualified models.** Every OpenCode context's `model:` must be written as `provider/model`. OpenCode has no implicit default provider, so an unqualified model fails fast at startup. Examples:
   - `openai/gpt-5.5`
   - `anthropic/claude-opus-4-7`
   - `google/gemini-2.5-pro`
2. **Pre-authenticate out-of-band.** Run `opencode auth login` on the host. This writes credentials to `~/.local/share/opencode/auth.json`, which OpenShrimp reuses.
3. **Discoverable binary.** At startup OpenShrimp locates the `opencode` binary by checking, in order:
   1. the `$OPENCODE_BIN` environment variable,
   2. `~/.opencode/bin/opencode`,
   3. your `PATH`.

### Minimal example

```yaml
backend: opencode
contexts:
  my-project:
    directory: /home/you/projects/my-project
    model: openai/gpt-5.5   # provider/model REQUIRED
```

## Interaction with sandboxes

OpenCode works inside sandboxes, with a few backend-specific details:

- **Docker** — OpenCode contexts use a separate image, `openshrimp-opencode:latest`, built lazily on first use (distinct from the `openshrimp-claude` image used by `claude_sdk`).
- **Libvirt and Lima** — when the host has an `opencode` binary, OpenShrimp auto-installs it into the guest. Otherwise the base image or `provision:` script must supply it.

See the [Docker Sandbox](/guides/docker-sandbox/), [VM Sandbox](/guides/vm-sandbox/), and [Lima Sandbox](/guides/lima-sandbox/) guides for sandbox setup, and the [Configuration Reference](/reference/config/) for all fields.
