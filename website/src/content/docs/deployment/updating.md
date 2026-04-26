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

You can still update manually with `./openshrimp update`.

## macOS menu bar app

The `.app` bundle doesn't auto-update. To upgrade:

1. Download the latest `.dmg` from [Releases](https://github.com/yjwong/open-shrimp/releases)
2. Quit OpenShrimp from the menu bar
3. Drag the new `OpenShrimp.app` into `/Applications`, replacing the old one
4. Launch it again

Your config and sessions are stored outside the app bundle (`~/Library/Application Support/openshrimp/`), so they survive the upgrade.

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
