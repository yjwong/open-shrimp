interface SummaryScreenProps {
  stagedCount: number;
  skippedCount: number;
  onRefresh: () => void;
  onClose: () => void;
}

export function SummaryScreen({
  stagedCount,
  skippedCount,
  onRefresh,
  onClose,
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
