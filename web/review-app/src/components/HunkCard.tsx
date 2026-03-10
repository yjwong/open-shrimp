import { useEffect, useState } from "react";
import type { Hunk } from "../lib/types";
import { highlightLines, type HighlightedLine } from "../lib/shiki";

interface HunkCardProps {
  hunk: Hunk;
}

export function HunkCard({ hunk }: HunkCardProps) {
  const [lines, setLines] = useState<HighlightedLine[] | null>(null);

  useEffect(() => {
    let cancelled = false;

    if (hunk.is_binary || hunk.lines.length === 0) {
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

  if (hunk.is_binary) {
    return (
      <div className="hunk-card">
        <div className={`hunk-card-header${hunk.staged ? " staged" : ""}`}>
          <span className="hunk-card-filepath">{hunk.file_path}</span>
          {hunk.staged && <span className="hunk-card-badge">Staged</span>}
        </div>
        <div className="hunk-card-binary">
          <div className="binary-icon">📦</div>
          <div>Binary file changed</div>
        </div>
      </div>
    );
  }

  return (
    <div className="hunk-card">
      <div className={`hunk-card-header${hunk.staged ? " staged" : ""}`}>
        <span className="hunk-card-filepath">{hunk.file_path}</span>
        {hunk.staged && <span className="hunk-card-badge">Staged</span>}
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
    </div>
  );
}
