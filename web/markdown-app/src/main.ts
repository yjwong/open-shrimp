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

  const marked = new Marked(
    markedHighlight({
      langPrefix: "hljs language-",
      highlight(code: string, lang: string) {
        if (lang && hljs.default.getLanguage(lang)) {
          return hljs.default.highlight(code, { language: lang }).value;
        }
        return hljs.default.highlightAuto(code).value;
      },
    })
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

  // Set page title.
  document.title = data.filename;
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
  `;
}
