---
title: Installation
description: Install OpenShrimp and its prerequisites.
sidebar:
  order: 1
---

## Prerequisites

OpenShrimp bundles the Claude Code CLI via the Agent SDK, so there's nothing to install separately. You just need to authenticate Claude — either by running `/login` from inside the bot (see [Authenticate Claude](#authenticate-claude) below), or by setting `ANTHROPIC_API_KEY` in your environment.

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

## Run the setup wizard

On first run, the binary launches an interactive setup wizard:

```bash
./openshrimp
```

The wizard walks you through:

1. Entering your Telegram bot token (from [@BotFather](https://t.me/BotFather))
2. Your Telegram user ID (from [@userinfobot](https://t.me/userinfobot))
3. Creating your first context (project directory, description, model)

It writes the config to a platform-specific location (`~/.config/openshrimp/config.yaml` on Linux, `~/Library/Application Support/openshrimp/config.yaml` on macOS). You can also set this up manually — see [Configuration](/getting-started/configuration/).

On subsequent runs, the binary starts instantly.

## Authenticate Claude

Once the bot is running, open it in Telegram and send `/start` to see a welcome message confirming you're connected and showing your current context.

If you haven't set `ANTHROPIC_API_KEY`, send `/login` (in a private chat) to authenticate Claude Code via OAuth. This opens a Mini App that runs the same OAuth flow you'd get from the Claude Code CLI — paste the resulting token to finish login. Use `/login` again any time you need to re-authenticate.

## Building from source

If you need to build from source (older Linux distros, development, etc.), see [Building from Source](/reference/building-from-source/).

## Next steps

Before running the bot, you need a Telegram bot token. Head to [Telegram Setup](/getting-started/telegram-setup/).
