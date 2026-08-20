"""Fetching the shared assets a sandbox backend needs before it can boot.

A sandboxed context's first turn pays for a download the user never asked
for: a rootfs template, a cloud image, the backend's own binary.  This
module is the entry point that lets a front end pay it earlier, somewhere a
wait is legible, and report byte-level progress while it does.

Only the *shared* assets are fetched.  With *N* contexts there is one image
download but *N* guest creations, so pre-creating guests would be slower than
the problem this solves — nothing here creates or boots anything.

This is not a diagnosis, so it does not live in :mod:`open_shrimp.doctor`,
which only reports whether an asset is already present.  Nor does it live in
:mod:`open_shrimp.sandbox.manager`: a manager is the factory for a *running*
backend and needs a live host behind it (a Docker daemon, a libvirt
connection), while a prefetch must work on a host where none of that is up
yet — it is a download and a filesystem, nothing more.

Every fetch reached from here is idempotent and lands through a temporary
file, so an interrupted prefetch never leaves a partial artifact at the path
the sandbox boots from; the next attempt writes the same temporary again and
removes it, so nothing accumulates either.  That is what makes this safe to
abandon: a front end may stop waiting whenever it likes.
"""

from __future__ import annotations

import http.client
import logging
import os
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Room for a slow link on the largest artifact this project publishes.
_TIMEOUT = 300

#: Called per chunk with ``(bytes_done, total_bytes_or_None)``.  The total is
#: ``None`` whenever the server declined to say, which a consumer renders as
#: indeterminate — never as zero.
ProgressFn = Callable[[int, "int | None"], None]

#: Roughly four progress reports a second.  A 64 KB chunk on a fast link
#: fires the callback thousands of times a second, and a consumer parsing
#: every one of those spends longer reading than the transfer spends moving.
_MIN_INTERVAL = 0.25


def content_length(raw: str | None) -> int | None:
    """The ``Content-Length`` header as an int, or ``None`` when there is none.

    A header that is absent, empty, or not a number means *unknown*, and the
    three fetchers must agree that unknown is ``None`` rather than ``0``: a
    zero denominator renders as a bar that never moves, which is a worse lie
    than a bar that admits it does not know.
    """
    if not raw:
        return None
    try:
        size = int(raw.strip())
    except ValueError:
        return None
    return size if size >= 0 else None


def stream_to_file(
    url: str,
    dest: Path,
    *,
    progress: ProgressFn | None = None,
    chunk_size: int = 65536,
    headers: dict[str, str] | None = None,
    timeout: int = _TIMEOUT,
) -> None:
    """Fetch *url* into *dest*, counting the bytes as they land.

    A body that stops short of the length the server declared is an error
    rather than a shorter file.  ``http.client`` ends its read loop on a
    premature close instead of raising, so without this a dropped connection
    produces a truncated artifact that every later run adopts as complete —
    which fails as a corrupt image rather than as a failed download.

    Every failure is a :class:`RuntimeError` naming the URL, because the
    caller reporting it has no other way to say which download died.
    """
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, \
                open(dest, "wb") as f:
            total = content_length(resp.headers.get("Content-Length"))
            done = 0
            while chunk := resp.read(chunk_size):
                f.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc
    if total is not None and done != total:
        raise RuntimeError(
            f"Failed to download {url}: the connection ended after {done} "
            f"bytes of the {total} the server declared."
        )


def throttled(report: ProgressFn, *, clock: Callable[[], float] = time.monotonic) -> ProgressFn:
    """Wrap *report* so it fires at most :data:`_MIN_INTERVAL` apart.

    The throttle belongs to the emitter, not to the fetchers: the byte
    counter stays exact and every chunk still advances it, only the reporting
    is coarse.  Putting it at the call site instead would mean each fetcher
    carrying its own timer and disagreeing about the rate.
    """
    last: float | None = None

    def emit(done: int, total: int | None) -> None:
        nonlocal last
        now = clock()
        if last is not None and now - last < _MIN_INTERVAL:
            return
        last = now
        report(done, total)

    return emit


@dataclass(frozen=True)
class SharedAsset:
    """One download every context on a backend seeds its own copy from.

    *needs_bytes* is room for the transfer and for anything unpacked out of
    it, sized at roughly twice what the asset measures today: a floor with
    slack, not a measurement.  The exact figure would take a ``HEAD`` per
    asset and still could not cover the unpacked size, and a number pitched
    too high refuses hosts that would have managed — the point is only to
    fail before spending minutes filling a disk that was never going to hold
    the result.
    """

    name: str
    directory: Path
    needs_bytes: int
    present: Callable[[], bool]
    fetch: Callable[[ProgressFn], None]


def shared_assets(backend: str) -> list[SharedAsset]:
    """The shared assets *backend* downloads on its first use.

    Backend modules are imported here rather than at module scope so that
    asking about one platform's assets does not require another platform's
    imports to resolve.
    """
    if backend == "libvirt":
        return _libvirt_assets()
    if backend == "lima":
        return _lima_assets()
    if backend == "hcs":
        return _hcs_assets()
    if backend == "docker":
        # Docker's shared artifact is built, not fetched: ``docker build``
        # resolves its own base layers and reports its own progress, and the
        # build needs a running daemon this command deliberately does not.
        return []
    raise ValueError(f"Unknown sandbox backend: {backend!r}")


def _libvirt_assets() -> list[SharedAsset]:
    from open_shrimp.sandbox import libvirt_helpers as L

    return [
        SharedAsset(
            name="virtiofsd",
            directory=L.managed_virtiofsd_path().parent,
            needs_bytes=32 * 1024 * 1024,
            # The managed binary, not any binary: ``ensure_virtiofsd`` fetches
            # the patched build even where a system virtiofsd is on the host,
            # so a wider question here would report ready and leave the
            # download to the first turn anyway.
            present=lambda: L.find_managed_virtiofsd() is not None,
            fetch=lambda progress: L.ensure_virtiofsd(progress=progress),
        ),
        SharedAsset(
            name=L.DEFAULT_BASE_IMAGE_NAME,
            directory=L.base_image_path().parent,
            needs_bytes=1024 * 1024 * 1024,
            present=lambda: L.base_image_path().exists(),
            fetch=lambda progress: L.ensure_base_image(None, progress=progress),
        ),
    ]


def _lima_assets() -> list[SharedAsset]:
    from open_shrimp.sandbox import lima_helpers as L

    return [
        SharedAsset(
            name="limactl",
            directory=L.bin_dir(),
            needs_bytes=256 * 1024 * 1024,
            present=lambda: L.find_limactl() is not None,
            fetch=lambda progress: L.ensure_limactl_sync(progress=progress),
        ),
    ]


def _hcs_assets() -> list[SharedAsset]:
    from open_shrimp.sandbox import hcs
    from open_shrimp.sandbox import hcs_assets

    # A computer-use template, because a sandboxed context carries a desktop:
    # :func:`open_shrimp.config.build_context_dict` sets ``computer_use`` on
    # every sandbox block a setup UI writes.  Which asset that implies is the
    # backend's answer, not one restated here — fetching the wrong one spends
    # gigabytes on a file nothing reads and still leaves the first turn paying
    # for the image it does read, which is worse than not prefetching at all.
    rootfs_asset, rootfs, description = hcs.managed_rootfs_asset(computer_use=True)
    return [
        SharedAsset(
            name="initrd.img",
            directory=hcs.initrd_path().parent,
            needs_bytes=64 * 1024 * 1024,
            present=lambda: hcs.initrd_path().is_file(),
            fetch=lambda progress: hcs.ensure_initrd(progress=progress),
        ),
        SharedAsset(
            name=rootfs.name,
            directory=rootfs.parent,
            # Twice the base rootfs's allowance, which is what the desktop
            # image measures: it is built on an ext4 filesystem twice the size
            # and fills much of the difference with a desktop.  The compressed
            # download and the image unpacked out of it are both on disk at
            # once, so the room has to hold the pair.
            needs_bytes=6 * 1024 * 1024 * 1024,
            present=rootfs.is_file,
            fetch=lambda progress: hcs_assets.ensure_asset(
                rootfs_asset,
                rootfs,
                description=description,
                progress=progress,
            ),
        ),
    ]


def space_shortfall(assets: list[SharedAsset]) -> str | None:
    """Why *assets* will not fit, or ``None`` when they will.

    Checked before the first byte moves, because gigabytes onto a full disk
    fail minutes later and blame whichever write happened to be unlucky.
    Assets are grouped by the filesystem they land on, so two assets sharing
    one disk must fit on it together.
    """
    # Keyed by device, not by path: two assets under one data directory can
    # resolve to different existing ancestors once one of their destinations
    # has been created, and checking those separately would let a pair that
    # only fits one at a time both pass.
    wanted: dict[int, tuple[Path, int]] = {}
    for asset in assets:
        mount = _existing_ancestor(asset.directory)
        try:
            device = os.stat(mount).st_dev
        except OSError:
            logger.debug("Could not stat %s", mount, exc_info=True)
            continue
        seen, needed = wanted.get(device, (mount, 0))
        wanted[device] = (seen, needed + asset.needs_bytes)

    for mount, needed in wanted.values():
        try:
            free = shutil.disk_usage(mount).free
        except OSError:
            # A filesystem that will not answer is not a filesystem that is
            # full; the download gets its chance.
            logger.debug("Could not read free space on %s", mount, exc_info=True)
            continue
        if free < needed:
            return (
                f"Not enough free space to fetch into {mount}: about "
                f"{_gib(needed)} is needed and {_gib(free)} is free."
            )
    return None


def prefetch(backend: str, *, emit: Callable[[dict[str, object]], None]) -> None:
    """Download every shared asset *backend* needs, reporting through *emit*.

    Emits one ``{"asset", "done", "total"}`` event per progress tick, an
    ``{"asset", "state": "ready"}`` as each asset lands, and a final
    ``{"state": "finished"}``.  An asset already on disk reports only its
    ``ready``.  Failure raises — this reports progress, it does not decide
    what a caller does about a download that did not happen.
    """
    assets = shared_assets(backend)
    missing = [asset for asset in assets if not asset.present()]
    shortfall = space_shortfall(missing)
    if shortfall is not None:
        raise RuntimeError(shortfall)

    absent = {asset.name for asset in missing}
    for asset in assets:
        if asset.name in absent:
            asset.fetch(throttled(
                lambda done, total, name=asset.name: emit(
                    {"asset": name, "done": done}
                    if total is None
                    else {"asset": name, "done": done, "total": total}
                ),
            ))
        emit({"asset": asset.name, "state": "ready"})
    emit({"state": "finished"})


def _existing_ancestor(directory: Path) -> Path:
    """The nearest ancestor of *directory* that exists.

    The destination directory is created by the fetch itself, so the free
    space that matters is that of the filesystem it will be created on.
    """
    path = directory
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _gib(size: int) -> str:
    if size >= 1024 * 1024 * 1024:
        return f"{size / (1024 ** 3):.1f} GiB"
    return f"{size / (1024 ** 2):.0f} MiB"
