"""Fetching the guest artifacts an HCS sandbox boots from.

The control initramfs and the rootfs templates are Linux build products —
``gcc -static`` plus ``cpio`` for one, ``debootstrap`` with loop mounts and a
chroot for the others — that a Windows host consumes but cannot produce.  CI
builds them and publishes them as release assets; this module is the host side
of that arrangement, so installing OpenShrimp is enough to get a working
sandbox, and standing up a root shell in WSL is only for an operator who wants
to build their own.

Artifacts are cached per-user rather than per-context: each is a template that
every context seeds its own copy from, so one download serves all of them.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from platformdirs import user_data_path

from open_shrimp.sandbox.prefetch import (
    ProgressFn,
    exclusive,
    file_sha256,
    staging_path,
    stream_to_file,
    sweep_staging,
)

logger = logging.getLogger(__name__)

_REPO = "yjwong/open-shrimp"

#: The rootfs assets run to about a gigabyte, and both passes over one —
#: download and unpack — read it whole.  The checksum's read size is
#: ``prefetch``'s.
_CHUNK = 8 * 1024 * 1024

#: Room for a slow link on the largest asset the project ships.
_TIMEOUT = 300


def release_asset_url(asset: str) -> str:
    """The latest release's download URL for *asset*."""
    return f"https://github.com/{_REPO}/releases/latest/download/{asset}"


def asset_dir() -> Path:
    """Directory the downloaded guest artifacts are cached in."""
    return user_data_path("openshrimp") / "hcs"


def download_release_asset(
    asset: str, dest: Path, *, progress: ProgressFn | None = None,
) -> None:
    """Download *asset* from the latest release to *dest*.

    The body lands through a staging file and one ``os.replace``, so *dest*
    either does not exist or is complete: a truncated artifact that looks
    staged would be adopted on the next boot and fail as a corrupt guest
    rather than as a failed download.  One fetcher at a time holds *dest*, so
    two processes reaching this path write one body after the other rather
    than interleaving two into one file.

    An existing *dest* is overwritten.  Whether the copy already there is good
    enough to keep is the caller's to decide, since only it knows what the
    asset is for.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = release_asset_url(asset)
    logger.info("Downloading %s ...", url)
    with exclusive(dest):
        sweep_staging(dest)
        tmp = staging_path(dest)
        try:
            _fetch(url, tmp, progress=progress)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)


def ensure_asset(
    asset: str,
    dest: Path,
    *,
    description: str,
    log: Callable[[str], None] | None = None,
    progress: ProgressFn | None = None,
) -> Path:
    """*dest*, downloading the released *asset* into it if it is not there.

    A ``.zst`` asset is unpacked on the way in, so *dest* always names the
    artifact the sandbox boots rather than the form it travelled in.  The
    checksum published beside the asset is verified before anything is put in
    place, and the unpack lands through its own ``os.replace`` for the same
    reason the download does.

    *progress* counts the bytes of the asset as published — the compressed
    form for a ``.zst``, since that is what crosses the network.  The
    checksum and the unpack that follow report through *log* instead: they
    are the tail of the wait, not part of the transfer.

    The cache is shared by every context and by every process on the host, so
    the presence check is repeated under :func:`exclusive`.  A caller that
    lost the race blocks, wakes once the winner has landed the file, and
    returns it without fetching a byte.
    """
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with exclusive(
        dest,
        on_wait=lambda: _say(
            log,
            "Another download of this asset is already running — waiting "
            "for it.",
        ),
    ) as waited:
        if dest.is_file():
            if waited:
                _say(log, f"The other download finished; {description} is ready.")
            _say(log, f"{description} ready at {dest}.")
            return dest
        _say(log, f"Downloading {description} ({asset}) — this happens once...")
        url = release_asset_url(asset)
        sweep_staging(dest)
        download = staging_path(dest)
        try:
            _fetch(url, download, progress=progress)
            _verify_sha256(download, asset)
            if asset.endswith(".zst"):
                _say(log, f"Unpacking {description}...")
                unpacked = dest.with_name(dest.name + ".unpack")
                try:
                    _decompress_zstd(download, unpacked)
                    os.replace(unpacked, dest)
                finally:
                    unpacked.unlink(missing_ok=True)
            else:
                os.replace(download, dest)
        finally:
            download.unlink(missing_ok=True)
    _say(log, f"{description} ready at {dest}.")
    return dest


def _say(log: Callable[[str], None] | None, message: str) -> None:
    """Report progress through the caller's sink, or the log when it has none.
    A caller that supplies one is already logging through it."""
    if log is not None:
        log(message)
    else:
        logger.info("%s", message)


def _fetch(url: str, dest: Path, *, progress: ProgressFn | None = None) -> None:
    stream_to_file(
        url, dest,
        progress=progress,
        chunk_size=_CHUNK,
        headers={"Accept": "application/octet-stream"},
        timeout=_TIMEOUT,
    )


def _verify_sha256(path: Path, asset: str) -> None:
    """Check *path* against the ``<asset>.sha256`` published beside it.

    A release that carries the asset but not its checksum is an error rather
    than an unverified install: the two are produced by the same job, so a
    missing checksum means the artifact is not the one this code expects.
    """
    checksum_url = release_asset_url(f"{asset}.sha256")
    try:
        with urllib.request.urlopen(checksum_url, timeout=_TIMEOUT) as resp:
            line = resp.read(4096).decode("ascii", "replace")
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError(
            f"Downloaded {asset} but could not fetch its checksum from "
            f"{checksum_url}: {exc}"
        ) from exc
    # `sha256sum` writes "<hex>  <filename>".
    fields = line.split()
    expected = fields[0] if fields else ""
    actual = file_sha256(path)
    if actual != expected:
        raise RuntimeError(
            f"Checksum mismatch on {asset}: expected {expected}, got {actual}. "
            "The download is corrupt or the release was replaced mid-fetch; "
            "retry, and if it persists report it."
        )


def _decompress_zstd(src: Path, dest: Path) -> None:
    try:
        import zstandard
    except ImportError as exc:
        raise RuntimeError(
            "Unpacking a released HCS guest image needs the 'zstandard' "
            "package, which the 'hcs' extra installs: "
            "uv sync --extra hcs (or pip install 'open-shrimp[hcs]')."
        ) from exc

    dctx = zstandard.ZstdDecompressor()
    try:
        with open(src, "rb") as fin, open(dest, "wb") as fout:
            dctx.copy_stream(fin, fout, read_size=_CHUNK, write_size=_CHUNK)
    except zstandard.ZstdError as exc:
        raise RuntimeError(
            f"{src.name} passed its checksum but is not a valid zstd archive: "
            f"{exc}. The published asset is malformed; report it."
        ) from exc
