import { createContext, useContext, useState, useCallback, type ReactNode } from "react";
import type { Comment } from "../types";

interface ReviewContextValue {
  reviewMode: boolean;
  toggleReviewMode: () => void;
  comments: Comment[];
  addComment: (blockIndex: number, blockText: string, comment: string) => void;
  editComment: (id: string, comment: string) => void;
  deleteComment: (id: string) => void;
}

const ReviewContext = createContext<ReviewContextValue | null>(null);

export function ReviewProvider({ children }: { children: ReactNode }) {
  const [reviewMode, setReviewMode] = useState(false);
  const [comments, setComments] = useState<Comment[]>([]);

  const toggleReviewMode = useCallback(() => setReviewMode((prev) => !prev), []);

  const addComment = useCallback((blockIndex: number, blockText: string, comment: string) => {
    setComments((prev) => [
      ...prev,
      {
        id: crypto.randomUUID(),
        blockIndex,
        blockText: blockText.slice(0, 200),
        comment,
        createdAt: Date.now(),
      },
    ]);
  }, []);

  const editComment = useCallback((id: string, comment: string) => {
    setComments((prev) =>
      prev.map((c) => (c.id === id ? { ...c, comment } : c))
    );
  }, []);

  const deleteComment = useCallback((id: string) => {
    setComments((prev) => prev.filter((c) => c.id !== id));
  }, []);

  return (
    <ReviewContext.Provider
      value={{ reviewMode, toggleReviewMode, comments, addComment, editComment, deleteComment }}
    >
      {children}
    </ReviewContext.Provider>
  );
}

export function useReview(): ReviewContextValue {
  const ctx = useContext(ReviewContext);
  if (!ctx) throw new Error("useReview must be used within ReviewProvider");
  return ctx;
}
