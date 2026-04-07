import type { AppConfig } from "../lib/types";

interface ContextListProps {
  config: AppConfig;
  onEdit: (name: string) => void;
  onAdd: () => void;
}

export default function ContextList({ config, onEdit, onAdd }: ContextListProps) {
  const entries = Object.entries(config.contexts);

  return (
    <div className="context-list">
      {entries.map(([name, ctx]) => (
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
          </div>
          <span className="context-card-chevron">&rsaquo;</span>
        </div>
      ))}
      <button type="button" className="add-btn" onClick={onAdd}>
        + Add Context
      </button>
    </div>
  );
}
