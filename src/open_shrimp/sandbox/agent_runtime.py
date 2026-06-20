"""Agent-runtime profiles: *what* agent a sandbox launches, decoupled from
*where* it runs.

A :class:`Sandbox` (``base.py``) owns the *where* — Docker, libvirt, Lima.
An :class:`AgentRuntime` owns the *what* — the agent and how it is launched.
The two meet at :meth:`Sandbox.start_agent`, which takes an ``AgentRuntime``
and dispatches on its :attr:`AgentRuntime.launch` strategy.

This module is intentionally import-light: it must not pull in any agent SDK or
backend at module load.  It describes data and hooks plus a small amount of
launch plumbing shared by every sandbox.  Per-agent constants, bundle
constructors, and runtime factories live under
``open_shrimp.backend.<agent>/``; the sandbox layer never names an agent.

Two launch strategies exist.  :class:`WrappedCLI` — the runtime tells the
sandbox "launch me by generating a wrapped-CLI script; you know how", and the
sandbox runs its existing per-backend ``build_cli_wrapper`` body.
:class:`ServedEndpoint` — the runtime tells the sandbox to run a serve argv in
the guest and reach its port; the shared :func:`run_served_endpoint` helper
below owns that body so every backend supplies only its exec mechanism.
"""

from __future__ import annotations

import base64
import logging
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

if TYPE_CHECKING:
    import subprocess

logger = logging.getLogger(__name__)


def _no_watch_host_credentials(stop: threading.Event) -> None:
    """Default ``watch_host_credentials`` body — no host-side watcher.

    A runtime keeps this default when its host-side credential storage is
    re-read per request (so a per-dispatch re-inject is sufficient) or when it
    has no host-side credential storage at all.
    """


def _no_host_credentials_available() -> bool:
    """Default ``host_credentials_available`` body — no host-side probe."""
    return False


def _no_write_cred_target(home_dir: Path, payload: str) -> None:
    """Default ``write_cred_target`` body — drop the payload.

    A runtime that doesn't watch host credentials never reaches this writer
    (the watcher is what calls it), so the default is a quiet no-op rather
    than an error.
    """


@dataclass(frozen=True)
class WrappedCLI:
    """Launch strategy: the sandbox generates a wrapper script and returns its
    ``cli_path``.

    This is a **marker** — it carries no argv.  Each backend generates its own
    wrapper (``docker exec``, ``ssh``, ``limactl shell``) that execs the agent
    CLI via a backend-specific ``build_cli_wrapper`` helper; that generation is
    correctly a ``(sandbox × agent)`` cell and stays per-sandbox.  The
    argv-passing form only becomes meaningful if a second wrapped-CLI agent
    appears (PTY+JSONL), which can extend this dataclass then.
    """

    kind: Literal["wrapped_cli"] = "wrapped_cli"


@dataclass(frozen=True)
class GuestMount:
    """A host directory synced into the guest at ``guest_mount_point``.

    Used by :class:`ServedEndpoint` to declare per-launch extra mounts (the
    served-endpoint runtime's data home + plugin-config dir) without baking
    the agent's name or layout into the sandbox.
    """

    host_dir: Path
    guest_mount_point: str
    writable: bool = True


@dataclass(frozen=True)
class ServedEndpoint:
    """Launch strategy: the sandbox runs a serve argv, calls :meth:`reach`,
    and returns an endpoint handle.

    The runtime supplies the serve ``argv`` plus the in-guest ``guest_port``;
    the sandbox owns *reach* (a published-port lookup for Docker, an
    ``ssh -L`` tunnel for libvirt/lima).  The served process is whatever
    ``serve_argv`` names; the sandbox wraps the ``reach(guest_port)`` result as
    the endpoint's ``base_url``.

    ``home_mounts`` declares any extra host dirs the launch needs synced into
    the guest (the runtime's data home, plugin-config dir, …); each sandbox
    consumes them where its mount mechanism lives.  ``auth_username`` is the
    Basic-auth username half of the served credential and
    ``password_env_var`` is the env var the serve process reads it from.
    ``make_endpoint`` constructs the concrete endpoint handle from
    ``(base_url, auth_header, owner)``; ``wait_ready`` blocks until the
    server is reachable; ``drain_output`` reads server stdout in the
    background.  All three live in the runtime's backend module so the
    sandbox layer never imports a backend type.
    """

    serve_argv: list[str]
    guest_port: int
    home_mounts: tuple[GuestMount, ...]
    auth_username: str
    password_env_var: str
    make_endpoint: Callable[[str, str, object], Any]
    wait_ready: Callable[["subprocess.Popen[str]"], None]
    drain_output: Callable[["subprocess.Popen[str]"], None]
    kind: Literal["served_endpoint"] = "served_endpoint"


# A launch strategy.  WrappedCLI generates a wrapper script; ServedEndpoint
# serves a port and reaches it.
LaunchStrategy = WrappedCLI | ServedEndpoint


@dataclass(frozen=True)
class ImageBundle:
    """The Docker image bundle a runtime needs, carried as data.

    Every field describes the *image* (build inputs + guest layout), not the
    agent's name — so the Docker sandbox can dispatch without any
    ``if flavour == "..."`` branches, and so adding a third image bundle
    touches one constructor in the runtime module.

    Docker uses every field; the VM backends consult ``tag_suffix`` only as an
    opaque key (their guest binary is the operator's precondition unless
    ``guest_installer`` is set).

    ``tag_suffix`` is the per-bundle slug appended after the instance prefix to
    form the image tag (e.g. ``openshrimp-<instance>-<suffix>:latest``).
    ``bundled_dockerfile`` is the bundled Dockerfile path (read from the
    package's resources).  ``binary_finder`` returns the host path to the CLI
    binary copied into the build context; ``context_binary_name`` is the name
    that binary is copied as inside the build dir.  ``build_arg`` is the
    Dockerfile build-arg ``(NAME, VALUE)`` pair.  ``guest_home`` is the
    container's ``HOME``.  ``dind_user`` is the username the DinD entrypoint's
    passwd rewrite registers — defaults to ``"claude"`` for the wrapped-CLI
    image; the served image overrides it to match its own ``HOME``.

    ``computer_use_image`` is the optional layered "computer-use" image tag
    built on top of the base bundle.  ``None`` → this runtime has no
    computer-use variant.

    ``computer_use_build_args`` is the extra build args injected into the
    layered computer-use build.  The sandbox helper forwards these as
    ``--build-arg`` flags, so agent-specific build knobs (e.g.
    ``INSTALL_CLAUDE_CODE``) live next to the bundle constructor rather than
    the sandbox layer.

    ``libvirt_install`` and ``lima_install`` are optional in-guest installers
    for the VM sandboxes.  When set, the matching sandbox's
    ``provision_workspace`` calls them; otherwise the guest binary is the
    operator's precondition.  Each signature is backend-shaped: libvirt
    passes SSH credentials, Lima passes the ``limactl`` instance handle.
    Bodies live in the per-backend module under
    ``open_shrimp.backend.<agent>/``.
    """

    tag_suffix: str
    bundled_dockerfile: str
    binary_finder: Callable[[], str]
    context_binary_name: str
    build_arg: tuple[str, str]
    guest_home: str
    dind_user: str = "claude"
    computer_use_image: str | None = None
    computer_use_build_args: tuple[tuple[str, str], ...] = ()
    libvirt_install: Callable[[Path, int, str], None] | None = None
    lima_install: Callable[[str, str, str], None] | None = None


@dataclass(frozen=True)
class HomeMount:
    """The agent-data home directory: host dir ⇄ guest dir, and whether session
    state (the ``/resume`` corpus) lives under it.

    For the wrapped-CLI flavour, ``guest_dir`` is the agent's home (e.g.
    ``/home/<user>/.claude``) and ``holds_session_state`` is ``True`` — the
    resumable session corpus lives under ``host_dir/projects``.
    """

    host_dir: Path
    guest_dir: str
    holds_session_state: bool


@dataclass
class AgentRuntime:
    """A data-and-hooks profile describing one agent.  Owns no control flow.

    ``inject`` writes creds+config into the home dir; ``env`` is merged into the
    guest env.  These are *declared* here so each agent can fill them
    differently; their live bodies stay where they execute today (the credential
    copy-in inside each backend and the API-key forwarding inside each wrapper).
    This object names the hooks; it does not re-route the implementations.

    Three optional hooks describe the runtime's mid-session credential-refresh
    model:

    * ``re_inject_on_dispatch`` — when ``True``, the dispatcher re-runs the
      runtime's ``inject`` against the active sandbox's home dir before each
      dispatch.  Cheap when no host-side refresh has happened; the inject body
      is a read + filter + write of a small JSON file.
    * ``watch_host_credentials`` — a long-lived host-side watcher body that
      keeps every registered sandbox home in sync with host-side token
      refreshes.  The runtime-agnostic registration table in
      :mod:`open_shrimp.sandbox.agent_runtime_watcher` starts this on the
      first sandbox registration and stops it after the last unregistration.
    * ``host_credentials_available`` — a non-blocking probe used by the
      registration plumbing to decide whether watching is meaningful at all.
    * ``write_cred_target`` — given a host-side credentials payload, write
      the runtime-specific on-disk shape into a registered sandbox home.
      Paired with ``watch_host_credentials`` (the watcher is what calls it);
      ignored when the runtime doesn't watch.

    The two shapes are not mutually exclusive but the typical runtime needs
    exactly one: a runtime whose host-side store is re-read per request sets
    ``re_inject_on_dispatch=True`` and leaves the watcher hook at default; a
    runtime whose host-side store is *not* re-read per session populates the
    watcher hook and leaves ``re_inject_on_dispatch=False``.

    ``provision_credentials`` is an optional host→guest credential-provisioning
    hook called from VM sandboxes' ``provision_workspace`` after the
    ``guest_installer``.  Set when the runtime has a host-side credential file
    that VM sandboxes need to copy into the virtiofs-shared agent home (e.g.
    Claude's ``~/.claude/.credentials.json``).  ``None`` → no host-side
    credentials to copy (OpenCode's ``auth.json`` is covered by
    ``runtime.inject`` via the served-endpoint home mount).
    """

    name: str
    home_mount: HomeMount
    inject: Callable[[Path], None]
    env: dict[str, str]
    launch: LaunchStrategy
    # The container image this runtime needs, carried as data.  ``None`` →
    # sandbox default (VM backends ignore it; their guest image is the
    # operator's precondition).
    image_bundle: "ImageBundle | None" = None

    # Mid-session credential-refresh hooks.  Defaults match a runtime that
    # needs neither shape; concrete runtimes opt into the one that matches
    # their host-side refresh model.  See the class docstring.
    re_inject_on_dispatch: bool = False
    watch_host_credentials: Callable[[threading.Event], None] = field(
        default=_no_watch_host_credentials,
    )
    host_credentials_available: Callable[[], bool] = field(
        default=_no_host_credentials_available,
    )
    write_cred_target: Callable[[Path, str], None] = field(
        default=_no_write_cred_target,
    )

    provision_credentials: Callable[[Path], None] | None = None


@dataclass(frozen=True)
class AgentHandle:
    """What :meth:`Sandbox.start_agent` returns.

    The :class:`WrappedCLI` flavour fills ``cli_path`` (and ``cleanup_paths``
    for the temp files to delete on session end); the :class:`ServedEndpoint`
    flavour fills ``endpoint`` (the host-reachable served endpoint, whose
    concrete type lives in the runtime's backend module) instead.
    """

    cli_path: str | None = None
    cleanup_paths: list[str] = field(default_factory=list)
    endpoint: Any = None


def run_served_endpoint(
    runtime: AgentRuntime,
    launch: ServedEndpoint,
    *,
    spawn: Callable[[list[str], dict[str, str]], "subprocess.Popen[str]"],
    reach: Callable[[int], str],
    owner: object,
    log_label: str,
) -> tuple["subprocess.Popen[str]", Any]:
    """Run the agent server in the guest and return ``(proc, endpoint)``.

    Shared body for every sandbox's served-endpoint launch.  The only
    per-backend variation is how the serve argv is spawned in the guest, so the
    caller passes:

    * ``spawn(serve_argv, env)`` — start the serve process inside the guest with
      the given environment, returning its host-side :class:`subprocess.Popen`.
    * ``reach(guest_port)`` — map the in-guest port to a host-reachable
      ``"host:port"`` address (the sandbox's own ``reach``).
    * ``owner`` — the sandbox instance; stored on the endpoint so the served
      client can check liveness via the owner's served-process attribute.
    * ``log_label`` — a human label for the "server up" log line.

    This helper applies the runtime's inject hook, mints the Basic-auth
    credential, assembles the guest env, spawns and waits for readiness via
    the launch's ``wait_ready`` hook, constructs the endpoint via the
    launch's ``make_endpoint`` factory, and starts a daemon drain thread via
    the launch's ``drain_output`` hook.  The caller stores
    ``proc``/``endpoint`` on the sandbox instance (so the liveness contract
    that reads ``owner``'s served-process attribute keeps working).
    """
    from open_shrimp.sandbox.skill_paths import SANDBOX_HOME

    # Apply the runtime's home/auth/plugin contributions before launch.
    runtime.inject(runtime.home_mount.host_dir)

    password = secrets.token_hex(32)
    token = base64.b64encode(
        f"{launch.auth_username}:{password}".encode()
    ).decode("ascii")

    env = {
        "HOME": SANDBOX_HOME,
        **runtime.env,
        launch.password_env_var: password,
    }

    proc = spawn(list(launch.serve_argv), env)
    try:
        launch.wait_ready(proc)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
        raise

    host_addr = reach(launch.guest_port)
    endpoint = launch.make_endpoint(
        f"http://{host_addr}", f"Basic {token}", owner,
    )

    threading.Thread(
        target=launch.drain_output,
        args=(proc,),
        daemon=True,
    ).start()
    base_url = getattr(endpoint, "base_url", host_addr)
    logger.info("%s: agent server up at %s", log_label, base_url)
    return proc, endpoint


def terminate_served_proc(proc: "subprocess.Popen[str] | None") -> None:
    """Tear down a served process: terminate, wait briefly, then kill.

    Shared teardown for every sandbox's ``stop()``.  Tolerant of ``None`` and of
    an already-exited process, and never raises.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
