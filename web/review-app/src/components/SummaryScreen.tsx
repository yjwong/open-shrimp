interface SummaryScreenProps {
  stagedCount: number;
  skippedCount: number;
  hasStagedHunks: boolean;
  commentCount: number;
  onRefresh: () => void;
  onClose: () => void;
  onCommit: () => void;
  onSubmitComments: () => void;
}

export function SummaryScreen({
  stagedCount,
  skippedCount,
  hasStagedHunks,
  commentCount,
  onRefresh,
  onClose,
  onCommit,
  onSubmitComments,
}: SummaryScreenProps) {
  const total = stagedCount + skippedCount;

  return (
    <div className="summary-screen">
      <h2>Review Complete</h2>
      <div className="summary-stats">
        <div className="summary-stat">
          <div className="summary-stat-value staged">{stagedCount}</div>
          <div className="summary-stat-label">Staged</div>
        </div>
        <div className="summary-stat">
          <div className="summary-stat-value skipped">{skippedCount}</div>
          <div className="summary-stat-label">Skipped</div>
        </div>
        <div className="summary-stat">
          <div className="summary-stat-value total">{total}</div>
          <div className="summary-stat-label">Total</div>
        </div>
      </div>
      <div className="summary-actions">
        {commentCount > 0 && (
          <button className="summary-btn summary-btn-comments" onClick={onSubmitComments}>
            Submit {commentCount} Comment{commentCount !== 1 ? "s" : ""}
          </button>
        )}
        {hasStagedHunks && (
          <button className="summary-btn summary-btn-commit" onClick={onCommit}>
            Commit Staged Changes
          </button>
        )}
        <button className="summary-btn summary-btn-primary" onClick={onClose}>
          Done
        </button>
        <button className="summary-btn summary-btn-secondary" onClick={onRefresh}>
          Refresh &amp; Review Again
        </button>
      </div>
    </div>
  );
}
