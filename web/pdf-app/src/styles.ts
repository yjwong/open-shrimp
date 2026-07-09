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

    /* Page column — leave room for the bottom filmstrip. */
    .pages {
      max-width: 800px;
      margin: 0 auto;
      padding: 8px 8px 112px;
    }

    .pdf-page {
      margin-bottom: 12px;
    }
    .pdf-page-frame {
      position: relative;
      width: 100%;
      background: #fff;
      border-radius: 4px;
      overflow: hidden;
      box-shadow: 0 1px 4px rgba(0,0,0,0.4);
    }
    .pdf-page-canvas {
      display: block;
      width: 100%;
      height: auto;
    }
    .pdf-page-loading {
      position: absolute;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #999;
      background: #f0f0f0;
      font-size: 13px;
    }
    .pdf-page-badge {
      position: absolute;
      bottom: 8px;
      left: 8px;
      padding: 2px 8px;
      background: rgba(0,0,0,0.55);
      color: #fff;
      border-radius: 10px;
      font-size: 11px;
      pointer-events: none;
    }
    .pdf-page-comment-btn {
      position: absolute;
      top: 8px;
      right: 8px;
      min-width: 40px;
      height: 36px;
      padding: 0 10px;
      background: rgba(0,0,0,0.55);
      color: #fff;
      border: none;
      border-radius: 18px;
      font-size: 15px;
      cursor: pointer;
    }
    .pdf-page-comment-btn.has-comments {
      background: ${link};
    }

    .page-comments { margin-top: 6px; }

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
      padding: 6px 14px;
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
      padding: 8px 10px;
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
      color: ${fg};
    }
    .comment-indicator-actions {
      display: flex;
      gap: 8px;
      margin-top: 6px;
    }

    /* Floating submit button above the filmstrip. */
    .review-toolbar {
      position: fixed;
      bottom: 104px;
      right: 16px;
      z-index: 100;
    }
    .submit-review-btn {
      padding: 10px 18px;
      background: ${link};
      color: #fff;
      border: none;
      border-radius: 22px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      box-shadow: 0 2px 8px rgba(0,0,0,0.4);
    }

    /* Bottom filmstrip — horizontal thumbnail scroller. */
    .filmstrip {
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      display: flex;
      gap: 8px;
      padding: 8px 12px calc(8px + env(safe-area-inset-bottom));
      background: ${secondaryBg}f2;
      border-top: 1px solid ${hint}33;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      z-index: 90;
    }
    .filmstrip-thumb {
      flex: 0 0 auto;
      width: 56px;
      padding: 0;
      background: #fff;
      border: 2px solid transparent;
      border-radius: 4px;
      cursor: pointer;
      position: relative;
      overflow: hidden;
    }
    .filmstrip-thumb.current {
      border-color: ${link};
    }
    .filmstrip-canvas {
      display: block;
      width: 100%;
      height: auto;
      min-height: 40px;
      background: #f0f0f0;
    }
    .filmstrip-label {
      position: absolute;
      bottom: 2px;
      right: 2px;
      padding: 0 5px;
      background: rgba(0,0,0,0.6);
      color: #fff;
      border-radius: 8px;
      font-size: 10px;
      display: flex;
      align-items: center;
      gap: 3px;
    }
    .filmstrip-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: ${link};
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
    .submit-dialog-keepopen {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 0 0 16px;
      font-size: 13px;
      color: ${hint};
      cursor: pointer;
    }
    .submit-dialog-keepopen input {
      cursor: pointer;
    }
    .submit-dialog-actions {
      display: flex;
      gap: 8px;
      justify-content: flex-end;
    }
  `;
  document.head.appendChild(style);
}
