import { useEffect, useRef, useState } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { useReview } from "../context/ReviewContext";

const THUMB_WIDTH = 56;

interface ThumbProps {
  pdf: PDFDocumentProxy;
  pageNumber: number;
  current: boolean;
  hasComments: boolean;
  onSelect: (page: number) => void;
  scrollRoot: HTMLDivElement | null;
}

function Thumb({ pdf, pageNumber, current, hasComments, onSelect, scrollRoot }: ThumbProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapperRef = useRef<HTMLButtonElement>(null);
  const renderedRef = useRef(false);

  useEffect(() => {
    const el = wrapperRef.current;
    if (!el || !scrollRoot) return;
    const obs = new IntersectionObserver(
      async (entries) => {
        if (!entries[0]?.isIntersecting || renderedRef.current) return;
        renderedRef.current = true;
        obs.disconnect();
        try {
          const page = await pdf.getPage(pageNumber);
          const base = page.getViewport({ scale: 1 });
          const viewport = page.getViewport({ scale: (THUMB_WIDTH * 2) / base.width });
          const canvas = canvasRef.current;
          const ctx = canvas?.getContext("2d");
          if (!canvas || !ctx) return;
          canvas.width = viewport.width;
          canvas.height = viewport.height;
          await page.render({ canvasContext: ctx, viewport }).promise;
        } catch (e) {
          console.warn(`Failed to render thumbnail ${pageNumber}:`, e);
        }
      },
      { root: scrollRoot, rootMargin: "0px 200px" },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [pdf, pageNumber, scrollRoot]);

  return (
    <button
      ref={wrapperRef}
      className={`filmstrip-thumb ${current ? "current" : ""}`}
      onClick={() => onSelect(pageNumber)}
      aria-label={`Go to page ${pageNumber}`}
    >
      <canvas ref={canvasRef} className="filmstrip-canvas" />
      <span className="filmstrip-label">
        {pageNumber}
        {hasComments && <span className="filmstrip-dot" />}
      </span>
    </button>
  );
}

interface Props {
  pdf: PDFDocumentProxy;
  numPages: number;
  currentPage: number;
  onSelect: (page: number) => void;
}

export default function Filmstrip({ pdf, numPages, currentPage, onSelect }: Props) {
  const { comments } = useReview();
  const stripRef = useRef<HTMLDivElement>(null);
  const [root, setRoot] = useState<HTMLDivElement | null>(null);

  useEffect(() => setRoot(stripRef.current), []);

  // Keep the current-page thumbnail in view as the user scrolls the deck.
  useEffect(() => {
    const strip = stripRef.current;
    if (!strip) return;
    const thumb = strip.querySelector<HTMLElement>(
      `.filmstrip-thumb:nth-child(${currentPage})`,
    );
    thumb?.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
  }, [currentPage]);

  const commentedPages = new Set(comments.map((c) => c.page));

  return (
    <div className="filmstrip" ref={stripRef}>
      {Array.from({ length: numPages }, (_, i) => (
        <Thumb
          key={i + 1}
          pdf={pdf}
          pageNumber={i + 1}
          current={currentPage === i + 1}
          hasComments={commentedPages.has(i + 1)}
          onSelect={onSelect}
          scrollRoot={root}
        />
      ))}
    </div>
  );
}
