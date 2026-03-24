import { useState, useRef, type ReactNode } from "react";
import { useReview } from "../context/ReviewContext";
import CommentEditor from "./CommentEditor";
import CommentIndicator from "./CommentIndicator";

interface Props {
  blockIndex: number;
  children: ReactNode;
}

export default function CommentableBlock({ blockIndex, children }: Props) {
  const { reviewMode, comments, addComment, editComment, deleteComment } = useReview();
  const [showEditor, setShowEditor] = useState(false);
  const blockRef = useRef<HTMLDivElement>(null);

  const blockComments = comments.filter((c) => c.blockIndex === blockIndex);

  const handleTap = () => {
    if (!reviewMode || showEditor) return;
    setShowEditor(true);
  };

  const getBlockText = (): string => {
    return (blockRef.current?.textContent ?? "").slice(0, 200);
  };

  return (
    <div
      ref={blockRef}
      className={`commentable-block ${reviewMode ? "commentable-block--active" : ""}`}
      onClick={handleTap}
    >
      {children}

      {blockComments.map((c) => (
        <CommentIndicator
          key={c.id}
          comment={c}
          onEdit={editComment}
          onDelete={deleteComment}
        />
      ))}

      {showEditor && (
        <CommentEditor
          onSave={(text) => {
            addComment(blockIndex, getBlockText(), text);
            setShowEditor(false);
          }}
          onCancel={() => setShowEditor(false)}
        />
      )}
    </div>
  );
}
