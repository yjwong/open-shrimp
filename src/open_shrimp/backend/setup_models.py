"""What a setup wizard draws: the model menu, and the credential step's copy.

The terminal wizard reads this directly; the Windows tray's and the macOS app's
read it through ``openshrimp models --json`` and ``auth status --json``.  One
source, because a Windows user offered a model the Mac's picker does not have
is a bug reported against the wrong component — and because the ``.app`` CI job
builds, signs and notarizes the bundle without ever running it, so a sentence
that exists only in Swift is one nothing checks.

Nothing here says "backend" to a user.  The backend is a fact about how a turn
runs, and nobody choosing a model is choosing it — so the menu names the lab,
which is what tells somebody whose account they are about to be asked to log
into, and :func:`open_shrimp.backend.factory.backend_for_model` derives the
rest.
"""

from __future__ import annotations

from dataclasses import dataclass

from open_shrimp.backend.claude_sdk.models import MODEL_CHOICES
from open_shrimp.backend.opencode.models import SETUP_MODEL_CHOICES, provider_label
from open_shrimp.backend.protocol import ModelChoice

_ANTHROPIC = "Anthropic"


@dataclass(frozen=True)
class ConnectCopy:
    """What a wizard's credential step says, for the credential it asks for.

    Not ``BackendCopy``: those fields are the ``/login`` command's own
    descriptions and are keyed by backend, while these are keyed by provider —
    "Connect OpenAI" and "Connect Google" are different sentences from one
    backend.

    Five fields because the step has five texts, and a GUI that had to invent
    one of them is a GUI whose wording drifts from the other two's.
    """

    title: str
    subtitle: str
    body: str
    waiting: str
    done: str


def setup_models() -> tuple[ModelChoice, ...]:
    """Every model a first config may be pinned to here, in menu order.

    Claude first because it is what most people run, then one model per other
    lab.  The entry that pins nothing is not here: it names whichever agent's
    default it defers to, which follows from the backend rather than from a
    model, so it travels with ``default_model_label`` instead.

    "Here" is load-bearing.  A platform opencode publishes no build for is
    offered none of its models, because ``_validate_raw`` refuses a config that
    names one and the wizard would otherwise write a first config the core will
    not start from.
    """
    from open_shrimp.backend.opencode.release import no_build_reason

    if no_build_reason() is not None:
        return MODEL_CHOICES
    return MODEL_CHOICES + SETUP_MODEL_CHOICES


def menu_provider(model: str) -> str:
    """The lab to name beside *model* in a picker.

    Read off the id where there is one to read — OpenCode addresses models as
    ``provider/model`` — and Anthropic otherwise, because a bare alias is one
    only the Claude catalog carries.
    """
    from open_shrimp.backend.factory import provider_id_for_model

    provider_id = provider_id_for_model(model)
    return _ANTHROPIC if provider_id is None else provider_label(provider_id)


def connect_copy(provider_id: str | None) -> ConnectCopy:
    """What the credential step says for *provider_id*, or for Claude at
    ``None``.

    "this computer" rather than "this PC" or "this Mac": one sentence for three
    wizards is the point, and the platform is the thing the reader is looking
    at.
    """
    if provider_id is None:
        return ConnectCopy(
            title="Sign in to Claude",
            subtitle=(
                "OpenShrimp answers your messages by running Claude Code on "
                "this computer, under your own Claude Code login."
            ),
            body=(
                "Signing in happens in a terminal window and then in your "
                "browser; you'll choose there between a Claude subscription "
                "and API billing. It's once per computer. Skip it only if "
                "you'd rather do it later — /login runs it from Telegram."
            ),
            waiting=(
                "When that window says Login successful, type /exit or just "
                "close it. This step notices on its own — you don't have to "
                "tell it."
            ),
            done="Signed in. Claude Code is ready on this computer.",
        )

    name = provider_label(provider_id)
    return ConnectCopy(
        title=f"Connect {name}",
        subtitle=(
            f"OpenShrimp answers your messages by running that model through "
            f"OpenCode on this computer, under your own {name} account."
        ),
        body=(
            f"Connecting happens in a terminal window: OpenCode asks for "
            f"whatever {name} accepts — an API key, or a sign-in in your "
            f"browser — and keeps it. OpenShrimp never sees it. Skip it only "
            f"if you'd rather run the login yourself later."
        ),
        waiting=(
            f"Answer OpenCode's prompt in that window. This step notices on "
            f"its own once {name} is connected — you don't have to tell it."
        ),
        done=f"Connected. OpenCode can reach {name} from this computer.",
    )


__all__ = ["ConnectCopy", "connect_copy", "menu_provider", "setup_models"]
