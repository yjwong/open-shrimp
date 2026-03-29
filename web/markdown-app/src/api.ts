import { getAuthHeader } from "./telegram";
import type { Comment } from "./types";

interface SubmitReviewParams {
  chatId: number;
  threadId: number | null;
  path?: string;
  contentId?: string;
  comments: Comment[];
}

export async function submitReview({ chatId, threadId, path, contentId, comments }: SubmitReviewParams): Promise<void> {
  const payload: Record<string, unknown> = {
    comments: comments.map((c) => ({
      block_text: c.blockText,
      comment: c.comment,
    })),
  };

  if (contentId) {
    payload.content_id = contentId;
  } else {
    payload.chat_id = chatId;
    payload.thread_id = threadId;
    payload.path = path;
  }

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
