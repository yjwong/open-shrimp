#!/bin/bash
# Build the base rootfs VHDX the HCS sandbox backend runs the agent inside.
#
# The control initramfs (scripts/build_hcs_initrd.sh) is busybox and nothing
# else; the guest userland lives here, on a SCSI-attached ext4 volume that the
# guest resolves by its `clauderoot` label and chroots into.  This image is
# what a context's `base_image` points at, and what
# scripts/build_hcs_gui_rootfs.sh takes as input to bake the computer-use
# variant from — the two scripts are a chain: base -> GUI.
#
# What the backend requires of the image, and why each is installed here:
#
#   * python3 — the in-chroot exec agent, the host->guest relay and the
#     guest->host bridge are all Python, launched as `python3 <script>`,
#   * e2fsprogs — persistent volumes are formatted (mkfs.ext4) and resolved
#     (blkid) from inside the chroot, because the initramfs busybox has
#     neither,
#   * ca-certificates + curl/wget — the in-guest agent-CLI installers fetch
#     over TLS,
#   * Node.js — the precondition for the npm-installed agent CLI.
#
# The agent CLI itself is deliberately *not* baked in: it is installed into
# the rootfs from inside the guest on first provision, so one image serves
# every agent backend.
#
# Run as root on any Linux (WSL included); loop mounts, chroot and network are
# all required:
#   sudo bash scripts/build_hcs_base_rootfs.sh /mnt/c/images/claude-root.vhdx
#
# Knobs: SIZE (sparse ext4 size), SUITE (Ubuntu release), MIRROR, and WORK
# (scratch directory — the build needs a few GB, so point it at a filesystem
# that has them).  With PUBLISH=1 a zstd-compressed copy and a `.sha256` are
# written alongside the VHDX, which is the form the release job uploads.
set -eu

OUT=${1:?usage: build_hcs_base_rootfs.sh /path/to/base.vhdx}
LABEL=clauderoot
SIZE=${SIZE:-6G}
SUITE=${SUITE:-noble}
MIRROR=${MIRROR:-http://archive.ubuntu.com/ubuntu/}
WORK=${WORK:-/root/hcs-base-build}
NODE_DIST=${NODE_DIST:-https://nodejs.org/dist/latest-v22.x}
[ "$(id -u)" = 0 ] || { echo "must run as root (loop mount, chroot)"; exit 1; }

need_pkgs=""
command -v debootstrap >/dev/null || need_pkgs="$need_pkgs debootstrap"
command -v qemu-img >/dev/null || need_pkgs="$need_pkgs qemu-utils"
command -v mkfs.ext4 >/dev/null || need_pkgs="$need_pkgs e2fsprogs"
command -v wget >/dev/null || need_pkgs="$need_pkgs wget"
[ -n "${PUBLISH:-}" ] && ! command -v zstd >/dev/null && need_pkgs="$need_pkgs zstd"
if [ -n "$need_pkgs" ]; then
    apt-get update -qq
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $need_pkgs >/dev/null
fi

RAW=$WORK/rootfs.raw
MNT=$WORK/mnt
mkdir -p "$MNT"
cleanup() {
    for m in proc sys dev/pts dev ""; do
        umount "$MNT/$m" 2>/dev/null || true
    done
}
trap cleanup EXIT

echo "=== raw ext4 image ($SIZE sparse, label $LABEL) ==="
rm -f "$RAW"
truncate -s "$SIZE" "$RAW"
mkfs.ext4 -q -F -L "$LABEL" "$RAW"
mount -o loop "$RAW" "$MNT"

echo "=== debootstrap $SUITE (minbase) ==="
# minbase keeps the image to the package set below plus its dependencies; no
# init system runs in the guest (the chroot is entered directly), so nothing
# larger buys anything.
debootstrap --variant=minbase --components=main,universe \
    "$SUITE" "$MNT" "$MIRROR"

echo "=== apt sources: main + universe ==="
rm -f "$MNT/etc/apt/sources.list"
cat > "$MNT/etc/apt/sources.list.d/ubuntu.sources" <<SRC
Types: deb
URIs: $MIRROR
Suites: $SUITE $SUITE-updates $SUITE-backports
Components: main universe
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: http://security.ubuntu.com/ubuntu/
Suites: $SUITE-security
Components: main universe
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
SRC

# debootstrap leaves no working resolver, and every step below needs one.
rm -f "$MNT/etc/resolv.conf"
cp /etc/resolv.conf "$MNT/etc/resolv.conf"
mount -t proc proc "$MNT/proc"
mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts" 2>/dev/null || true
mount -t sysfs sys "$MNT/sys" 2>/dev/null || true

echo "=== apt install: guest-side runtime + toolbelt ==="
chroot "$MNT" /usr/bin/env HOME=/root DEBIAN_FRONTEND=noninteractive \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin bash -c '
    set -e
    apt-get update 2>&1 | tail -3
    apt-get install -y --no-install-recommends \
      python3 e2fsprogs ca-certificates curl wget git openssh-client \
      procps less nano ripgrep unzip xz-utils bash-completion locales tzdata \
      2>&1 | tail -5
    # The backend launches these by name; a missing one only surfaces as a
    # dead relay inside a booted guest, so fail the build instead.
    command -v python3 mkfs.ext4 blkid curl wget git
  '

echo "=== Node.js (latest v22 LTS, linux-x64) into /usr/local ==="
# From nodejs.org rather than apt: the agent CLIs track a Node newer than the
# distro package, and /usr/local keeps the distro python3/apt untouched.
NODE_TARBALL=$(wget -qO- "$NODE_DIST/SHASUMS256.txt" \
    | grep -o 'node-v[0-9.]*-linux-x64\.tar\.xz' | head -1)
[ -n "$NODE_TARBALL" ] || { echo "could not resolve node tarball name"; exit 1; }
echo "node tarball: $NODE_TARBALL"
wget -q -O "$WORK/$NODE_TARBALL" "$NODE_DIST/$NODE_TARBALL"
tar -xJf "$WORK/$NODE_TARBALL" -C "$MNT/usr/local" --strip-components=1
rm -f "$WORK/$NODE_TARBALL"
chroot "$MNT" /usr/bin/env HOME=/root \
  PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin bash -c \
  'echo "node: $(node --version), npm: $(npm --version)"'

echo "=== guest identity + cleanup ==="
echo openshrimp-hcs > "$MNT/etc/hostname"
chroot "$MNT" /usr/bin/env DEBIAN_FRONTEND=noninteractive bash -c \
  'apt-get clean; rm -rf /var/lib/apt/lists/*'
# The host pushes the guest's nameservers by copying a file onto this path,
# so it must be a plain file — a systemd-resolved stub symlink would send the
# write somewhere the chroot cannot read back.
rm -f "$MNT/etc/resolv.conf"; touch "$MNT/etc/resolv.conf"

cleanup
trap - EXIT
e2fsck -pf "$RAW" >/dev/null || [ $? -le 1 ]
blkid "$RAW"

echo "=== raw -> dynamic VHDX ==="
# Convert on the local filesystem (on Windows, drvfs random writes are slow),
# then copy the finished image out.
VL=$WORK/base.vhdx
qemu-img convert -O vhdx -o subformat=dynamic "$RAW" "$VL"
qemu-img info "$VL" | sed -n '1,5p'
mkdir -p "$(dirname "$OUT")"
cp "$VL" "$OUT"
rm -f "$RAW" "$VL"

if [ -n "${PUBLISH:-}" ]; then
    echo "=== publish: zstd + sha256 ==="
    zstd -q -f -12 -T0 "$OUT" -o "$OUT.zst"
    (cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT").zst") > "$OUT.zst.sha256"
    cat "$OUT.zst.sha256"
    ls -l "$OUT.zst"
fi
ls -l "$OUT"
echo "HCS-BASE-ROOTFS-OK label=$LABEL out=$OUT"
