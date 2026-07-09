import React, { Component, useEffect, useRef, useState } from "react";
import * as pdfjsLib from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { initTelegram } from "./telegram";
import { injectStyles } from "./styles";
import { fetchPdf, submitReview } from "./api";
import PdfPage from "./components/PdfPage";
import Filmstrip from "./components/Filmstrip";
import SubmitDialog from "./components/SubmitDialog";
import { ReviewProvider, useReview } from "./context/ReviewContext";

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl;

class ErrorBoundary extends Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="loading error">
          <strong>Render error:</strong> {this.state.error.message}
        </div>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  return (
    <ErrorBoundary>
      <ReviewProvider>
        <AppInner />
      </ReviewProvider>
    </ErrorBoundary>
  );
}

function AppInner() {
  const { comments, clearComments } = useReview();
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [defaultAspect, setDefaultAspect] = useState(297 / 210);
  const [error, setError] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [showSubmitDialog, setShowSubmitDialog] = useState(false);
  const pagesRef = useRef<HTMLDivElement>(null);

  const params = new URLSearchParams(window.location.search);
  const filePath = params.get("path");
  const chatId = Number(params.get("chat_id"));
  const threadIdParam = params.get("thread_id");
  const threadId = threadIdParam ? Number(threadIdParam) : null;

  useEffect(() => {
    initTelegram();
    injectStyles();

    if (!filePath) {
      setError("No path provided.");
      return;
    }

    let doc: PDFDocumentProxy | null = null;
    let cancelled = false;
    (async () => {
      try {
        const data = await fetchPdf(filePath);
        doc = await pdfjsLib.getDocument({
          data,
          isEvalSupported: false,
          disableAutoFetch: true,
        }).promise;
        if (cancelled) return;
        const page1 = await doc.getPage(1);
        const vp = page1.getViewport({ scale: 1 });
        if (cancelled) return;
        setDefaultAspect(vp.height / vp.width);
        setPdf(doc);
        document.title = filePath.split("/").pop() ?? "PDF Review";
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();

    return () => {
      cancelled = true;
      doc?.destroy();
    };
  }, []);

  // Track which page occupies the middle of the viewport.
  useEffect(() => {
    if (!pdf) return;
    const container = pagesRef.current;
    if (!container) return;
    const obs = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            const page = Number((entry.target as HTMLElement).dataset.page);
            if (page) setCurrentPage(page);
          }
        }
      },
      { rootMargin: "-45% 0px -45% 0px" },
    );
    container.querySelectorAll(".pdf-page").forEach((el) => obs.observe(el));
    return () => obs.disconnect();
  }, [pdf]);

  const scrollToPage = (page: number) => {
    pagesRef.current
      ?.querySelector(`.pdf-page[data-page="${page}"]`)
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const handleSubmit = async (keepOpen: boolean) => {
    await submitReview({
      chatId,
      threadId,
      path: filePath!,
      comments,
    });
    if (keepOpen) {
      clearComments();
      setShowSubmitDialog(false);
    } else {
      window.Telegram?.WebApp?.close();
    }
  };

  if (error) return <div className="loading error">{error}</div>;
  if (!pdf) return <div className="loading">Loading PDF...</div>;

  return (
    <>
      <div className="pages" ref={pagesRef}>
        {Array.from({ length: pdf.numPages }, (_, i) => (
          <PdfPage
            key={i + 1}
            pdf={pdf}
            pageNumber={i + 1}
            defaultAspect={defaultAspect}
          />
        ))}
      </div>
      {comments.length > 0 && (
        <div className="review-toolbar">
          <button
            className="submit-review-btn"
            onClick={() => setShowSubmitDialog(true)}
          >
            Submit Review ({comments.length})
          </button>
        </div>
      )}
      <Filmstrip
        pdf={pdf}
        numPages={pdf.numPages}
        currentPage={currentPage}
        onSelect={scrollToPage}
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
