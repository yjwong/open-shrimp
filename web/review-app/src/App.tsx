import { useMemo, useState, useEffect } from "react";
import { useHunks } from "./hooks/useHunks";
import { initShiki } from "./lib/shiki";
import { SwipeDeck } from "./components/SwipeDeck";

function getChatId(): string | null {
  const params = new URLSearchParams(window.location.search);
  return params.get("chat_id");
}

export default function App() {
  const chatId = useMemo(() => getChatId(), []);
  const { hunks, totalHunks, loading, error, refresh } = useHunks(chatId);
  const [shikiReady, setShikiReady] = useState(false);
  const [shikiError, setShikiError] = useState<string | null>(null);

  useEffect(() => {
    initShiki()
      .then(() => setShikiReady(true))
      .catch((err) =>
        setShikiError(
          err instanceof Error ? err.message : "Failed to load syntax highlighter",
        ),
      );
  }, []);

  if (loading || !shikiReady) {
    return (
      <div className="loading">
        <div className="spinner" />
      </div>
    );
  }

  if (shikiError) {
    return (
      <div className="error-container">
        <p>Syntax highlighter failed to load: {shikiError}</p>
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
    <SwipeDeck
      hunks={hunks}
      totalHunks={totalHunks}
      onRefresh={refresh}
    />
  );
}
