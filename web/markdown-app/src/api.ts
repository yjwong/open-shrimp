import { getAuthHeader } from "./telegram";
import type { Comment } from "./types";

interface SubmitReviewParams {
  chatId: number;
  threadId: number | null;
  path: string;
  comments: Comment[];
}

export async function submitReview({ chatId, threadId, path, comments }: SubmitReviewParams): Promise<void> {
  const resp = await fetch("/api/preview/submit-review", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...getAuthHeader(),
    },
    body: JSON.stringify({
      chat_id: chatId,
      thread_id: threadId,
      path,
      comments: comments.map((c) => ({
        block_text: c.blockText,
        comment: c.comment,
      })),
    }),
  });

  if (!resp.ok) {
    const data = await resp.json().catch(() => ({ error: "Unknown error" }));
    throw new Error(data.error || `HTTP ${resp.status}`);
  }
}
