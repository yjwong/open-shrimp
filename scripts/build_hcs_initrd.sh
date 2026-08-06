#!/bin/bash
# Build the control initramfs the HCS sandbox backend boots the guest with.
#
# The image is PID 1 plus one service: the static vsock control agent
# (src/open_shrimp/sandbox/hcs_control_agent.c) listening on AF_VSOCK port
# 0x5000.  Everything the host does to a fresh guest goes through it —
# mounting the 9p shares (its in-agent @mount, because busybox mount cannot
# dial a socket), mounting the rootfs VHDX by ext4 label, binding the shares
# into the chroot, configuring the interface, and launching the in-chroot
# exec agent.  So the image needs exactly three things:
#
#   * the control agent, statically linked (an initramfs root has no dynamic
#     loader and no libc),
#   * `labelfind` (src/open_shrimp/sandbox/hcs_labelfind.c) — Ubuntu's
#     busybox-static is built without the volumeid feature, so there is no
#     blkid/findfs to resolve the `clauderoot` label with,
#   * a static busybox whose applet set covers every command the host issues
#     over the control channel.
#
# The guest userland itself is *not* here: it lives in the rootfs VHDX that
# scripts/build_hcs_base_rootfs.sh produces and that this image chroots into.
#
# Run as root on any Linux (WSL included); the output belongs somewhere the
# Windows host can read, and is pointed at by OPENSHRIMP_HCS_INITRD (default
# C:\ProgramData\openshrimp\hcs\initrd.img):
#   sudo bash scripts/build_hcs_initrd.sh /mnt/c/ProgramData/openshrimp/hcs/initrd.img
#
# With PUBLISH=1 a `<out>.sha256` is written alongside, which is the form the
# release job uploads.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
SRC_DIR=$HERE/../src/open_shrimp/sandbox
OUT=${1:?usage: build_hcs_initrd.sh /path/to/initrd.img}
AGENT_SRC=${2:-$SRC_DIR/hcs_control_agent.c}
LABELFIND_SRC=${3:-$SRC_DIR/hcs_labelfind.c}
[ "$(id -u)" = 0 ] || { echo "must run as root (mknod)"; exit 1; }
[ -f "$AGENT_SRC" ] || { echo "missing control agent source: $AGENT_SRC"; exit 1; }
[ -f "$LABELFIND_SRC" ] || { echo "missing labelfind source: $LABELFIND_SRC"; exit 1; }

need_pkgs=""
command -v gcc >/dev/null || need_pkgs="$need_pkgs gcc"
command -v cpio >/dev/null || need_pkgs="$need_pkgs cpio"
[ -e /usr/include/linux/vm_sockets.h ] || need_pkgs="$need_pkgs linux-libc-dev"
# libc.a marks a static-capable libc6-dev; probe the multiarch path too.
if ! ls /usr/lib/x86_64-linux-gnu/libc.a /usr/lib/libc.a >/dev/null 2>&1; then
    need_pkgs="$need_pkgs libc6-dev"
fi

# A static busybox is required: the initramfs has no loader or libc.
BB=""
for cand in /usr/lib/initramfs-tools/bin/busybox /bin/busybox /usr/bin/busybox; do
    if [ -x "$cand" ] && ! ldd "$cand" >/dev/null 2>&1; then
        BB=$cand
        break
    fi
done
[ -n "$BB" ] || need_pkgs="$need_pkgs busybox-static"

if [ -n "$need_pkgs" ]; then
    apt-get update -qq
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $need_pkgs >/dev/null
fi
[ -n "$BB" ] || BB=/bin/busybox

echo "=== toolchain ==="
echo "gcc: $(gcc --version | head -n1)"
echo "busybox: $BB"

# Every applet below is load-bearing for some command the host issues over the
# control channel: the mount sequence (mount/umount/mkdir/chroot), the rootfs
# label probe (sh/echo), the network push (ip/printf/cp), the detached
# launches of the exec agent and desktop (setsid/sleep), and teardown (sync).
# Fail the build rather than discover a missing applet inside a booted guest.
for applet in sh mount umount mkdir chroot setsid ip printf cp cat echo \
              sleep sync grep kill; do
    if ! "$BB" --list | grep -qx "$applet"; then
        echo "busybox at $BB lacks required applet: $applet" >&2
        exit 1
    fi
done

W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/bin" "$W/dev" "$W/proc" "$W/sys" "$W/tmp" "$W/etc" \
         "$W/mnt/ws" "$W/mnt/home" "$W/mnt/cfg" "$W/mnt/tasktmp" "$W/mnt/root"

echo "=== control agent + labelfind (static) ==="
gcc -O2 -static -o "$W/bin/agent" "$AGENT_SRC"
if ldd "$W/bin/agent" >/dev/null 2>&1; then
    echo "agent is dynamically linked -- static link failed" >&2
    exit 1
fi
echo "agent: $(stat -c %s "$W/bin/agent") bytes"

gcc -O2 -static -o "$W/bin/labelfind" "$LABELFIND_SRC"
if ldd "$W/bin/labelfind" >/dev/null 2>&1; then
    echo "labelfind is dynamically linked -- static link failed" >&2
    exit 1
fi
echo "labelfind: $(stat -c %s "$W/bin/labelfind") bytes"

cp "$BB" "$W/bin/busybox"
# The required set above plus the ones an operator wants when hand-debugging a
# guest over the control channel.
for applet in sh mount umount cat uname grep sleep echo dd time date find cp \
              mkdir sync ls wc du rm mv sort head tail stat touch df md5sum \
              sed cut tr xargs cmp true chmod mktemp ln kill ps \
              ip nc wget nslookup httpd ping netstat ifconfig route \
              dmesg timeout hostname printf chroot setsid; do
    ln -s busybox "$W/bin/$applet"
done
# The kernel opens /dev/console for PID 1's stdio; devtmpfs is not
# auto-mounted for an initramfs root, so the nodes must exist in the archive.
mknod -m 600 "$W/dev/console" c 5 1
mknod -m 666 "$W/dev/null" c 1 3
mknod -m 666 "$W/dev/zero" c 1 5
mknod -m 666 "$W/dev/urandom" c 1 9

cat > "$W/init" <<'EOF'
#!/bin/sh
export PATH=/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
# The SCSI-attached VHDX surfaces as /dev/sdX only through devtmpfs; the
# static /dev nodes cover PID 1's stdio, which is already open, so the
# overmount is safe.
mount -t devtmpfs dev /dev
echo "===HCS-AGENT: init up==="
uname -r
# The agent inherits this stdio, so its AGENT-LISTENING line — the marker the
# host waits for before issuing commands — reaches the console.
/bin/agent &
while true; do sleep 3600; done
EOF
chmod +x "$W/init"

echo "=== cpio archive ==="
mkdir -p "$(dirname "$OUT")"
(cd "$W" && find . | cpio -o -H newc 2>/dev/null | gzip -9) > "$OUT"
if [ -n "${PUBLISH:-}" ]; then
    (cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")") > "$OUT.sha256"
    cat "$OUT.sha256"
fi
ls -l "$OUT"
echo "HCS-INITRD-OK out=$OUT"
