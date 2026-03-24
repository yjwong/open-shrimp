import { useState } from "react";
import type { Comment } from "../types";

interface Props {
  comments: Comment[];
  onConfirm: () => Promise<void>;
  onCancel: () => void;
}

export default function SubmitDialog({ comments, onConfirm, onCancel }: Props) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleConfirm = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await onConfirm();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSubmitting(false);
    }
  };

  return (
    <div className="submit-overlay" onClick={(e) => { if (e.target === e.currentTarget && !submitting) onCancel(); }}>
      <div className="submit-dialog">
        <h3 className="submit-dialog-title">Submit Review</h3>
        <p className="submit-dialog-summary">
          Submit {comments.length} comment{comments.length !== 1 ? "s" : ""} as feedback to the agent?
        </p>
        {error && <p className="submit-dialog-error">{error}</p>}
        <div className="submit-dialog-actions">
          <button
            className="comment-btn comment-btn-save"
            onClick={handleConfirm}
            disabled={submitting}
          >
            {submitting ? "Submitting..." : "Submit"}
          </button>
          <button
            className="comment-btn comment-btn-cancel"
            onClick={onCancel}
            disabled={submitting}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
