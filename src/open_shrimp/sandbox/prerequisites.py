"""Registry of prerequisites declared by sandbox backends."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from open_shrimp.sandbox.hcs_prereq import DECLARATION as HCS
from open_shrimp.sandbox.libvirt_prereq import DECLARATION as LIBVIRT
from open_shrimp.sandbox.lima_prereq import DECLARATION as LIMA

Check = Callable[[Any], tuple[bool, str]]
Declaration = tuple[str, str, tuple[tuple[str, Check], ...]]

DECLARATIONS: tuple[Declaration, ...] = (LIBVIRT, LIMA, HCS)
