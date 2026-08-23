"""The order in which a stopping core lets go of what it holds.

The control channel is released last, because a supervising UI treats the
endpoint going quiet as the core being down and reaps the process handle it
holds.  Released before the tunnel, a Windows tray Quit kills the core partway
through teardown and leaves ``cloudflared.exe`` running.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from open_shrimp.main import _release_resources


def _recorder() -> tuple[list[str], MagicMock, MagicMock, MagicMock]:
    order: list[str] = []

    def spy(name: str) -> MagicMock:
        holder = MagicMock()
        holder.shutdown = AsyncMock(side_effect=lambda: order.append(name))
        holder.close = AsyncMock(side_effect=lambda: order.append(name))
        return holder

    return order, spy("control"), spy("proxy"), spy("db")


@pytest.mark.asyncio
async def test_control_channel_is_released_last() -> None:
    order, control, proxy, db = _recorder()
    tunnel = MagicMock()
    tunnel.returncode = None
    tunnel.terminate = MagicMock(side_effect=lambda: order.append("tunnel"))
    tunnel.wait = AsyncMock(return_value=0)

    await _release_resources(control, proxy, tunnel, db)

    assert order == ["proxy", "tunnel", "db", "control"]


@pytest.mark.asyncio
async def test_a_failed_release_does_not_skip_the_rest() -> None:
    order, control, proxy, db = _recorder()
    proxy.shutdown = AsyncMock(side_effect=RuntimeError("proxy is wedged"))

    await _release_resources(control, proxy, None, db)

    # Still last, and still released — a wedged proxy must not leave a UI
    # waiting on an endpoint that never closes.
    assert order == ["db", "control"]


@pytest.mark.asyncio
async def test_nothing_held_is_nothing_released() -> None:
    order, _control, _proxy, db = _recorder()

    await _release_resources(None, None, None, db)

    assert order == ["db"]
