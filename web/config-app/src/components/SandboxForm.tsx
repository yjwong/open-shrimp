import type {
  SandboxCapability,
  SandboxCatalog,
  SandboxConfig,
} from "../lib/types";

interface SandboxFormProps {
  sandbox: SandboxConfig | null | undefined;
  catalog: SandboxCatalog;
  onChange: (sandbox: SandboxConfig | null) => void;
}

export default function SandboxForm({
  sandbox,
  catalog,
  onChange,
}: SandboxFormProps) {
  if (!sandbox) {
    const backend = catalog.sandbox.backend;
    return (
      <div className="form-group">
        {backend && (
          <button
            type="button"
            className="add-btn"
            onClick={() => onChange({ backend, enabled: true })}
          >
            + Enable Sandbox
          </button>
        )}
        <span
          className={`form-hint${catalog.sandbox.available ? "" : " error"}`}
        >
          {catalog.sandbox.note}
        </span>
      </div>
    );
  }

  const selectedOffer = catalog.sandboxes.find(
    (offer) => offer.backend === sandbox.backend,
  );
  const has = (capability: SandboxCapability) =>
    selectedOffer?.capabilities.includes(capability) ?? false;

  const update = (patch: Partial<SandboxConfig>) => {
    onChange({ ...sandbox, ...patch });
  };

  const switchBackend = (backend: SandboxConfig["backend"]) => {
    const offer = catalog.sandboxes.find((item) => item.backend === backend);
    if (!offer) return;
    const capabilities = new Set<string>(offer.capabilities);
    const next: Record<string, unknown> = { backend };
    for (const [key, value] of Object.entries(sandbox)) {
      if (capabilities.has(key)) next[key] = value;
    }
    onChange(next as unknown as SandboxConfig);
  };

  const updateAndroid = (
    patch: Partial<NonNullable<SandboxConfig["android"]>>,
  ) => {
    update({ android: { ...(sandbox.android ?? {}), ...patch } });
  };

  const selectedIsForeign = selectedOffer === undefined;

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
            switchBackend(e.target.value as SandboxConfig["backend"])
          }
        >
          {selectedIsForeign && (
            <option value={sandbox.backend}>
              {sandbox.backend} (unavailable on this platform)
            </option>
          )}
          {catalog.sandboxes.map((offer) => (
            <option key={offer.backend} value={offer.backend}>
              {offer.label}{offer.available ? "" : " (unavailable)"}
            </option>
          ))}
        </select>
        {selectedOffer && !selectedOffer.available && (
          <span className="form-hint error">{selectedOffer.detail}</span>
        )}
        {selectedOffer?.available && (
          <span className="form-hint">{selectedOffer.summary}</span>
        )}
        {selectedIsForeign && (
          <span className="form-hint error">
            This backend is not available on this platform. It will remain
            selected unless you choose another backend.
          </span>
        )}
      </div>

      {has("enabled") && (
        <div className="form-toggle-row">
          <span className="form-toggle-label">Enabled</span>
          <button
            type="button"
            className={`toggle${sandbox.enabled !== false ? " on" : ""}`}
            onClick={() => update({ enabled: sandbox.enabled === false })}
          />
        </div>
      )}

      {has("computer_use") && (
        <div className="form-toggle-row">
          <span className="form-toggle-label">Computer Use</span>
          <button
            type="button"
            className={`toggle${sandbox.computer_use ? " on" : ""}`}
            onClick={() => update({ computer_use: !sandbox.computer_use })}
          />
        </div>
      )}

      {has("virgl") && sandbox.computer_use && (
        <div className="form-toggle-row">
          <span className="form-toggle-label">VirGL (3D GPU)</span>
          <button
            type="button"
            className={`toggle${sandbox.virgl ? " on" : ""}`}
            onClick={() => update({ virgl: !sandbox.virgl })}
          />
        </div>
      )}

      {has("phone_use") && (
        <div className="form-toggle-row">
          <span className="form-toggle-label">Phone Use (Android)</span>
          <button
            type="button"
            className={`toggle${sandbox.phone_use ? " on" : ""}`}
            onClick={() => update({ phone_use: !sandbox.phone_use })}
          />
        </div>
      )}
      {has("android") && sandbox.phone_use && (
        <>
          <span className="form-hint">
            Runs Waydroid (Android) inside the VM, driven via phone_* tools.
            Implies Computer Use (labwc desktop + VNC) and hardware GPU (VirGL)
            unless GPU is set to software.
          </span>
          <div className="form-group">
            <label className="form-label">Android Image</label>
            <select
              className="form-input"
              value={sandbox.android?.image_type ?? "VANILLA"}
              onChange={(e) =>
                updateAndroid({
                  image_type: e.target.value as "VANILLA" | "GAPPS",
                })
              }
            >
              <option value="VANILLA">VANILLA</option>
              <option value="GAPPS">GAPPS (Google apps)</option>
            </select>
          </div>
          <div className="form-group">
            <label className="form-label">Resolution</label>
            <input
              className="form-input"
              value={sandbox.android?.resolution ?? ""}
              onChange={(e) =>
                updateAndroid({ resolution: e.target.value || null })
              }
              placeholder="e.g. 720x1280"
            />
          </div>
          <div className="form-group">
            <label className="form-label">DPI</label>
            <input
              className="form-input"
              type="number"
              value={sandbox.android?.dpi ?? ""}
              onChange={(e) =>
                updateAndroid({
                  dpi: e.target.value ? parseInt(e.target.value) : null,
                })
              }
              placeholder="e.g. 320"
            />
          </div>
          <div className="form-group">
            <label className="form-label">GPU</label>
            <select
              className="form-input"
              value={sandbox.android?.gpu ?? "virgl"}
              onChange={(e) =>
                updateAndroid({
                  gpu: e.target.value as "virgl" | "software",
                })
              }
            >
              <option value="virgl">virgl (hardware GLES)</option>
              <option value="software">software (llvmpipe, slow)</option>
            </select>
          </div>
        </>
      )}

      {has("allow_host_escape") && (
        <>
          <div className="form-toggle-row">
            <span className="form-toggle-label">Allow Host Escape (sudo)</span>
            <button
              type="button"
              className={`toggle${sandbox.allow_host_escape ? " on" : ""}`}
              onClick={() =>
                update({ allow_host_escape: !sandbox.allow_host_escape })
              }
            />
          </div>
          {sandbox.allow_host_escape && (
            <span className="form-hint error">
              Grants a host_bash MCP tool that runs shell commands on the host
              outside the sandbox. Each call requires explicit Telegram approval
              (auto-deny after 10s).
            </span>
          )}
        </>
      )}

      {has("guest_os") && (
        <div className="form-group">
          <label className="form-label">Guest OS</label>
          <select
            className="form-input"
            value={sandbox.guest_os ?? "linux"}
            onChange={(e) =>
              update({ guest_os: e.target.value as "linux" | "macos" })
            }
          >
            <option value="linux">Linux</option>
            <option value="macos">macOS</option>
          </select>
        </div>
      )}

      {has("memory") && (
        <div className="form-group">
          <label className="form-label">Memory (MB)</label>
          <input
            className="form-input"
            type="number"
            value={sandbox.memory ?? 2048}
            onChange={(e) => update({ memory: parseInt(e.target.value) || 2048 })}
          />
        </div>
      )}
      {has("cpus") && (
        <div className="form-group">
          <label className="form-label">CPUs</label>
          <input
            className="form-input"
            type="number"
            value={sandbox.cpus ?? 2}
            onChange={(e) => update({ cpus: parseInt(e.target.value) || 2 })}
          />
        </div>
      )}
      {has("disk_size") && (
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
      )}
      {has("base_image") && (
        <div className="form-group">
          <label className="form-label">Base Image</label>
          <input
            className="form-input"
            value={sandbox.base_image ?? ""}
            onChange={(e) => update({ base_image: e.target.value || null })}
            placeholder={selectedOffer?.base_image_placeholder}
          />
        </div>
      )}
      {has("provision") && (
        <div className="form-group">
          <label className="form-label">Provision Script</label>
          <textarea
            className="form-input"
            value={sandbox.provision ?? ""}
            onChange={(e) => update({ provision: e.target.value || null })}
            placeholder="Shell script to run on first boot"
            rows={3}
          />
        </div>
      )}
      {has("mingw_bin") && (
        <div className="form-group">
          <label className="form-label">MinGW Bin Directory</label>
          <input
            className="form-input"
            value={sandbox.mingw_bin ?? ""}
            onChange={(e) => update({ mingw_bin: e.target.value || null })}
            placeholder="Path containing x86_64-w64-mingw32-gcc"
          />
        </div>
      )}

      {has("persistent_paths") && (
        <div className="form-group">
          <label className="form-label">Persistent Paths</label>
          <span className="form-hint">
            Guest paths with dedicated disk volumes that survive VM rebuilds
          </span>
          <div className="list-input-items">
            {(sandbox.persistent_paths ?? []).map((path, index) => (
              <div key={index} className="list-input-row">
                <input
                  className="form-input"
                  value={path}
                  onChange={(e) => {
                    const next = [...(sandbox.persistent_paths ?? [])];
                    next[index] = e.target.value;
                    update({ persistent_paths: next });
                  }}
                  placeholder="/var/lib/postgresql"
                />
                <button
                  type="button"
                  className="list-input-remove"
                  onClick={() =>
                    update({
                      persistent_paths: (sandbox.persistent_paths ?? []).filter(
                        (_, itemIndex) => itemIndex !== index,
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
