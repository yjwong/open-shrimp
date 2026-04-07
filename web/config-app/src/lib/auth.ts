declare global {
  interface Window {
    Telegram?: {
      WebApp: {
        initData: string;
        close: () => void;
        sendData: (data: string) => void;
      };
    };
  }
}

export function getInitData(): string | null {
  return window.Telegram?.WebApp?.initData || null;
}

export function getAuthHeader(): Record<string, string> {
  const initData = getInitData();
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
