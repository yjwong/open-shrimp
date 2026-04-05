import { useCallback, useEffect, useRef, useState } from "react";
import { fetchHunks } from "../lib/api";
import type { FileSummary, Hunk } from "../lib/types";

const PAGE_SIZE = 20;

interface UseHunksResult {
  hunks: Hunk[];
  totalHunks: number;
  files: FileSummary[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
  loadMore: () => void;
  updateFileStagedCount: (filePath: string, delta: number) => void;
}

export function useHunks(
  chatId: string | null,
  dir: string,
  threadId: string | null = null,
): UseHunksResult {
  const [hunks, setHunks] = useState<Hunk[]>([]);
  const [totalHunks, setTotalHunks] = useState(0);
  const [files, setFiles] = useState<FileSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  // Track the next offset to fetch. Use a ref so loadMore() doesn't
  // trigger a re-render cycle — we only update state when the fetch
  // actually completes.
  const nextOffsetRef = useRef(0);
  const loadingMoreRef = useRef(false);
  const allLoadedRef = useRef(false);

  const refresh = useCallback(() => {
    // Reset pagination state on refresh.
    nextOffsetRef.current = 0;
    loadingMoreRef.current = false;
    allLoadedRef.current = false;
    setHunks([]);
    setTotalHunks(0);
    setFiles([]);
    setRefreshKey((k) => k + 1);
  }, []);

  // Initial load (and reload on refresh).
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
      nextOffsetRef.current = 0;
      allLoadedRef.current = false;

      try {
        const result = await fetchHunks(chatId!, dir, 0, PAGE_SIZE, threadId);
        if (!cancelled) {
          setHunks(result.hunks);
          setTotalHunks(result.total_hunks);
          setFiles(result.files);
          nextOffsetRef.current = result.hunks.length;
          if (result.hunks.length >= result.total_hunks) {
            allLoadedRef.current = true;
          }
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
  }, [chatId, dir, threadId, refreshKey]);

  const loadMore = useCallback(() => {
    if (!chatId || loadingMoreRef.current || allLoadedRef.current) return;

    loadingMoreRef.current = true;
    const offset = nextOffsetRef.current;

    fetchHunks(chatId, dir, offset, PAGE_SIZE, threadId)
      .then((result) => {
        setHunks((prev) => [...prev, ...result.hunks]);
        setTotalHunks(result.total_hunks);
        nextOffsetRef.current = offset + result.hunks.length;
        if (offset + result.hunks.length >= result.total_hunks) {
          allLoadedRef.current = true;
        }
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Failed to load more hunks");
      })
      .finally(() => {
        loadingMoreRef.current = false;
      });
  }, [chatId, dir, threadId]);

  const updateFileStagedCount = useCallback(
    (filePath: string, delta: number) => {
      setFiles((prev) =>
        prev.map((f) =>
          f.path === filePath
            ? {
                ...f,
                staged_count: Math.max(
                  0,
                  Math.min(f.hunk_count, f.staged_count + delta),
                ),
              }
            : f,
        ),
      );
    },
    [],
  );

  return { hunks, totalHunks, files, loading, error, refresh, loadMore, updateFileStagedCount };
}
