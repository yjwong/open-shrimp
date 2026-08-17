---
title: Telegram Setup
description: Create a Telegram bot and let the setup wizard enroll you.
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

While you're in BotFather, configure these:

- **Threaded Mode** — Settings → Bot Settings → Threads Settings → **Threaded Mode**, then turn it on. This is what unlocks parallel conversations (see below). Strongly recommended.
- **Privacy** — `/setprivacy` and set to **Disable**. Lets the bot see every message in groups, which is required for forum topic support and for group chats to work without @mentions.

## Run conversations in parallel with Threaded Mode (strongly recommended)

OpenShrimp comes alive when you can run more than one conversation at a time, and **Threaded Mode** is what makes that possible — even in a 1-on-1 private chat with the bot.

With Threaded Mode enabled in BotFather, your private chat with the bot can hold many separate threads. Each thread is an independent conversation with its own context, working directory, Claude session, and tool-approval state. Think one thread per project, per task, or per investigation.

Why this matters: without threads, every message lands in the same Claude session, so a long-running task blocks anything else you'd want to do. With threads, you can have Claude refactoring one repo while you ask it questions about another — neither conversation interferes with the other.

The same model extends to **forum groups** (a group with Topics enabled): each topic is a separate thread, and the bot responds to every message inside a topic without needing an @mention. Use a forum group if you want to share a workspace with other allowed users; otherwise a private chat with Threaded Mode is the simplest setup.

## Who the bot answers

OpenShrimp only responds to users in the `allowed_users` list. That list is the
only thing standing between a stranger and a bot that runs shell commands and
edits files on your computer, so the setup wizard fills it in by proving who
you are rather than by asking you to type a number.

The wizard shows you your bot's `@name` and asks you to message it. It replies
with a six-digit **setup code**; you type that back into the wizard, which then
names the account it's about to grant access to and asks you to confirm. Only
that account is written in.

Three things follow from doing it this way:

- A message the bot received **before** the wizard opened — Telegram queues them
  for up to a day — can never enroll anyone, and never gets a code.
- Group messages and messages from other bots are ignored outright.
- If several people message the bot during setup, the wizard says so and stops
  handing out codes rather than quietly picking one.

If Telegram Desktop is on the same computer as the wizard, it also shows a link
that skips the code entirely.

### Adding somebody later

The handshake enrolls one operator. To allow a second account, add its numeric
user ID to `allowed_users` in the config by hand — [@userinfobot](https://t.me/userinfobot)
will tell them theirs.

The bot deliberately says **nothing** to anyone who isn't on the list: replying
would confirm to anyone scanning bot usernames that a real machine sits behind
this one. It logs every turned-away sender instead, and sends you an
at-most-hourly note that it happened, so a wrong `allowed_users` still tells
you something — just not them.

## Next steps

Now configure the bot — see [Configuration](/getting-started/configuration/).
