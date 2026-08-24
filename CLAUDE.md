# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

OpenShrimp — a Telegram bot for remote coding-agent access. Python 3.11+, fully async, managed with `uv`. Backends: Claude Agent SDK (default) or OpenCode. Telegram is the only UI; Mini Apps (review, terminal, VNC, config, preview) provide richer views.

## Commands

```bash
uv sync                                  # install deps (dev group included)
uv run openshrimp                        # run the bot (setup wizard if no config)
uv run pytest                            # run all tests
uv run pytest tests/test_stream.py       # one file
uv run pytest tests/test_stream.py::test_name -x   # one test
```

- Tests: `pytest` + `pytest-asyncio`; async tests are marked `@pytest.mark.asyncio` individually (no global asyncio_mode). Mock the Agent SDK and Telegram API.
- Web Mini Apps (`web/<app>/`, Node 18+): `npm install && npm run build` per app. `uv sync` needs each `web/<app>/dist` to exist — `mkdir -p web/<app>/dist` as a placeholder if you don't need them.
- Android companion (`android/companion/`): Gradle project, `./gradlew assembleDebug`.
- Version bump: edit the root `VERSION` file — `open-shrimp` and `moonshine-stt` both read it dynamically (hatchling).
- No linter/formatter is configured; match surrounding style.

## Architecture

```
Telegram <-> bot.py / handlers/ <-> client_manager.py <-> backend/ (claude_sdk | opencode)
                                          |                    |
              config.py (YAML) ── db.py (SQLite) ──── sandbox/ (libvirt | lima | hcs)
```

### Core concepts (the vocabulary everything else uses)

- **Context**: a working directory + per-context model, tool policy, and optional sandbox. Defined in config; switched with `/context`.
- **ChatScope**: `(chat_id, thread_id)` — the unit of conversation isolation. Each Telegram forum topic is an independent scope with its own context binding, session, and approval state.
- **Session**: a persistent agent conversation. The backend owns persistence; OpenShrimp maps `(chat_id, thread_id, context_name) -> session_id` in SQLite (`db.py`).
- **Client manager** (`client_manager.py`): keeps one live backend client per ChatScope across messages so the CLI subprocess survives between turns; only the first message after a restart uses resume.
- **dispatch_registry**: the cross-component path for injecting a turn into a scope from outside the normal message handler (scheduled tasks, host_monitor events, event pick-up).

### Backend layer (`backend/`)

Everything downstream of `client_manager` speaks the backend-neutral contract in `backend/protocol.py` + `backend/types.py`. `backend/claude_sdk/` wraps the Claude Agent SDK; `backend/opencode/` drives `opencode serve` over HTTP (models must be provider-qualified like `openai/gpt-5.5`). Per-agent sandbox integration (image bundles, runtime factory, in-guest installers) lives under `backend/<agent>/` — the `sandbox/` package is generic plumbing and must never name an agent.

### Tool approval (`hooks.py`, `handlers/approval.py`, `bash_parse.py`)

Layered: read-only file tools auto-approved inside the context directory; Edit/Write always prompt via inline keyboard (unless the user taps "Accept all edits" for the session); other tools use `allowed_tools` patterns or session-scoped `ApprovalRule(tool_name, pattern)` fnmatch rules; paths outside the context dirs always prompt. Sandboxed contexts auto-approve Bash (the sandbox is the boundary) — but the `is_host_escape` check for `host_bash`/`host_monitor` runs *before* all other checks and always demands a fresh approval; never reorder it. Bash commands are parsed with tree-sitter (`bash_parse.py`) for prefix-pattern approval.

### Sandbox layer (`sandbox/`)

`Sandbox` protocol in `sandbox/base.py` (lifecycle: `ensure_environment -> ensure_running -> provision_workspace -> build_cli_wrapper -> cleanup/stop`), `SandboxManager` factory in `sandbox/manager.py`. One backend per platform: libvirt/QEMU (Linux; supports `persistent_paths` qcow2 volumes, computer use, and Waydroid phone use), Lima (macOS), HCS (Windows; a Linux guest on the Hyper-V Compute Service — `persistent_paths` VHDX volumes, computer use over a weston/RDP desktop, port forwarding; shares are 9p, not virtiofs, and phone use and security-key forwarding are unsupported because the WSL-shipped kernel builds without binder and uhid). The agent CLI runs inside via a generated wrapper script pointed at by the SDK's `cli_path`; all SDK streaming/callback machinery is unchanged.

### Inbound events (`events/`)

External sources deliver into per-source forum topics with **zero LLM processing on receipt** — the sink renders, persists (`inbound_events` table), and posts an inert message with a "Pick up" button. No adapter exposes a provider-facing webhook: Telegram intake long-polls and Lark holds a WebSocket, both outbound. A *passive* adapter instead records `emit` and is fed by a first-party authenticated push — WhatsApp is one (the paired Android companion posts batches to the device-signed `/api/whatsapp/messages` in `whatsapp_api.py`); it declares `SupportsIngest` and is reached through `EventManager.get_adapter_of_type`, never a registry of its own. The invariant is that inbound HTTP is first-party and signed, not that it does not exist. Scheduled tasks (`events/schedule.py`) are built on the same machinery; cron expressions are POSIX crontab (0=Sunday) and must go through `build_cron_trigger`, never raw into APScheduler's `CronTrigger` (which numbers 0=Monday).

**Untrusted-content rule (load-bearing, applies everywhere):** agent prompts never carry untrusted content — not even provider-delivered event text. Prompts reference events by id; the agent fetches content itself via the `read_inbound_event` MCP tool, which returns it wrapped in an `<inbound-event untrusted="true">` envelope as a tool result. A human tapping "Pick up" is the trust gate: any turn dispatched from an event without one must be capability-restricted — read-only tools, mandatory approvals — never blanket approvals, and not even in a sandboxed context.

### Control channel (`control/`)

Lets a UI process outside Python supervise the core: `status`, `shutdown`, `restart`, plus pushed `state`/`stopping` events. Newline-delimited JSON over a Windows named pipe or a Unix domain socket, chosen by platform behind one `ControlServer`; the address is derived from the instance name alone (`endpoint_address`) so no discovery file is needed. Authorisation is the OS's — pipe DACL, or a `0600` socket in a `0700` directory — never a token, and the channel must never be mounted on the main Starlette app, which is published through a tunnel. It exists because `request_shutdown()` is in-process only and `os.kill(pid, SIGTERM)` on Windows is an unconditional `TerminateProcess` that skips shutdown and strands the sandbox guest; `SIGBREAK` is bound to the debug dump, not to stopping. Two cores sharing an instance name must refuse to start rather than both serve — asyncio's pipes allow unlimited instances of a name, so the collision is otherwise silent on Windows. Process control only: it carries no conversation or event content.

Front ends that must work while the core is *stopped* use the CLI instead — `openshrimp config write --json -` and `openshrimp models --json`. The config schema lives only in Python; a GUI collects values and pipes JSON in.

## Config

Runtime config: `~/.config/openshrimp/config.yaml` (schema documented inline in `config.example.yaml`). `ANTHROPIC_API_KEY` comes from the environment, not config. Session/container state under `~/.config/openshrimp/`.

## Conventions

- All async, `asyncio` throughout — no blocking calls. Type hints on all signatures. No classes where a function will do.
- SQLite only through `aiosqlite`.
- Errors: catch at the handler level, log, send a friendly Telegram message. Never crash the bot.
- Telegram output is MarkdownV2 via `markdown.py` (GFM conversion); streaming uses `sendMessageDraft` through raw API calls (not yet in python-telegram-bot). Max message length 4096 — split at paragraph/code-block boundaries.
- No backwards-compatibility shims: single-user project deployed in lockstep, so there are no old clients to support — rename fields, drop enum values, change both sides of a wire contract cleanly, and don't raise compat as a consideration.
- No temporal references in code comments (no plan/tier/date mentions); plans and tiers are ephemeral and such comments rot. State the invariant the code enforces instead.
- Writing standard for code comments, docs, commit messages and chat replies: be concrete (name the mechanism, the number, the consequence). Cut any sentence that would survive being moved to another project unchanged. Show rather than label — no asides telling the reader a point is subtle or important. No binary contrasts ("not X, it's Y"), rhetorical questions, or summary-recap endings. Active voice, plain words; never "delve", "leverage", "robust", "streamline", "it's worth noting". Em dashes at most one or two per passage. Formatting follows content: no bold mid-sentence, no header over a two-sentence section. Sources: [no-ai-slop](https://github.com/petergyang/no-ai-slop/blob/main/skills/no-ai-slop/SKILL.md) and the Nature Masterclass *[Writing for Greater Impact](https://www.nature.com/masterclasses/writing-for-greater-impact/50732650)* (login-walled — ask rather than guess at its specifics).

## Documentation

Three trees. Decide by audience: how to run it → a `website/` guide; how to reproduce a build or test environment → a tracked file beside the code it serves; how it was designed or why a decision was made → `docs/`.

- `website/src/content/docs/` is the operator-facing site, Astro Starlight, published at https://shrimp.wong.place, with `getting-started/`, `guides/`, `reference/` and `deployment/` sections. The sidebar is autogenerated from `sidebar.order` frontmatter; fractional values (`1.5`, `5.5`) slot a page between existing ones. Verify with `npm run build` in `website/`, which renders every page and catches frontmatter errors. `README.md` is a standalone quickstart that links to the site by absolute URL.
- Contributor how-to for an environment that has to be rebuilt is **tracked**, next to the code it serves: `windows/TESTING.md` (building the two Windows 11 libvirt guests, reaching them over ssh, driving their desktop) and `windows/BUILDING.md` (the MSBuild/WiX recipe for the tray and MSI, and which guest validates what). These are reference, not plans, so the delete-once-shipped rule below does not reach them.
- `docs/` (internal design notes) and `spikes/` (scratch work, including its smoke and verification scripts) are **deliberately untracked** — their appearance in `git status` is the intended state, not a loose end. Never `git add` them or suggest committing them. A plan in `docs/` is **deleted once it ships**: the code is the source of truth, and a doc restating shipped behaviour is a second, drifting copy. Only docs describing work that does not exist yet survive, however old. If a doomed doc flags something still actionable, surface it in chat before deleting rather than keeping the file for it.

## Releases

Bump `VERSION`, commit as `chore: release <x.y.z>`, and push that commit **alone** — do not tag by hand. `.github/workflows/release.yaml` fires on a push to master touching `VERSION`, resolves the version from that file, and creates and pushes the annotated tag itself. Tagging manually gets you two runs, and the VERSION-push run then fails at "Create the tag" with `$tag already exists — bump VERSION to cut a release`.

- Adding a `web/<name>-app` Mini App means adding its `npm run build` step to **both** frontend-building jobs (sdist and macOS `.app`), or hatchling's force-include of `web/<name>-app/dist` fails the sdist build.
- The `build-macos-app` job builds with py2app, codesigns, notarizes and staples, but never executes anything from the bundle — nothing runs the SDK-bundled `claude`, nothing imports the py2app site-packages. It goes green on bundles that crash on launch (a missing `_cffi_backend` and a hardened-runtime JIT denial both shipped through it in one day). Never report a `.app` fix as verified on CI evidence alone; say what still needs checking on a real Mac. To isolate a bundled-binary failure, copy the binary out of the bundle and ad-hoc re-sign it (`codesign --force --sign -`) — that strips the hardened runtime and nothing else, giving a single-variable A/B.

## Icons

`windows/src/OpenShrimp.Tray/Assets/tray.ico` is checked in with no build step, generated from `assets/logo-square.svg` at 16/20/24/32/40/48/64/128/256. Two things the generator has to get right: render each frame from the vector at its native size rather than downscaling one bitmap, and below 32px drop the near-black outline path (index 2 of the SVG's paths). That path is thin linework across the whole body, so sub-pixel strokes smear into the red fill and a naive 16px export reads as a featureless blob; body and shading alone keep a legible silhouette. Pillow's ICO writer silently drops any requested size larger than the base image, so `.save()` must be called on the largest frame.

That one file is `ARPPRODUCTICON` and the Start-menu shortcut icon (`windows/packaging/OpenShrimp.wxs`), the setup wizard's title bar, and the tray icon, so a regression shows in four places at once. `macos/`'s `menubar-icon*.png` stay black: they are template images that macOS recolours per menu-bar appearance, and Windows, which has no such concept, would draw them as a near-invisible smudge.
