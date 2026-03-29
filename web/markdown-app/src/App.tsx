import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import "highlight.js/styles/github-dark.min.css";
import { initTelegram, getAuthHeader } from "./telegram";
import { injectStyles } from "./styles";
import { submitReview } from "./api";
import CodeBlock from "./components/CodeBlock";
import CommentableBlock from "./components/CommentableBlock";
import ReviewToolbar from "./components/ReviewToolbar";
import SubmitDialog from "./components/SubmitDialog";
import { ReviewProvider, useReview } from "./context/ReviewContext";
import type { DocumentData } from "./types";

function wrapBlock(Tag: string) {
  return function WrappedBlock({ node, ...props }: any) {
    const blockIndex = node?.position?.start?.line ?? 0;
    return (
      <CommentableBlock blockIndex={blockIndex}>
        <Tag {...props} />
      </CommentableBlock>
    );
  };
}

export default function App() {
  return (
    <ReviewProvider>
      <AppInner />
    </ReviewProvider>
  );
}

function AppInner() {
  const { reviewMode, comments } = useReview();
  const [data, setData] = useState<DocumentData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showSubmitDialog, setShowSubmitDialog] = useState(false);

  const params = new URLSearchParams(window.location.search);
  const filePath = params.get("path");
  const contentId = params.get("content_id");
  const chatId = Number(params.get("chat_id"));
  const threadIdParam = params.get("thread_id");
  const threadId = threadIdParam ? Number(threadIdParam) : null;

  useEffect(() => {
    initTelegram();
    injectStyles();

    let url: string;
    if (contentId) {
      url = `/api/preview/content/${encodeURIComponent(contentId)}`;
    } else if (filePath) {
      url = `/api/preview/read?path=${encodeURIComponent(filePath)}`;
    } else {
      setError("No path or content_id provided.");
      return;
    }

    fetch(url, { headers: getAuthHeader() })
      .then(async (resp) => {
        if (!resp.ok) {
          const text = await resp.text();
          setError(`Error (${resp.status}): ${text}`);
          return;
        }
        const json = await resp.json();
        setData(json);
        document.title = json.filename;
      })
      .catch((e) => setError(`Fatal: ${e}`));
  }, []);

  const handleSubmit = async () => {
    await submitReview({
      chatId,
      threadId,
      ...(contentId ? { contentId } : { path: filePath! }),
      comments,
    });
    window.Telegram?.WebApp?.close();
  };

  if (error) return <div className="loading error">{error}</div>;
  if (!data) return <div className="loading">Loading preview...</div>;

  return (
    <>
      <div id="content">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          rehypePlugins={[[rehypeHighlight, { ignoreMissing: true }]]}
          components={{
            code: CodeBlock,
            p: wrapBlock("p"),
            h1: wrapBlock("h1"),
            h2: wrapBlock("h2"),
            h3: wrapBlock("h3"),
            h4: wrapBlock("h4"),
            h5: wrapBlock("h5"),
            h6: wrapBlock("h6"),
            li: wrapBlock("li"),
            table: wrapBlock("table"),
          }}
        >
          {data.content}
        </ReactMarkdown>
      </div>
      <ReviewToolbar
        onSubmit={() => setShowSubmitDialog(true)}
        copyContent={!reviewMode ? data.content : undefined}
      />
      {showSubmitDialog && (
        <SubmitDialog
          comments={comments}
          onConfirm={handleSubmit}
          onCancel={() => setShowSubmitDialog(false)}
        />
      )}
    </>
  );
}
