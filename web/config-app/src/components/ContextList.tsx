import { useState } from "react";
import { rebootSandbox, resetSandbox } from "../lib/api";
import type { AppConfig } from "../lib/types";

interface ContextListProps {
  config: AppConfig;
  onEdit: (name: string) => void;
  onAdd: () => void;
}

type SandboxAction = "reboot" | "reset";

export default function ContextList({ config, onEdit, onAdd }: ContextListProps) {
  const entries = Object.entries(config.contexts);
  const [busy, setBusy] = useState<Record<string, SandboxAction | undefined>>({});
  const [status, setStatus] = useState<{
    name: string;
    kind: "ok" | "err";
    message: string;
  } | null>(null);

  async function handleSandboxAction(name: string, action: SandboxAction) {
    const verb = action === "reboot" ? "Reboot" : "Reset";
    const detail =
      action === "reboot"
        ? "Active sessions will be closed."
        : "This destroys all sandbox state (overlays, images). Persistent volumes survive. Active sessions will be closed.";
    if (!window.confirm(`${verb} sandbox for "${name}"? ${detail}`)) {
      return;
    }
    setBusy((prev) => ({ ...prev, [name]: action }));
    setStatus(null);
    try {
      const fn = action === "reboot" ? rebootSandbox : resetSandbox;
      const result = await fn(name);
      const sessions = result.closed_sessions;
      const sessionMsg =
        sessions === 0
          ? "no active sessions"
          : `${sessions} session${sessions === 1 ? "" : "s"} closed`;
      setStatus({
        name,
        kind: "ok",
        message: `${verb} OK (${sessionMsg}).`,
      });
    } catch (e) {
      setStatus({
        name,
        kind: "err",
        message: e instanceof Error ? e.message : `${verb} failed`,
      });
    } finally {
      setBusy((prev) => {
        const next = { ...prev };
        delete next[name];
        return next;
      });
    }
  }

  return (
    <div className="context-list">
      {entries.map(([name, ctx]) => {
        const activeAction = busy[name];
        const isBusy = activeAction !== undefined;
        return (
          <div
            key={name}
            className={`context-card${name === config.default_context ? " default" : ""}`}
            onClick={() => onEdit(name)}
          >
            <div className="context-card-info">
              <div className="context-card-name">
                {name}
                {name === config.default_context && (
                  <span className="context-card-badge">default</span>
                )}
                {ctx.sandbox && (
                  <span className="context-card-badge">{ctx.sandbox.backend}</span>
                )}
              </div>
              <div className="context-card-dir">{ctx.directory}</div>
              <div className="context-card-desc">{ctx.description}</div>
              {ctx.sandbox && (
                <div
                  className="context-card-actions"
                  onClick={(e) => e.stopPropagation()}
                >
                  <button
                    type="button"
                    className="sandbox-action-btn"
                    disabled={isBusy}
                    onClick={() => handleSandboxAction(name, "reboot")}
                  >
                    {activeAction === "reboot" ? "Rebooting…" : "Reboot"}
                  </button>
                  <button
                    type="button"
                    className="sandbox-action-btn destructive"
                    disabled={isBusy}
                    onClick={() => handleSandboxAction(name, "reset")}
                  >
                    {activeAction === "reset" ? "Resetting…" : "Reset"}
                  </button>
                </div>
              )}
              {status && status.name === name && (
                <div className={`sandbox-action-status ${status.kind}`}>
                  {status.message}
                </div>
              )}
            </div>
            <span className="context-card-chevron">&rsaquo;</span>
          </div>
        );
      })}
      <button type="button" className="add-btn" onClick={onAdd}>
        + Add Context
      </button>
    </div>
  );
}
