import { useReview } from "../context/ReviewContext";
import CopyMarkdownButton from "./CopyMarkdownButton";

interface Props {
  onSubmit: () => void;
  copyContent?: string;
}

export default function ReviewToolbar({ onSubmit, copyContent }: Props) {
  const { reviewMode, toggleReviewMode, comments } = useReview();

  return (
    <div className="review-toolbar">
      {copyContent && <CopyMarkdownButton content={copyContent} />}
      <button
        className={`review-toggle-btn ${reviewMode ? "active" : ""}`}
        onClick={toggleReviewMode}
      >
        {reviewMode ? "Exit Review" : "Review"}
        {comments.length > 0 && (
          <span className="comment-badge">{comments.length}</span>
        )}
      </button>
      {comments.length > 0 && (
        <button className="submit-review-btn" onClick={onSubmit}>
          Submit Review
        </button>
      )}
    </div>
  );
}
