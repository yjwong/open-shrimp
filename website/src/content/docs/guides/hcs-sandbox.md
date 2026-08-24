---
title: HCS Sandbox (Windows)
description: Run the agent in a Linux guest on Windows' Host Compute Service, the layer beneath WSL2.
sidebar:
  order: 5.5
---

The HCS sandbox runs the agent inside a Linux virtual machine on Windows, driving the Host Compute Service — the same layer WSL2 and Windows Sandbox are built on. It is the Windows counterpart to the [Libvirt](/guides/vm-sandbox/) and [Lima](/guides/lima-sandbox/) sandboxes: the guest gets your project directory and nothing else of the host.

The guest is a plain Linux userland on a VHDX, booted directly with the kernel WSL ships. It is not a WSL distro and does not appear in `wsl -l`.

:::note[The first boot downloads a guest image]
Nothing needs staging by hand, but a context's first boot fetches a rootfs image of roughly a gigabyte (more with computer use) and unpacks it. It happens once per machine; every context after that seeds from the same cached copy.
:::

## Requirements

- **Windows 11**, Home or Pro. The Hyper-V role is *not* required; the `VirtualMachinePlatform` optional feature is what HCS actually needs — it installs `vmcompute.exe`, the service the backend calls. It is off on a default install, and installing WSL from the standalone MSI does *not* turn it on. `dism /online /enable-feature /featurename:VirtualMachinePlatform /all` does, and it wants a restart.
- **WSL installed.** The guest boots the kernel WSL ships at `C:\Program Files\WSL\tools\kernel`. Nothing is ever *run* in WSL — the kernel file just has to be on disk. Point `OPENSHRIMP_HCS_KERNEL` elsewhere if you stage your own.
- **OpenShrimp with the `hcs` extra.** The `openshrimp-windows-x86_64.exe` binary from [Releases](https://github.com/yjwong/open-shrimp/releases) bundles it. From source, install the extra explicitly — it pulls in `win32more`, the binding the backend calls Windows through:

  ```powershell
  uv sync --extra hcs        # or: pip install "open-shrimp[hcs]"
  ```

- **Hyper-V rights.** The account running the bot must be a member of **Hyper-V Administrators**, or be a local Administrator with the process running elevated. Without one of those, Windows refuses to create the compute system at all. Prefer the group: it is the least privilege HCS accepts, and it means the bot never runs elevated. Windows checks the membership against the process token, so it only takes effect after a sign-out and back in.

Nothing else needs installing. The host-side launcher the agent CLI is invoked through is compiled with the in-box .NET Framework compiler that ships with Windows, so there is no toolchain to set up.

Run `openshrimp doctor` at any point — it checks each of the above, plus the guest images below, and names the fix for whatever is missing.

### Letting setup supply them

Three of the requirements above need administrator rights — the group membership, the platform feature and the WSL install — and a per-user MSI install never asks for any, so a PC nobody has prepared cannot turn the sandbox on. The setup wizard offers it instead: on its last step, one administrator approval takes whichever of the three this PC is missing, and it then offers the restart that makes them take effect.

That approval is worth reading before you give it. The group membership does not expire and is not scoped to OpenShrimp: from then on anything running as your account can create virtual machines, with no further prompt. The platform feature is machine-wide and stays on, and other virtualisation software cannot share it, so VMware, VirtualBox and Android emulators may stop working.

Declining costs nothing but the sandbox — contexts without one are unaffected, and the first turn in a sandboxed context says what is missing rather than failing with a bare error code. Installing from source does them by hand.

## Guest images

A guest boots from three artifacts, and none of them needs staging by hand:

| Artifact | Where it comes from |
|----------|---------------------|
| **Kernel** | WSL's, found automatically. Override with `OPENSHRIMP_HCS_KERNEL`. |
| **Control initramfs** | Downloaded from the release on first use, into `%LOCALAPPDATA%\openshrimp\hcs\initrd.img`. |
| **Rootfs VHDX** | Downloaded from the release on first use, into `%LOCALAPPDATA%\openshrimp\hcs\`. |

Both downloads are checksum-verified and cached per machine, not per context: each context copies its own guest root from the cached template, so only the first boot on a host pays for them.

That means a working context needs nothing but a directory and a backend:

```yaml
contexts:
  myproject:
    directory: C:\Users\you\Documents\myproject
    description: "My project"
    sandbox:
      backend: hcs
```

The rootfs is the Linux userland the agent runs in: an ext4 VHDX labelled `clauderoot` carrying Python, Node.js, and the certificate and download tools the in-guest agent installer needs. The agent CLI is deliberately not baked in — it installs itself inside the guest on first provision, so one image serves either agent backend.

The image is a template. Each context gets its own copy as its guest root, and that copy is reborn on every boot — only `persistent_paths` survive.

### Building your own images instead

Set `base_image` and the download is skipped entirely; that path is for an operator who wants a different userland in the guest.

```bash
# in a WSL distro, as root
sudo bash scripts/build_hcs_base_rootfs.sh /mnt/c/images/claude-root.vhdx
```

```yaml
    sandbox:
      backend: hcs
      base_image: C:\images\claude-root.vhdx
```

What to expect from the build:

- **Root is mandatory** — it uses loop mounts and `chroot`.
- **Several GB of scratch space.** The default work directory is `/root/hcs-base-build`; set `WORK` to a filesystem that has room. The image itself is a 6 GB sparse ext4 volume (`SIZE`).
- **Network access**, to debootstrap an Ubuntu release (`SUITE`, `MIRROR`) and fetch Node.js.

`scripts/build_hcs_initrd.sh` likewise builds the control initramfs — busybox plus a small statically linked control agent, so it is quick — and `OPENSHRIMP_HCS_INITRD` points the backend at the result. Setting that variable suppresses the download, so a path that does not exist is an error rather than a silent fall back to the released image.

## Guest configuration

```yaml
contexts:
  myproject:
    sandbox:
      backend: hcs
      base_image: C:\images\claude-root.vhdx
      memory: 4096            # MB (default: 2048)
      cpus: 4                 # vCPUs (default: 2) — at most the host's
      disk_size: 20           # GB per persistent volume (sparse)
      persistent_paths:
        - /home/claude/.cache
      provision: |
        npm install -g typescript
```

`cpus` **may not exceed the host's logical processor count** — HCS rejects a larger processor topology with a bare error code, so OpenShrimp checks it against the host before creating the guest. Any count from 1 up to that limit is accepted. Note that client editions of Windows use at most two CPU sockets, so a Windows host that is itself a VM may see far fewer logical processors than its hypervisor was configured to give it.

Each entry in `persistent_paths` gets its own ext4 VHDX, mounted by label and untouched by rebuilds — that is where package caches and anything else worth keeping belong. Changing any sandbox field rebuilds the guest; the persistent volumes survive.

`additional_directories` are shared in alongside the project directory, and files you send the bot are copied into the workspace share. Shares are 9p, mounted with `cache=mmap` so memory-mapped files — including SQLite in WAL mode — work correctly.

## Computer use

`computer_use: true` boots a desktop variant of the rootfs — weston on its RDP backend, Google Chrome, fonts and clipboard tools — which is a separate, larger image. With no `base_image` set it is downloaded like the plain one, and it is downloaded *instead of*, not in addition to: the desktop image is built from the base and already carries the whole userland.

If you build your own images, bake the desktop variant from your base:

```bash
# in a WSL distro, as root — takes the base image as input
sudo bash scripts/build_hcs_gui_rootfs.sh /mnt/c/images/claude-root.vhdx
```

That writes `claude-root-gui.vhdx` next to the base image, which is exactly where the backend looks for it. Same requirements as the base build: root, loop mounts, network, and scratch space in `WORK`.

```yaml
contexts:
  browser-tasks:
    directory: C:\Users\you\Documents\browser-project
    sandbox:
      backend: hcs
      computer_use: true
      memory: 4096          # a desktop wants headroom
      cpus: 2

review:
  tunnel: cloudflared       # needed for the VNC Mini App
```

The host talks to the guest desktop over RDP through a small helper, which is downloaded prebuilt and bundled with the FreeRDP libraries it loads. `mingw_bin` is therefore optional, and only worth setting if you want to compile the helper from source instead:

```yaml
    sandbox:
      mingw_bin: C:\msys64\mingw64\bin   # optional; MSYS2 with
                                         # mingw-w64-x86_64-{freerdp,gcc,pkgconf}
```

`/vnc` renders the live desktop in the VNC Mini App. Window focus by name is not available — a single-surface RDP desktop has nothing to switch between, the same as on the Libvirt backend — so use key combos like `alt+Tab` instead. See [Computer Use](/guides/computer-use/) for the tools themselves.

## What works

Both agent backends run on HCS: `claude_sdk` through a generated launcher, and `opencode` through its serve endpoint, with the CLI installed into the guest on first provision. Runtime port forwarding works, so the `port_forward` tool and the preview Mini App are available in HCS contexts.

## What is not supported

These are platform constraints rather than unfinished work, so an error from one of them is not going to be fixed by upgrading:

- **Phone use.** Waydroid needs binder, and the WSL kernel is built without `CONFIG_ANDROID_BINDER_IPC`. Phone use remains Libvirt-only.
- **Security-key forwarding.** The same kernel is built without `CONFIG_UHID`, so `/dev/uhid` cannot exist. The forwarding implementation itself is complete and works — supply a UHID-enabled kernel through `OPENSHRIMP_HCS_KERNEL` and it runs with no code changes. OpenShrimp does not ship such a kernel.
- **virtiofs shares.** The blocker is the host, not the guest. The HCS create document has no virtiofs device in its vocabulary at all, and Windows rejects every spelling of one; WSL's own virtiofs runs through a private device host that is WSL's, not Hyper-V's. 9p with `cache=mmap` covers what the shares are needed for.

A few `sandbox:` keys have no meaning on HCS — `virgl`, `guest_os`, `phone_use`, and `android`. Setting any of them fails config validation rather than being silently ignored, so a context that asks for one tells you instead of quietly not doing it.

## Environment variables

| Variable | Purpose |
|----------|---------|
| `OPENSHRIMP_HCS_KERNEL` | Kernel to boot, when not WSL's at its default path |
| `OPENSHRIMP_HCS_INITRD` | Control initramfs you built yourself. Unset, a copy staged at `C:\ProgramData\openshrimp\hcs\initrd.img` wins if present, and the released asset is downloaded to the cache otherwise |
| `OPENSHRIMP_HCS_RDP_HELPER` | Directory holding an RDP helper bundle you staged yourself |
