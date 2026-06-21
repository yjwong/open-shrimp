"""Capability-driven command registration in ``bot.py:build_application``.

The registration loop installs the opt-in handlers (``/login``,
``/usage``, ``/mcp``) only when at least one configured backend
declares the corresponding capability.  Stub backends here implement
only ``command_capabilities`` and ``copy`` — the rest of the
``Backend`` protocol is unused by the registration path.
"""

from __future__ import annotations

import asyncio
from typing import Iterator

import aiosqlite
import pytest
from telegram.ext import CommandHandler

from open_shrimp.backend import BackendCopy
from open_shrimp.bot import build_application
from open_shrimp.config import (
    Config,
    ContextConfig,
    ReviewConfig,
    TelegramConfig,
)


class _StubBackend:
    def __init__(self, capabilities: set[str]) -> None:
        self._caps = capabilities

    def command_capabilities(self) -> set[str]:
        return set(self._caps)

    def copy(self) -> BackendCopy:
        return BackendCopy(
            login_command_description="stub login",
            login_mini_app_body="stub login",
        )


def _config() -> Config:
    return Config(
        telegram=TelegramConfig(token="0:fake"),
        allowed_users=[1],
        contexts={
            "default": ContextConfig(
                directory="/tmp",
                description="test",
                allowed_tools=[],
            ),
        },
        default_context="default",
        review=ReviewConfig(),
    )


def _registered_commands(app) -> set[str]:
    """Extract the set of ``/<name>`` commands registered on the app."""
    names: set[str] = set()
    for group in app.handlers.values():
        for h in group:
            if isinstance(h, CommandHandler):
                names |= set(h.commands)
    return names


@pytest.fixture
def _db() -> Iterator[aiosqlite.Connection]:
    loop = asyncio.new_event_loop()
    try:
        db = loop.run_until_complete(aiosqlite.connect(":memory:"))
        try:
            yield db
        finally:
            loop.run_until_complete(db.close())
    finally:
        loop.close()


def test_no_opt_in_capabilities_omits_handlers(_db) -> None:
    """Zero opt-in capabilities: no /login, /usage, or /mcp handlers."""
    app = build_application(_config(), _db, backends=[_StubBackend(set())])
    commands = _registered_commands(app)
    assert "login" not in commands
    assert "usage" not in commands
    assert "mcp" not in commands
    # Always-on commands stay registered.
    assert "context" in commands
    assert "clear" in commands


def test_full_capabilities_registers_all_handlers(_db) -> None:
    """All three opt-in capabilities: /login, /usage, /mcp all registered."""
    app = build_application(
        _config(),
        _db,
        backends=[_StubBackend({"login", "usage", "mcp"})],
    )
    commands = _registered_commands(app)
    assert "login" in commands
    assert "usage" in commands
    assert "mcp" in commands


def test_capabilities_union_across_backends(_db) -> None:
    """The registration loop unions capabilities across configured backends."""
    backends = [
        _StubBackend({"login"}),
        _StubBackend({"mcp"}),
    ]
    app = build_application(_config(), _db, backends=backends)
    commands = _registered_commands(app)
    assert "login" in commands
    assert "mcp" in commands
    assert "usage" not in commands


def test_backends_none_registers_every_opt_in_handler(_db) -> None:
    """``backends=None`` registers every opt-in handler — the seam stays
    additive for callers that haven't been wired through ``run_bot``."""
    app = build_application(_config(), _db, backends=None)
    commands = _registered_commands(app)
    assert "login" in commands
    assert "usage" in commands
    assert "mcp" in commands
