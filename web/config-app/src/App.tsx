import { useCallback, useEffect, useState } from "react";
import { useConfig } from "./hooks/useConfig";
import type { AppConfig, ContextConfig } from "./lib/types";
import ContextList from "./components/ContextList";
import ContextEditor from "./components/ContextEditor";
import AllowedUsers from "./components/AllowedUsers";

type View = { type: "list" } | { type: "edit"; name: string | null };

export default function App() {
  const { config, setConfig, loading, saving, error, dirty, save, toast, dismissToast } =
    useConfig();
  const [tab, setTab] = useState<"contexts" | "users">("contexts");
  const [view, setView] = useState<View>({ type: "list" });

  // Auto-dismiss toast.
  useEffect(() => {
    if (toast) {
      const t = setTimeout(dismissToast, 3000);
      return () => clearTimeout(t);
    }
  }, [toast, dismissToast]);

  if (loading) {
    return (
      <div className="loading">
        <div className="spinner" />
      </div>
    );
  }

  if (error || !config) {
    return (
      <div className="loading">
        <div style={{ textAlign: "center", padding: 24 }}>
          <p style={{ color: "var(--error)", marginBottom: 12 }}>{error}</p>
          <button
            className="btn btn-secondary"
            onClick={() => window.location.reload()}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (view.type === "edit") {
    return (
      <ContextEditorView
        config={config}
        setConfig={setConfig}
        contextName={view.name}
        onBack={() => setView({ type: "list" })}
      />
    );
  }

  return (
    <>
      <div className="app-header">
        <h1>Configuration</h1>
        {dirty && (
          <button
            className="btn btn-success btn-small"
            onClick={save}
            disabled={saving}
          >
            {saving ? "Saving..." : "Save"}
          </button>
        )}
      </div>

      <div className="tabs">
        <button
          className={`tab${tab === "contexts" ? " active" : ""}`}
          onClick={() => setTab("contexts")}
        >
          Contexts
        </button>
        <button
          className={`tab${tab === "users" ? " active" : ""}`}
          onClick={() => setTab("users")}
        >
          Users
        </button>
      </div>

      {tab === "contexts" && (
        <ContextList
          config={config}
          onEdit={(name) => setView({ type: "edit", name })}
          onAdd={() => setView({ type: "edit", name: null })}
        />
      )}

      {tab === "users" && (
        <AllowedUsers
          users={config.allowed_users}
          onChange={(users) =>
            setConfig((prev) => (prev ? { ...prev, allowed_users: users } : prev))
          }
        />
      )}

      {dirty && (
        <div className="save-footer">
          <button
            className="btn btn-success"
            onClick={save}
            disabled={saving}
            style={{ width: "100%" }}
          >
            {saving ? "Saving..." : "Save Changes"}
          </button>
        </div>
      )}

      {toast && <div className={`toast ${toast.type}`}>{toast.message}</div>}
    </>
  );
}

function ContextEditorView({
  config,
  setConfig,
  contextName,
  onBack,
}: {
  config: AppConfig;
  setConfig: React.Dispatch<React.SetStateAction<AppConfig | null>>;
  contextName: string | null;
  onBack: () => void;
}) {
  const handleSave = useCallback(
    (name: string, ctx: ContextConfig, isDefault: boolean) => {
      setConfig((prev) => {
        if (!prev) return prev;
        const next = { ...prev, contexts: { ...prev.contexts } };

        // If renaming (editing existing, name changed), remove old key.
        if (contextName && contextName !== name) {
          delete next.contexts[contextName];
          if (prev.default_context === contextName) {
            next.default_context = name;
          }
        }

        next.contexts[name] = ctx;

        if (isDefault) {
          next.default_context = name;
        } else if (next.default_context === name && Object.keys(next.contexts).length > 1) {
          // If unchecking default, pick another.
          const other = Object.keys(next.contexts).find((k) => k !== name);
          if (other) next.default_context = other;
        }

        // If only one context, it must be default.
        if (Object.keys(next.contexts).length === 1) {
          next.default_context = Object.keys(next.contexts)[0]!;
        }

        return next;
      });
      onBack();
    },
    [contextName, setConfig, onBack],
  );

  const handleDelete = useCallback(
    (name: string) => {
      setConfig((prev) => {
        if (!prev) return prev;
        const next = { ...prev, contexts: { ...prev.contexts } };
        delete next.contexts[name];
        // If we deleted the default, pick the first remaining.
        if (prev.default_context === name) {
          const remaining = Object.keys(next.contexts);
          next.default_context = remaining[0] ?? "";
        }
        return next;
      });
      onBack();
    },
    [setConfig, onBack],
  );

  return (
    <ContextEditor
      config={config}
      contextName={contextName}
      onSave={handleSave}
      onDelete={handleDelete}
      onBack={onBack}
    />
  );
}
