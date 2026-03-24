import { useState } from "react";

interface Props {
  initialValue?: string;
  onSave: (text: string) => void;
  onCancel: () => void;
}

export default function CommentEditor({ initialValue = "", onSave, onCancel }: Props) {
  const [text, setText] = useState(initialValue);

  return (
    <div className="comment-editor" onClick={(e) => e.stopPropagation()}>
      <textarea
        className="comment-textarea"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Add a comment..."
        rows={3}
        autoFocus
      />
      <div className="comment-editor-actions">
        <button className="comment-btn comment-btn-save" onClick={() => { if (text.trim()) onSave(text.trim()); }} disabled={!text.trim()}>
          Save
        </button>
        <button className="comment-btn comment-btn-cancel" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
