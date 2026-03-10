import { useCallback, useEffect, useState } from "react";
import { fetchHunks } from "../lib/api";
import type { Hunk } from "../lib/types";

interface UseHunksResult {
  hunks: Hunk[];
  totalHunks: number;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useHunks(
  chatId: string | null,
  offset: number = 0,
  limit: number = 20,
): UseHunksResult {
  const [hunks, setHunks] = useState<Hunk[]>([]);
  const [totalHunks, setTotalHunks] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const refresh = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  useEffect(() => {
    if (!chatId) {
      setLoading(false);
      setError("Missing chat_id parameter");
      return;
    }

    let cancelled = false;

    async function load() {
      setLoading(true);
      setError(null);

      try {
        const result = await fetchHunks(chatId!, offset, limit);
        if (!cancelled) {
          setHunks(result.hunks);
          setTotalHunks(result.total_hunks);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Unknown error");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void load();

    return () => {
      cancelled = true;
    };
  }, [chatId, offset, limit, refreshKey]);

  return { hunks, totalHunks, loading, error, refresh };
}
