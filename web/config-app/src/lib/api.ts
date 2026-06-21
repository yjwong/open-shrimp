import { getAuthHeader } from "./auth";
import type { AppConfig } from "./types";

export interface ModelOption {
  value: string;
  label: string;
}

export async function fetchConfig(): Promise<AppConfig> {
  const response = await fetch("/api/config", {
    headers: getAuthHeader(),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `Failed to fetch config: ${response.status}`);
  }
  return response.json() as Promise<AppConfig>;
}

export async function saveConfig(config: AppConfig): Promise<void> {
  const response = await fetch("/api/config", {
    method: "PUT",
    headers: {
      ...getAuthHeader(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify(config),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `Failed to save config: ${response.status}`);
  }
}

export async function validatePath(path: string): Promise<{ exists: boolean; path: string }> {
  const response = await fetch("/api/config/validate-path", {
    method: "POST",
    headers: {
      ...getAuthHeader(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ path }),
  });
  if (!response.ok) {
    throw new Error(`Failed to validate path: ${response.status}`);
  }
  return response.json();
}

export async function fetchModels(directory: string): Promise<ModelOption[]> {
  const params = new URLSearchParams();
  if (directory.trim()) params.set("directory", directory.trim());
  const query = params.toString();
  const response = await fetch(`/api/config/models${query ? `?${query}` : ""}`, {
    headers: getAuthHeader(),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `Failed to fetch models: ${response.status}`);
  }
  const body = (await response.json()) as { models?: ModelOption[] };
  return Array.isArray(body.models) ? body.models : [];
}

export interface SandboxActionResult {
  ok: true;
  closed_sessions: number;
}

async function sandboxAction(
  contextName: string,
  action: "reboot" | "reset",
): Promise<SandboxActionResult> {
  const response = await fetch(
    `/api/sandbox/${encodeURIComponent(contextName)}/${action}`,
    {
      method: "POST",
      headers: getAuthHeader(),
    },
  );
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      body.error || `Failed to ${action} sandbox: ${response.status}`,
    );
  }
  return response.json() as Promise<SandboxActionResult>;
}

export function rebootSandbox(contextName: string): Promise<SandboxActionResult> {
  return sandboxAction(contextName, "reboot");
}

export function resetSandbox(contextName: string): Promise<SandboxActionResult> {
  return sandboxAction(contextName, "reset");
}
