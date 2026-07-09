import { getAuthHeader } from "./telegram";
import type { PageComment } from "./types";

export async function fetchPdf(path: string): Promise<ArrayBuffer> {
  const resp = await fetch(
    `/api/preview/pdf?path=${encodeURIComponent(path)}`,
    { headers: getAuthHeader(), cache: "no-cache" },
  );
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
    throw new Error(data.error || `HTTP ${resp.status}`);
  }
  return resp.arrayBuffer();
}

interface SubmitReviewParams {
  chatId: number;
  threadId: number | null;
  path: string;
  comments: PageComment[];
}

export async function submitReview({ chatId, threadId, path, comments }: SubmitReviewParams): Promise<void> {
  const payload = {
    chat_id: chatId,
    thread_id: threadId,
    path,
    comments: comments.map((c) => ({
      page: c.page,
      comment: c.comment,
    })),
  };

  const resp = await fetch("/api/preview/submit-review", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...getAuthHeader(),
    },
    body: JSON.stringify(payload),
  });

  if (!resp.ok) {
    const data = await resp.json().catch(() => ({ error: "Unknown error" }));
    throw new Error(data.error || `HTTP ${resp.status}`);
  }
}
