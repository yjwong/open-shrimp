import DOMPurify from "dompurify";
import { marked } from "marked";

// ── Events (mirrors open_shrimp/terminal/transcript.py) ──

export type TranscriptEvent =
  | { kind: "prompt"; text: string }
  | { kind: "text"; text: string }
  | {
      kind: "tool";
      id: string;
      name: string;
      summary: string;
      input: Record<string, unknown>;
    }
  | {
      kind: "tool_result";
      id: string;
      text: string;
      is_error: boolean;
      truncated: boolean;
    };

export type NoticeLevel = "info" | "ok" | "warn" | "error";

export type RunState = "waiting" | "running" | "done" | "ended";

const TOOL_ICONS: Record<string, string> = {
  Bash: "💻",
  Read: "📖",
  Edit: "✏️",
  MultiEdit: "✏️",
  Write: "📝",
  NotebookEdit: "📝",
  Grep: "🔍",
  Glob: "🔍",
  WebFetch: "🌐",
  WebSearch: "🌐",
  Task: "🤖",
  Agent: "🤖",
  TodoWrite: "📋",
};

const STATE_LABELS: Record<RunState, string> = {
  waiting: "Waiting",
  running: "Running",
  done: "Finished",
  ended: "Stopped",
};

// A prompt taller than this many pixels folds behind "Show full task".
const PROMPT_FOLD_PX = 140;

// Tool output taller than this folds behind "Show all output" (about 12 lines).
const OUTPUT_FOLD_PX = 220;

// How close to the bottom still counts as "at the bottom" for auto-follow.
const FOLLOW_SLACK_PX = 48;

function renderMarkdown(text: string): string {
  const html = marked.parse(text, { async: false, gfm: true, breaks: true });
  return DOMPurify.sanitize(html);
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Some Telegram webviews deny the async clipboard API.
    const area = el("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

/** A labelled block of preformatted text with a copy button. */
function codeBlock(label: string, text: string, extraClass = ""): HTMLElement {
  const wrap = el("div", `code ${extraClass}`.trim());
  const head = el("div", "code-head");
  head.appendChild(el("span", "code-label", label));
  const copy = el("button", "copy", "Copy");
  copy.type = "button";
  copy.onclick = async (e) => {
    e.preventDefault();
    copy.textContent = (await copyText(text)) ? "Copied" : "Copy failed";
    setTimeout(() => (copy.textContent = "Copy"), 1500);
  };
  head.appendChild(copy);
  wrap.appendChild(head);
  wrap.appendChild(el("pre", undefined, text));
  return wrap;
}

/** Fold *body* behind a toggle when it renders taller than *limit* pixels.
 * Measured after insertion, so *body* must already be in the document. */
function foldIfTall(
  body: HTMLElement,
  limit: number,
  labels: [string, string]
): void {
  if (body.scrollHeight <= limit + 40) return;
  body.style.setProperty("--fold", `${limit}px`);
  body.classList.add("foldable", "folded");
  const toggle = el("button", "fold-toggle", labels[0]);
  toggle.type = "button";
  toggle.onclick = () => {
    const folded = body.classList.toggle("folded");
    toggle.textContent = folded ? labels[0] : labels[1];
  };
  body.after(toggle);
}

/** Order a tool's input so the field that identifies the call comes first. */
const LEADING_FIELDS = ["command", "file_path", "path", "pattern", "url", "query", "prompt"];

function inputFields(input: Record<string, unknown>): [string, unknown][] {
  const entries = Object.entries(input);
  const rank = (key: string) => {
    const i = LEADING_FIELDS.indexOf(key);
    return i === -1 ? LEADING_FIELDS.length : i;
  };
  return entries.sort(([a], [b]) => rank(a) - rank(b));
}

interface ToolRow {
  root: HTMLDetailsElement;
  body: HTMLElement;
  status: HTMLElement;
}

/** The transcript of one agent, drawn as a page of cards. */
export class TranscriptView {
  private readonly list: HTMLElement;
  private readonly stateEl: HTMLElement;
  private readonly countEl: HTMLElement;
  private readonly titleEl: HTMLElement;
  private readonly jump: HTMLButtonElement;
  private readonly tools = new Map<string, ToolRow>();
  private toolCount = 0;
  private following = true;

  constructor(
    root: HTMLElement,
    statusBar: HTMLElement
  ) {
    statusBar.replaceChildren();
    this.titleEl = el("span", "status-title");
    this.stateEl = el("span", "status-state");
    this.countEl = el("span", "status-count");
    statusBar.append(this.titleEl, this.stateEl, this.countEl);
    statusBar.style.display = "flex";

    this.list = el("div", "transcript");
    root.appendChild(this.list);

    this.jump = el("button", "jump", "↓ New activity");
    this.jump.type = "button";
    this.jump.onclick = () => this.scrollToEnd();
    document.body.appendChild(this.jump);

    window.addEventListener(
      "scroll",
      () => {
        this.following = this.atBottom();
        if (this.following) this.jump.classList.remove("shown");
      },
      { passive: true }
    );

    // Links leave the Mini App through Telegram, not by navigating it away.
    root.addEventListener("click", (e) => {
      const link = (e.target as HTMLElement).closest("a");
      if (!link?.href) return;
      e.preventDefault();
      const tg = window.Telegram?.WebApp;
      if (tg?.openLink) tg.openLink(link.href);
      else window.open(link.href, "_blank", "noopener");
    });
  }

  /** Clear the page for a different agent. */
  reset(title: string): void {
    this.list.replaceChildren();
    this.tools.clear();
    this.toolCount = 0;
    this.following = true;
    this.jump.classList.remove("shown");
    this.titleEl.textContent = title;
    this.countEl.textContent = "";
    this.setState("waiting");
    window.scrollTo(0, 0);
  }

  setState(state: RunState): void {
    this.stateEl.textContent = STATE_LABELS[state];
    this.stateEl.dataset.state = state;
    if (state === "done" || state === "ended") {
      // A call still without a result never got one.
      for (const row of this.tools.values()) {
        if (row.status.dataset.state === "pending") {
          row.status.dataset.state = "unknown";
          row.status.textContent = "";
        }
      }
    }
  }

  addEvents(events: TranscriptEvent[]): void {
    if (!events.length) return;
    const follow = this.following;
    for (const event of events) {
      switch (event.kind) {
        case "prompt":
          this.addPrompt(event.text);
          break;
        case "text":
          this.addText(event.text);
          break;
        case "tool":
          this.addTool(event);
          break;
        case "tool_result":
          this.addResult(event);
          break;
      }
    }
    if (this.stateEl.dataset.state === "waiting") this.setState("running");
    this.afterAppend(follow);
  }

  notice(level: NoticeLevel, msg: string): void {
    const follow = this.following;
    this.list.appendChild(el("div", `notice ${level}`, msg));
    this.afterAppend(follow);
  }

  private afterAppend(follow: boolean): void {
    if (follow) this.scrollToEnd();
    else this.jump.classList.add("shown");
  }

  private atBottom(): boolean {
    const doc = document.documentElement;
    return window.innerHeight + window.scrollY >= doc.scrollHeight - FOLLOW_SLACK_PX;
  }

  private scrollToEnd(): void {
    this.following = true;
    this.jump.classList.remove("shown");
    window.scrollTo(0, document.documentElement.scrollHeight);
  }

  private addPrompt(text: string): void {
    const card = el("section", "prompt");
    card.appendChild(el("div", "prompt-label", "Task"));
    const body = el("div", "prompt-body md");
    body.innerHTML = renderMarkdown(text);
    card.appendChild(body);
    this.list.appendChild(card);
    foldIfTall(body, PROMPT_FOLD_PX, ["Show full task", "Show less"]);
  }

  private addText(text: string): void {
    const block = el("div", "say md");
    block.innerHTML = renderMarkdown(text);
    this.list.appendChild(block);
  }

  private addTool(event: Extract<TranscriptEvent, { kind: "tool" }>): void {
    this.toolCount += 1;
    this.countEl.textContent =
      this.toolCount === 1 ? "1 tool call" : `${this.toolCount} tool calls`;

    const root = el("details", "tool");
    const summary = el("summary");
    summary.appendChild(el("span", "tool-icon", TOOL_ICONS[event.name] ?? "🔧"));
    const label = el("span", "tool-label");
    label.appendChild(el("span", "tool-name", event.name));
    // The agent's own one-line description of a call reads better than the
    // start of the command, which is often just a `cd`.
    const description = event.input["description"];
    const gist = typeof description === "string" && description ? description : event.summary;
    if (gist) {
      label.appendChild(
        el("span", `tool-summary${gist === description ? " prose" : ""}`, gist)
      );
    }
    summary.appendChild(label);
    // Transcripts without call ids never carry results to match.
    const status = el("span", "tool-status");
    status.dataset.state = event.id ? "pending" : "unknown";
    summary.appendChild(status);
    root.appendChild(summary);

    const body = el("div", "tool-body");
    for (const [key, value] of inputFields(event.input)) {
      if (key === "description" && value === description) continue;
      const text =
        typeof value === "string" ? value : JSON.stringify(value, null, 2);
      body.appendChild(codeBlock(key, text));
    }
    root.appendChild(body);
    this.list.appendChild(root);

    if (event.id) this.tools.set(event.id, { root, body, status });
  }

  private addResult(event: Extract<TranscriptEvent, { kind: "tool_result" }>): void {
    const row = this.tools.get(event.id);
    if (!row) return;
    row.status.dataset.state = event.is_error ? "error" : "ok";
    row.status.textContent = event.is_error ? "✗" : "✓";
    if (event.is_error) row.root.classList.add("failed");
    const label = event.is_error ? "error" : "output";
    const text = event.text || "(no output)";
    const block = codeBlock(label, text, event.is_error ? "error" : "output");
    row.body.appendChild(block);
    // A closed <details> lays its body out at zero height, so measuring has
    // to wait until the row is first opened.
    const pre = block.querySelector("pre")!;
    const fold = () => foldIfTall(pre, OUTPUT_FOLD_PX, ["Show all output", "Show less"]);
    if (row.root.open) fold();
    else row.root.addEventListener("toggle", fold, { once: true });
    if (event.truncated) {
      row.body.appendChild(
        el("div", "truncated", "Output cut off after 8,000 characters.")
      );
    }
  }
}

export function injectTranscriptStyles(): void {
  const style = document.createElement("style");
  style.textContent = `
    :root {
      --bg: var(--tg-theme-bg-color, #1a1b26);
      --bg2: var(--tg-theme-secondary-bg-color, #24283b);
      --fg: var(--tg-theme-text-color, #c0caf5);
      --hint: var(--tg-theme-hint-color, #787c99);
      --link: var(--tg-theme-link-color, #7aa2f7);
      --accent: var(--tg-theme-button-color, #7aa2f7);
      --accent-fg: var(--tg-theme-button-text-color, #1a1b26);
      --line: color-mix(in srgb, var(--hint) 30%, transparent);
      --ok: #9ece6a;
      --err: #f7768e;
      --mono: ui-monospace, "SF Mono", "Cascadia Code", "JetBrains Mono", monospace;
    }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body {
      background: var(--bg);
      color: var(--fg);
      font: 15px/1.5 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
      -webkit-text-size-adjust: 100%;
    }
    #top {
      position: sticky;
      top: 0;
      z-index: 10;
      background: var(--bg2);
      border-bottom: 1px solid var(--line);
    }
    #status-bar {
      display: none;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      font-size: 13px;
    }
    .status-title {
      flex: 1;
      min-width: 0;
      font-weight: 600;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .status-state {
      padding: 2px 8px;
      border-radius: 10px;
      font-size: 12px;
      font-weight: 600;
      background: var(--line);
    }
    .status-state[data-state="running"] { background: var(--accent); color: var(--accent-fg); }
    .status-state[data-state="running"]::before {
      content: "● ";
      animation: pulse 1.4s ease-in-out infinite;
    }
    .status-state[data-state="done"] { background: color-mix(in srgb, var(--ok) 25%, transparent); color: var(--ok); }
    .status-state[data-state="ended"] { color: var(--hint); }
    .status-count { color: var(--hint); font-size: 12px; white-space: nowrap; }
    @keyframes pulse { 50% { opacity: 0.3; } }

    #transcript-root { padding: 12px 12px 72px; }
    .transcript { display: flex; flex-direction: column; gap: 10px; }

    .md { overflow-wrap: anywhere; }
    .md p, .md ul, .md ol, .md pre, .md blockquote, .md table { margin: 0 0 8px; }
    .md > :last-child { margin-bottom: 0; }
    .md ul, .md ol { padding-left: 22px; }
    .md h1, .md h2, .md h3, .md h4 { font-size: 15px; margin: 12px 0 4px; }
    .md a { color: var(--link); }
    .md code {
      font-family: var(--mono);
      font-size: 13px;
      background: var(--bg2);
      padding: 1px 4px;
      border-radius: 4px;
    }
    .md pre {
      background: var(--bg2);
      padding: 8px 10px;
      border-radius: 8px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .md pre code { padding: 0; background: none; }
    .md blockquote { border-left: 3px solid var(--line); padding-left: 10px; color: var(--hint); }
    .md table { border-collapse: collapse; display: block; overflow-x: auto; }
    .md th, .md td { border: 1px solid var(--line); padding: 4px 8px; }

    .prompt {
      background: var(--bg2);
      border-left: 3px solid var(--accent);
      border-radius: 8px;
      padding: 10px 12px;
    }
    .prompt-label {
      font-size: 12px;
      font-weight: 600;
      color: var(--hint);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-bottom: 4px;
    }
    .foldable.folded {
      max-height: var(--fold);
      overflow: hidden;
      -webkit-mask-image: linear-gradient(#000 60%, transparent);
      mask-image: linear-gradient(#000 60%, transparent);
    }
    .fold-toggle {
      display: block;
      margin-top: 2px;
      min-height: 36px;
      padding: 0 10px;
      border: none;
      background: none;
      color: var(--link);
      font: inherit;
      font-weight: 600;
    }

    .tool {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--bg);
    }
    .tool.failed { border-color: color-mix(in srgb, var(--err) 50%, transparent); }
    .tool > summary {
      list-style: none;
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 44px;
      padding: 6px 10px;
      cursor: pointer;
      -webkit-tap-highlight-color: transparent;
    }
    .tool > summary::-webkit-details-marker { display: none; }
    .tool > summary::after {
      content: "›";
      color: var(--hint);
      font-size: 18px;
      transition: transform 0.15s;
    }
    .tool[open] > summary::after { transform: rotate(90deg); }
    .tool-icon { flex: none; }
    .tool-label {
      flex: 1;
      min-width: 0;
      display: flex;
      gap: 6px;
      font-size: 14px;
    }
    .tool-name { flex: none; font-weight: 600; }
    .tool-summary {
      min-width: 0;
      color: var(--hint);
      font-family: var(--mono);
      font-size: 13px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      line-height: 21px;
    }
    .tool-summary.prose { font-family: inherit; font-size: 14px; }
    .tool-status { flex: none; font-weight: 700; }
    .tool-status[data-state="ok"] { color: var(--ok); }
    .tool-status[data-state="error"] { color: var(--err); }
    .tool-status[data-state="pending"]::before {
      content: "";
      display: inline-block;
      width: 12px;
      height: 12px;
      border: 2px solid var(--hint);
      border-top-color: transparent;
      border-radius: 50%;
      animation: spin 0.9s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .tool-body {
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding: 0 10px 10px;
    }

    .code { background: var(--bg2); border-radius: 6px; overflow: hidden; }
    .code.error { box-shadow: inset 3px 0 var(--err); }
    .code-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 2px 4px 0 10px;
      font-size: 12px;
      color: var(--hint);
    }
    .copy {
      min-height: 32px;
      padding: 0 8px;
      border: none;
      background: none;
      color: var(--link);
      font: inherit;
      font-size: 12px;
    }
    .code pre {
      padding: 2px 10px 8px;
      font-family: var(--mono);
      font-size: 12.5px;
      line-height: 1.45;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .truncated { font-size: 12px; color: var(--hint); }

    .notice { font-size: 13px; color: var(--hint); text-align: center; padding: 6px 0; }
    .notice.ok { color: var(--ok); }
    .notice.error { color: var(--err); }

    .jump {
      position: fixed;
      left: 50%;
      bottom: 16px;
      transform: translate(-50%, 80px);
      padding: 10px 16px;
      min-height: 44px;
      border: none;
      border-radius: 22px;
      background: var(--accent);
      color: var(--accent-fg);
      font: inherit;
      font-size: 14px;
      font-weight: 600;
      box-shadow: 0 2px 10px rgba(0, 0, 0, 0.35);
      transition: transform 0.2s;
      z-index: 20;
    }
    .jump.shown { transform: translate(-50%, 0); }

    #agent-bar {
      display: none;
      align-items: center;
      gap: 6px;
      padding: 8px;
      overflow-x: auto;
      scrollbar-width: none;
      border-bottom: 1px solid var(--line);
    }
    .agent-chip {
      flex: none;
      min-height: 32px;
      padding: 0 12px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: var(--bg);
      color: var(--fg);
      font: inherit;
      font-size: 13px;
      white-space: nowrap;
    }
    .agent-chip.selected {
      background: var(--accent);
      border-color: var(--accent);
      color: var(--accent-fg);
      font-weight: 600;
    }
    #loading { font-family: inherit; }
  `;
  document.head.appendChild(style);
}
