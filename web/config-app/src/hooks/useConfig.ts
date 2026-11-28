import { useCallback, useEffect, useRef, useState } from "react";
import { fetchConfig, fetchSandboxes, saveConfig } from "../lib/api";
import type { AppConfig, SandboxCatalog } from "../lib/types";

export function useConfig() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [sandboxCatalog, setSandboxCatalog] =
    useState<SandboxCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Removed contexts whose history the next save also deletes.
  const [purgeHistory, setPurgeHistory] = useState<Set<string>>(new Set());
  const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);
  const initialRef = useRef<string>("");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [data, sandboxes] = await Promise.all([
        fetchConfig(),
        fetchSandboxes(),
      ]);
      setConfig(data);
      setSandboxCatalog(sandboxes);
      setPurgeHistory(new Set());
      initialRef.current = JSON.stringify(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load config");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const dirty = config !== null && JSON.stringify(config) !== initialRef.current;

  const save = useCallback(async () => {
    if (!config) return;
    setSaving(true);
    try {
      await saveConfig(config, [...purgeHistory]);
      initialRef.current = JSON.stringify(config);
      setPurgeHistory(new Set());
      setToast({ message: "Config saved", type: "success" });
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Failed to save";
      setToast({ message: msg, type: "error" });
    } finally {
      setSaving(false);
    }
  }, [config, purgeHistory]);

  const dismissToast = useCallback(() => setToast(null), []);

  return {
    config,
    setConfig,
    setPurgeHistory,
    sandboxCatalog,
    loading,
    saving,
    error,
    dirty,
    save,
    toast,
    dismissToast,
    reload: load,
  };
}
