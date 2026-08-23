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
the sandbox boots from.  That is what makes this safe to abandon: a front end
may stop waiting whenever it likes, and the process that picks the asset up
next serialises against whatever is still running through :func:`exclusive`.
"""

from __future__ import annotations

import errno
import http.client
import logging
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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

#: How often a transfer writes a line to a build log.  Slower than the wire
#: by three orders of magnitude, fast enough that a reader watching the log
#: sees it move.
LOG_INTERVAL = 3.0

#: Decimal units.  Nothing a setup UI or a chat message says is in GiB, and
#: two places quoting one download in different units read as two downloads.
_GB = 1_000_000_000
_MB = 1_000_000


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


def throttled(
    report: ProgressFn,
    *,
    clock: Callable[[], float] = time.monotonic,
    interval: float = _MIN_INTERVAL,
) -> ProgressFn:
    """Wrap *report* so it fires at most *interval* apart.

    The throttle belongs to the emitter, not to the fetchers: the byte
    counter stays exact and every chunk still advances it, only the reporting
    is coarse.  Putting it at the call site instead would mean each fetcher
    carrying its own timer and disagreeing about the rate.

    Emitters differ in what they can afford by orders of magnitude, hence the
    *interval*: a line appended to a build log costs a write, while an edit to
    a Telegram message is rate-limited by the API and starts failing long
    before a fast link would stop producing chunks.
    """
    last: float | None = None

    def emit(done: int, total: int | None) -> None:
        nonlocal last
        now = clock()
        if last is not None and now - last < interval:
            return
        last = now
        report(done, total)

    return emit


def describe_bytes(done: int, total: int | None) -> str:
    """How far a transfer has got, phrased for someone waiting on it.

    A server that declined to declare a length leaves the percentage out
    altogether: one pinned at 0% for ten minutes is a worse lie than a count
    of bytes with nothing to measure it against.  Both figures take the unit
    the larger one calls for, so a reader is never asked to compare
    megabytes against gigabytes inside one parenthesis.
    """
    if total is None or total <= 0:
        return f"{_amount(done, done)} downloaded"
    percent = min(100, done * 100 // total)
    return (
        f"{percent}% ({_amount(done, total, unit=False)} of "
        f"{_amount(total, total)})"
    )


def _amount(size: int, scale: int, *, unit: bool = True) -> str:
    """*size* rendered in whichever unit *scale* is large enough to want."""
    if scale >= _GB:
        text = f"{size / _GB:.1f}"
        return f"{text} GB" if unit else text
    text = f"{size / _MB:.0f}"
    return f"{text} MB" if unit else text


def logged(
    label: str,
    log: Callable[[str], None],
    downstream: ProgressFn | None = None,
    *,
    interval: float = LOG_INTERVAL,
) -> ProgressFn:
    """A progress sink writing *label* and the transfer's state to *log*.

    The build log is where a first-turn download is legible at byte level, so
    the line is written whether or not a front end supplied *downstream*;
    without it, a multi-gigabyte transfer is two lines, one before and one
    after.  *downstream* rides along untouched, at whatever rate it throttles
    itself to — its reader is a chat message, not a log, and the two cannot
    share a rate.
    """
    to_log = throttled(
        lambda done, total: log(f"{label}: {describe_bytes(done, total)}"),
        interval=interval,
    )

    def report(done: int, total: int | None) -> None:
        to_log(done, total)
        if downstream is not None:
            downstream(done, total)

    return report


# ---------------------------------------------------------------------------
# One fetcher per cached asset
# ---------------------------------------------------------------------------


def staging_path(dest: Path) -> Path:
    """The temporary a fetch of *dest* writes into before renaming it.

    Named for the process rather than for the destination, so that two
    fetchers reaching one asset on a filesystem where :func:`exclusive`
    cannot be honoured waste a download each instead of interleaving their
    bytes into a single file that passes every length check both of them
    make.
    """
    return dest.with_name(f"{dest.name}.{os.getpid()}.download")


def sweep_staging(dest: Path) -> None:
    """Delete staging files left beside *dest* by fetchers that died.

    A per-pid temporary is not self-cleaning: the next run writes a different
    path instead of truncating the one an earlier run abandoned.
    Call this from inside :func:`exclusive`, where holding the lock is what
    makes every sibling stale — outside it, a sibling may be a live download.
    """
    mine = staging_path(dest).name
    prefix, suffix = f"{dest.name}.", ".download"
    try:
        siblings = list(dest.parent.iterdir())
    except OSError:
        logger.debug("Could not list %s", dest.parent, exc_info=True)
        return
    for stale in siblings:
        name = stale.name
        if name == mine or not name.startswith(prefix) or not name.endswith(suffix):
            continue
        # The middle segment must be a pid and nothing else.  Without that,
        # the staging file of an asset whose name merely starts with this
        # one's — ``rootfs.vhdx`` against ``rootfs.vhdx.gui`` — matches, and
        # that asset is guarded by a different lock, so it may be live.
        if not name[len(prefix):-len(suffix)].isdigit():
            continue
        try:
            stale.unlink()
        except OSError:
            logger.debug("Could not remove %s", stale, exc_info=True)


@contextmanager
def exclusive(
    dest: Path, *, on_wait: Callable[[], None] | None = None,
) -> Iterator[bool]:
    """Serialise every fetcher of *dest*, yielding whether the wait was real.

    Cached sandbox assets are keyed per user and shared by every context, so
    the fetchers that collide on one are not in one program: a front end's
    prefetch, orphaned when its window closed, and the core downloading on a
    first turn reach the same path with nothing between them.  No in-process
    lock can see that pair, which is why this is a lock on a file.

    A caller must re-check for *dest* inside the block: the loser blocks,
    wakes once the winner has landed the file, sees it there, and downloads
    nothing.

    The lock file is created once and never removed.  Unlinking it would let
    two processes hold locks on two different inodes reached by one name,
    which is the race it exists to close.

    *on_wait* fires before the blocking acquire and only when the
    non-blocking one failed, so a caller can say it is waiting on somebody
    else rather than appear to have hung.  The yielded bool reports the same
    fact afterwards, for a caller deciding what to say about who fetched.
    """
    lock_path = dest.with_name(dest.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        waited = False
        try:
            waited = not _acquire(fd, blocking=False)
            if waited:
                if on_wait is not None:
                    on_wait()
                _acquire(fd, blocking=True)
        except Unlockable:
            logger.warning(
                "Cannot lock %s, so a concurrent fetch of this asset is not "
                "excluded; the per-process staging name is what keeps the two "
                "downloads from writing one file.", lock_path, exc_info=True,
            )
            locked = False
        else:
            locked = True
        try:
            yield waited
        finally:
            if locked:
                _release(fd)
    finally:
        os.close(fd)


class Unlockable(OSError):
    """The filesystem under a lock file refuses to lock it at all.

    Distinct from contention, which is the answer this whole mechanism wants.
    A share that does not implement locks would otherwise turn a download
    that used to work into a failure, and :func:`staging_path` already keeps
    two unsynchronised fetchers out of each other's files.
    """


#: Errnos meaning "somebody else holds it", as opposed to "this filesystem
#: does not do locks".  ``EACCES`` is what Windows answers a refused
#: ``LK_NBLCK`` with; POSIX answers ``EWOULDBLOCK``.
_CONTENDED = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})


if sys.platform == "win32":
    import msvcrt

    def _acquire(fd: int, *, blocking: bool) -> bool:
        """Take the byte-zero lock on *fd*, or report that it is held.

        ``msvcrt.locking`` locks a range from the file position, so every
        call seeks first: a shared position would have the second lock cover
        a different byte and grant immediately.
        """
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, mode, 1)
                return True
            except OSError as exc:
                if not blocking:
                    if exc.errno in _CONTENDED:
                        return False
                    raise Unlockable(exc.errno, str(exc)) from exc
                # ``LK_LOCK`` retries internally for about ten seconds and
                # then raises ``EDEADLOCK``.  A multi-gigabyte download
                # outlasts that many times over, so the give-up is retried.
                # Anything else is a lock that will never be granted, and
                # retrying it would spin.
                if exc.errno != errno.EDEADLOCK:
                    raise Unlockable(exc.errno, str(exc)) from exc

    def _release(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _acquire(fd: int, *, blocking: bool) -> bool:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except OSError as exc:
            if exc.errno in _CONTENDED and not blocking:
                return False
            raise Unlockable(exc.errno, str(exc)) from exc
        return True

    def _release(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


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
    from open_shrimp.sandbox import hcs_rdp

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
        SharedAsset(
            name=hcs_rdp.HELPER_ASSET,
            directory=hcs_rdp.shipped_helper_dir(),
            # The desktop above is what this drives, and the helper is
            # resolved on the first computer-use call rather than on boot, so
            # left out of this list it downloads in the middle of a turn —
            # where the wait can be neither shown nor abandoned.
            #
            # A 50 MB archive unpacking to 130 MB of FreeRDP DLLs, both on
            # disk at once.
            needs_bytes=256 * 1024 * 1024,
            present=hcs_rdp.helper_staged,
            fetch=lambda progress: hcs_rdp.download_shipped_helper(progress),
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
