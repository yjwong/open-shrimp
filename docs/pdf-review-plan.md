# PDF Review Mini App — Page-Level Feedback Plan

## Goal

Let a user review agent-generated PDF artifacts (slide decks, reports) and leave
page-level comments that get dispatched back into the agent session as a prompt —
the same feedback loop the markdown preview app already provides for prose.

Scope for **v1**: page-number-anchored comments only. No pixel/region annotation.
"Page 4: fix this title" is dispatched as text; the agent regenerates or edits the
source. Region/coordinate anchoring is explicitly out of scope (see Non-goals).

## Decisions (locked)

| Question | Choice |
|----------|--------|
| Entry point | **`send_file` with a `.pdf`** — when the agent delivers a PDF via the `send_file` tool, the document message gets a "📄 Review" `web_app` button. Identical to how `.md` gets its "📖 Preview" button today (`tools.py:356`). Plain `Write`s do **not** trigger a button. |
| PDF delivery | **On-disk, stream by path** — new `/api/preview/pdf?path=` endpoint mirroring `image_endpoint` (path-traversal guard + token auth). |
| Comment anchor | **Page number + comment** — each comment is `{page: N, comment: str}`. |
| Frontend | **New `web/pdf-app/`** — standalone PDF.js viewer + comment sidebar, served at `/pdf/`. Mobile-first UI (Telegram Mini App is primarily used on phones). |
| Sandboxed contexts | **Works as-is, same as `.md`** — `send_file`/`pdf_endpoint` read the host path, which is shared into the sandbox (bind mount / virtiofs), so the file resolves on host disk. No special handling. |
| Feedback timing | **Queue as next turn** — dispatch behaves like a normal user message, delivered at the next turn boundary (same as markdown review today). |
| CI / build | **No CI *workflow* change** (the only workflow, `docs.yml`, is scoped to `website/**` and doesn't touch the package). **But packaging changes are required**: the app must be `npm run build`-ed and force-included in `pyproject.toml` — see CI / build. |

## Why this reuses ~80% of existing machinery

The markdown preview app (`src/open_shrimp/preview/api.py`, `web/markdown-app/`)
already implements every non-PDF-specific piece:

- **Disk streaming with token auth** — `image_endpoint` (`preview/api.py:179`)
  is the exact template: `token`-param auth (for tags that can't send headers),
  `_is_within_context_directories()` traversal guard, `FileResponse`.
- **Feedback round-trip** — `submit_review_endpoint` (`preview/api.py:231`)
  formats a `comments[]` array into a prompt and calls
  `dispatch_registry.dispatch()`. We add a parallel path keyed on page number.
- **Mini App surfacing via `send_file`** — the `send_file` tool (`tools.py:308`)
  already attaches a "📖 Preview" `web_app` button when the delivered file is a
  `.md` (`tools.py:356`), using `make_web_app_button()` (private-chat `WebAppInfo`
  vs group-chat HMAC-token-URL split). A `.pdf` branch is a near-exact copy.
- **Shared base URL** — the Mini App HTTP server that already hosts `/preview/`,
  `/terminal/`, `/review/`, `/vnc/` also hosts `/pdf/`. `send_file` already
  derives the base URL from `config.review.public_url` / `host:port`
  (`tools.py:358`) to build the `.md` preview URL, so the `.pdf` button reuses
  the same computation — no new config or plumbing.

PDF-specific new work: the PDF.js viewer, the binary streaming endpoint, and the
page-keyed comment prompt format.

## Backend changes (`src/open_shrimp/preview/api.py`)

### 1. `pdf_endpoint` — stream the PDF by path

New route `GET /api/preview/pdf?path=&token=`. Copy `image_endpoint` almost
verbatim, changing only the extension allowlist to `{".pdf"}` and the media type
to `application/pdf`. Keeps: `validate_token_param` fallback,
`_is_within_context_directories`, absolute-path check, `FileResponse`.

```python
_PDF_EXTENSIONS = {".pdf"}

async def pdf_endpoint(request: Request) -> JSONResponse | FileResponse:
    # auth (token param OR header) -> path checks -> suffix in _PDF_EXTENSIONS
    # -> FileResponse(resolved, media_type="application/pdf")
```

Register in `create_preview_routes()`:
```python
Route("/api/preview/pdf", pdf_endpoint, methods=["GET"]),
```

### 2. `submit_review_endpoint` — accept page-anchored comments

The existing endpoint already branches on file-based vs `content_id`. Extend the
file-based branch to accept an optional `page` field per comment. The comment
object becomes `{"page": <int|null>, "comment": <str>}` (the markdown app's
`block_text` stays supported; PDF just sends `page` instead).

In the prompt-building loop (`preview/api.py:343`), when `page` is present emit:

```
### Comment 1 (page 4)
<comment text>
```

instead of the `> {block_text}` quote line. Validate `page` is a positive int
within a plausible bound (e.g. 1–2000) and clamp comment count/length as today
(max 50 comments, 2000 chars each).

`subject` for a PDF reads `the PDF at \`<path>\``. No `tool_use_id` /
plan-auto-deny path is involved (that's ExitPlanMode-specific).

No new endpoint needed — one code path, page is additive.

## Frontend: `web/pdf-app/`

New Vite app mirroring `web/markdown-app/` layout. Reuse the patterns, not
necessarily the components, from `web/markdown-app/src/`:

- **Rendering**: `pdfjs-dist`. Load the PDF from
  `/api/preview/pdf?path=<path>&token=<t>`. Render pages to `<canvas>` lazily
  (render-on-scroll / current-page-window) so large decks don't blow memory.
- **Navigation** (mobile-first): a thumbnail filmstrip to jump between pages,
  but laid out for a phone — a horizontally-scrolling filmstrip along the
  bottom (not a wide desktop side rail), large tap targets, current page in a
  single-column canvas view. Slide decks are page-oriented so thumbnails are the
  right primitive; the constraint is fitting them to a narrow touch viewport.
- **Commenting**: a "💬 Comment on this page" button per page (or a global
  "add comment" that captures the current page). Comments collect in a sidebar
  list showing `Page N — <text>`, editable/removable before submit. Reuse the
  `ReviewContext` state pattern and `SubmitDialog` (including the
  **keep-open-after-submit** checkbox from commit `6e7f635`) so iterative review
  works — submit a batch, keep viewing, submit again.
- **Submit**: POST to `/api/preview/submit-review` with
  `{chat_id, thread_id, path, comments:[{page, comment}]}`. Auth via the shared
  `authenticate()` scheme (`tg-init-data` or `tg-token`) already used by the
  preview/review apps (`web/markdown-app/src/api.ts` is the reference).
- **Build/serve**: add a `Mount("/pdf", StaticFiles(...))` in
  `create_preview_routes()` pointing at `web/pdf-app/dist` (or packaged
  `preview/static-pdf/`), matching the existing markdown-app mount logic at
  `preview/api.py:471`.

## Wiring the button (`src/open_shrimp/tools.py`, `send_file`)

The button is attached by the **`send_file` tool handler** (`tools.py:308`),
exactly where the `.md` → "📖 Preview" button is built today (`tools.py:356`).
The delivered document already carries `reply_markup`; we add a `.pdf` case.

Add a branch alongside the existing `.md` check:

```python
elif filename.lower().endswith(".pdf") and config is not None:
    base_url = ...  # same resolution as the .md branch (tools.py:358-361)
    if base_url:
        pdf_params = f"path={quote(path, safe='')}&chat_id={chat_id}"
        if thread_id is not None:
            pdf_params += f"&thread_id={thread_id}"
        pdf_url = f"{base_url}/pdf/?{pdf_params}"
        reply_markup = InlineKeyboardMarkup([[
            make_web_app_button(
                "📄 Review", pdf_url,
                chat_id=chat_id, user_id=user_id,
                bot_token=config.telegram.token,
                is_private_chat=is_private_chat,
            ),
        ]])
```

The base-URL resolution and `make_web_app_button` call are identical to the
`.md` branch — factor them into a small helper
(`_preview_button(path, label, app, ...)`) shared by both `.md` and `.pdf` to
avoid duplication. `path` is `os.path.abspath(file_path)` (`tools.py:316`), the
host path — which is what `pdf_endpoint` reads.

### Why plain `Write`s don't trigger a button

The button rides on `send_file`, not on `Write`. The agent iterating on
`deck.pdf` across many `Write`s produces no buttons; a button appears only when
the agent explicitly delivers the finished PDF via `send_file`. This is the same
intentional-delivery model as `.md` today — no clutter, no dedup logic needed.

### Sandboxed contexts

No special handling. `send_file` resolves `os.path.abspath(file_path)` and reads
it on the host; the context directory is shared into the sandbox (Docker bind
mount / Lima/libvirt virtiofs), so the path resolves on host disk. `pdf_endpoint`
reads the same host path. This is exactly why `.md` preview already works in
sandboxed contexts — PDF inherits it for free.

## Data flow (end to end)

```
Agent calls send_file(deck.pdf)
  -> tools.py send_file detects .pdf, attaches "📄 Review" web_app button
  -> document sent to Telegram with button (url -> /pdf/?path=...&chat_id=...)
User taps button (Telegram Mini App)
  -> pdf-app loads /api/preview/pdf?path=&token=  (FileResponse, streamed)
  -> user adds page-anchored comments, taps Submit
  -> POST /api/preview/submit-review {path, chat_id, thread_id, comments:[{page,comment}]}
  -> submit_review_endpoint builds prompt "Page 4: ..." and dispatch()es it
  -> agent receives feedback as a normal user turn, edits source / regenerates
```

## Security

- Reuse `_is_within_context_directories()` on every path (traversal guard).
- Reuse `validate_token_param` / `authenticate` — no new auth surface.
- PDF served as `FileResponse` with `application/pdf`; no execution risk.
- Enforce comment count/length caps already present in `submit_review_endpoint`.
- `pdfjs-dist` worker: pin version, disable `isEvalSupported`, and consider
  `disableAutoFetch`/range requests off for simplicity in v1.

## Testing

- **Backend unit** (`pytest`): `pdf_endpoint` returns 200 + `application/pdf`
  for an in-context PDF; 403 for out-of-context; 404 for missing; token-param
  auth accepted. `submit_review_endpoint` with `page` builds the expected
  prompt and calls a mocked `dispatch`.
- **Frontend**: manual pass in the Telegram Mini App against a real deck — load,
  scroll, comment on pages 1/4/last, submit, verify the dispatched prompt lands
  in the session and cites the right pages. (No headless PDF.js test in v1.)
- **Wiring**: mock a `Write` tool-result for `deck.pdf`, assert a single
  "📄 Review PDF" button with a well-formed `/pdf/?path=` URL; assert no button
  for `.md`/`.txt` writes.

## CI / build

**No CI *workflow* change** — the repository's only workflow,
`.github/workflows/docs.yml`, is triggered on `website/**` and deploys the Astro
docs site; it does not build or test the Python package or any `web/*` Mini App.

**But a build + packaging step is required** — this is not "free". Mini Apps are
packaged via hatch `force-include` in `pyproject.toml` (lines 43-59), which copies
each app's **pre-built** `dist/` into the package static dir. There is **no build
hook that runs `npm run build`**, so the `dist/` must already exist when the wheel
is built. Adding `web/pdf-app` therefore requires:

1. Build the app: `npm install && npm run build` in `web/pdf-app/` (produces
   `web/pdf-app/dist`). This must be run before `uv build` / wheel packaging —
   locally or in whatever release process builds the package.
2. Add two `force-include` entries in `pyproject.toml`, mirroring the markdown app:
   ```toml
   # [tool.hatch.build.targets.sdist.force-include]
   "web/pdf-app/dist" = "web/pdf-app/dist"
   # [tool.hatch.build.targets.wheel.force-include]
   "web/pdf-app/dist" = "open_shrimp/preview/static-pdf"
   ```
   (The markdown app maps to `open_shrimp/preview/static`; the PDF app gets its
   own `static-pdf` so the two `/preview/` and `/pdf/` mounts don't collide.)
3. `create_preview_routes()` mounts `/pdf` from `preview/static-pdf` (packaged) or
   `web/pdf-app/dist` (dev), the same dual-path lookup the markdown mount uses
   (`preview/api.py:455-462`).

In dev, the mount falls back to `web/pdf-app/dist`, so a local `npm run build` is
enough to see it without reinstalling the package.

## Non-goals (v1)

- Region / coordinate / text-selection anchoring (pixel annotation).
- In-PDF highlight overlays or drawing.
- Editing the PDF directly; feedback always routes back to the agent.
- Buttons on plain `Write`s — delivery is via `send_file` only.

## Follow-ups (post-v1, only if page granularity proves too coarse)

1. **Region anchoring** — capture a tap/rect on the page canvas, store
   `{page, x, y, w, h}` normalized coords, include in the prompt as a hint, and
   optionally crop that region to a thumbnail image for the agent.
2. **Source-vs-render** — if decks are HTML/reveal.js rendered to PDF, add a
   toggle to review the source in the markdown app instead.

## Touch list

- `src/open_shrimp/tools.py` — add a `.pdf` branch in `send_file` (`tools.py:356`)
  attaching a "📄 Review" button to `/pdf/?path=...`; factor the shared base-URL +
  `make_web_app_button` logic into a small helper reused by `.md` and `.pdf`.
- `src/open_shrimp/preview/api.py` — `pdf_endpoint`, `page` in
  `submit_review_endpoint`, route + mount registration.
- `web/pdf-app/` — new Vite + PDF.js app (mobile-first viewer, comment sidebar,
  submit), reusing `ReviewContext`/`SubmitDialog`/`api.ts` patterns from
  `web/markdown-app/`.
- `pyproject.toml` — two new `force-include` lines (sdist + wheel, ~lines 47/56)
  mapping `web/pdf-app/dist` → `open_shrimp/preview/static-pdf`.
- **Build step** — `npm install && npm run build` in `web/pdf-app/` must run
  before packaging (no hatch build hook does this automatically).
- Tests under the existing preview/tools test modules.
