---
title: Installation
description: Install OpenShrimp and its prerequisites.
sidebar:
  order: 1
---

## Prerequisites

- **Claude CLI** — the Anthropic Claude Code CLI, installed and authenticated

### Install and authenticate Claude CLI

Follow the [official instructions](https://docs.anthropic.com/en/docs/claude-code/getting-started) to install Claude Code. Then authenticate:

```bash
claude  # follow the prompts to log in
```

You can also set `ANTHROPIC_API_KEY` in your environment if you prefer API key authentication.

## Download

Grab the latest binary for your platform. No Python or package manager required — just download, configure, and run.

### Linux x86_64

Requires glibc ≥ 2.39 (Ubuntu 24.04+, Debian 13+, Fedora 40+). On older distros, [build from source](/reference/building-from-source/) instead.

```bash
curl -fsSL https://github.com/yjwong/open-shrimp/releases/latest/download/openshrimp-linux-x86_64 -o openshrimp
chmod +x openshrimp
```

### Linux ARM64

Requires glibc ≥ 2.39 (Ubuntu 24.04+, Debian 13+, Fedora 40+). On older distros, [build from source](/reference/building-from-source/) instead.

```bash
curl -fsSL https://github.com/yjwong/open-shrimp/releases/latest/download/openshrimp-linux-aarch64 -o openshrimp
chmod +x openshrimp
```

### macOS Apple Silicon

```bash
curl -fsSL https://github.com/yjwong/open-shrimp/releases/latest/download/openshrimp-macos-aarch64 -o openshrimp
chmod +x openshrimp
```

All binaries and source archives are also available on the [GitHub Releases](https://github.com/yjwong/open-shrimp/releases) page.

## macOS App

On macOS, OpenShrimp is also available as a menu bar app. Download the `.dmg` from [Releases](https://github.com/yjwong/open-shrimp/releases), drag to Applications, and launch — no terminal needed.

- Lives in the menu bar (shrimp icon) with no Dock icon
- First-run setup wizard walks you through configuration with native macOS dialogs
- Start/stop the bot, open config, view logs — all from the menu bar
- "Start at Login" toggle for automatic launch

:::note
The macOS app is currently unsigned. On first launch, macOS will block it — right-click the app and choose "Open" to bypass Gatekeeper, or go to System Settings → Privacy & Security and click "Open Anyway".
:::

## Run the setup wizard

On first run, the binary launches an interactive setup wizard:

```bash
./openshrimp
```

The wizard walks you through:

1. Entering your Telegram bot token (from [@BotFather](https://t.me/BotFather))
2. Your Telegram user ID (from [@userinfobot](https://t.me/userinfobot))
3. Creating your first context (project directory, description, model)

It writes the config to `~/.config/openshrimp/config.yaml`. You can also set this up manually — see [Configuration](/getting-started/configuration/).

On subsequent runs, the binary starts instantly.

## Building from source

If you need to build from source (older Linux distros, development, etc.), see [Building from Source](/reference/building-from-source/).

## Next steps

Before running the bot, you need a Telegram bot token. Head to [Telegram Setup](/getting-started/telegram-setup/).
