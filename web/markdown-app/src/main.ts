import "highlight.js/styles/github-dark.min.css";

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
        themeParams: {
          bg_color?: string;
          text_color?: string;
          hint_color?: string;
          link_color?: string;
          button_color?: string;
          button_text_color?: string;
          secondary_bg_color?: string;
        };
      };
    };
  }
}

const loadingEl = document.getElementById("loading")!;
const contentEl = document.getElementById("content")!;

function showError(msg: string): void {
  loadingEl.style.color = "#f7768e";
  loadingEl.textContent = msg;
}

// ── Main ──

main().catch((e) => showError(`Fatal: ${e}`));

async function main(): Promise<void> {
  // Telegram SDK
  try {
    window.Telegram?.WebApp?.ready();
    window.Telegram?.WebApp?.expand();
  } catch {
    // Not in Telegram.
  }

  const params = new URLSearchParams(window.location.search);
  const filePath = params.get("path");

  if (!filePath) {
    showError("No path provided.");
    return;
  }

  // Dynamic imports for code splitting.
  const [{ Marked }, { markedHighlight }, hljs] = await Promise.all([
    import("marked"),
    import("marked-highlight"),
    import("highlight.js/lib/core"),
  ]);

  // Register common languages on demand.
  const langModules = await Promise.all([
    import("highlight.js/lib/languages/javascript"),
    import("highlight.js/lib/languages/typescript"),
    import("highlight.js/lib/languages/python"),
    import("highlight.js/lib/languages/bash"),
    import("highlight.js/lib/languages/json"),
    import("highlight.js/lib/languages/yaml"),
    import("highlight.js/lib/languages/xml"),
    import("highlight.js/lib/languages/css"),
    import("highlight.js/lib/languages/sql"),
    import("highlight.js/lib/languages/go"),
    import("highlight.js/lib/languages/rust"),
    import("highlight.js/lib/languages/java"),
    import("highlight.js/lib/languages/c"),
    import("highlight.js/lib/languages/cpp"),
    import("highlight.js/lib/languages/diff"),
    import("highlight.js/lib/languages/markdown"),
    import("highlight.js/lib/languages/shell"),
    import("highlight.js/lib/languages/dockerfile"),
    import("highlight.js/lib/languages/ini"),
  ]);

  const langNames = [
    "javascript", "typescript", "python", "bash", "json", "yaml",
    "xml", "css", "sql", "go", "rust", "java", "c", "cpp", "diff",
    "markdown", "shell", "dockerfile", "ini",
  ];
  langModules.forEach((mod, i) => {
    hljs.default.registerLanguage(langNames[i]!, mod.default);
  });

  // Aliases.
  hljs.default.registerLanguage("js", langModules[0]!.default);
  hljs.default.registerLanguage("ts", langModules[1]!.default);
  hljs.default.registerLanguage("py", langModules[2]!.default);
  hljs.default.registerLanguage("sh", langModules[3]!.default);
  hljs.default.registerLanguage("zsh", langModules[3]!.default);
  hljs.default.registerLanguage("fish", langModules[3]!.default);
  hljs.default.registerLanguage("html", langModules[6]!.default);
  hljs.default.registerLanguage("yml", langModules[5]!.default);
  hljs.default.registerLanguage("toml", langModules[18]!.default);

  const mermaidExtension: import("marked").MarkedExtension = {
    renderer: {
      code({ text, lang }: { text: string; lang?: string }) {
        if (lang === "mermaid") {
          return `<pre class="mermaid">${text}</pre>`;
        }
        return false; // Fall through to default renderer.
      },
    },
  };

  const marked = new Marked(
    markedHighlight({
      langPrefix: "hljs language-",
      highlight(code: string, lang: string) {
        if (lang === "mermaid") return code; // Handled by mermaid extension.
        if (lang && hljs.default.getLanguage(lang)) {
          return hljs.default.highlight(code, { language: lang }).value;
        }
        return hljs.default.highlightAuto(code).value;
      },
    }),
    mermaidExtension
  );

  // Fetch the markdown content.
  const resp = await fetch(
    `/api/preview/read?path=${encodeURIComponent(filePath)}`,
    { headers: getAuthHeader() }
  );

  if (!resp.ok) {
    const err = await resp.text();
    showError(`Error (${resp.status}): ${err}`);
    return;
  }

  const data = (await resp.json()) as {
    path: string;
    filename: string;
    content: string;
  };

  // Render markdown.
  const html = await marked.parse(data.content);

  // Inject styles.
  const style = document.createElement("style");
  style.textContent = getStyles();
  document.head.appendChild(style);

  // Show rendered content.
  loadingEl.remove();
  contentEl.innerHTML = html;

  // Render mermaid diagrams (lazy-loaded only when needed).
  if (contentEl.querySelector(".mermaid")) {
    const mermaid = (await import("mermaid")).default;
    const tp = window.Telegram?.WebApp?.themeParams;
    mermaid.initialize({
      startOnLoad: false,
      theme: "dark",
      themeVariables: tp ? {
        primaryColor: tp.button_color ?? "#7aa2f7",
        primaryTextColor: tp.button_text_color ?? "#c9d1d9",
        lineColor: tp.hint_color ?? "#8b949e",
        secondaryColor: tp.secondary_bg_color ?? "#161b22",
        tertiaryColor: tp.bg_color ?? "#1a1b26",
      } : undefined,
    });
    await mermaid.run({ nodes: contentEl.querySelectorAll(".mermaid") });

    // Wrap each rendered diagram with a container and fullscreen button.
    contentEl.querySelectorAll<HTMLPreElement>("pre.mermaid").forEach((pre) => {
      const wrapper = document.createElement("div");
      wrapper.className = "mermaid-wrapper";

      const btn = document.createElement("button");
      btn.className = "mermaid-fullscreen-btn";
      btn.textContent = "Fullscreen";
      btn.addEventListener("click", () => openMermaidFullscreen(pre));

      pre.parentNode!.insertBefore(wrapper, pre);
      wrapper.appendChild(pre);
      wrapper.appendChild(btn);
    });
  }

  // Add copy-as-markdown button.
  const copyBtn = document.createElement("button");
  copyBtn.id = "copy-md-btn";
  copyBtn.textContent = "Copy as Markdown";
  copyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(data.content);
      copyBtn.textContent = "Copied!";
      setTimeout(() => { copyBtn.textContent = "Copy as Markdown"; }, 1500);
    } catch {
      // Fallback for environments without clipboard API.
      const ta = document.createElement("textarea");
      ta.value = data.content;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      copyBtn.textContent = "Copied!";
      setTimeout(() => { copyBtn.textContent = "Copy as Markdown"; }, 1500);
    }
  });
  document.body.appendChild(copyBtn);

  // Set page title.
  document.title = data.filename;
}

function openMermaidFullscreen(pre: HTMLPreElement): void {
  const svg = pre.querySelector("svg");
  if (!svg) return;

  const overlay = document.createElement("div");
  overlay.className = "mermaid-overlay";

  const viewport = document.createElement("div");
  viewport.className = "mermaid-viewport";

  const container = document.createElement("div");
  container.className = "mermaid-zoom-container";
  container.innerHTML = svg.outerHTML;

  const closeBtn = document.createElement("button");
  closeBtn.className = "mermaid-close-btn";
  closeBtn.textContent = "\u00d7";

  viewport.appendChild(container);
  overlay.appendChild(viewport);
  overlay.appendChild(closeBtn);
  document.body.appendChild(overlay);

  // Zoom/pan state.
  let scale = 1;
  let translateX = 0;
  let translateY = 0;
  let startDist = 0;
  let startScale = 1;
  let isPanning = false;
  let panStartX = 0;
  let panStartY = 0;
  let startTranslateX = 0;
  let startTranslateY = 0;

  function applyTransform(): void {
    container.style.transform =
      `translate(${translateX}px, ${translateY}px) scale(${scale})`;
  }

  // Mouse wheel zoom.
  viewport.addEventListener("wheel", (e) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    scale = Math.min(Math.max(scale * delta, 0.2), 10);
    applyTransform();
  }, { passive: false });

  // Touch: pinch-to-zoom + pan.
  viewport.addEventListener("touchstart", (e) => {
    if (e.touches.length === 2) {
      startDist = Math.hypot(
        e.touches[1]!.clientX - e.touches[0]!.clientX,
        e.touches[1]!.clientY - e.touches[0]!.clientY
      );
      startScale = scale;
    } else if (e.touches.length === 1) {
      isPanning = true;
      panStartX = e.touches[0]!.clientX;
      panStartY = e.touches[0]!.clientY;
      startTranslateX = translateX;
      startTranslateY = translateY;
    }
  });

  viewport.addEventListener("touchmove", (e) => {
    e.preventDefault();
    if (e.touches.length === 2) {
      const dist = Math.hypot(
        e.touches[1]!.clientX - e.touches[0]!.clientX,
        e.touches[1]!.clientY - e.touches[0]!.clientY
      );
      scale = Math.min(Math.max(startScale * (dist / startDist), 0.2), 10);
      applyTransform();
    } else if (e.touches.length === 1 && isPanning) {
      translateX = startTranslateX + (e.touches[0]!.clientX - panStartX);
      translateY = startTranslateY + (e.touches[0]!.clientY - panStartY);
      applyTransform();
    }
  }, { passive: false });

  viewport.addEventListener("touchend", () => {
    isPanning = false;
  });

  // Mouse drag to pan.
  viewport.addEventListener("mousedown", (e) => {
    isPanning = true;
    panStartX = e.clientX;
    panStartY = e.clientY;
    startTranslateX = translateX;
    startTranslateY = translateY;
    viewport.style.cursor = "grabbing";
  });

  viewport.addEventListener("mousemove", (e) => {
    if (!isPanning) return;
    translateX = startTranslateX + (e.clientX - panStartX);
    translateY = startTranslateY + (e.clientY - panStartY);
    applyTransform();
  });

  viewport.addEventListener("mouseup", () => {
    isPanning = false;
    viewport.style.cursor = "grab";
  });

  // Double-tap/click to reset.
  let lastTap = 0;
  viewport.addEventListener("click", () => {
    const now = Date.now();
    if (now - lastTap < 300) {
      scale = 1;
      translateX = 0;
      translateY = 0;
      applyTransform();
    }
    lastTap = now;
  });

  // Close.
  function close(): void {
    overlay.remove();
  }
  closeBtn.addEventListener("click", close);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close();
  });
  document.addEventListener("keydown", function handler(e) {
    if (e.key === "Escape") {
      close();
      document.removeEventListener("keydown", handler);
    }
  });
}

function getAuthHeader(): Record<string, string> {
  const initData = window.Telegram?.WebApp?.initData;
  if (!initData) {
    return {};
  }
  return { Authorization: `tg-init-data ${initData}` };
}

function getStyles(): string {
  // Use Telegram theme colors when available, fall back to dark theme.
  const tp = window.Telegram?.WebApp?.themeParams;
  const bg = tp?.bg_color ?? "#1a1b26";
  const fg = tp?.text_color ?? "#c9d1d9";
  const secondaryBg = tp?.secondary_bg_color ?? "#161b22";
  const link = tp?.link_color ?? "#58a6ff";
  const hint = tp?.hint_color ?? "#8b949e";

  return `
    * { margin: 0; padding: 0; box-sizing: border-box; }

    html, body {
      background: ${bg};
      color: ${fg};
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans",
        Helvetica, Arial, sans-serif;
      font-size: 14px;
      line-height: 1.6;
      -webkit-text-size-adjust: 100%;
    }

    #loading {
      padding: 16px;
      color: ${hint};
      font-family: monospace;
      font-size: 13px;
    }

    #content {
      max-width: 800px;
      margin: 0 auto;
      padding: 16px;
    }

    /* Headings */
    h1, h2, h3, h4, h5, h6 {
      margin-top: 24px;
      margin-bottom: 16px;
      font-weight: 600;
      line-height: 1.25;
    }
    h1 { font-size: 1.75em; padding-bottom: 0.3em; border-bottom: 1px solid ${hint}33; }
    h2 { font-size: 1.5em; padding-bottom: 0.3em; border-bottom: 1px solid ${hint}33; }
    h3 { font-size: 1.25em; }
    h4 { font-size: 1em; }

    /* Paragraphs & text */
    p { margin-bottom: 16px; }
    strong { font-weight: 600; }
    a { color: ${link}; text-decoration: none; }
    a:hover { text-decoration: underline; }
    hr {
      height: 2px;
      margin: 24px 0;
      background: ${hint}33;
      border: 0;
    }

    /* Lists */
    ul, ol {
      padding-left: 2em;
      margin-bottom: 16px;
    }
    li { margin-bottom: 4px; }
    li + li { margin-top: 4px; }

    /* Code */
    code {
      padding: 0.2em 0.4em;
      background: ${secondaryBg};
      border-radius: 4px;
      font-family: "Fira Code", "Cascadia Code", "JetBrains Mono",
        ui-monospace, monospace;
      font-size: 0.9em;
    }
    pre {
      margin-bottom: 16px;
      padding: 12px 16px;
      background: ${secondaryBg};
      border-radius: 6px;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
    }
    pre code {
      padding: 0;
      background: transparent;
      border-radius: 0;
      font-size: 0.85em;
      line-height: 1.5;
    }

    /* Blockquotes */
    blockquote {
      margin-bottom: 16px;
      padding: 0 1em;
      border-left: 3px solid ${hint}66;
      color: ${hint};
    }
    blockquote p { margin-bottom: 0; }

    /* Tables */
    table {
      width: 100%;
      margin-bottom: 16px;
      border-collapse: collapse;
      overflow-x: auto;
      display: block;
    }
    th, td {
      padding: 6px 13px;
      border: 1px solid ${hint}33;
    }
    th {
      font-weight: 600;
      background: ${secondaryBg};
    }
    tr:nth-child(even) { background: ${secondaryBg}80; }

    /* Images */
    img {
      max-width: 100%;
      height: auto;
      border-radius: 4px;
    }

    /* Task lists */
    input[type="checkbox"] {
      margin-right: 0.5em;
    }

    /* Mermaid diagrams */
    .mermaid-wrapper {
      position: relative;
      margin-bottom: 16px;
    }
    .mermaid-wrapper pre.mermaid {
      overflow-x: auto;
      text-align: center;
    }
    .mermaid-fullscreen-btn {
      position: absolute;
      top: 8px;
      right: 8px;
      padding: 4px 10px;
      background: ${secondaryBg};
      color: ${hint};
      border: 1px solid ${hint}44;
      border-radius: 4px;
      font-size: 12px;
      cursor: pointer;
      opacity: 0;
      transition: opacity 0.15s;
    }
    .mermaid-wrapper:hover .mermaid-fullscreen-btn,
    .mermaid-wrapper:active .mermaid-fullscreen-btn {
      opacity: 1;
    }
    .mermaid-fullscreen-btn:hover {
      color: ${fg};
      border-color: ${hint}88;
    }

    /* Fullscreen overlay */
    .mermaid-overlay {
      position: fixed;
      inset: 0;
      z-index: 9999;
      background: ${bg}f0;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .mermaid-viewport {
      width: 100%;
      height: 100%;
      overflow: hidden;
      cursor: grab;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .mermaid-zoom-container {
      transform-origin: center center;
      will-change: transform;
    }
    .mermaid-zoom-container svg {
      max-width: 90vw;
      max-height: 90vh;
    }
    .mermaid-close-btn {
      position: absolute;
      top: 12px;
      right: 16px;
      width: 36px;
      height: 36px;
      background: ${secondaryBg};
      color: ${fg};
      border: 1px solid ${hint}44;
      border-radius: 50%;
      font-size: 20px;
      line-height: 1;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .mermaid-close-btn:hover {
      background: ${hint}44;
    }

    /* Copy as Markdown button */
    #copy-md-btn {
      position: fixed;
      bottom: 20px;
      right: 20px;
      padding: 8px 16px;
      background: ${secondaryBg};
      color: ${hint};
      border: 1px solid ${hint}44;
      border-radius: 20px;
      font-size: 13px;
      cursor: pointer;
      z-index: 100;
      transition: color 0.15s, border-color 0.15s, background 0.15s;
      box-shadow: 0 2px 8px rgba(0,0,0,0.3);
    }
    #copy-md-btn:hover {
      color: ${fg};
      border-color: ${hint}88;
      background: ${hint}22;
    }
  `;
}
