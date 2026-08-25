#!/usr/bin/env python3
"""Repin opencode: rewrite ``OPENCODE_VERSION`` and ``OPENCODE_CHECKSUMS``.

The CLI archives have no upstream checksum file — the ``latest.json`` and
``latest-*.yml`` a release publishes are electron-updater manifests for the
desktop app, and list sha512s for the AppImage, ``.deb`` and ``.rpm`` only.
So the table is produced by downloading all six assets and hashing them, which
is six manual downloads per bump without this.

    scripts/bump_opencode.py            # the latest release
    scripts/bump_opencode.py 1.18.23    # a specific one

Each asset is hashed as it streams, so ~340 MB crosses the wire and nothing
lands on disk.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_RELEASE_PY = _ROOT / "src" / "open_shrimp" / "backend" / "opencode" / "release.py"


def _release_module():
    """``release.py`` loaded by path, so this runs on a bare interpreter.

    Importing it as ``open_shrimp.backend.opencode.release`` would drag in the
    package's ``__init__``, and through it the Claude SDK — a dependency a
    checksum table has nothing to do with.
    """
    spec = importlib.util.spec_from_file_location("_opencode_release", _RELEASE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_LATEST_API = "https://api.github.com/repos/anomalyco/opencode/releases/latest"
_RELEASE_PAGE = "https://github.com/anomalyco/opencode/releases/tag/v{version}"


def latest_version() -> str:
    req = urllib.request.Request(
        _LATEST_API, headers={"Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        tag = json.load(resp)["tag_name"]
    return tag.lstrip("v")


def sha256_of(url: str) -> str:
    """Hash *url* as it streams, reporting progress on one line."""
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=300) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(1 << 20):
            digest.update(chunk)
            done += len(chunk)
            print(
                f"\r  {done / 1e6:.0f} MB" + (f" of {total / 1e6:.0f}" if total else ""),
                end="",
                flush=True,
            )
    print()
    if total and done != total:
        raise SystemExit(
            f"{url} ended after {done} bytes of the {total} declared"
        )
    return digest.hexdigest()


def rewrite(version: str, checksums: dict[str, str]) -> None:
    source = _RELEASE_PY.read_text(encoding="utf-8")

    source, count = re.subn(
        r'^OPENCODE_VERSION = ".*"$',
        f'OPENCODE_VERSION = "{version}"',
        source,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise SystemExit(f"No OPENCODE_VERSION assignment in {_RELEASE_PY}")

    table = "\n".join(
        f'    "{slug}": "{checksums[slug]}",' for slug in checksums
    )
    source, count = re.subn(
        r"^OPENCODE_CHECKSUMS: dict\[str, str\] = \{\n.*?^\}$",
        f"OPENCODE_CHECKSUMS: dict[str, str] = {{\n{table}\n}}",
        source,
        count=1,
        flags=re.MULTILINE | re.DOTALL,
    )
    if count != 1:
        raise SystemExit(f"No OPENCODE_CHECKSUMS table in {_RELEASE_PY}")

    _RELEASE_PY.write_text(source, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "version",
        nargs="?",
        help="the release to pin, without the leading v (default: the latest)",
    )
    args = parser.parse_args()

    release = _release_module()
    version = (args.version or latest_version()).lstrip("v")
    print(f"Pinning opencode {version}")
    print(f"Changelog: {_RELEASE_PAGE.format(version=version)}")

    checksums: dict[str, str] = {}
    for slug in release.OPENCODE_CHECKSUMS:
        print(f"{release.opencode_asset_name(slug)}:")
        checksums[slug] = sha256_of(release.opencode_download_url(slug, version))

    rewrite(version, checksums)
    print(f"Rewrote {_RELEASE_PY.relative_to(_ROOT)}")


if __name__ == "__main__":
    main()
