import type { SandboxConfig } from "../lib/types";

interface SandboxFormProps {
  sandbox: SandboxConfig | null | undefined;
  onChange: (sandbox: SandboxConfig | null) => void;
}

const BACKENDS = ["docker", "libvirt", "lima"] as const;

export default function SandboxForm({ sandbox, onChange }: SandboxFormProps) {
  if (!sandbox) {
    return (
      <div className="form-group">
        <button
          type="button"
          className="add-btn"
          onClick={() =>
            onChange({ backend: "docker", enabled: true })
          }
        >
          + Enable Sandbox
        </button>
      </div>
    );
  }

  const update = (patch: Partial<SandboxConfig>) => {
    onChange({ ...sandbox, ...patch });
  };

  const isVM = sandbox.backend === "libvirt" || sandbox.backend === "lima";

  return (
    <div className="sandbox-section">
      <div className="sandbox-header">
        <h3>Sandbox</h3>
        <button
          type="button"
          className="btn btn-danger btn-small"
          onClick={() => onChange(null)}
        >
          Remove
        </button>
      </div>

      <div className="form-group">
        <label className="form-label">Backend</label>
        <select
          className="form-input"
          value={sandbox.backend}
          onChange={(e) =>
            update({ backend: e.target.value as SandboxConfig["backend"] })
          }
        >
          {BACKENDS.map((b) => (
            <option key={b} value={b}>
              {b}
            </option>
          ))}
        </select>
      </div>

      <div className="form-toggle-row">
        <span className="form-toggle-label">Enabled</span>
        <button
          type="button"
          className={`toggle${sandbox.enabled !== false ? " on" : ""}`}
          onClick={() => update({ enabled: sandbox.enabled === false })}
        />
      </div>

      <div className="form-toggle-row">
        <span className="form-toggle-label">Computer Use</span>
        <button
          type="button"
          className={`toggle${sandbox.computer_use ? " on" : ""}`}
          onClick={() => update({ computer_use: !sandbox.computer_use })}
        />
      </div>

      <div className="form-toggle-row">
        <span className="form-toggle-label">VirGL (3D GPU)</span>
        <button
          type="button"
          className={`toggle${sandbox.virgl ? " on" : ""}`}
          onClick={() => update({ virgl: !sandbox.virgl })}
        />
      </div>

      {sandbox.backend === "lima" && (
        <div className="form-group">
          <label className="form-label">Guest OS</label>
          <select
            className="form-input"
            value={sandbox.guest_os ?? "linux"}
            onChange={(e) =>
              update({
                guest_os: e.target.value as "linux" | "macos",
              })
            }
          >
            <option value="linux">Linux</option>
            <option value="macos">macOS</option>
          </select>
        </div>
      )}

      {sandbox.backend === "docker" && (
        <>
          <div className="form-toggle-row">
            <span className="form-toggle-label">Docker-in-Docker</span>
            <button
              type="button"
              className={`toggle${sandbox.docker_in_docker ? " on" : ""}`}
              onClick={() =>
                update({ docker_in_docker: !sandbox.docker_in_docker })
              }
            />
          </div>
          <div className="form-group">
            <label className="form-label">Dockerfile</label>
            <input
              className="form-input"
              value={sandbox.dockerfile ?? ""}
              onChange={(e) =>
                update({
                  dockerfile: e.target.value || null,
                })
              }
              placeholder="Optional custom Dockerfile path"
            />
          </div>
        </>
      )}

      {isVM && (
        <>
          <div className="form-group">
            <label className="form-label">Memory (MB)</label>
            <input
              className="form-input"
              type="number"
              value={sandbox.memory ?? 2048}
              onChange={(e) =>
                update({ memory: parseInt(e.target.value) || 2048 })
              }
            />
          </div>
          <div className="form-group">
            <label className="form-label">CPUs</label>
            <input
              className="form-input"
              type="number"
              value={sandbox.cpus ?? 2}
              onChange={(e) =>
                update({ cpus: parseInt(e.target.value) || 2 })
              }
            />
          </div>
          <div className="form-group">
            <label className="form-label">Disk Size (GB)</label>
            <input
              className="form-input"
              type="number"
              value={sandbox.disk_size ?? 20}
              onChange={(e) =>
                update({ disk_size: parseInt(e.target.value) || 20 })
              }
            />
          </div>
          <div className="form-group">
            <label className="form-label">Base Image</label>
            <input
              className="form-input"
              value={sandbox.base_image ?? ""}
              onChange={(e) =>
                update({ base_image: e.target.value || null })
              }
              placeholder="Path to base qcow2/cloud image"
            />
          </div>
          <div className="form-group">
            <label className="form-label">Provision Script</label>
            <textarea
              className="form-input"
              value={sandbox.provision ?? ""}
              onChange={(e) =>
                update({ provision: e.target.value || null })
              }
              placeholder="Shell script to run on first boot"
              rows={3}
            />
          </div>
        </>
      )}

      {sandbox.backend === "libvirt" && (
        <div className="form-group">
          <label className="form-label">Persistent Paths</label>
          <span className="form-hint">
            Guest paths with dedicated qcow2 volumes that survive VM rebuilds
          </span>
          <div className="list-input-items">
            {(sandbox.persistent_paths ?? []).map((p, i) => (
              <div key={i} className="list-input-row">
                <input
                  className="form-input"
                  value={p}
                  onChange={(e) => {
                    const next = [...(sandbox.persistent_paths ?? [])];
                    next[i] = e.target.value;
                    update({ persistent_paths: next });
                  }}
                  placeholder="/var/lib/docker"
                />
                <button
                  type="button"
                  className="list-input-remove"
                  onClick={() =>
                    update({
                      persistent_paths: (sandbox.persistent_paths ?? []).filter(
                        (_, j) => j !== i,
                      ),
                    })
                  }
                >
                  x
                </button>
              </div>
            ))}
          </div>
          <button
            type="button"
            className="add-btn"
            onClick={() =>
              update({
                persistent_paths: [...(sandbox.persistent_paths ?? []), ""],
              })
            }
          >
            + Add Path
          </button>
        </div>
      )}
    </div>
  );
}
