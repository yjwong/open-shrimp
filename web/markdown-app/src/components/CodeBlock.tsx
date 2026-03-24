import type { HTMLAttributes } from "react";
import MermaidDiagram from "./MermaidDiagram";

interface Props extends HTMLAttributes<HTMLElement> {
  className?: string;
  children?: React.ReactNode;
}

export default function CodeBlock({ className, children, ...props }: Props) {
  const match = /language-(\w+)/.exec(className || "");
  const lang = match?.[1];
  const code = String(children).replace(/\n$/, "");

  if (lang === "mermaid") {
    return <MermaidDiagram chart={code} />;
  }

  return (
    <code className={className} {...props}>
      {children}
    </code>
  );
}
