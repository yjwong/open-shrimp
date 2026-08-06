---
title: HCS Sandbox (Windows)
description: Run the agent in a Linux guest on Windows' Host Compute Service, the layer beneath WSL2.
sidebar:
  order: 5.5
---

The HCS sandbox runs the agent inside a Linux virtual machine on Windows, driving the Host Compute Service — the same layer WSL2 and Windows Sandbox are built on. It is the Windows counterpart to the [Libvirt](/guides/vm-sandbox/) and [Lima](/guides/lima-sandbox/) sandboxes: the guest gets your project directory and nothing else of the host.

The guest is a plain Linux userland on a VHDX, booted directly with the kernel WSL ships. It is not a WSL distro and does not appear in `wsl -l`.

:::caution[Read the guest images section before you start]
Two of the three artifacts a guest boots from are not published — you build them yourself with scripts that need root inside WSL and several GB of scratch space. Everything else installs from a download. Budget for that step.
:::

## Requirements

- **Windows 11**, Home or Pro. The Hyper-V role is *not* required; the `VirtualMachinePlatform` optional feature is what HCS actually needs, and installing WSL enables it.
- **WSL installed.** The guest boots the kernel WSL ships at `C:\Program Files\WSL\tools\kernel`. You do not need to run anything in WSL to *use* the sandbox — but you do to *build* its images, and the kernel has to be on disk either way. Point `OPENSHRIMP_HCS_KERNEL` elsewhere if you stage your own.
- **OpenShrimp with the `hcs` extra.** The `openshrimp-windows-x86_64.exe` binary from [Releases](https://github.com/yjwong/open-shrimp/releases) bundles it. From source, install the extra explicitly — it pulls in `win32more`, the binding the backend calls Windows through:

  ```powershell
  uv sync --extra hcs        # or: pip install "open-shrimp[hcs]"
  ```

- **Elevation.** The account running the bot must be a local Administrator with the process running elevated, or a member of **Hyper-V Administrators**. Without one of those, Windows refuses to create the compute system at all. Adding the account to Hyper-V Administrators is the option that avoids running the bot elevated; the membership only takes effect after a sign-out.

Nothing else needs installing. The host-side launcher the agent CLI is invoked through is compiled with the in-box .NET Framework compiler that ships with Windows, so there is no toolchain to set up.

Run `openshrimp doctor` at any point — it checks each of the above, plus the guest images below, and names the fix for whatever is missing.

## Guest images

A guest boots from three artifacts. They are handled very differently, and only one of them arrives on its own:

| Artifact | Where it comes from |
|----------|---------------------|
| **Kernel** | WSL's, found automatically. Override with `OPENSHRIMP_HCS_KERNEL`. |
| **Control initramfs** | Published as the `openshrimp-hcs-initrd.img` release asset — but **you stage it by hand**. Nothing downloads it for you. |
| **Base rootfs VHDX** | **You build it.** Not published anywhere. |

### Stage the control initramfs

Download `openshrimp-hcs-initrd.img` from the [latest release](https://github.com/yjwong/open-shrimp/releases) and put it at `C:\ProgramData\openshrimp\hcs\initrd.img`, or anywhere you like with `OPENSHRIMP_HCS_INITRD` pointing at it:

```powershell
New-Item -ItemType Directory -Force C:\ProgramData\openshrimp\hcs
curl.exe -fsSL https://github.com/yjwong/open-shrimp/releases/latest/download/openshrimp-hcs-initrd.img `
  -o C:\ProgramData\openshrimp\hcs\initrd.img
```

If you would rather build it, `scripts/build_hcs_initrd.sh` does so — as root, on any Linux including a WSL distro. It is busybox plus a small statically linked control agent, so the build is quick.

### Build the base rootfs

This is the one real chore. The rootfs is the Linux userland the agent runs in: an ext4 VHDX labelled `clauderoot` carrying Python, Node.js, and the certificate and download tools the in-guest agent installer needs. No CI job publishes it, so you build it once:

```bash
# in a WSL distro, as root
sudo bash scripts/build_hcs_base_rootfs.sh /mnt/c/images/claude-root.vhdx
```

What to expect:

- **Root is mandatory** — the build uses loop mounts and `chroot`.
- **Several GB of scratch space.** The default work directory is `/root/hcs-base-build`; set `WORK` to a filesystem that has room. The image itself is a 6 GB sparse ext4 volume (`SIZE`).
- **Network access**, to debootstrap an Ubuntu release (`SUITE`, `MIRROR`) and fetch Node.js.

The agent CLI is deliberately not baked in — it installs itself inside the guest on first provision, so one image serves either agent backend.

Then point a context at the result:

```yaml
contexts:
  myproject:
    directory: C:\Users\you\Documents\myproject
    description: "My project"
    sandbox:
      backend: hcs
      base_image: C:\images\claude-root.vhdx
```

The base image is a template. Each context gets its own copy as its guest root, and that copy is reborn on every boot — only `persistent_paths` survive.

## Guest configuration

```yaml
contexts:
  myproject:
    sandbox:
      backend: hcs
      base_image: C:\images\claude-root.vhdx
      memory: 4096            # MB (default: 2048)
      cpus: 4                 # vCPUs (default: 2) — must be even
      disk_size: 20           # GB per persistent volume (sparse)
      persistent_paths:
        - /home/claude/.cache
      provision: |
        npm install -g typescript
```

`cpus` **must be an even number of at least 2**. HCS rejects odd processor topologies outright, so OpenShrimp refuses them when the config loads rather than letting the create fail with a bare error code.

Each entry in `persistent_paths` gets its own ext4 VHDX, mounted by label and untouched by rebuilds — that is where package caches and anything else worth keeping belong. Changing any sandbox field rebuilds the guest; the persistent volumes survive.

`additional_directories` are shared in alongside the project directory, and files you send the bot are copied into the workspace share. Shares are 9p, mounted with `cache=mmap` so memory-mapped files — including SQLite in WAL mode — work correctly.

## Computer use

`computer_use: true` boots a desktop variant of your rootfs, so it needs a second image baked from the first:

```bash
# in a WSL distro, as root — takes the base image as input
sudo bash scripts/build_hcs_gui_rootfs.sh /mnt/c/images/claude-root.vhdx
```

That writes `claude-root-gui.vhdx` next to the base image, which is exactly where the backend looks for it — a 12 GB variant carrying weston with its RDP backend, Google Chrome, fonts, and clipboard tools. Same requirements as the base build: root, loop mounts, network, and scratch space in `WORK`. Like the base rootfs, it is not published; you bake it yourself.

```yaml
contexts:
  browser-tasks:
    directory: C:\Users\you\Documents\browser-project
    sandbox:
      backend: hcs
      base_image: C:\images\claude-root.vhdx
      computer_use: true
      memory: 4096          # a desktop wants headroom
      cpus: 2

review:
  tunnel: cloudflared       # needed for the VNC Mini App
```

The host talks to the guest desktop over RDP through a small helper. **That helper is downloaded automatically**, prebuilt and bundled with the FreeRDP libraries it loads — it is the one HCS artifact that fetches itself. `mingw_bin` is therefore optional, and only worth setting if you want to compile the helper from source instead:

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

A few `sandbox:` keys have no meaning on HCS — `virgl`, `docker_in_docker`, `dockerfile`, `guest_os`, `phone_use`, and `android`. Setting any of them fails config validation rather than being silently ignored, so a context that asks for one tells you instead of quietly not doing it.

## Environment variables

| Variable | Purpose |
|----------|---------|
| `OPENSHRIMP_HCS_KERNEL` | Kernel to boot, when not WSL's at its default path |
| `OPENSHRIMP_HCS_INITRD` | Control initramfs, when not at `C:\ProgramData\openshrimp\hcs\initrd.img` |
| `OPENSHRIMP_HCS_RDP_HELPER` | Directory holding an RDP helper bundle you staged yourself |
