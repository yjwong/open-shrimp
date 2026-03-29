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
    buildToolbar(toolbarEl, rfb, isMobile);
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

// ── Toolbar ──

function buildToolbar(
  toolbar: HTMLElement,
  rfb: RFBType,
  isMobile: boolean,
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
