import "@xterm/xterm/css/xterm.css";

// ── Globals ──

declare global {
  interface Window {
    Telegram?: {
      WebApp: {
        initData: string;
        close: () => void;
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

// ── Main (wrapped in try-catch for visible errors) ──

main().catch((e) => showError(`Fatal: ${e}`));

async function main(): Promise<void> {
  showStatus("Initializing...");

  // Telegram SDK
  try {
    window.Telegram?.WebApp?.ready();
    window.Telegram?.WebApp?.expand();
  } catch {
    // Not in Telegram.
  }

  const params = new URLSearchParams(window.location.search);
  const sourceType = params.get("type") ?? "task";
  const sourceId = params.get("id");
  const taskType = params.get("task_type");

  if (!sourceId) {
    showError("No id provided.");
    return;
  }

  // Build query string for API calls.
  let apiQuery = `type=${encodeURIComponent(sourceType)}&id=${encodeURIComponent(sourceId)}`;
  if (taskType) {
    apiQuery += `&task_type=${encodeURIComponent(taskType)}`;
  }

  const labels = getLabels(sourceType);

  showStatus(`Loading xterm.js...`);

  // Dynamic import so we can catch load errors.
  const { Terminal } = await import("@xterm/xterm");
  const { FitAddon } = await import("@xterm/addon-fit");

  showStatus("Creating terminal...");

  const container = document.getElementById("terminal-container")!;

  // Inject styles.
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

  const term = new Terminal({
    convertEol: true,
    cursorBlink: false,
    cursorStyle: "bar",
    disableStdin: true,
    scrollback: 10000,
    smoothScrollDuration: 100,
    fontSize: 13,
    fontFamily: '"Fira Code", "Cascadia Code", "JetBrains Mono", monospace',
    theme: {
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
    },
  });

  const fitAddon = new FitAddon();
  term.loadAddon(fitAddon);

  showStatus("Opening terminal...");
  term.open(container);

  // Remove loading indicator now that the terminal is open.
  loadingEl.remove();

  requestAnimationFrame(() => fitAddon.fit());
  window.addEventListener("resize", () => fitAddon.fit());

  try {
    window.Telegram?.WebApp?.onEvent("viewportChanged", () => {
      fitAddon.fit();
    });
  } catch {
    // ignore
  }

  // ── Start tailing ──

  term.writeln(
    `\x1b[1;34m● Tailing ${labels.tailPrefix} \x1b[1;37m${sourceId}\x1b[0m`
  );
  term.writeln("");

  // Read existing content.
  let offset = 0;
  try {
    const readResp = await fetch(
      `/api/terminal/read?${apiQuery}`,
      { headers: getAuthHeader() }
    );
    if (readResp.ok) {
      const data = (await readResp.json()) as {
        content: string;
        size: number;
      };
      if (data.content) {
        term.write(data.content);
        offset = data.size;
      }
    } else {
      const err = await readResp.text();
      term.writeln(`\x1b[31mRead error (${readResp.status}): ${err}\x1b[0m`);
    }
  } catch (e) {
    term.writeln(`\x1b[31mRead failed: ${e}\x1b[0m`);
  }

  // Stream new output via fetch-based SSE.
  const url = `/api/terminal/tail?${apiQuery}&offset=${offset}`;

  try {
    const resp = await fetch(url, {
      headers: {
        ...getAuthHeader(),
        Accept: "text/event-stream",
      },
    });

    if (!resp.ok) {
      const text = await resp.text();
      term.writeln(`\x1b[31mStream error (${resp.status}): ${text}\x1b[0m`);
      return;
    }

    const reader = resp.body?.getReader();
    if (!reader) {
      term.writeln("\x1b[31mStreaming not supported.\x1b[0m");
      return;
    }

    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (line.startsWith("data: ")) {
          try {
            const event = JSON.parse(line.slice(6)) as {
              text?: string;
              offset?: number;
            };
            if (event.text) {
              term.write(event.text);
            }
          } catch {
            // Ignore malformed JSON.
          }
        } else if (line.startsWith("event: done")) {
          // Next "data:" line carries the done payload.
          const dataLine = lines.find(
            (l, j) => j > lines.indexOf(line) && l.startsWith("data: ")
          );
          let completed = false;
          if (dataLine) {
            try {
              const d = JSON.parse(dataLine.slice(6)) as {
                completed?: boolean;
              };
              completed = !!d.completed;
            } catch {
              // ignore
            }
          }
          term.writeln("");
          if (completed) {
            term.writeln(`\x1b[1;32m● ${labels.completedMsg}\x1b[0m`);
          } else {
            term.writeln(
              `\x1b[1;33m● ${labels.endedMsg}\x1b[0m`
            );
          }
          return;
        }
      }
    }

    term.writeln("");
    term.writeln("\x1b[1;33m● Connection closed.\x1b[0m");
  } catch (e) {
    term.writeln(`\x1b[31mStream error: ${e}\x1b[0m`);
  }
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
