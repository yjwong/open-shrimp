export interface SandboxConfig {
  backend: "docker" | "libvirt" | "lima" | "macos";
  enabled?: boolean;
  docker_in_docker?: boolean;
  dockerfile?: string | null;
  computer_use?: boolean;
  virgl?: boolean;
  memory?: number;
  cpus?: number;
  disk_size?: number;
  base_image?: string | null;
  provision?: string | null;
}

export interface ContextConfig {
  directory: string;
  description: string;
  allowed_tools: string[];
  model?: string | null;
  additional_directories?: string[];
  default_for_chats?: number[];
  locked_for_chats?: number[];
  sandbox?: SandboxConfig | null;
}

export interface AppConfig {
  contexts: Record<string, ContextConfig>;
  allowed_users: number[];
  default_context: string;
}
