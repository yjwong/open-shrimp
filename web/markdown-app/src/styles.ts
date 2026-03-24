import { getThemeParams } from "./telegram";

let injected = false;

export function injectStyles(): void {
  if (injected) return;
  injected = true;

  const tp = getThemeParams();
  const bg = tp?.bg_color ?? "#1a1b26";
  const fg = tp?.text_color ?? "#c9d1d9";
  const secondaryBg = tp?.secondary_bg_color ?? "#161b22";
  const link = tp?.link_color ?? "#58a6ff";
  const hint = tp?.hint_color ?? "#8b949e";

  const style = document.createElement("style");
  style.textContent = `
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

    .loading {
      padding: 16px;
      color: ${hint};
      font-family: monospace;
      font-size: 13px;
    }
    .error { color: #f7768e; }

    #content {
      max-width: 800px;
      margin: 0 auto;
      padding: 16px;
    }

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

    ul, ol {
      padding-left: 2em;
      margin-bottom: 16px;
    }
    li { margin-bottom: 4px; }
    li + li { margin-top: 4px; }

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

    blockquote {
      margin-bottom: 16px;
      padding: 0 1em;
      border-left: 3px solid ${hint}66;
      color: ${hint};
    }
    blockquote p { margin-bottom: 0; }

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

    img {
      max-width: 100%;
      height: auto;
      border-radius: 4px;
    }

    input[type="checkbox"] {
      margin-right: 0.5em;
    }

    /* Mermaid diagrams */
    .mermaid-wrapper {
      position: relative;
      margin-bottom: 16px;
    }
    .mermaid-rendered {
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
    .copy-md-btn {
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
    .copy-md-btn:hover {
      color: ${fg};
      border-color: ${hint}88;
      background: ${hint}22;
    }

    /* Review toolbar */
    .review-toolbar {
      position: fixed;
      bottom: 20px;
      right: 20px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      align-items: flex-end;
      z-index: 100;
    }

    .review-toggle-btn {
      padding: 8px 16px;
      background: ${secondaryBg};
      color: ${hint};
      border: 1px solid ${hint}44;
      border-radius: 20px;
      font-size: 13px;
      cursor: pointer;
      transition: color 0.15s, border-color 0.15s, background 0.15s;
      box-shadow: 0 2px 8px rgba(0,0,0,0.3);
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .review-toggle-btn:hover {
      color: ${fg};
      border-color: ${hint}88;
      background: ${hint}22;
    }
    .review-toggle-btn.active {
      background: ${link}22;
      color: ${link};
      border-color: ${link}66;
    }

    .comment-badge {
      background: ${link};
      color: #fff;
      font-size: 11px;
      font-weight: 600;
      padding: 1px 6px;
      border-radius: 10px;
      min-width: 18px;
      text-align: center;
    }

    .submit-review-btn {
      padding: 8px 16px;
      background: ${link};
      color: #fff;
      border: none;
      border-radius: 20px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      box-shadow: 0 2px 8px rgba(0,0,0,0.3);
      transition: opacity 0.15s;
    }
    .submit-review-btn:hover {
      opacity: 0.9;
    }

    /* Commentable blocks */
    .commentable-block {
      position: relative;
      transition: background 0.15s;
      border-radius: 4px;
      padding: 2px 4px;
      margin: -2px -4px;
    }
    .commentable-block--active {
      cursor: pointer;
    }
    .commentable-block--active:hover {
      background: ${hint}11;
    }

    .comment-editor {
      margin-top: 8px;
      margin-bottom: 8px;
      padding: 8px;
      background: ${secondaryBg};
      border: 1px solid ${hint}44;
      border-radius: 6px;
    }
    .comment-textarea {
      width: 100%;
      min-height: 60px;
      padding: 8px;
      background: ${bg};
      color: ${fg};
      border: 1px solid ${hint}33;
      border-radius: 4px;
      font-family: inherit;
      font-size: 13px;
      resize: vertical;
    }
    .comment-textarea:focus {
      outline: none;
      border-color: ${link};
    }
    .comment-editor-actions {
      display: flex;
      gap: 8px;
      margin-top: 8px;
      justify-content: flex-end;
    }
    .comment-btn {
      padding: 4px 12px;
      border-radius: 4px;
      font-size: 12px;
      cursor: pointer;
      border: none;
    }
    .comment-btn-save {
      background: ${link};
      color: #fff;
    }
    .comment-btn-save:disabled {
      opacity: 0.5;
      cursor: default;
    }
    .comment-btn-cancel {
      background: ${hint}33;
      color: ${fg};
    }
    .comment-btn-edit {
      background: ${hint}33;
      color: ${fg};
    }
    .comment-btn-delete {
      background: #f7768e33;
      color: #f7768e;
    }

    .comment-indicator {
      margin-top: 6px;
      margin-bottom: 6px;
      padding: 6px 10px;
      background: ${link}11;
      border-left: 3px solid ${link};
      border-radius: 0 4px 4px 0;
      cursor: pointer;
      font-size: 12px;
      color: ${hint};
    }
    .comment-indicator:hover {
      background: ${link}22;
    }
    .comment-indicator-preview {
      display: block;
      line-height: 1.4;
    }
    .comment-indicator-actions {
      display: flex;
      gap: 8px;
      margin-top: 6px;
    }

    /* Submit dialog */
    .submit-overlay {
      position: fixed;
      inset: 0;
      z-index: 9998;
      background: rgba(0,0,0,0.6);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 16px;
    }
    .submit-dialog {
      background: ${secondaryBg};
      border: 1px solid ${hint}44;
      border-radius: 12px;
      padding: 20px;
      max-width: 360px;
      width: 100%;
    }
    .submit-dialog-title {
      margin: 0 0 8px;
      font-size: 16px;
      font-weight: 600;
    }
    .submit-dialog-summary {
      margin: 0 0 16px;
      font-size: 14px;
      color: ${hint};
    }
    .submit-dialog-error {
      margin: 0 0 12px;
      font-size: 13px;
      color: #f7768e;
    }
    .submit-dialog-actions {
      display: flex;
      gap: 8px;
      justify-content: flex-end;
    }
  `;
  document.head.appendChild(style);
}
