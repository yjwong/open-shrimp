import { useEffect, useRef, useState, useCallback } from "react";
import { getThemeParams } from "../telegram";

interface Props {
  chart: string;
}

export default function MermaidDiagram({ chart }: Props) {
  const [svgHtml, setSvgHtml] = useState<string>("");
  const [fullscreen, setFullscreen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const mermaid = (await import("mermaid")).default;
      const tp = getThemeParams();
      mermaid.initialize({
        startOnLoad: false,
        theme: "dark",
        themeVariables: tp
          ? {
              primaryColor: tp.button_color ?? "#7aa2f7",
              primaryTextColor: tp.button_text_color ?? "#c9d1d9",
              lineColor: tp.hint_color ?? "#8b949e",
              secondaryColor: tp.secondary_bg_color ?? "#161b22",
              tertiaryColor: tp.bg_color ?? "#1a1b26",
            }
          : undefined,
      });
      const id = `mermaid-${Math.random().toString(36).slice(2)}`;
      const { svg } = await mermaid.render(id, chart);
      if (!cancelled) setSvgHtml(svg);
    })();
    return () => { cancelled = true; };
  }, [chart]);

  if (!svgHtml) return <pre className="mermaid">{chart}</pre>;

  return (
    <>
      <div className="mermaid-wrapper">
        <div
          className="mermaid-rendered"
          dangerouslySetInnerHTML={{ __html: svgHtml }}
        />
        <button
          className="mermaid-fullscreen-btn"
          onClick={() => setFullscreen(true)}
        >
          Fullscreen
        </button>
      </div>
      {fullscreen && (
        <FullscreenViewer svgHtml={svgHtml} onClose={() => setFullscreen(false)} />
      )}
    </>
  );
}

function FullscreenViewer({ svgHtml, onClose }: { svgHtml: string; onClose: () => void }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef({
    scale: 1, translateX: 0, translateY: 0,
    startDist: 0, startScale: 1,
    isPanning: false, panStartX: 0, panStartY: 0,
    startTranslateX: 0, startTranslateY: 0,
    lastTap: 0,
  });

  const applyTransform = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const s = stateRef.current;
    el.style.transform = `translate(${s.translateX}px, ${s.translateY}px) scale(${s.scale})`;
  }, []);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  const onWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    const s = stateRef.current;
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    s.scale = Math.min(Math.max(s.scale * delta, 0.2), 10);
    applyTransform();
  }, [applyTransform]);

  const onTouchStart = useCallback((e: React.TouchEvent) => {
    const s = stateRef.current;
    if (e.touches.length === 2) {
      s.startDist = Math.hypot(
        e.touches[1]!.clientX - e.touches[0]!.clientX,
        e.touches[1]!.clientY - e.touches[0]!.clientY,
      );
      s.startScale = s.scale;
    } else if (e.touches.length === 1) {
      s.isPanning = true;
      s.panStartX = e.touches[0]!.clientX;
      s.panStartY = e.touches[0]!.clientY;
      s.startTranslateX = s.translateX;
      s.startTranslateY = s.translateY;
    }
  }, []);

  const onTouchMove = useCallback((e: React.TouchEvent) => {
    e.preventDefault();
    const s = stateRef.current;
    if (e.touches.length === 2) {
      const dist = Math.hypot(
        e.touches[1]!.clientX - e.touches[0]!.clientX,
        e.touches[1]!.clientY - e.touches[0]!.clientY,
      );
      s.scale = Math.min(Math.max(s.startScale * (dist / s.startDist), 0.2), 10);
      applyTransform();
    } else if (e.touches.length === 1 && s.isPanning) {
      s.translateX = s.startTranslateX + (e.touches[0]!.clientX - s.panStartX);
      s.translateY = s.startTranslateY + (e.touches[0]!.clientY - s.panStartY);
      applyTransform();
    }
  }, [applyTransform]);

  const onTouchEnd = useCallback(() => {
    stateRef.current.isPanning = false;
  }, []);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    const s = stateRef.current;
    s.isPanning = true;
    s.panStartX = e.clientX;
    s.panStartY = e.clientY;
    s.startTranslateX = s.translateX;
    s.startTranslateY = s.translateY;
  }, []);

  const onMouseMove = useCallback((e: React.MouseEvent) => {
    const s = stateRef.current;
    if (!s.isPanning) return;
    s.translateX = s.startTranslateX + (e.clientX - s.panStartX);
    s.translateY = s.startTranslateY + (e.clientY - s.panStartY);
    applyTransform();
  }, [applyTransform]);

  const onMouseUp = useCallback(() => {
    stateRef.current.isPanning = false;
  }, []);

  const onClick = useCallback(() => {
    const s = stateRef.current;
    const now = Date.now();
    if (now - s.lastTap < 300) {
      s.scale = 1;
      s.translateX = 0;
      s.translateY = 0;
      applyTransform();
    }
    s.lastTap = now;
  }, [applyTransform]);

  return (
    <div className="mermaid-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div
        className="mermaid-viewport"
        onWheel={onWheel}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseUp={onMouseUp}
        onClick={onClick}
        style={{ cursor: "grab" }}
      >
        <div
          ref={containerRef}
          className="mermaid-zoom-container"
          dangerouslySetInnerHTML={{ __html: svgHtml }}
        />
      </div>
      <button className="mermaid-close-btn" onClick={onClose}>×</button>
    </div>
  );
}
