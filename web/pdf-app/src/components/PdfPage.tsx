import { useEffect, useRef, useState } from "react";
import type { PDFDocumentProxy, RenderTask } from "pdfjs-dist";
import { useReview } from "../context/ReviewContext";
import CommentEditor from "./CommentEditor";
import PageComments from "./PageComments";

interface Props {
  pdf: PDFDocumentProxy;
  pageNumber: number;
  defaultAspect: number;
}

// Render pages within ~1.5 viewports of the visible area; release the
// canvas bitmap once a page scrolls far away so large decks stay cheap.
const RENDER_MARGIN = "150% 0px";

export default function PdfPage({ pdf, pageNumber, defaultAspect }: Props) {
  const { comments, addComment } = useReview();
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const renderTaskRef = useRef<RenderTask | null>(null);
  const [near, setNear] = useState(false);
  const [aspect, setAspect] = useState(defaultAspect);
  const [rendered, setRendered] = useState(false);
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      (entries) => setNear(entries[0]?.isIntersecting ?? false),
      { rootMargin: RENDER_MARGIN },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    const el = containerRef.current;
    if (!canvas || !el) return;

    if (!near) {
      renderTaskRef.current?.cancel();
      renderTaskRef.current = null;
      canvas.width = 0;
      canvas.height = 0;
      setRendered(false);
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        const page = await pdf.getPage(pageNumber);
        if (cancelled) return;
        const base = page.getViewport({ scale: 1 });
        setAspect(base.height / base.width);
        const cssWidth = el.clientWidth;
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const viewport = page.getViewport({ scale: (cssWidth / base.width) * dpr });
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        const task = page.render({ canvasContext: ctx, viewport });
        renderTaskRef.current = task;
        await task.promise;
        if (!cancelled) setRendered(true);
      } catch (e) {
        // RenderingCancelledException on fast scroll — expected.
        if (!cancelled) console.warn(`Failed to render page ${pageNumber}:`, e);
      }
    })();

    return () => {
      cancelled = true;
      renderTaskRef.current?.cancel();
      renderTaskRef.current = null;
    };
  }, [near, pdf, pageNumber]);

  const pageComments = comments.filter((c) => c.page === pageNumber);

  return (
    <div className="pdf-page" data-page={pageNumber} ref={containerRef}>
      <div
        className="pdf-page-frame"
        style={{ aspectRatio: `${1 / aspect}` }}
      >
        <canvas
          ref={canvasRef}
          className="pdf-page-canvas"
          style={{ visibility: rendered ? "visible" : "hidden" }}
        />
        {!rendered && <div className="pdf-page-loading">Page {pageNumber}</div>}
        <div className="pdf-page-badge">{pageNumber}</div>
        <button
          className={`pdf-page-comment-btn ${pageComments.length > 0 ? "has-comments" : ""}`}
          onClick={() => setEditing(true)}
          aria-label={`Comment on page ${pageNumber}`}
        >
          💬{pageComments.length > 0 ? ` ${pageComments.length}` : ""}
        </button>
      </div>
      {editing && (
        <CommentEditor
          onSave={(text) => {
            addComment(pageNumber, text);
            setEditing(false);
          }}
          onCancel={() => setEditing(false)}
        />
      )}
      <PageComments page={pageNumber} />
    </div>
  );
}
