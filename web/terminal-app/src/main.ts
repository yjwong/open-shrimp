import "@xterm/xterm/css/xterm.css";
import {
  injectTranscriptStyles,
  TranscriptView,
  type NoticeLevel,
  type TranscriptEvent,
} from "./transcript";

// ── Globals ──

declare global {
  interface Window {
    Telegram?: {
      WebApp: {
        initData: string;
        close: () => void;
        openLink?: (url: string) => void;
        ready: () => void;
        expand: () => void;
        viewportHeight: number;
        viewportStableHeight: number;
        onEvent: (event: string, cb: () => void) => void;
      };
    };
  }
}

const loadingEl = document.getElementById("loading")!;

function showError(msg: string): void {
  // A view may already have taken the page; put the banner back over it.
  if (!loadingEl.isConnected) document.body.appendChild(loadingEl);
  loadingEl.style.color = "#f7768e";
  loadingEl.textContent = msg;
}

function showStatus(msg: string): void {
  loadingEl.textContent = msg;
}

// ── Labels by source type ──

interface SourceLabels {
  tailPrefix: string;
  completedMsg: string;
  endedMsg: string;
}

function getLabels(sourceType: string): SourceLabels {
  switch (sourceType) {
    case "container_build":
      return {
        tailPrefix: "build",
        completedMsg: "Build completed.",
        endedMsg: "Build output stream ended.",
      };
    case "task":
    default:
      return {
        tailPrefix: "task",
        completedMsg: "Task completed.",
        endedMsg: "Task output stream ended.",
      };
  }
}

const params = new URLSearchParams(window.location.search);

// ── Tail mode ──

async function tailMain(): Promise<void> {
  showStatus("Initializing...");

  // Telegram SDK
  try {
    window.Telegram?.WebApp?.ready();
    window.Telegram?.WebApp?.expand();
  } catch {
    // Not in Telegram.
  }

  const sourceType = params.get("type") ?? "task";
  const sourceId = params.get("id");
  const taskType = params.get("task_type");

  if (!sourceId) {
    showError("No id provided.");
    return;
  }

  // A workflow's own output file is its final summary, written once the run
  // ends; each of its agents keeps a live transcript of its own instead.
  if (sourceType === "task" && taskType === "local_workflow") {
    injectTranscriptStyles();
    loadingEl.remove();
    const view = new TranscriptView(
      document.getElementById("transcript-root")!,
      document.getElementById("status-bar")!
    );
    await followWorkflow(view, sourceId);
    return;
  }

  let apiQuery = `type=${encodeURIComponent(sourceType)}&id=${encodeURIComponent(sourceId)}`;
  if (taskType) {
    apiQuery += `&task_type=${encodeURIComponent(taskType)}`;
  }

  showStatus("Loading...");
  let initial: ReadResult;
  try {
    initial = await readSource(apiQuery);
  } catch (e) {
    showError(`${e}`);
    return;
  }

  const labels = getLabels(sourceType);
  let sink: Sink;
  if (initial.render === "jsonl") {
    injectTranscriptStyles();
    loadingEl.remove();
    const view = new TranscriptView(
      document.getElementById("transcript-root")!,
      document.getElementById("status-bar")!
    );
    view.reset("Subagent");
    sink = transcriptSink(view, AGENT_LABELS);
  } else {
    sink = await openTerminal(labels, sourceId);
  }
  sink.apply(initial);
  await streamSource(sink, apiQuery, initial.size);
}

// ── Reading and tailing one source ──

type ReadResult =
  | { render: "raw"; content: string; size: number; active: boolean }
  | { render: "jsonl"; events: TranscriptEvent[]; size: number; active: boolean };

/** Where a source's content goes: a terminal for raw output, a transcript
 * page for an agent. */
interface Sink {
  apply(data: ReadResult | { text?: string; events?: TranscriptEvent[] }): void;
  finish(completed: boolean): void;
  notice(level: NoticeLevel, msg: string): void;
}

async function readSource(
  apiQuery: string,
  signal?: AbortSignal
): Promise<ReadResult> {
  const resp = await fetch(`/api/terminal/read?${apiQuery}`, {
    headers: getAuthHeader(),
    signal,
  });
  if (!resp.ok) {
    throw new Error(`Read error (${resp.status}): ${await resp.text()}`);
  }
  return (await resp.json()) as ReadResult;
}

/** Stream what a source appends past *offset* until the server reports it
 * done.  Returns quietly once *signal* aborts. */
async function streamSource(
  sink: Sink,
  apiQuery: string,
  offset: number,
  signal?: AbortSignal
): Promise<void> {
  const url = `/api/terminal/tail?${apiQuery}&offset=${offset}`;

  try {
    const resp = await fetch(url, {
      headers: {
        ...getAuthHeader(),
        Accept: "text/event-stream",
      },
      signal,
    });

    if (!resp.ok) {
      const text = await resp.text();
      sink.notice("error", `Stream error (${resp.status}): ${text}`);
      return;
    }

    const reader = resp.body?.getReader();
    if (!reader) {
      sink.notice("error", "Streaming not supported.");
      return;
    }

    const decoder = new TextDecoder();
    let buffer = "";
    let doneNext = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done || signal?.aborted) break;

      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (line.startsWith("event: done")) {
          // The next "data:" line carries the done payload.
          doneNext = true;
        } else if (line.startsWith("data: ")) {
          let payload: {
            text?: string;
            events?: TranscriptEvent[];
            completed?: boolean;
          };
          try {
            payload = JSON.parse(line.slice(6));
          } catch {
            continue;
          }
          if (doneNext) {
            sink.finish(!!payload.completed);
            return;
          }
          sink.apply(payload);
        }
      }
    }

    if (signal?.aborted) return;
    sink.notice("warn", "Connection closed.");
  } catch (e) {
    if (signal?.aborted) return;
    sink.notice("error", `Stream error: ${e}`);
  }
}

function transcriptSink(view: TranscriptView, labels: SourceLabels): Sink {
  return {
    apply(data) {
      if ("events" in data && data.events) view.addEvents(data.events);
      if ("active" in data) view.setState(data.active ? "running" : "done");
    },
    finish(completed) {
      view.setState(completed ? "done" : "ended");
      view.notice(
        completed ? "ok" : "warn",
        completed ? labels.completedMsg : labels.endedMsg
      );
    },
    notice(level, msg) {
      view.notice(level, msg);
    },
  };
}

const NOTICE_COLOURS: Record<NoticeLevel, string> = {
  info: "1;34",
  ok: "1;32",
  warn: "1;33",
  error: "31",
};

/** Raw output keeps the terminal: it carries real ANSI colour, progress bars
 * and carriage returns. */
async function openTerminal(labels: SourceLabels, sourceId: string): Promise<Sink> {
  showStatus("Loading xterm.js...");

  // Dynamic import so we can catch load errors.
  const { Terminal } = await import("@xterm/xterm");
  const { FitAddon } = await import("@xterm/addon-fit");

  showStatus("Creating terminal...");
  injectBaseStyles();

  const term = new Terminal({
    convertEol: true,
    cursorBlink: false,
    cursorStyle: "bar",
    disableStdin: true,
    scrollback: 10000,
    smoothScrollDuration: 100,
    fontSize: 13,
    fontFamily: '"Fira Code", "Cascadia Code", "JetBrains Mono", monospace',
    theme: THEME,
  });

  const fitAddon = new FitAddon();
  term.loadAddon(fitAddon);
  term.open(document.getElementById("terminal-container")!);
  loadingEl.remove();

  requestAnimationFrame(() => fitAddon.fit());
  window.addEventListener("resize", () => fitAddon.fit());
  try {
    window.Telegram?.WebApp?.onEvent("viewportChanged", () => fitAddon.fit());
  } catch {
    // ignore
  }

  term.writeln(
    `\x1b[1;34m● Tailing ${labels.tailPrefix} \x1b[1;37m${sourceId}\x1b[0m`
  );
  term.writeln("");

  const notice = (level: NoticeLevel, msg: string) => {
    term.writeln(`\x1b[${NOTICE_COLOURS[level]}m${msg}\x1b[0m`);
  };
  return {
    apply(data) {
      const text = "content" in data ? data.content : "text" in data ? data.text : "";
      if (text) term.write(text);
    },
    finish(completed) {
      term.writeln("");
      notice(
        completed ? "ok" : "warn",
        `● ${completed ? labels.completedMsg : labels.endedMsg}`
      );
    },
    notice,
  };
}

// ── Workflow mode ──

interface WorkflowAgent {
  agent_id: string;
  label: string;
  phase: string;
  state: "running" | "done" | "stopped";
}

// How often the agent list is re-read while the workflow runs.
const AGENT_POLL_MS = 3000;

const AGENT_ICONS: Record<WorkflowAgent["state"], string> = {
  running: "⏳",
  done: "✅",
  stopped: "⚠️",
};

const AGENT_LABELS: SourceLabels = {
  tailPrefix: "agent",
  completedMsg: "Agent finished.",
  endedMsg: "Agent output stream ended.",
};

/** Show a bar of the workflow's agents and follow whichever one is picked,
 * starting with the first.  The bar grows as the script starts agents. */
async function followWorkflow(
  view: TranscriptView,
  taskId: string
): Promise<void> {
  const bar = document.getElementById("agent-bar")!;
  bar.style.display = "flex";
  const sink = transcriptSink(view, AGENT_LABELS);

  let agents: WorkflowAgent[] = [];
  let selected: string | null = null;
  let tail: AbortController | null = null;

  async function follow(query: string, signal: AbortSignal): Promise<void> {
    let initial: ReadResult;
    try {
      initial = await readSource(query, signal);
    } catch (e) {
      if (!signal.aborted) view.notice("error", `${e}`);
      return;
    }
    if (signal.aborted) return;
    sink.apply(initial);
    await streamSource(sink, query, initial.size, signal);
  }

  function select(agent: WorkflowAgent): void {
    if (agent.agent_id === selected) return;
    selected = agent.agent_id;
    tail?.abort();
    tail = new AbortController();
    view.reset(agent.phase ? `${agent.label} · ${agent.phase}` : agent.label);
    const query =
      `type=workflow_agent&id=${encodeURIComponent(taskId)}` +
      `&agent=${encodeURIComponent(agent.agent_id)}`;
    void follow(query, tail.signal);
    renderBar();
    bar.querySelector(".selected")?.scrollIntoView({ inline: "nearest" });
  }

  function renderBar(): void {
    bar.replaceChildren(
      ...agents.map((agent) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className =
          "agent-chip" + (agent.agent_id === selected ? " selected" : "");
        chip.textContent = `${AGENT_ICONS[agent.state]} ${agent.label}`;
        chip.title = agent.phase;
        chip.onclick = () => select(agent);
        return chip;
      })
    );
  }

  view.reset("Workflow");
  view.notice("info", "Waiting for the workflow to start an agent...");

  while (true) {
    let active = false;
    try {
      const resp = await fetch(
        `/api/terminal/workflow?id=${encodeURIComponent(taskId)}`,
        { headers: getAuthHeader() }
      );
      if (!resp.ok) {
        if (!selected) {
          const err = await resp.text();
          view.notice("error", `Workflow error (${resp.status}): ${err}`);
        }
        return;
      }
      const data = (await resp.json()) as {
        active: boolean;
        agents: WorkflowAgent[];
      };
      active = data.active;
      agents = data.agents;
      const first = agents[0];
      if (!selected && first) {
        select(first);
      } else {
        renderBar();
      }
      if (!active && !agents.length) {
        view.setState("ended");
        view.notice("warn", "The workflow ended without starting an agent.");
      }
    } catch (e) {
      view.notice("error", `Workflow poll failed: ${e}`);
      return;
    }
    if (!active) return;
    await new Promise((r) => setTimeout(r, AGENT_POLL_MS));
  }
}

// ── Login mode ──

async function loginMain(): Promise<void> {
  showStatus("Initializing login...");

  try {
    window.Telegram?.WebApp?.ready();
    window.Telegram?.WebApp?.expand();
  } catch {
    // Not in Telegram.
  }

  showStatus("Loading xterm.js...");

  const { Terminal } = await import("@xterm/xterm");
  const { FitAddon } = await import("@xterm/addon-fit");

  showStatus("Creating terminal...");

  const container = document.getElementById("terminal-container")!;
  const authLinkBar = document.getElementById("login-auth-link")!;
  const authLink = document.getElementById("auth-link") as HTMLAnchorElement;

  injectBaseStyles();
  injectLoginStyles();

  const term = new Terminal({
    convertEol: true,
    cursorBlink: true,
    cursorStyle: "bar",
    disableStdin: false,
    scrollback: 5000,
    fontSize: 13,
    fontFamily: '"Fira Code", "Cascadia Code", "JetBrains Mono", monospace',
    theme: THEME,
  });

  const fitAddon = new FitAddon();
  term.loadAddon(fitAddon);

  showStatus("Connecting...");
  term.open(container);
  loadingEl.remove();

  requestAnimationFrame(() => fitAddon.fit());
  window.addEventListener("resize", () => fitAddon.fit());
  try {
    window.Telegram?.WebApp?.onEvent("viewportChanged", () => fitAddon.fit());
  } catch {
    // ignore
  }

  // ── WebSocket connection ──

  const tokenValue =
    window.Telegram?.WebApp?.initData ||
    params.get("token") ||
    "";
  const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${wsProto}//${location.host}/ws/terminal/login?token=${encodeURIComponent(tokenValue)}`;

  let loginDone = false;
  let authUrlFound = false;
  let outputBuffer = "";
  let ws: WebSocket | null = null;

  function connect(): void {
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      ws!.send(JSON.stringify({
        type: "resize",
        cols: term.cols,
        rows: term.rows,
      }));
    };

    ws.onmessage = (event) => {
      const data = event.data as string;
      term.write(data);
      outputBuffer += data;

      // Scrape the OAuth URL from TUI output and show as a tappable button.
      // The TUI wraps the long URL across multiple lines, so we strip all
      // ANSI escape sequences, control chars, and whitespace before matching.
      if (!authUrlFound) {
        const clean = outputBuffer
          // Strip ANSI escape sequences (CSI, OSC, etc.)
          .replace(/\x1b\[[0-9;]*[A-Za-z]/g, "")
          .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "")
          .replace(/\x1b[^[\]].?/g, "")
          // Strip all whitespace and control chars within URLs.
          // First, find the URL start, then reassemble.
          ;
        const urlStart = clean.indexOf("https://claude.ai/oauth/authorize");
        const urlStart2 = clean.indexOf("https://claude.com/cai/oauth/authorize");
        const urlStart3 = clean.indexOf("https://platform.claude.com/oauth/authorize");
        const start = [urlStart, urlStart2, urlStart3]
          .filter(i => i >= 0)
          .sort((a, b) => a - b)[0];
        if (start !== undefined) {
          // Extract from the start to the next non-URL character.
          // Remove any embedded whitespace/newlines (from terminal wrapping).
          const rest = clean.slice(start);
          const urlChars = rest.replace(/[\s\r\n]+/g, "");
          // Match the full URL (stops at first char that can't be in a URL).
          const urlMatch = urlChars.match(/^(https:\/\/[^\s"'<>]+)/);
          if (urlMatch && urlMatch[1] && urlMatch[1].includes("state=")) {
            authLink.href = urlMatch[1];
            authLinkBar.style.display = "flex";
            authUrlFound = true;
          }
        }
      }

      // Detect successful login — auto-close the mini app.
      if (data.includes("Login successful") || data.includes("login successful")) {
        loginDone = true;
        authLinkBar.style.display = "none";
        setTimeout(() => {
          try {
            window.Telegram?.WebApp?.close();
          } catch {
            // Not in Telegram.
          }
        }, 1500);
      }
    };

    ws.onclose = () => {
      ws = null;
      if (loginDone) {
        term.writeln("");
        term.writeln("\x1b[1;32m● Login complete.\x1b[0m");
        authLinkBar.style.display = "none";
      }
    };

    ws.onerror = () => {
      // Will trigger onclose.
    };
  }

  // Auto-reconnect when the page regains focus (user returns from browser).
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !ws && !loginDone) {
      term.writeln("\x1b[90mReconnecting...\x1b[0m");
      connect();
    }
  });

  connect();

  // Forward keyboard input to the PTY.
  term.onData((data) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "stdin", data }));
    }
  });

  term.onResize(({ cols, rows }) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resize", cols, rows }));
    }
  });
}

// ── Shared helpers ──

const THEME = {
  background: "#1a1b26",
  foreground: "#a9b1d6",
  cursor: "#a9b1d6",
  selectionBackground: "#33467c",
  black: "#32344a",
  red: "#f7768e",
  green: "#9ece6a",
  yellow: "#e0af68",
  blue: "#7aa2f7",
  magenta: "#ad8ee6",
  cyan: "#449dab",
  white: "#787c99",
  brightBlack: "#444b6a",
  brightRed: "#ff7a93",
  brightGreen: "#b9f27c",
  brightYellow: "#ff9e64",
  brightBlue: "#7da6ff",
  brightMagenta: "#bb9af7",
  brightCyan: "#0db9d7",
  brightWhite: "#acb0d0",
};

function injectBaseStyles(): void {
  const style = document.createElement("style");
  style.textContent = `
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body {
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: #1a1b26;
    }
    #terminal-container {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
    }
    #loading {
      position: fixed;
      top: 0; left: 0; right: 0;
      padding: 16px;
      color: #a9b1d6;
      background: #1a1b26;
      font-family: monospace;
      font-size: 13px;
      z-index: 9999;
    }
  `;
  document.head.appendChild(style);
}

function injectLoginStyles(): void {
  const style = document.createElement("style");
  style.textContent = `
    #terminal-container {
      bottom: 50px !important;
    }
    #login-auth-link {
      position: fixed;
      left: 0; right: 0; bottom: 0;
      height: 50px;
      display: none;
      align-items: center;
      justify-content: center;
      background: #24283b;
      border-top: 1px solid #414868;
      z-index: 100;
    }
    #auth-link {
      display: block;
      width: calc(100% - 24px);
      padding: 10px 0;
      text-align: center;
      background: #7aa2f7;
      color: #1a1b26;
      font-family: monospace;
      font-size: 14px;
      font-weight: bold;
      text-decoration: none;
      border-radius: 6px;
    }
    #auth-link:active {
      background: #5d8bdb;
    }
  `;
  document.head.appendChild(style);
}

function getAuthHeader(): Record<string, string> {
  const initData = window.Telegram?.WebApp?.initData;
  if (initData) {
    return { Authorization: `tg-init-data ${initData}` };
  }
  // Fallback: use HMAC token from URL (group chat / external browser).
  const token = new URLSearchParams(window.location.search).get("token");
  if (token) {
    return { Authorization: `tg-token ${token}` };
  }
  return {};
}

// ── Mode dispatch ──
//
// Last in the module: a mode may run synchronously up to its first await,
// and every module-level const it touches must be initialised by then.

if (params.get("mode") === "login") {
  loginMain().catch((e) => showError(`Fatal: ${e}`));
} else {
  tailMain().catch((e) => showError(`Fatal: ${e}`));
}
