---
title: Building from Source
description: Install OpenShrimp from source using Python and uv.
---

## Prerequisites

- **Python 3.11+** — check with `python3 --version`
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager
- **Git**

### Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Clone and install

```bash
git clone https://github.com/yjwong/open-shrimp.git
cd open-shrimp
uv sync
```

## Run

```bash
uv run openshrimp
```

If no config file exists, the interactive setup wizard will launch automatically. See [Configuration](/getting-started/configuration/) for manual setup.
