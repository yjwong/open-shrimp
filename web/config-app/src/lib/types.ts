export interface AndroidConfig {
  image_type?: "VANILLA" | "GAPPS";
  resolution?: string | null;
  dpi?: number | null;
  gpu?: "virgl" | "software";
}

export interface SandboxConfig {
  backend: string;
  enabled?: boolean;
  guest_os?: "linux" | "macos";
  computer_use?: boolean;
  virgl?: boolean;
  phone_use?: boolean;
  android?: AndroidConfig | null;
  memory?: number;
  cpus?: number;
  disk_size?: number;
  base_image?: string | null;
  provision?: string | null;
  persistent_paths?: string[];
  allow_host_escape?: boolean;
  mingw_bin?: string | null;
}

export type SandboxCapability = Exclude<keyof SandboxConfig, "backend">;

export interface SandboxOffer {
  backend: SandboxConfig["backend"];
  label: string;
  summary: string;
  capabilities: SandboxCapability[];
  base_image_placeholder: string;
  unsupported_reasons: Partial<Record<SandboxCapability, string>>;
  available: boolean;
  detail: string;
}

export interface SandboxCatalog {
  sandbox: {
    backend: SandboxConfig["backend"] | null;
    available: boolean;
    note: string;
  };
  sandboxes: SandboxOffer[];
}

export interface ContextConfig {
  directory: string;
  description: string;
  allowed_tools: string[];
  disallowed_tools?: string[];
  model?: string | null;
  effort?: EffortLevel | null;
  backend?: string | null;
  additional_directories?: string[];
  default_for_chats?: number[];
  locked_for_chats?: number[];
  sandbox?: SandboxConfig | null;
}

export interface AppConfig {
  contexts: Record<string, ContextConfig>;
  allowed_users: number[];
  default_context: string;
  backend?: string | null;
}

export const BACKENDS = ["claude_sdk", "opencode"] as const;

// What to call each backend's agent in user-facing copy — mirrors
// ``Backend.display_name`` in backend/protocol.py.  A backend name is the
// config key; this is the product.
const BACKEND_DISPLAY_NAMES: Record<string, string> = {
  claude_sdk: "Claude Code",
  opencode: "OpenCode",
};

/**
 * How to name a field that pins nothing.  An unset model or effort is not a
 * default OpenShrimp picked — it passes nothing to the agent, so the agent's
 * own configuration decides.  The label names that agent: a bare "default"
 * reads as our choice, and "CLI" does not say which one where two backends
 * ship a CLI each.
 */
export function defaultLabel(backend: string): string {
  return `${BACKEND_DISPLAY_NAMES[backend] ?? backend} default`;
}

export type EffortLevel = "low" | "medium" | "high" | "xhigh" | "max";

export const EFFORT_LEVELS: EffortLevel[] = [
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
];
