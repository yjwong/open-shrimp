"""Local control channel between the agent core and a supervising UI process.

Exists because a UI written outside Python cannot call
:func:`open_shrimp.main.request_shutdown` in-process.  Without it the only way
to stop the core from another process on Windows is ``TerminateProcess``,
which skips shutdown entirely and strands any running sandbox guest.
"""

from open_shrimp.control.methods import CoreStatus, build_methods
from open_shrimp.control.server import ControlServer, endpoint_address

__all__ = [
    "ControlServer",
    "CoreStatus",
    "build_methods",
    "endpoint_address",
]
