---
title: Running as a Service
description: Keep OpenShrimp running without a terminal — a systemd user service on Linux, a launchd agent on macOS, or a logon task on Windows.
sidebar:
  order: 1
---

OpenShrimp includes a built-in installer that registers the bot with whatever your platform uses to start things when nobody is at a terminal: a systemd user service on Linux, a launchd user agent on macOS, or a logon task on Windows.

The terminal setup wizard offers this at the end, so a fresh install usually needs nothing on this page. `openshrimp install` is how you turn it on later, or change your mind.

## Automatic installation

```bash
openshrimp install
```

This will:
1. Check that the config file exists and loads
2. Detect your platform
3. Find the `openshrimp` executable
4. Register the bot — a unit file, an agent, or a logon task
5. Start it now, except on Windows, where the task runs at your next sign-in
6. On Linux, enable login lingering (so the service runs without an active login session)

Where the registration lands:

| Platform | Location |
|---|---|
| Linux | `~/.config/systemd/user/open-shrimp.service` |
| macOS | `~/Library/LaunchAgents/com.openshrimp.bot.plist` |
| Windows | a Task Scheduler logon task named `OpenShrimp` |

### Set up the bot first

`openshrimp install` refuses to install anything unless it finds a config file the bot can actually load. The service restarts the bot on failure, so a bot that cannot start is a bot that restarts forever — which looks, from the outside, like a machine doing nothing and saying nothing. Run `openshrimp` and complete the setup wizard first.

## Manual installation (Linux)

If you prefer to create the service file manually:

```ini
[Unit]
Description=OpenShrimp Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/path/to/openshrimp --config /home/you/.config/openshrimp/config.yaml
Restart=on-failure
RestartSec=5
Environment=ANTHROPIC_API_KEY=sk-ant-...

[Install]
WantedBy=default.target
```

Save this to `~/.config/systemd/user/open-shrimp.service`, then:

```bash
systemctl --user daemon-reload
systemctl --user enable open-shrimp
systemctl --user start open-shrimp
```

### Using an environment file

Instead of putting the API key directly in the unit file, you can use an environment file:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' > ~/.config/openshrimp/.env
chmod 600 ~/.config/openshrimp/.env
```

Then add to the `[Service]` section:

```ini
EnvironmentFile=/home/you/.config/openshrimp/.env
```

## Login lingering

By default, systemd user services stop when you log out. Enable lingering to keep the service running:

```bash
loginctl enable-linger
```

The automatic installer does this for you.

## Useful commands (Linux)

```bash
systemctl --user status open-shrimp    # check status
journalctl --user -u open-shrimp -f    # follow logs
systemctl --user restart open-shrimp   # restart
systemctl --user stop open-shrimp      # stop
```

## macOS (launchd)

On macOS, `openshrimp install` creates a launchd user agent at `~/Library/LaunchAgents/com.openshrimp.bot.plist`. Logs are written to `~/Library/Logs/OpenShrimp/`.

```bash
launchctl list | grep com.openshrimp    # check status
tail -f ~/Library/Logs/OpenShrimp/openshrimp.stderr.log  # follow logs
```

An agent the setup wizard registered has not been loaded yet — it is registered for your *next* login, so that it does not start a second bot alongside the one already running. `launchctl list` shows nothing for it until then, which is expected and not a failed install.

## Windows (logon task)

On Windows, `openshrimp install` registers a Task Scheduler logon task that starts the bot when you sign in. The task is named `OpenShrimp`, or `OpenShrimp-<name>` if your config sets an `instance_name`.

```powershell
schtasks /Query /TN OpenShrimp   # check status
schtasks /Run /TN OpenShrimp     # start it now
```

The tray app registers its own task under the same name, so if a task of that name already exists the installer asks before replacing it — and refuses rather than replacing it when there is no terminal to ask at.

## Uninstalling

```bash
openshrimp uninstall
```

This removes whatever was registered — the unit file, the agent, or the logon task. On Linux and macOS the running bot is stopped as well; on Windows, deleting the task means nothing starts at your next sign-in, but a bot that is already running keeps running.
