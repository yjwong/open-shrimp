import { useEffect, useState, useRef, useCallback } from "react";
import type { Hunk } from "../lib/types";
import { highlightLines, type HighlightedLine } from "../lib/shiki";

interface HunkCardProps {
  hunk: Hunk;
  onStageFile?: () => void;
  comment?: string;
  onCommentChange?: (comment: string) => void;
}

export function HunkCard({ hunk, onStageFile, comment, onCommentChange }: HunkCardProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [commentOpen, setCommentOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Close menu on outside tap.
  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e: MouseEvent | TouchEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    document.addEventListener("touchstart", handler);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("touchstart", handler);
    };
  }, [menuOpen]);

  const handleStageFile = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      setMenuOpen(false);
      onStageFile?.();
    },
    [onStageFile],
  );
  // Auto-open comment area if there's already a comment.
  useEffect(() => {
    if (comment) setCommentOpen(true);
  }, [hunk.id]);

  // Focus textarea when comment area opens.
  useEffect(() => {
    if (commentOpen && textareaRef.current) {
      textareaRef.current.focus();
    }
  }, [commentOpen]);

  const handleCommentToggle = useCallback((e: React.MouseEvent) => {
    e.stopPropagation();
    setCommentOpen((o) => !o);
  }, []);

  const [lines, setLines] = useState<HighlightedLine[] | null>(null);

  useEffect(() => {
    let cancelled = false;

    if (hunk.is_binary || hunk.is_empty || hunk.lines.length === 0) {
      setLines([]);
      return;
    }

    highlightLines(hunk.lines, hunk.language).then((result) => {
      if (!cancelled) setLines(result);
    });

    return () => {
      cancelled = true;
    };
  }, [hunk]);

  const headerRight = (
    <>
      {hunk.staged && <span className="hunk-card-badge">Staged</span>}
      {onStageFile && (
        <div className="hunk-card-menu" ref={menuRef}>
          <button
            className="hunk-card-menu-btn"
            onClick={(e) => {
              e.stopPropagation();
              setMenuOpen((o) => !o);
            }}
            aria-label="File actions"
          >
            ⋮
          </button>
          {menuOpen && (
            <div className="hunk-card-menu-dropdown">
              <button
                className="hunk-card-menu-item"
                onClick={handleStageFile}
              >
                Stage entire file
              </button>
            </div>
          )}
        </div>
      )}
    </>
  );

  if (hunk.is_binary) {
    return (
      <div className="hunk-card">
        <div className={`hunk-card-header${hunk.staged ? " staged" : ""}`}>
          <span className="hunk-card-filepath">{hunk.file_path}</span>
          {headerRight}
        </div>
        <div className="hunk-card-binary">
          <div className="binary-icon">📦</div>
          <div>Binary file changed</div>
        </div>
      </div>
    );
  }

  if (hunk.is_empty) {
    return (
      <div className="hunk-card">
        <div className={`hunk-card-header${hunk.staged ? " staged" : ""}`}>
          <span className="hunk-card-filepath">{hunk.file_path}</span>
          {headerRight}
        </div>
        <div className="hunk-card-binary">
          <div className="binary-icon">📄</div>
          <div>Empty file</div>
        </div>
      </div>
    );
  }

  const commentFooter = onCommentChange && (
    <div className="hunk-card-comment">
      {commentOpen ? (
        <textarea
          ref={textareaRef}
          className="hunk-card-comment-input"
          placeholder="Leave a comment for Claude..."
          value={comment ?? ""}
          onChange={(e) => onCommentChange(e.target.value)}
          rows={2}
        />
      ) : (
        <button
          className={`hunk-card-comment-btn${comment ? " has-comment" : ""}`}
          onClick={handleCommentToggle}
        >
          {comment ? `Comment: ${comment.slice(0, 40)}${comment.length > 40 ? "..." : ""}` : "Add comment"}
        </button>
      )}
    </div>
  );

  return (
    <div className="hunk-card">
      <div className={`hunk-card-header${hunk.staged ? " staged" : ""}`}>
        <span className="hunk-card-filepath">{hunk.file_path}</span>
        {headerRight}
      </div>
      <div className="hunk-card-meta">{hunk.hunk_header}</div>
      <div className="hunk-card-body">
        {lines === null ? (
          <div className="hunk-card-loading">
            <div className="spinner" />
          </div>
        ) : (
          <div className="hunk-card-lines">
            {lines.map((line, i) => (
              <div key={i} className={`diff-line diff-line-${line.type}`}>
                <span className="line-no">{line.old_no ?? " "}</span>
                <span className="line-no">{line.new_no ?? " "}</span>
                <span className="line-prefix">
                  {line.type === "add" ? "+" : line.type === "delete" ? "-" : " "}
                </span>
                <span
                  className="line-content"
                  dangerouslySetInnerHTML={{ __html: line.html || "&nbsp;" }}
                />
              </div>
            ))}
          </div>
        )}
      </div>
      {commentFooter}
    </div>
  );
}
