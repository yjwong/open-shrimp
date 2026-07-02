export interface AndroidConfig {
  image_type?: "VANILLA" | "GAPPS";
  resolution?: string | null;
  dpi?: number | null;
  gpu?: "virgl" | "software";
}

export interface SandboxConfig {
  backend: "docker" | "libvirt" | "lima";
  enabled?: boolean;
  guest_os?: "linux" | "macos";
  docker_in_docker?: boolean;
  dockerfile?: string | null;
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
}

export interface ContextConfig {
  directory: string;
  description: string;
  allowed_tools: string[];
  model?: string | null;
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
