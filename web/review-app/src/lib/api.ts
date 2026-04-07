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
  dir: string,
  offset: number = 0,
  limit: number = 20,
  threadId: string | null = null,
): Promise<HunkResult> {
  const params = new URLSearchParams({
    chat_id: chatId,
    dir,
    offset: String(offset),
    limit: String(limit),
  });
  if (threadId !== null) {
    params.set("thread_id", threadId);
  }

  const response = await fetch(`${BASE_URL}/hunks?${params}`, {
    headers: getAuthHeader(),
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch hunks: ${response.status} ${response.statusText}`);
  }

  return response.json() as Promise<HunkResult>;
}

export async function stageHunk(hunkId: string, chatId: string, dir: string, threadId: string | null = null): Promise<void> {
  const response = await fetch(`${BASE_URL}/stage`, {
    method: "POST",
    headers: {
      ...getAuthHeader(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ hunk_id: hunkId, chat_id: chatId, dir, ...(threadId !== null && { thread_id: threadId }) }),
  });

  if (response.status === 409) {
    throw new StaleHunkError();
  }

  if (!response.ok) {
    throw new Error(`Failed to stage hunk: ${response.status} ${response.statusText}`);
  }
}

export async function unstageHunk(hunkId: string, chatId: string, dir: string, threadId: string | null = null): Promise<void> {
  const response = await fetch(`${BASE_URL}/unstage`, {
    method: "POST",
    headers: {
      ...getAuthHeader(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ hunk_id: hunkId, chat_id: chatId, dir, ...(threadId !== null && { thread_id: threadId }) }),
  });

  if (response.status === 409) {
    throw new StaleHunkError();
  }

  if (!response.ok) {
    throw new Error(`Failed to unstage hunk: ${response.status} ${response.statusText}`);
  }
}

export async function skipHunk(hunkId: string, chatId: string, dir: string, threadId: string | null = null): Promise<void> {
  const response = await fetch(`${BASE_URL}/skip`, {
    method: "POST",
    headers: {
      ...getAuthHeader(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ hunk_id: hunkId, chat_id: chatId, dir, ...(threadId !== null && { thread_id: threadId }) }),
  });

  if (response.status === 409) {
    throw new StaleHunkError();
  }

  if (!response.ok) {
    throw new Error(`Failed to skip hunk: ${response.status} ${response.statusText}`);
  }
}

export async function stageFile(
  filePath: string,
  chatId: string,
  dir: string,
  threadId: string | null = null,
): Promise<string[]> {
  const response = await fetch(`${BASE_URL}/stage-file`, {
    method: "POST",
    headers: {
      ...getAuthHeader(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      file_path: filePath,
      chat_id: chatId,
      dir,
      ...(threadId !== null && { thread_id: threadId }),
    }),
  });

  if (response.status === 409) {
    throw new StaleHunkError();
  }

  if (!response.ok) {
    throw new Error(`Failed to stage file: ${response.status} ${response.statusText}`);
  }

  const data = await response.json();
  return data.staged_ids as string[];
}

export async function unstageFile(
  hunkIds: string[],
  chatId: string,
  dir: string,
  threadId: string | null = null,
): Promise<void> {
  const response = await fetch(`${BASE_URL}/unstage-file`, {
    method: "POST",
    headers: {
      ...getAuthHeader(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      hunk_ids: hunkIds,
      chat_id: chatId,
      dir,
      ...(threadId !== null && { thread_id: threadId }),
    }),
  });

  if (response.status === 409) {
    throw new StaleHunkError();
  }

  if (!response.ok) {
    throw new Error(`Failed to unstage file: ${response.status} ${response.statusText}`);
  }
}

export async function commitChanges(chatId: string, threadId: string | null = null): Promise<void> {
  const response = await fetch(`${BASE_URL}/commit`, {
    method: "POST",
    headers: {
      ...getAuthHeader(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ chat_id: chatId, ...(threadId !== null && { thread_id: threadId }) }),
  });

  if (!response.ok) {
    throw new Error(`Failed to commit: ${response.status} ${response.statusText}`);
  }
}
