import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import "highlight.js/styles/github-dark.min.css";
import { initTelegram, getAuthHeader } from "./telegram";
import { injectStyles } from "./styles";
import CodeBlock from "./components/CodeBlock";
import type { DocumentData } from "./types";

export default function App() {
  const [data, setData] = useState<DocumentData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    initTelegram();
    injectStyles();
    const params = new URLSearchParams(window.location.search);
    const filePath = params.get("path");
    if (!filePath) {
      setError("No path provided.");
      return;
    }

    fetch(`/api/preview/read?path=${encodeURIComponent(filePath)}`, {
      headers: getAuthHeader(),
    })
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

  if (error) {
    return <div className="loading error">{error}</div>;
  }
  if (!data) {
    return <div className="loading">Loading preview...</div>;
  }

  return (
    <div id="content">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { ignoreMissing: true }]]}
        components={{
          code: CodeBlock,
        }}
      >
        {data.content}
      </ReactMarkdown>
    </div>
  );
}
