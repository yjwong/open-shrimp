---
title: Updating
description: How to update OpenShrimp to the latest version.
sidebar:
  order: 3
---

How you update depends on how you installed OpenShrimp.

## Binary installs (Linux, macOS Apple Silicon)

If you [downloaded the binary](/getting-started/installation/#download), OpenShrimp checks GitHub Releases for new versions every 6 hours and notifies you in Telegram.

When an update is available, you'll get a message with the new version number, release notes, and two buttons:

- **Update now** — downloads the new binary, atomically replaces the running one, and restarts the bot
- **Skip** — dismisses the notification (you won't be re-notified until an even newer version ships)

No SSH, no manual download — just tap the button.

### Manual check

To check immediately instead of waiting for the next scheduled check:

```bash
./openshrimp update
```

This prints the current version, fetches the latest release, and prompts before applying.

### Disabling auto-update

To turn off the periodic check, add this to `config.yaml`:

```yaml
auto_update: false
```

You can still update manually with `./openshrimp update`. On macOS this flag governs the menu bar app as well — see below.

## macOS menu bar app

The `.app` updates itself, unattended. Every 6 hours it checks a signed appcast published with each release; when there is a new version it downloads the DMG, verifies the EdDSA signature and Apple's notarization, installs it, and relaunches. No panel, no click — the person who would click is on Telegram, and an update waiting for somebody standing at the Mac is an update that never happens.

You still hear about it. When the core comes back it messages every allowed user with the version it came back at.

One release carries both halves. The DMG holds the core binaries, and the app installs the new core at `~/Library/Application Support/openshrimp/bin/openshrimp` as it relaunches. Because the app installs it, the core's own six-hourly check is switched off while the app supervises it, so one release produces one message rather than two. Config and sessions live outside the bundle and survive the upgrade.

### Checking now

**Check for Updates…** in the OpenShrimp menu. Unlike the scheduled check, it tells you when there is nothing to install.

### Turning it off

```yaml
auto_update: false
```

The app reads that flag at launch and stops both the scheduled check and the automatic install. **Check for Updates…** keeps working — that one you asked for. If `config.yaml` cannot be read at all, updates stay on.

### Quitting the app stops the core

The app stops the core it supervises when it quits, including a core you started yourself in a terminal — it adopts that one at launch rather than starting a second bot on the same token.

Installing an update is a quit. It stops that core too, and the relaunched app starts the version it just seeded in its place.

### Installing by hand

Still supported, and the way to move backwards:

1. Download the `.dmg` from [Releases](https://github.com/yjwong/open-shrimp/releases)
2. Quit OpenShrimp from the menu bar
3. Drag `OpenShrimp.app` into `/Applications`, replacing the old one
4. Launch it again

Installing an *older* app leaves a newer core alone: the seed only ever moves forwards, so a downgrade of the app is not a silent downgrade of the bot.

## Source builds

If you [built from source](/reference/building-from-source/), pull the latest code and sync dependencies:

```bash
cd /path/to/open-shrimp
git pull
uv sync
```

Then restart the service (see below).

## Restarting

After a manual update, restart the bot to pick up changes:

```bash
# From Telegram (works for any install)
/restart

# systemd (Linux)
systemctl --user restart open-shrimp

# launchd (macOS)
launchctl kickstart gui/$(id -u)/com.openshrimp.bot

# Windows (logon task) — the task only starts the bot at sign-in, so /restart
# above is the way to restart one that is already running
schtasks /Run /TN OpenShrimp
```

The `/restart` command is the quickest way to restart without SSH access.

## Versioning

Both the `open-shrimp` bot and the `moonshine-stt` speech-to-text binary share a single version number. To check the current version, run `./openshrimp update` — the first line of output prints the installed version before checking for newer ones.

## Docker sandbox images

If you use Docker sandboxes, updated code may require rebuilding the container image. OpenShrimp builds images lazily — the next time a sandboxed context is used, the image will be rebuilt if the Dockerfile or base image has changed.

To force a rebuild, remove the existing image:

```bash
docker rmi openshrimp-claude:your-context-name
```

## Configuration changes

Most configuration changes take effect after a restart. If you've changed `config.yaml`, restart the service to pick up the new settings.
