import { useState } from "react";
import type { Comment } from "../types";
import CommentEditor from "./CommentEditor";

interface Props {
  comment: Comment;
  onEdit: (id: string, text: string) => void;
  onDelete: (id: string) => void;
}

export default function CommentIndicator({ comment, onEdit, onDelete }: Props) {
  const [editing, setEditing] = useState(false);
  const [expanded, setExpanded] = useState(false);

  if (editing) {
    return (
      <CommentEditor
        initialValue={comment.comment}
        onSave={(text) => { onEdit(comment.id, text); setEditing(false); }}
        onCancel={() => setEditing(false)}
      />
    );
  }

  return (
    <div className="comment-indicator" onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}>
      <div className="comment-indicator-bar">
        <span className="comment-indicator-preview">
          {comment.comment.length > 80
            ? comment.comment.slice(0, 80) + "..."
            : comment.comment}
        </span>
      </div>
      {expanded && (
        <div className="comment-indicator-actions">
          <button className="comment-btn comment-btn-edit" onClick={(e) => { e.stopPropagation(); setEditing(true); }}>
            Edit
          </button>
          <button className="comment-btn comment-btn-delete" onClick={(e) => { e.stopPropagation(); onDelete(comment.id); }}>
            Delete
          </button>
        </div>
      )}
    </div>
  );
}
