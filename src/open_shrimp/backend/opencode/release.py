"""The opencode release this project installs, and how it is published.

The version is a constant rather than a probe of whichever opencode a machine
happens to carry.  Host and guest then agree by construction — a Windows host
running the HCS backend has no host opencode at all, and resolving from the
GitHub API instead meant two guests provisioned a day apart ran different
builds against one session database — and a bad upstream release cannot reach
a running instance.

The pinned tag makes each asset immutable; the checksum makes a retagged or
tampered one fail closed.  What this fetches runs with tool access inside the
user's project, which is why it is verified where cloudflared is not.

Six slugs, glibc only.  ``-musl`` would serve Alpine hosts, which nothing else
here supports, and ``-baseline`` would serve pre-AVX2 x86, which needs a
CPU-flags probe per platform for a 2013 cutoff.
"""

from __future__ import annotations

import platform
import shlex

#: The opencode release every host and every guest installs.
OPENCODE_VERSION = "1.18.23"

#: sha256 of each release asset, keyed by the platform slug in its name.
#: Regenerate with ``scripts/bump_opencode.py``; never edit by hand.
OPENCODE_CHECKSUMS: dict[str, str] = {
    "linux-x64": "ab7015cd8113e011a461f30a0c2b77d8299a144ff688cb62e93e8802835d7288",
    "linux-arm64": "86d3afaf4e8784f9adab189be2a315c12b27ec40a04b70defbe70595c3cc7c65",
    "darwin-x64": "6b617da75b5773836fcdc7247d7ea2bd39aec942a58b89a041bafb3d4d2a8c23",
    "darwin-arm64": "373cf36673836f2ce8847295a0bb2cd2447d03c769b44d84185916bd471b4274",
    "windows-x64": "a2fe9e8c2d074d26975024d494927b966680b3efdc3e0377eadb9afb05f7e191",
    "windows-arm64": "3ff8c553ae270e89499808fbce7635535762f75cfaae4b0bb818b10d7eb18d9b",
}

_DOWNLOAD_BASE = "https://github.com/anomalyco/opencode/releases/download"

#: Where to send someone whose platform this cannot fetch for.
INSTALL_DOCS = "https://opencode.ai/docs/"

#: ``(system, machine)`` → slug, where the pair is what ``platform.system()``
#: and ``platform.machine()`` answer, and ``machine`` is ``uname -m`` on the
#: two Unixes.  Only spellings that occur are listed: Linux says ``x86_64``
#: and ``aarch64``, macOS says ``x86_64`` and ``arm64``, Windows says
#: ``AMD64`` and ``ARM64``.
_SLUGS = {
    ("Linux", "x86_64"): "linux-x64",
    ("Linux", "aarch64"): "linux-arm64",
    ("Darwin", "x86_64"): "darwin-x64",
    ("Darwin", "arm64"): "darwin-arm64",
    ("Windows", "AMD64"): "windows-x64",
    ("Windows", "ARM64"): "windows-arm64",
}


def host_slug() -> str | None:
    """The slug for this host, or ``None`` where opencode publishes no build."""
    return _SLUGS.get((platform.system(), platform.machine()))


def linux_guest_slug(machine: str) -> str | None:
    """The slug for a Linux guest reporting *machine* from ``uname -m``.

    Guests are always Linux, so this is the Linux rows of the same table
    rather than a second one that could disagree with it.
    """
    return _SLUGS.get(("Linux", machine.strip()))


def no_build_reason() -> str | None:
    """Why this host cannot have opencode, or ``None`` when it can.

    The single place the refusal is worded: startup validation, the doctor and
    the fetch all report the same unrecoverable state, and three spellings of
    it would read as three different problems.
    """
    if host_slug() is not None:
        return None
    return (
        f"no opencode build is published for {platform.system()} "
        f"{platform.machine()}"
    )


def opencode_asset_name(slug: str) -> str:
    """The release asset carrying the CLI for *slug*."""
    ext = "tar.gz" if slug.startswith("linux-") else "zip"
    return f"opencode-{slug}.{ext}"


def opencode_download_url(slug: str, version: str | None = None) -> str:
    """URL of the release asset for *slug*.

    ``None`` means the pin, read now rather than bound as a default argument —
    a default would freeze the value this module was imported with, which is
    exactly what a test repointing the pin needs to change.
    ``scripts/bump_opencode.py`` passes a version explicitly, to hash the
    release it is about to pin, and is the only caller that may.
    """
    return (
        f"{_DOWNLOAD_BASE}/v{version or OPENCODE_VERSION}/"
        f"{opencode_asset_name(slug)}"
    )


def opencode_checksum(slug: str) -> str:
    """The sha256 the asset for *slug* must hash to.

    A slug with no entry is a bug in the table rather than a user's problem,
    so it raises here instead of downgrading to an unverified download.
    """
    digest = OPENCODE_CHECKSUMS.get(slug)
    if not digest:
        raise RuntimeError(
            f"No sha256 recorded for opencode {OPENCODE_VERSION} {slug}; "
            f"regenerate the table with scripts/bump_opencode.py."
        )
    return digest


def version_token(line: str) -> str | None:
    """The version out of a ``--version`` line, or ``None`` when there is none.

    ``opencode --version`` prints the bare number, but a guest image may carry
    a build that pads it with a name or a parenthetical; the number is always
    the first token, sometimes with a ``v`` on it.
    """
    parts = line.strip().split()
    return parts[0].lstrip("v") if parts else None


def guest_install_script(slug: str, *, sudo: bool) -> str:
    """A shell command installing the pinned CLI into a Linux guest's PATH.

    One recipe for both in-guest installers.  They differ only in whether the
    shell is already root — the HCS chroot is, a Lima guest is not — so a
    second copy would exist to carry a three-character difference and would
    drift on the part that matters: ``sha256sum`` runs before ``tar``, so a
    tampered archive is never unpacked.

    Either downloader will do; the rootfs images carry one or the other, not
    reliably both, and both families carry ``sha256sum``.
    """
    url = shlex.quote(opencode_download_url(slug))
    check = shlex.quote(f"{opencode_checksum(slug)}  /tmp/opencode.tar.gz")
    run = "sudo " if sudo else ""
    return (
        "set -e; "
        f"if command -v curl >/dev/null 2>&1; then curl -fsSL {url} -o /tmp/opencode.tar.gz; "
        f"else wget -q -O /tmp/opencode.tar.gz {url}; fi; "
        f"echo {check} | sha256sum -c -; "
        "tar -xzf /tmp/opencode.tar.gz -C /tmp; "
        f"{run}install -m 755 /tmp/opencode /usr/local/bin/opencode; "
        "rm -f /tmp/opencode.tar.gz /tmp/opencode"
    )
