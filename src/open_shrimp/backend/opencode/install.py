"""Fetch the pinned opencode CLI onto the host, once, on first use.

The archives are 46–61 MB compressed, so nothing is vendored into the wheel,
the MSI or the ``.app``: a user who never selects the opencode backend never
pays for it.  The first one who does pays here.

Sync, because its two call sites disagree about where they are:
``provision_workspace`` already runs on a worker thread
(:func:`open_shrimp.sandbox.launch.start_sandboxed_agent` wraps the whole
provisioning chain in ``asyncio.to_thread``), while ``OpenCodeServer._spawn``
is on the event loop and wraps this in a ``to_thread`` of its own.

The download itself is :mod:`open_shrimp.sandbox.prefetch`'s rather than a
fourth implementation of the same loop: the cross-process lock, the refusal of
a body that stops short of its ``Content-Length``, and the chunked checksum are
all things this needs and all things that module already gets right.  That
package must never name an agent, and nothing here asks it to.
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

from open_shrimp.backend.opencode.binary import (
    managed_opencode,
    opencode_override,
    opencode_path,
    version_stamp_path,
)
from open_shrimp.backend.opencode.release import (
    INSTALL_DOCS,
    OPENCODE_VERSION,
    host_slug,
    no_build_reason,
    opencode_asset_name,
    opencode_checksum,
    opencode_download_url,
)
from open_shrimp.binaries import make_executable
from open_shrimp.sandbox.prefetch import (
    ProgressFn,
    exclusive,
    file_sha256,
    stream_to_file,
)

logger = logging.getLogger(__name__)


def opencode_ready() -> bool:
    """Whether the pinned binary is already there, so a fetch would download
    nothing.  A caller asks before deciding whether the wait is worth saying
    anything about."""
    return _cached_at_pin() is not None


def ensure_opencode_binary(*, progress: ProgressFn | None = None) -> str:
    """The path to the pinned opencode, downloading it when it is not there.

    A cached binary at a version other than the pin is a stale cache, not an
    install: bumping the pin has to converge without asking anyone to empty
    ``BIN_DIR`` by hand.

    A stale binary beats no binary.  When the re-download fails and a cached
    one exists, that one runs and the mismatch is logged — refusing would turn
    a GitHub outage into a total outage for a user whose opencode works, and
    the next turn retries the upgrade for free.  A *first* download that fails
    raises: there is nothing to fall back to.
    """
    target = opencode_path()
    cached = _cached_at_pin()
    if cached is not None:
        return cached

    slug = host_slug()
    if slug is None:
        raise RuntimeError(
            f"{no_build_reason()}. Install it yourself and point OPENCODE_BIN "
            f"at it: {INSTALL_DOCS}"
        )

    with exclusive(target):
        # The loser of the lock wakes to find the winner's download in place.
        cached = _cached_at_pin()
        if cached is not None:
            return cached

        stale = managed_opencode()
        try:
            _download(slug, target, progress=progress)
        except Exception:
            if stale is None:
                raise
            logger.warning(
                "Could not fetch opencode %s, so the copy already at %s stays "
                "in use (it reports %s); the next start retries.",
                OPENCODE_VERSION, stale, _stamped_version() or "no version",
                exc_info=True,
            )
            return stale
    return str(target)


def _cached_at_pin() -> str | None:
    """The binary to run when it is the one to run, else ``None``.

    An override is taken as-is: a caller who named a binary is not asking this
    module to manage one, so its version is theirs to worry about.
    """
    override = opencode_override()
    if override is not None:
        return override
    binary = managed_opencode()
    if binary is None or _stamped_version() != OPENCODE_VERSION:
        return None
    return binary


def _stamped_version() -> str | None:
    try:
        return version_stamp_path().read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _download(slug: str, target: Path, *, progress: ProgressFn | None) -> None:
    """Fetch, verify, unpack and land the asset for *slug* at *target*.

    Both the archive and the binary unpacked out of it are on disk at once, in
    a scratch directory beside the target — same filesystem, so the final
    ``os.replace`` is a rename rather than a copy, and self-cleaning, so an
    exception anywhere leaves nothing behind for a later run to adopt.
    """
    url = opencode_download_url(slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading opencode %s from %s ...", OPENCODE_VERSION, url)

    with tempfile.TemporaryDirectory(dir=target.parent) as scratch:
        archive = Path(scratch) / "archive"
        unpacked = Path(scratch) / target.name
        stream_to_file(url, archive, progress=progress)
        _verify(archive, opencode_checksum(slug), url)
        _extract(archive, unpacked, slug)
        make_executable(unpacked)
        # Not Path.rename: that refuses an existing destination on Windows, so
        # a re-download after a pin bump would fail.
        os.replace(unpacked, target)

    version_stamp_path().write_text(OPENCODE_VERSION, encoding="utf-8")
    logger.info("opencode %s installed at %s", OPENCODE_VERSION, target)


def _verify(archive: Path, expected: str, url: str) -> None:
    """Refuse an asset that does not hash to *expected*.

    Before extraction, not after: unpacking an unverified archive is the thing
    the checksum is here to prevent.
    """
    actual = file_sha256(archive)
    if actual != expected:
        raise RuntimeError(
            f"{url} does not match the sha256 recorded for opencode "
            f"{OPENCODE_VERSION}: expected {expected}, got {actual}."
        )


def _extract(archive: Path, dest: Path, slug: str) -> None:
    """Write the archive's single ``opencode`` member to *dest*.

    The member is read out by name and written to a path this module chose, so
    an archive whose member name walks out of the scratch directory cannot: a
    name that is not exactly the CLI's is refused rather than sanitised.
    """
    name = "opencode.exe" if slug.startswith("windows-") else "opencode"
    missing = RuntimeError(
        f"{opencode_asset_name(slug)} carries no {name} at its root"
    )
    if slug.startswith("linux-"):
        with tarfile.open(archive, "r|gz") as tar:
            # Streamed (``r|gz``) and stopped at the first match: ``getmember``
            # would inflate all 172 MB to build an index of the one entry that
            # is there, then inflate it again to read the member out.
            while (member := tar.next()) is not None:
                if member.name != name:
                    continue
                if not member.isfile():
                    raise missing
                source = tar.extractfile(member)
                assert source is not None
                _write(source, dest)
                return
        raise missing

    with zipfile.ZipFile(archive) as zf:
        if name not in zf.namelist():
            raise missing
        with zf.open(name) as source:
            _write(source, dest)


def _write(source, dest: Path) -> None:
    with open(dest, "wb") as out:
        shutil.copyfileobj(source, out, 1 << 20)
