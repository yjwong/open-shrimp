# Lima patches

Patches OpenShrimp applies on top of pinned upstream Lima before building
the bundled `limactl`. Each `.patch` file is generated with `git diff`
and applied with `git apply` (or `git am` if rebased onto a branch).

## `lima-vznc-display.patch` — host-side `_VZVNCServer` for `video.display: vnc`

Adds a `vnc` mode to the vz driver that attaches Apple's private
`_VZVNCServer` SPI to the running `VZVirtualMachine` and exposes a
localhost TCP port through `DisplayConnection`. No `limactl` graphics
window is opened, so OpenShrimp can scrape the framebuffer headlessly.

The `vznc` Go module that this patch imports lives in
`../vznc/` of the OpenShrimp repo.

### What it changes

- `go.mod` — adds `require github.com/openshrimp/vznc` and a `replace`
  directive pointing at the sibling `vznc` checkout.
- `pkg/driver/vz/vm_darwin.go` — extends the `attachDisplay` switch so
  the same VirtIO/Mac graphics device wiring runs for `display: vnc`.
- `pkg/driver/vz/vz_driver_darwin.go` — adds `vncServer`/`vncPort`
  fields to `LimaVzDriver`; wires `_VZVNCServer` startup in `Start`,
  teardown in `Stop`, and reports the bound port from
  `DisplayConnection`. Validator switch grows a `"vnc"` case.
- `pkg/driver/vz/vz_vnc_darwin.go` *(new)* — owns the lifecycle and
  uses reflection to extract the unexported `*pointer` (raw
  `VZVirtualMachine` ObjC handle) field from the pinned Code-Hex/vz
  `*vz.VirtualMachine`.
- `pkg/driver/vz/vz_vnc_darwin_test.go` *(new)* — asserts the
  Code-Hex/vz field layout the reflection helper depends on. Fails
  fast at CI time if a future Code-Hex/vz bump renames fields.

### Build / apply

```sh
# Inside a fresh Lima clone, pinned to the version in patches/PIN
cd lima-source
git apply ../patches/lima-vznc-display.patch
go build -o limactl ./cmd/limactl
```

The bundled `replace` directive expects `vznc` to live at `../../vznc`
relative to the lima checkout (so a developer layout of
`open-shrimp/research/lima-source` + `open-shrimp/vznc` works
out-of-the-box). Adjust if the build pipeline lays things out
differently.

### Constraints baked into the patch

- `_VZVNCServer` is invoked through `NSClassFromString` /
  `NSSelectorFromString` inside the `vznc` package, so the binary still
  loads on macOS where Apple removes the SPI; `vznc.Available()` is the
  runtime probe and `Start` returns a clear error if false.
- `_VZVNCServer.setVirtualMachine:` requires the raw VZVirtualMachine
  `id`. Code-Hex/vz only exposes that pointer through an `internal/`
  package, so we read it via `reflect` against the pinned struct
  layout. The companion test pins those field names.
- The hostagent VNC machinery (`pkg/hostagent/hostagent.go:434`) is
  reused unchanged: `DisplayConnection` returns just the port number
  (matching QEMU's QMP `query-vnc`-derived semantics) and the hostagent
  computes the display string + writes the standard `vncdisplay` /
  `vncpasswd` files. The password file is generated but not consumed —
  this patch wires `_VZVNCNoSecuritySecurityConfiguration`; client-side
  protection is provided by OpenShrimp's RFB filter proxy binding to
  localhost.
- `ChangeDisplayPassword` is left as the existing no-op. Wiring it
  into a live `_VZVNCServer` would require building and swapping a
  `_VZVNCAuthenticationSecurityConfiguration` at runtime, which is
  uncharacterised against the SPI; the patch ships
  `_VZVNCNoSecuritySecurityConfiguration` and relies on the localhost
  bind + the RFB filter proxy as the access boundary.

### Pinned upstream

`patches/PIN` holds the Lima tag the patch was authored against. The
same tag is the source of the runtime `limactl` binary OpenShrimp
downloads — see `LIMA_VERSION` in
`src/open_shrimp/sandbox/lima_helpers.py`. **Bump both together** so
the runtime and the patch never disagree on Lima's API surface.

Bumping the pin:

1. Edit `LIMA_VERSION` in `lima_helpers.py` and `tag:` in
   `patches/PIN`.
2. `git fetch upstream && git checkout <new-tag>` in the lima source.
3. `git apply --3way ../../patches/lima-vznc-display.patch` — resolve
   any go.mod / driver conflicts.
4. `go test ./pkg/driver/vz/...` — the field-layout test will catch
   Code-Hex/vz layout drift; fix `vz_vnc_darwin.go::vmRawPointer`.
5. Regenerate the patch via
   `git diff HEAD > ../../patches/lima-vznc-display.patch`.
