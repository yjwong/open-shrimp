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

// ── Pinch-to-zoom ──

function setupPinchZoom(
  container: HTMLElement,
  rfb: RFBType,
): {
  reset: () => void;
  getScale: () => number;
} {
  let scale = 1;
  let translateX = 0;
  let translateY = 0;

  // Pinch tracking state.
  let initialPinchDistance = 0;
  let initialScale = 1;
  let pinchMidX = 0;
  let pinchMidY = 0;
  let initialTranslateX = 0;
  let initialTranslateY = 0;

  const MIN_SCALE = 1;
  const MAX_SCALE = 5;

  function applyTransform(): void {
    // Clamp translation so we don't pan outside the content.
    if (scale <= 1) {
      translateX = 0;
      translateY = 0;
    } else {
      // Use the container's own dimensions (CSS layout size, unaffected
      // by our transform since transform doesn't change layout).
      const w = container.offsetWidth;
      const h = container.offsetHeight;
      const maxTx = (w * (scale - 1)) / 2;
      const maxTy = (h * (scale - 1)) / 2;
      translateX = Math.max(-maxTx, Math.min(maxTx, translateX));
      translateY = Math.max(-maxTy, Math.min(maxTy, translateY));
    }
    container.style.transformOrigin = "center center";
    container.style.transform =
      `translate(${translateX}px, ${translateY}px) scale(${scale})`;
  }

  // Patch noVNC's display coordinate conversion to account for our CSS
  // scale. The chain is: clientToElement (uses getBoundingClientRect on
  // canvas) -> absX/absY (divides by display._scale). With our CSS
  // transform, getBoundingClientRect returns the *transformed* rect, so
  // element-relative coordinates from clientToElement are scaled by our
  // CSS factor. We patch absX/absY to divide that out.
  const display = (rfb as unknown as { _display: {
    absX: (x: number) => number;
    absY: (y: number) => number;
  } })._display;
  const origAbsX = display.absX.bind(display);
  const origAbsY = display.absY.bind(display);
  display.absX = (x: number) => origAbsX(x / scale);
  display.absY = (y: number) => origAbsY(y / scale);

  function getTouchDistance(t1: Touch, t2: Touch): number {
    const dx = t1.clientX - t2.clientX;
    const dy = t1.clientY - t2.clientY;
    return Math.sqrt(dx * dx + dy * dy);
  }

  container.addEventListener(
    "touchstart",
    (e: TouchEvent) => {
      if (e.touches.length === 2) {
        e.preventDefault();
        const t1 = e.touches[0]!;
        const t2 = e.touches[1]!;
        initialPinchDistance = getTouchDistance(t1, t2);
        initialScale = scale;
        pinchMidX = (t1.clientX + t2.clientX) / 2;
        pinchMidY = (t1.clientY + t2.clientY) / 2;
        initialTranslateX = translateX;
        initialTranslateY = translateY;
      }
    },
    { passive: false },
  );

  container.addEventListener(
    "touchmove",
    (e: TouchEvent) => {
      if (e.touches.length === 2) {
        e.preventDefault();
        const t1 = e.touches[0]!;
        const t2 = e.touches[1]!;
        const currentDistance = getTouchDistance(t1, t2);
        const ratio = currentDistance / initialPinchDistance;
        scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, initialScale * ratio));

        // Pan: track how the midpoint has moved.
        const currentMidX = (t1.clientX + t2.clientX) / 2;
        const currentMidY = (t1.clientY + t2.clientY) / 2;
        translateX = initialTranslateX + (currentMidX - pinchMidX);
        translateY = initialTranslateY + (currentMidY - pinchMidY);

        applyTransform();
      }
    },
    { passive: false },
  );

  // Allow single-finger pan when zoomed in.
  let panStartX = 0;
  let panStartY = 0;
  let panInitialTx = 0;
  let panInitialTy = 0;
  let isPanning = false;

  container.addEventListener(
    "touchstart",
    (e: TouchEvent) => {
      if (e.touches.length === 1 && scale > 1) {
        isPanning = true;
        panStartX = e.touches[0]!.clientX;
        panStartY = e.touches[0]!.clientY;
        panInitialTx = translateX;
        panInitialTy = translateY;
      }
    },
    { passive: true },
  );

  container.addEventListener(
    "touchmove",
    (e: TouchEvent) => {
      if (e.touches.length === 1 && isPanning && scale > 1) {
        e.preventDefault();
        translateX = panInitialTx + (e.touches[0]!.clientX - panStartX);
        translateY = panInitialTy + (e.touches[0]!.clientY - panStartY);
        applyTransform();
      }
    },
    { passive: false },
  );

  container.addEventListener("touchend", () => {
    isPanning = false;
  });

  return {
    reset() {
      scale = 1;
      translateX = 0;
      translateY = 0;
      applyTransform();
    },
    getScale: () => scale,
  };
}

// ── Clipboard ──

/** Clipboard helpers that use the server-side wl-clipboard API. */
function setupClipboard(context: string, authToken: string, rfb: RFBType): {
  copyToLocal: () => Promise<void>;
  pasteFromLocal: () => void;
} {
  const clipboardUrl =
    `${window.location.origin}/api/vnc/clipboard` +
    `?context=${encodeURIComponent(context)}` +
    `&token=${encodeURIComponent(authToken)}`;

  async function sendToRemote(text: string): Promise<void> {
    await fetch(clipboardUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
  }

  // Reusable paste modal — shown when the paste button is clicked.
  let modal: HTMLElement | null = null;

  function showPasteModal(): void {
    if (modal) return;

    modal = document.createElement("div");
    modal.className = "paste-modal-overlay";

    const box = document.createElement("div");
    box.className = "paste-modal";

    const label = document.createElement("div");
    label.className = "paste-modal-label";
    label.textContent = "Paste text to send to VM";
    box.appendChild(label);

    const textarea = document.createElement("textarea");
    textarea.className = "paste-modal-input";
    textarea.placeholder = "Long-press and paste here...";
    textarea.rows = 4;
    box.appendChild(textarea);

    const btnRow = document.createElement("div");
    btnRow.className = "paste-modal-buttons";

    const cancelBtn = document.createElement("button");
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", closePasteModal);
    btnRow.appendChild(cancelBtn);

    const sendBtn = document.createElement("button");
    sendBtn.textContent = "Send";
    sendBtn.classList.add("primary");
    sendBtn.addEventListener("click", () => {
      const text = textarea.value;
      closePasteModal();
      if (text) {
        sendToRemote(text);
        for (const ch of text) {
          rfb.sendKey(charToKeysym(ch), null);
        }
      }
    });
    btnRow.appendChild(sendBtn);

    box.appendChild(btnRow);
    modal.appendChild(box);

    // Close on overlay click (outside the box).
    modal.addEventListener("click", (e) => {
      if (e.target === modal) closePasteModal();
    });

    document.body.appendChild(modal);
    textarea.focus();
  }

  function closePasteModal(): void {
    if (modal) {
      modal.remove();
      modal = null;
    }
  }

  return {
    async copyToLocal() {
      try {
        const resp = await fetch(clipboardUrl);
        if (!resp.ok) return;
        const { text } = await resp.json();
        if (text) {
          await navigator.clipboard.writeText(text);
        }
      } catch {
        // Clipboard API or fetch may fail silently.
      }
    },
    pasteFromLocal() {
      showPasteModal();
    },
  };
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

  // Auth token: prefer Telegram initData, fall back to HMAC token from URL.
  const initData = window.Telegram?.WebApp?.initData ?? "";
  const authToken =
    initData || new URLSearchParams(window.location.search).get("token") || "";

  // Build WebSocket URL.
  const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl =
    `${wsProto}//${window.location.host}/api/vnc/ws` +
    `?context=${encodeURIComponent(context)}` +
    `&token=${encodeURIComponent(authToken)}`;

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
  rfb.dragViewport = false; // We handle pan via pinch-zoom instead.
  rfb.viewOnly = isMobile;
  rfb.qualityLevel = isMobile ? 4 : 6;
  rfb.compressionLevel = isMobile ? 6 : 2;
  rfb.background = "#1a1b26";

  // Set up pinch-to-zoom on mobile.
  const pinchZoom = isMobile ? setupPinchZoom(container, rfb) : null;

  // ── Events ──

  const clipboard = setupClipboard(context, authToken, rfb);

  rfb.addEventListener("connect", () => {
    loadingEl.remove();
    buildToolbar(toolbarEl, rfb, isMobile, context, authToken, pinchZoom, clipboard);
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

// ── Text-input state SSE stream ──

function startTextInputSSE(
  context: string,
  initData: string,
  rfb: RFBType,
  keyboard: ReturnType<typeof setupKeyboardInput>,
  kbdBtn: HTMLButtonElement,
): () => void {
  let lastActive = false;
  const abortController = new AbortController();

  const applyState = (active: boolean) => {
    if (active === lastActive) return;
    lastActive = active;
    if (active && !rfb.viewOnly) {
      keyboard.show();
      kbdBtn.classList.add("active");
    } else {
      keyboard.hide();
      kbdBtn.classList.remove("active");
    }
  };

  const connect = async () => {
    const url =
      `/api/vnc/text-input-state/stream` +
      `?context=${encodeURIComponent(context)}` +
      `&token=${encodeURIComponent(initData)}`;

    try {
      const resp = await fetch(url, {
        headers: { Accept: "text/event-stream" },
        signal: abortController.signal,
      });
      if (!resp.ok || !resp.body) return;

      const reader = resp.body.getReader();
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
              const data = JSON.parse(line.slice(6)) as { active: boolean };
              applyState(data.active);
            } catch {
              /* ignore malformed */
            }
          }
        }
      }
    } catch {
      if (abortController.signal.aborted) return;
    }

    // Reconnect after a short delay on disconnect.
    if (!abortController.signal.aborted) {
      setTimeout(connect, 2000);
    }
  };

  connect();
  return () => abortController.abort();
}

// ── Toolbar ──

function buildToolbar(
  toolbar: HTMLElement,
  rfb: RFBType,
  isMobile: boolean,
  context: string,
  initData: string,
  pinchZoom: { reset: () => void; getScale: () => number } | null,
  clipboard: { copyToLocal: () => Promise<void>; pasteFromLocal: () => void },
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
    .paste-modal-overlay {
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0, 0, 0, 0.6);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 10000;
      padding: 16px;
    }
    .paste-modal {
      background: #24283b;
      border-radius: 8px;
      padding: 16px;
      width: 100%;
      max-width: 360px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .paste-modal-label {
      color: #a9b1d6;
      font-family: monospace;
      font-size: 13px;
    }
    .paste-modal-input {
      background: #1a1b26;
      color: #a9b1d6;
      border: 1px solid #414868;
      border-radius: 4px;
      padding: 8px;
      font-family: monospace;
      font-size: 13px;
      resize: vertical;
      outline: none;
    }
    .paste-modal-input:focus {
      border-color: #7aa2f7;
    }
    .paste-modal-buttons {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
    }
    .paste-modal-buttons button {
      background: #414868;
      color: #a9b1d6;
      border: none;
      border-radius: 4px;
      padding: 6px 14px;
      font-size: 12px;
      font-family: monospace;
      cursor: pointer;
    }
    .paste-modal-buttons button:hover {
      background: #565f89;
    }
    .paste-modal-buttons button.primary {
      background: #7aa2f7;
      color: #1a1b26;
    }
    .paste-modal-buttons button.primary:hover {
      background: #89b4fa;
    }
  `;
  document.head.appendChild(style);

  // View-only toggle.
  const viewOnlyBtn = document.createElement("button");
  viewOnlyBtn.textContent = rfb.viewOnly ? "View only" : "Interactive";
  if (!rfb.viewOnly) viewOnlyBtn.classList.add("active");
  // Buttons to hide/show based on interactive mode.
  const interactiveOnly: HTMLElement[] = [];
  function updateInteractiveButtons(): void {
    for (const btn of interactiveOnly) {
      btn.style.display = rfb.viewOnly ? "none" : "";
    }
  }
  viewOnlyBtn.addEventListener("click", () => {
    rfb.viewOnly = !rfb.viewOnly;
    viewOnlyBtn.textContent = rfb.viewOnly ? "View only" : "Interactive";
    viewOnlyBtn.classList.toggle("active", !rfb.viewOnly);
    updateInteractiveButtons();
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

  // Clipboard buttons.
  const copyBtn = document.createElement("button");
  copyBtn.textContent = "Copy";
  copyBtn.addEventListener("click", () => {
    clipboard.copyToLocal();
  });
  toolbar.appendChild(copyBtn);

  const pasteBtn = document.createElement("button");
  pasteBtn.textContent = "Paste";
  pasteBtn.addEventListener("click", () => {
    clipboard.pasteFromLocal();
  });
  interactiveOnly.push(pasteBtn);
  toolbar.appendChild(pasteBtn);

  updateInteractiveButtons();

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

    // Stream text-input state to auto-show/hide keyboard.
    startTextInputSSE(context, initData, rfb, keyboard, kbdBtn);
  }

  // Zoom reset button (mobile only).
  if (isMobile && pinchZoom) {
    const zoomBtn = document.createElement("button");
    zoomBtn.textContent = "1:1";
    zoomBtn.addEventListener("click", () => {
      pinchZoom.reset();
    });
    toolbar.appendChild(zoomBtn);
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
