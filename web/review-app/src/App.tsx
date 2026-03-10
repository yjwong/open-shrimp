import { useMemo } from "react";
import { useHunks } from "./hooks/useHunks";

function getChatId(): string | null {
  const params = new URLSearchParams(window.location.search);
  return params.get("chat_id");
}

export default function App() {
  const chatId = useMemo(() => getChatId(), []);
  const { hunks, totalHunks, loading, error, refresh } = useHunks(chatId);

  if (loading) {
    return (
      <div className="loading">
        <div className="spinner" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="error-container">
        <p>{error}</p>
        <button onClick={refresh}>Retry</button>
      </div>
    );
  }

  if (hunks.length === 0) {
    return (
      <div className="empty-container">
        <h2>No changes</h2>
        <p>There are no uncommitted changes to review.</p>
      </div>
    );
  }

  return (
    <>
      <div style={{ padding: "12px 16px", color: "var(--text-secondary)", fontSize: 13 }}>
        {totalHunks} hunk{totalHunks !== 1 ? "s" : ""} to review
      </div>
      <div className="hunk-list">
        {hunks.map((hunk) => (
          <div key={hunk.id} className={`hunk-item${hunk.staged ? " staged" : ""}`}>
            <div className="file-path">{hunk.file_path}</div>
            <div className="hunk-id">{hunk.id}</div>
          </div>
        ))}
      </div>
    </>
  );
}
