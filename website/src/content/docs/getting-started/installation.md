---
title: Installation
description: Install OpenShrimp and its prerequisites.
sidebar:
  order: 1
---

## Prerequisites

- **Python 3.11+** — check with `python3 --version`
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager
- **Claude CLI** — the Anthropic Claude Code CLI, installed and authenticated
- **Git**

### Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Install and authenticate Claude CLI

Follow the [official instructions](https://docs.anthropic.com/en/docs/claude-code/getting-started) to install Claude Code. Then authenticate:

```bash
claude  # follow the prompts to log in
```

You can also set `ANTHROPIC_API_KEY` in your environment if you prefer API key authentication.

## Install OpenShrimp

Clone the repository and install dependencies:

```bash
git clone https://github.com/yjwong/open-shrimp.git
cd open-shrimp
uv sync
```

## Run the setup wizard

If this is your first time, run the bot and it will launch an interactive setup wizard:

```bash
uv run openshrimp
```

The wizard walks you through:

1. Entering your Telegram bot token (from [@BotFather](https://t.me/BotFather))
2. Your Telegram user ID (from [@userinfobot](https://t.me/userinfobot))
3. Creating your first context (project directory, description, model)

It writes the config to `~/.config/openshrimp/config.yaml`. You can also set this up manually — see [Configuration](/getting-started/configuration/).

## Next steps

Before running the bot, you need a Telegram bot token. Head to [Telegram Setup](/getting-started/telegram-setup/).
