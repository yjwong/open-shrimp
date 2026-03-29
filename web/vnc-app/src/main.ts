import type RFBType from "@novnc/novnc";

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

// ── X11 keysym helpers ──

// Common special keys: DOM key name -> X11 keysym.
const SPECIAL_KEYSYMS: Record<string, number> = {
  Backspace: 0xff08,
  Tab: 0xff09,
  Enter: 0xff0d,
  Escape: 0xff1b,
  Delete: 0xffff,
  Home: 0xff50,
  End: 0xff57,
  ArrowLeft: 0xff51,
  ArrowUp: 0xff52,
  ArrowRight: 0xff53,
  ArrowDown: 0xff54,
};

/** Convert a single character to an X11 keysym. */
function charToKeysym(ch: string): number {
  const cp = ch.codePointAt(0)!;
  // Latin-1 maps directly.
  if (cp >= 0x20 && cp <= 0xff) return cp;
  // Unicode BMP and beyond: X11 convention is 0x01000000 | codepoint.
  return 0x01000000 | cp;
}

// ── Main ──

main().catch((e) => showError(`Fatal: ${e}`));

async function main(): Promise<void> {
  showStatus("Connecting to desktop...");

  // Telegram SDK
  try {
    window.Telegram?.WebApp?.ready();
    window.Telegram?.WebApp?.expand();
  } catch {
    // Not in Telegram.
  }

  const params = new URLSearchParams(window.location.search);
  const context = params.get("context");

  if (!context) {
    showError("No context provided.");
    return;
  }

  // Auth token from Telegram initData.
  const initData = window.Telegram?.WebApp?.initData ?? "";

  // Build WebSocket URL.
  const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl =
    `${wsProto}//${window.location.host}/api/vnc/ws` +
    `?context=${encodeURIComponent(context)}` +
    `&token=${encodeURIComponent(initData)}`;

  showStatus("Loading noVNC...");

  const { default: RFB } = await import("@novnc/novnc");

  showStatus("Connecting...");

  const container = document.getElementById("vnc-container")!;
  const toolbarEl = document.getElementById("toolbar")!;

  // Detect mobile.
  const isMobile =
    navigator.maxTouchPoints > 0 || window.innerWidth < 768;

  const rfb = new RFB(container, wsUrl);
  rfb.scaleViewport = true;
  rfb.clipViewport = isMobile;
  rfb.dragViewport = isMobile;
  rfb.viewOnly = isMobile;
  rfb.qualityLevel = isMobile ? 4 : 6;
  rfb.compressionLevel = isMobile ? 6 : 2;
  rfb.background = "#1a1b26";

  // ── Events ──

  rfb.addEventListener("connect", () => {
    loadingEl.remove();
    buildToolbar(toolbarEl, rfb, isMobile, context, initData);
  });

  rfb.addEventListener("disconnect", (ev: Event) => {
    const detail = (ev as CustomEvent).detail as
      | { clean: boolean }
      | undefined;
    if (detail?.clean) {
      showStatus("Disconnected.");
    } else {
      showError("Connection lost.");
    }
    // Show loading overlay again.
    if (!document.getElementById("loading")) {
      document.body.appendChild(loadingEl);
    }
  });

  rfb.addEventListener("credentialsrequired", () => {
    showError("VNC server requires credentials (not supported).");
    rfb.disconnect();
  });
}

// ── Mobile keyboard input via hidden textarea ──

function setupKeyboardInput(rfb: RFBType): {
  show: () => void;
  hide: () => void;
  isVisible: () => boolean;
} {
  const textarea = document.createElement("textarea");
  textarea.autocapitalize = "off";
  textarea.setAttribute("autocorrect", "off");
  textarea.setAttribute("autocomplete", "off");
  textarea.spellcheck = false;
  // Position offscreen but keep it focusable.
  Object.assign(textarea.style, {
    position: "fixed",
    left: "-9999px",
    top: "50%",
    width: "1px",
    height: "1px",
    opacity: "0",
    fontSize: "16px", // Prevent iOS zoom on focus.
  });
  document.body.appendChild(textarea);

  let visible = false;

  // Handle text input from the mobile keyboard.
  textarea.addEventListener("input", () => {
    const text = textarea.value;
    textarea.value = "";
    if (rfb.viewOnly) return;
    for (const ch of text) {
      rfb.sendKey(charToKeysym(ch), null);
    }
  });

  // Handle special keys (Backspace, Enter, etc.).
  textarea.addEventListener("keydown", (e: KeyboardEvent) => {
    const keysym = SPECIAL_KEYSYMS[e.key];
    if (keysym && !rfb.viewOnly) {
      e.preventDefault();
      rfb.sendKey(keysym, null);
    }
  });

  return {
    show() {
      visible = true;
      textarea.focus();
    },
    hide() {
      visible = false;
      textarea.blur();
    },
    isVisible: () => visible,
  };
}

// ── Text-input state polling ──

function startTextInputPolling(
  context: string,
  initData: string,
  rfb: RFBType,
  keyboard: ReturnType<typeof setupKeyboardInput>,
  kbdBtn: HTMLButtonElement,
): () => void {
  let lastActive = false;
  const url =
    `/api/vnc/text-input-state` +
    `?context=${encodeURIComponent(context)}` +
    `&token=${encodeURIComponent(initData)}`;

  const poll = async () => {
    try {
      const resp = await fetch(url);
      if (!resp.ok) return;
      const data = (await resp.json()) as { active: boolean };
      if (data.active !== lastActive) {
        lastActive = data.active;
        if (data.active && !rfb.viewOnly) {
          keyboard.show();
          kbdBtn.classList.add("active");
        } else {
          keyboard.hide();
          kbdBtn.classList.remove("active");
        }
      }
    } catch {
      // Ignore transient fetch errors.
    }
  };

  const intervalId = setInterval(poll, 1500);
  poll(); // Initial check.
  return () => clearInterval(intervalId);
}

// ── Toolbar ──

function buildToolbar(
  toolbar: HTMLElement,
  rfb: RFBType,
  isMobile: boolean,
  context: string,
  initData: string,
): void {
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
    #vnc-container {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 36px;
    }
    #toolbar {
      position: fixed;
      left: 0;
      right: 0;
      bottom: 0;
      height: 36px;
      background: #24283b;
      display: flex;
      align-items: center;
      padding: 0 8px;
      gap: 8px;
      z-index: 100;
    }
    #toolbar button {
      background: #414868;
      color: #a9b1d6;
      border: none;
      border-radius: 4px;
      padding: 4px 10px;
      font-size: 12px;
      font-family: monospace;
      cursor: pointer;
      white-space: nowrap;
    }
    #toolbar button:hover {
      background: #565f89;
    }
    #toolbar button.active {
      background: #7aa2f7;
      color: #1a1b26;
    }
    #toolbar .spacer {
      flex: 1;
    }
    #toolbar .status {
      color: #565f89;
      font-size: 11px;
      font-family: monospace;
    }
    #loading {
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #a9b1d6;
      background: #1a1b26;
      font-family: monospace;
      font-size: 13px;
      z-index: 9999;
    }
  `;
  document.head.appendChild(style);

  // View-only toggle.
  const viewOnlyBtn = document.createElement("button");
  viewOnlyBtn.textContent = rfb.viewOnly ? "View only" : "Interactive";
  if (!rfb.viewOnly) viewOnlyBtn.classList.add("active");
  viewOnlyBtn.addEventListener("click", () => {
    rfb.viewOnly = !rfb.viewOnly;
    viewOnlyBtn.textContent = rfb.viewOnly ? "View only" : "Interactive";
    viewOnlyBtn.classList.toggle("active", !rfb.viewOnly);
  });
  toolbar.appendChild(viewOnlyBtn);

  // Quality toggle.
  const qualityBtn = document.createElement("button");
  const qualities = [
    { label: "Low", q: 2, c: 8 },
    { label: "Med", q: 5, c: 4 },
    { label: "High", q: 8, c: 1 },
  ] as const;
  let qualityIdx = isMobile ? 0 : 1;
  function updateQuality(): void {
    const preset = qualities[qualityIdx]!;
    qualityBtn.textContent = `Q: ${preset.label}`;
    rfb.qualityLevel = preset.q;
    rfb.compressionLevel = preset.c;
  }
  updateQuality();
  qualityBtn.addEventListener("click", () => {
    qualityIdx = (qualityIdx + 1) % qualities.length;
    updateQuality();
  });
  toolbar.appendChild(qualityBtn);

  // Keyboard toggle (mobile only).
  if (isMobile) {
    const keyboard = setupKeyboardInput(rfb);
    const kbdBtn = document.createElement("button");
    kbdBtn.textContent = "Kbd";
    kbdBtn.addEventListener("click", () => {
      if (keyboard.isVisible()) {
        keyboard.hide();
        kbdBtn.classList.remove("active");
      } else {
        // Ensure interactive mode when opening keyboard.
        if (rfb.viewOnly) {
          rfb.viewOnly = false;
          viewOnlyBtn.textContent = "Interactive";
          viewOnlyBtn.classList.add("active");
        }
        keyboard.show();
        kbdBtn.classList.add("active");
      }
    });
    toolbar.appendChild(kbdBtn);

    // Start polling for text-input state to auto-show/hide keyboard.
    startTextInputPolling(context, initData, rfb, keyboard, kbdBtn);
  }

  // Spacer.
  const spacer = document.createElement("div");
  spacer.className = "spacer";
  toolbar.appendChild(spacer);

  // Status indicator.
  const statusEl = document.createElement("span");
  statusEl.className = "status";
  statusEl.textContent = "Connected";
  toolbar.appendChild(statusEl);
}
