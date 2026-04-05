import { useMemo } from "react";
import { useHunks } from "./hooks/useHunks";
import { SwipeDeck } from "./components/SwipeDeck";

function getParams() {
  const params = new URLSearchParams(window.location.search);
  return {
    chatId: params.get("chat_id"),
    dir: params.get("dir") ?? "0",
    threadId: params.get("thread_id"),
  };
}

export default function App() {
  const { chatId, dir, threadId } = useMemo(() => getParams(), []);
  const { hunks, totalHunks, files, loading, error, refresh, loadMore, updateFileStagedCount } = useHunks(chatId, dir, threadId);

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
    <SwipeDeck
      hunks={hunks}
      totalHunks={totalHunks}
      files={files}
      chatId={chatId!}
      dir={dir}
      threadId={threadId}
      onRefresh={refresh}
      onNeedMore={loadMore}
      onUpdateFileStagedCount={updateFileStagedCount}
    />
  );
}
