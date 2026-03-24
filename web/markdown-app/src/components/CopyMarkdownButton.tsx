import { useState, useCallback } from "react";

interface Props {
  content: string;
}

export default function CopyMarkdownButton({ content }: Props) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(content);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = content;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }, [content]);

  return (
    <button className="copy-md-btn" onClick={handleCopy}>
      {copied ? "Copied!" : "Copy as Markdown"}
    </button>
  );
}
