import { createHighlighter, type Highlighter, type BundledLanguage } from "shiki";
import type { HunkLine } from "./types";

let highlighterPromise: Promise<Highlighter> | null = null;
const loadedLanguages = new Set<string>();

const LANG_MAP: Record<string, string> = {
  py: "python",
  js: "javascript",
  ts: "typescript",
  tsx: "tsx",
  jsx: "jsx",
  rs: "rust",
  go: "go",
  yml: "yaml",
  yaml: "yaml",
  json: "json",
  md: "markdown",
  css: "css",
  html: "html",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  toml: "toml",
  sql: "sql",
  rb: "ruby",
  java: "java",
  c: "c",
  cpp: "cpp",
  h: "c",
  hpp: "cpp",
  swift: "swift",
  kt: "kotlin",
  dockerfile: "dockerfile",
  makefile: "makefile",
};

function resolveLanguage(lang: string): string {
  const lower = lang.toLowerCase();
  return LANG_MAP[lower] ?? lower;
}

async function getHighlighter(): Promise<Highlighter> {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighter({
      themes: ["github-dark"],
      langs: [],
    });
  }
  return highlighterPromise;
}

async function ensureLanguage(
  highlighter: Highlighter,
  language: string,
): Promise<string> {
  const resolved = resolveLanguage(language);
  if (!loadedLanguages.has(resolved)) {
    try {
      await highlighter.loadLanguage(resolved as Parameters<Highlighter["loadLanguage"]>[0]);
      loadedLanguages.add(resolved);
    } catch {
      // Language not available in Shiki — fall back to plaintext
      if (!loadedLanguages.has("text")) {
        loadedLanguages.add("text");
      }
      return "text";
    }
  }
  return resolved;
}

export async function initShiki(): Promise<void> {
  await getHighlighter();
}

export interface HighlightedLine {
  html: string;
  type: HunkLine["type"];
  old_no: number | null;
  new_no: number | null;
}

export async function highlightLines(
  lines: HunkLine[],
  language: string,
): Promise<HighlightedLine[]> {
  const highlighter = await getHighlighter();
  const resolvedLang = await ensureLanguage(highlighter, language);

  // Combine all lines into a single string for correct tokenization context
  const code = lines.map((l) => l.content).join("\n");

  const result = highlighter.codeToTokens(code, {
    lang: resolvedLang as BundledLanguage,
    theme: "github-dark",
  });

  return result.tokens.map((tokenLine, i) => {
    const line = lines[i]!;
    const html = tokenLine
      .map((token) => {
        const escaped = escapeHtml(token.content);
        if (token.color) {
          return `<span style="color:${token.color}">${escaped}</span>`;
        }
        return escaped;
      })
      .join("");

    return {
      html,
      type: line.type,
      old_no: line.old_no,
      new_no: line.new_no,
    };
  });
}

function escapeHtml(str: string): string {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
