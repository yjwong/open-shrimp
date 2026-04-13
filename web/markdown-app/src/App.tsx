import { useEffect, useMemo, useState } from "react";
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

/**
 * Resolve a potentially relative image `src` to an absolute filesystem path
 * using the parent directory of the markdown file, then return a proxy URL.
 * External URLs (http/https/data) are returned as-is.
 */
function resolveImageSrc(
  src: string,
  fileDir: string | null,
  authToken: string | undefined,
): string {
  if (!src) return src;
  if (/^https?:\/\/|^data:/i.test(src)) return src;
  if (!fileDir) return src;

  let absolute: string;
  if (src.startsWith("/")) {
    absolute = src;
  } else {
    const parts = (fileDir + "/" + src).split("/");
    const resolved: string[] = [];
    for (const p of parts) {
      if (p === "" || p === ".") continue;
      if (p === ".." && resolved.length > 0) {
        resolved.pop();
      } else if (p !== "..") {
        resolved.push(p);
      }
    }
    absolute = "/" + resolved.join("/");
  }

  const params = new URLSearchParams({ path: absolute });
  if (authToken) params.set("token", authToken);
  return `/api/preview/image?${params.toString()}`;
}

function makeImageComponent(fileDir: string | null) {
  // Resolve auth token once per component creation, not per image.
  const authToken =
    new URLSearchParams(window.location.search).get("token") ??
    window.Telegram?.WebApp?.initData ??
    undefined;

  return function ProxiedImage(props: React.ImgHTMLAttributes<HTMLImageElement>) {
    const src = resolveImageSrc(props.src ?? "", fileDir, authToken);
    return <img {...props} src={src} />;
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

  const fileDir = data.path ? data.path.replace(/\/[^/]*$/, "") || "/" : null;
  const ProxiedImage = useMemo(() => makeImageComponent(fileDir), [fileDir]);

  return (
    <>
      <div id="content">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          rehypePlugins={[[rehypeHighlight, { ignoreMissing: true }]]}
          components={{
            code: CodeBlock,
            img: ProxiedImage,
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
