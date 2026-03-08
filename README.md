# OpenUdang

Telegram bot for remote Claude access via the Agent SDK. The prawn to [OpenClaw](https://openclaw.ai/)'s lobster.

*Udang* is Malay for "prawn" - small, personal, gets the job done.

## Features (planned)

- Stream Claude responses to Telegram via `sendMessageDraft` (Bot API 9.5)
- Interactive tool approval via inline keyboard buttons
- Multiple contexts (project directories with their own CLAUDE.md)
- Conversational sessions with persistence
- Group chat support with @mention/reply triggers
- User allowlist for access control

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for project management
- Anthropic API key (`ANTHROPIC_API_KEY`)
- Telegram Bot token (from [@BotFather](https://t.me/BotFather))

## Setup

```bash
uv sync
cp config.example.yaml ~/.config/openudang/config.yaml
# Edit config.yaml with your bot token, user IDs, and contexts
```

## Run

```bash
ANTHROPIC_API_KEY=sk-ant-... uv run openudang
```

## PRD

See [docs/prd.md](docs/prd.md) for the full product requirements document.
