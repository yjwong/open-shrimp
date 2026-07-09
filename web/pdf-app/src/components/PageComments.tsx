import { useState } from "react";
import { useReview } from "../context/ReviewContext";
import CommentEditor from "./CommentEditor";

interface Props {
  page: number;
}

export default function PageComments({ page }: Props) {
  const { comments, editComment, deleteComment } = useReview();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const pageComments = comments.filter((c) => c.page === page);
  if (pageComments.length === 0) return null;

  return (
    <div className="page-comments">
      {pageComments.map((c) =>
        editingId === c.id ? (
          <CommentEditor
            key={c.id}
            initialValue={c.comment}
            onSave={(text) => {
              editComment(c.id, text);
              setEditingId(null);
            }}
            onCancel={() => setEditingId(null)}
          />
        ) : (
          <div
            key={c.id}
            className="comment-indicator"
            onClick={() => setExpandedId(expandedId === c.id ? null : c.id)}
          >
            <span className="comment-indicator-preview">💬 {c.comment}</span>
            {expandedId === c.id && (
              <div className="comment-indicator-actions">
                <button
                  className="comment-btn comment-btn-edit"
                  onClick={(e) => {
                    e.stopPropagation();
                    setEditingId(c.id);
                    setExpandedId(null);
                  }}
                >
                  Edit
                </button>
                <button
                  className="comment-btn comment-btn-delete"
                  onClick={(e) => {
                    e.stopPropagation();
                    deleteComment(c.id);
                  }}
                >
                  Delete
                </button>
              </div>
            )}
          </div>
        )
      )}
    </div>
  );
}
