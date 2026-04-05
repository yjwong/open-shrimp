---
title: Telegram Setup
description: Create a Telegram bot and find your user ID.
sidebar:
  order: 2
---

## Create a bot with BotFather

1. Open Telegram and search for [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Choose a display name (e.g. "My Claude")
4. Choose a username ending in `bot` (e.g. `my_claude_bot`)
5. Copy the **bot token** — it looks like `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`

:::tip
Keep your bot token secret. Anyone with it can control your bot.
:::

### Recommended bot settings

While you're in BotFather, configure these optional settings:

- `/setprivacy` — set to **Disable** if you want the bot to see all messages in group chats (required for group chat support without @mentions)
- `/setcommands` — paste the following to register command autocompletion:

```
context - List or switch contexts
clear - Start a fresh session
status - Show current status
cancel - Abort running task
model - Show or change model
resume - List or resume sessions
review - Open the review Mini App
schedule - Manage scheduled tasks
tasks - List background tasks
usage - Show API usage stats
vnc - Open VNC viewer
login - Authenticate for Mini Apps
mcp - Manage MCP servers
restart - Restart the bot
```

## Find your Telegram user ID

OpenShrimp only responds to users in the `allowed_users` list. To find your user ID:

1. Open Telegram and search for [@userinfobot](https://t.me/userinfobot)
2. Send any message
3. It replies with your numeric user ID (e.g. `123456789`)

Add this number to your config file's `allowed_users` list.

## Next steps

Now configure the bot — see [Configuration](/getting-started/configuration/).
