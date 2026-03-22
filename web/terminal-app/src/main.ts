import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { Unicode11Addon } from "@xterm/addon-unicode11";
import "@xterm/xterm/css/xterm.css";

// ── Telegram WebApp auth ──

declare global {
  interface Window {
    Telegram?: {
      WebApp: {
        initData: string;
        close: () => void;
        ready: () => void;
        expand: () => void;
      };
    };
  }
}

function getAuthHeader(): Record<string, string> {
  const initData = window.Telegram?.WebApp?.initData;
  if (!initData) {
    return {};
  }
  return { Authorization: `tg-init-data ${initData}` };
}

// ── Parse query params ──

const params = new URLSearchParams(window.location.search);
const taskId = params.get("task_id");

// ── Setup ──

const container = document.getElementById("terminal-container")!;

document.body.style.margin = "0";
document.body.style.padding = "0";
document.body.style.overflow = "hidden";
document.body.style.backgroundColor = "#1a1b26";
container.style.width = "100vw";
container.style.height = "100vh";

try {
  window.Telegram?.WebApp?.ready();
  window.Telegram?.WebApp?.expand();
} catch {
  // Not in Telegram.
}

// ── Terminal setup ──

const term = new Terminal({
  cursorBlink: false,
  cursorStyle: "bar",
  disableStdin: true,
  scrollback: 10000,
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

const unicode11Addon = new Unicode11Addon();
term.loadAddon(unicode11Addon);
term.unicode.activeVersion = "11";

term.open(container);
fitAddon.fit();

window.addEventListener("resize", () => fitAddon.fit());

// ── Main ──

if (!taskId) {
  term.writeln("\x1b[31mNo task_id provided.\x1b[0m");
} else {
  startTailing(taskId);
}

async function startTailing(id: string): Promise<void> {
  term.writeln(
    `\x1b[1;34m● Tailing task \x1b[1;37m${id}\x1b[0m`
  );
  term.writeln("");

  // First, read existing content in one shot.
  let offset = 0;
  try {
    const readResp = await fetch(
      `/api/terminal/read?task_id=${encodeURIComponent(id)}`,
      { headers: getAuthHeader() }
    );
    if (readResp.ok) {
      const readData = (await readResp.json()) as {
        content: string;
        size: number;
      };
      if (readData.content) {
        term.write(readData.content);
        offset = readData.size;
      }
    }
  } catch {
    // Fall through to SSE which will read from beginning.
  }

  // Stream new output via SSE (using fetch for auth headers).
  const url = `/api/terminal/tail?task_id=${encodeURIComponent(id)}&offset=${offset}`;

  try {
    const resp = await fetch(url, {
      headers: {
        ...getAuthHeader(),
        Accept: "text/event-stream",
      },
    });

    if (!resp.ok) {
      const text = await resp.text();
      term.writeln(`\x1b[31mError: ${text}\x1b[0m`);
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

      // Parse SSE events from buffer.
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
          term.writeln("");
          term.writeln(
            "\x1b[1;32m● Task output stream ended.\x1b[0m"
          );
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
