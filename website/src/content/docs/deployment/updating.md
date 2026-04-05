---
title: Updating
description: How to update OpenShrimp to the latest version.
sidebar:
  order: 3
---

## Update steps

Pull the latest code and sync dependencies:

```bash
cd /path/to/open-shrimp
git pull
uv sync
```

Then restart the service:

```bash
# systemd (Linux)
systemctl --user restart open-shrimp

# launchd (macOS)
launchctl kickstart gui/$(id -u)/com.openshrimp.bot

# or from Telegram
/restart
```

## Versioning

Both the `open-shrimp` bot and the `moonshine-stt` speech-to-text binary share a single version number from the `VERSION` file at the repository root. Both `pyproject.toml` files read from it via hatchling's dynamic version source.

To check the current version:

```bash
cat VERSION
```

## Docker sandbox images

If you use Docker sandboxes, updated code may require rebuilding the container image. OpenShrimp builds images lazily — the next time a sandboxed context is used, the image will be rebuilt if the Dockerfile or base image has changed.

To force a rebuild, remove the existing image:

```bash
docker rmi openshrimp-claude:your-context-name
```

## Configuration changes

Most configuration changes take effect after a restart. If you've changed `config.yaml`, restart the service to pick up the new settings.

The `/restart` command from Telegram is the quickest way to restart without SSH access.
