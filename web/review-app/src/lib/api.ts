import { getAuthHeader } from "./auth";
import type { HunkResult } from "./types";

const BASE_URL = "/api/review";

export class StaleHunkError extends Error {
  constructor(message: string = "Hunk is stale and no longer matches the current diff") {
    super(message);
    this.name = "StaleHunkError";
  }
}

export async function fetchHunks(
  chatId: string,
  offset: number = 0,
  limit: number = 20,
): Promise<HunkResult> {
  const params = new URLSearchParams({
    chat_id: chatId,
    offset: String(offset),
    limit: String(limit),
  });

  const response = await fetch(`${BASE_URL}/hunks?${params}`, {
    headers: getAuthHeader(),
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch hunks: ${response.status} ${response.statusText}`);
  }

  return response.json() as Promise<HunkResult>;
}

export async function stageHunk(hunkId: string): Promise<void> {
  const response = await fetch(`${BASE_URL}/stage`, {
    method: "POST",
    headers: {
      ...getAuthHeader(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ hunk_id: hunkId }),
  });

  if (response.status === 409) {
    throw new StaleHunkError();
  }

  if (!response.ok) {
    throw new Error(`Failed to stage hunk: ${response.status} ${response.statusText}`);
  }
}

export async function unstageHunk(hunkId: string): Promise<void> {
  const response = await fetch(`${BASE_URL}/unstage`, {
    method: "POST",
    headers: {
      ...getAuthHeader(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ hunk_id: hunkId }),
  });

  if (response.status === 409) {
    throw new StaleHunkError();
  }

  if (!response.ok) {
    throw new Error(`Failed to unstage hunk: ${response.status} ${response.statusText}`);
  }
}
