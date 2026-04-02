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

export function initTelegram(): void {
  try {
    window.Telegram?.WebApp?.ready();
    window.Telegram?.WebApp?.expand();
  } catch {
    // Not in Telegram.
  }
}

export function getThemeParams() {
  return window.Telegram?.WebApp?.themeParams;
}

export function getAuthHeader(): Record<string, string> {
  const initData = window.Telegram?.WebApp?.initData;
  if (initData) return { Authorization: `tg-init-data ${initData}` };
  // Fallback: use HMAC token from URL (group chat / external browser).
  const token = new URLSearchParams(window.location.search).get("token");
  if (token) return { Authorization: `tg-token ${token}` };
  return {};
}
