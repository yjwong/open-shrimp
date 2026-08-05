#!/bin/bash
# Bake the computer-use rootfs template variant for the HCS sandbox backend.
#
# Takes the operator's base_image VHDX (ext4, label `clauderoot`, holding the
# guest userland + agent CLI) and produces `<base>-gui.vhdx` next to it: the
# same rootfs plus a full offscreen desktop stack —
#
#   * weston 13 with the RDP backend (renders with pixman, serves RDP over
#     TCP :3389 with a baked self-signed TLS cert),
#   * socat (the in-guest AF_VSOCK:3389 -> 127.0.0.1:3389 relay the host
#     dials over AF_HYPERV),
#   * Google Chrome (deb — noble's `chromium` apt package is a snap stub
#     that cannot run in a chroot), fonts, xdg-utils,
#   * wl-clipboard (wl-copy/wl-paste — the host implements the clipboard
#     protocol members over the exec channel),
#   * /usr/local/bin/start-weston.sh, the self-supervising bring-up script
#     the host provision pass launches detached at boot.
#
# The backend selects this variant instead of base_image whenever the
# context sets `computer_use: true` (see sandbox/hcs.py); everything else
# about the guest (label, layout, agent CLI) is inherited from the base.
#
# Run as root inside a WSL distro on the Windows host (loop mounts, chroot,
# network):
#   wsl -d <distro> -- sudo bash build_hcs_gui_rootfs.sh \
#       /mnt/c/images/claude-root.vhdx [/mnt/c/images/claude-root-gui.vhdx]
set -eu

BASE=${1:?usage: build_hcs_gui_rootfs.sh base.vhdx [out-gui.vhdx]}
OUT=${2:-$(dirname "$BASE")/$(basename "$BASE" .vhdx)-gui.vhdx}
SIZE=12G
[ "$(id -u)" = 0 ] || { echo "must run as root (loop mount, chroot)"; exit 1; }
[ -f "$BASE" ] || { echo "missing base image: $BASE"; exit 1; }

need_pkgs=""
command -v qemu-img >/dev/null || need_pkgs="$need_pkgs qemu-utils"
command -v resize2fs >/dev/null || need_pkgs="$need_pkgs e2fsprogs"
command -v wget >/dev/null || need_pkgs="$need_pkgs wget"
if [ -n "$need_pkgs" ]; then
    apt-get update -qq
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $need_pkgs >/dev/null
fi

W=/root/hcs-gui-build
RAW=$W/rootfs.raw
MNT=$W/mnt
mkdir -p "$MNT"
cleanup() {
    for m in proc sys dev/pts dev ""; do
        umount "$MNT/$m" 2>/dev/null || true
    done
}
trap cleanup EXIT

echo "=== base VHDX -> raw (build on the WSL-local fs; drvfs is slow) ==="
rm -f "$RAW"
qemu-img convert -O raw "$BASE" "$RAW"
truncate -s "$SIZE" "$RAW"
e2fsck -pf "$RAW" >/dev/null || [ $? -le 1 ]
resize2fs "$RAW"
blkid "$RAW"

mount -o loop "$RAW" "$MNT"
rm -f "$MNT/etc/resolv.conf"
cp /etc/resolv.conf "$MNT/etc/resolv.conf"
mount -t proc proc "$MNT/proc"
mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts" 2>/dev/null || true
mount -t sysfs sys "$MNT/sys" 2>/dev/null || true

echo "=== apt sources: main + universe (weston lives in universe) ==="
rm -f "$MNT/etc/apt/sources.list"
cat > "$MNT/etc/apt/sources.list.d/ubuntu.sources" <<'SRC'
Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: noble noble-updates noble-backports
Components: main universe
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: http://security.ubuntu.com/ubuntu/
Suites: noble-security
Components: main universe
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
SRC

echo "=== apt install: weston rdp-backend + relay + fonts + tools ==="
chroot "$MNT" /usr/bin/env HOME=/root DEBIAN_FRONTEND=noninteractive \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin bash -c '
    set -e
    apt-get update 2>&1 | tail -3
    apt-get install -y --no-install-recommends \
      weston libfreerdp-server2-2 socat openssl ca-certificates \
      xkb-data dbus dbus-x11 xdg-utils wget wl-clipboard \
      fonts-dejavu-core fonts-liberation fonts-noto-core fonts-noto-cjk \
      fonts-noto-color-emoji \
      2>&1 | tail -5
    echo "weston: $(weston --version)"
    ls -l /usr/lib/x86_64-linux-gnu/libweston-13/rdp-backend.so
    command -v socat weston-terminal xdg-open wl-copy wl-paste
  '

echo "=== Google Chrome (deb, not snap) ==="
wget -q -O "$MNT/tmp/google-chrome.deb" \
    'https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb'
chroot "$MNT" /usr/bin/env HOME=/root DEBIAN_FRONTEND=noninteractive \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin bash -c '
    set -e
    apt-get install -y --no-install-recommends /tmp/google-chrome.deb 2>&1 | tail -2
    rm -f /tmp/google-chrome.deb
    google-chrome-stable --version
  '

echo "=== TLS cert + weston.ini ==="
mkdir -p "$MNT/etc/weston"
chroot "$MNT" openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /etc/weston/rdp-key.pem -out /etc/weston/rdp-cert.pem \
  -days 3650 -subj /CN=weston-rdp 2>/dev/null
chmod 600 "$MNT/etc/weston/rdp-key.pem"

mkdir -p "$MNT/etc/xdg/weston"
cat > "$MNT/etc/xdg/weston/weston.ini" <<'INI'
[core]
backend=rdp-backend.so
shell=desktop-shell.so
require-input=false
idle-time=0
[rdp]
tls-cert=/etc/weston/rdp-cert.pem
tls-key=/etc/weston/rdp-key.pem
[shell]
background-color=0xff002244
panel-position=bottom
INI

echo "=== start-weston.sh (host launches this detached at boot) ==="
cat > "$MNT/usr/local/bin/start-weston.sh" <<'SH'
#!/bin/bash
# Desktop bring-up inside the guest chroot: weston-rdp + the AF_VSOCK:3389
# relay, each restarted by a supervision loop if it dies.  The host launches
# this with setsid as fire-and-forget — dbus-launch keeps an fd of the exec
# channel open, so the launching call must never wait for EOF.
export XDG_RUNTIME_DIR=/run/user/0
mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
mkdir -p /var/log
export WESTON_DISABLE_GL=1
# weston binds *:3389 but the relay dials 127.0.0.1 — without loopback up
# every RDP reply is 0 bytes.
ip link set lo up 2>/dev/null || true
# Session bus for desktop-shell.
eval "$(dbus-launch --sh-syntax)" 2>/dev/null || true

( while :; do
    weston --backend=rdp-backend.so --shell=desktop-shell.so \
      --renderer=pixman --idle-time=0 \
      --rdp-tls-cert=/etc/weston/rdp-cert.pem \
      --rdp-tls-key=/etc/weston/rdp-key.pem \
      --log=/var/log/weston.log >>/var/log/weston.out 2>&1
    sleep 1
  done ) </dev/null >/dev/null 2>&1 &

# weston numbers its socket dynamically (wayland-1 when wayland-0 is taken):
# discover the socket it actually created instead of assuming wayland-0.
WD=
for _ in $(seq 1 120); do
  WD=$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -type s -name 'wayland-*' \
       -printf '%f\n' 2>/dev/null | head -1)
  [ -n "$WD" ] && break
  sleep 0.5
done
export WAYLAND_DISPLAY="${WD:-wayland-0}"
# App launches read the discovered socket name from here.
printf '%s\n' "$WAYLAND_DISPLAY" > "$XDG_RUNTIME_DIR/wayland-display"

( while :; do
    socat VSOCK-LISTEN:3389,fork,reuseaddr TCP:127.0.0.1:3389 \
      >>/var/log/socat.log 2>&1
    sleep 1
  done ) </dev/null >/dev/null 2>&1 &

setsid weston-terminal >>/var/log/weston-terminal.log 2>&1 </dev/null &
echo "START-WESTON-OK display=$WAYLAND_DISPLAY"
SH
chmod +x "$MNT/usr/local/bin/start-weston.sh"

echo "=== chromium wrapper (root chroot: no setuid sandbox; ozone=wayland) ==="
cat > "$MNT/usr/local/bin/chromium" <<'SH'
#!/bin/sh
# Chrome runs as root inside the guest chroot, where the setuid sandbox
# cannot work; the compositor is Wayland-only, so ozone must not probe X11.
export XDG_RUNTIME_DIR=/run/user/0
WD=$(cat "$XDG_RUNTIME_DIR/wayland-display" 2>/dev/null || echo wayland-0)
export WAYLAND_DISPLAY="$WD"
exec /usr/bin/google-chrome-stable --no-sandbox --ozone-platform=wayland \
  --disable-gpu --no-first-run --disable-crash-reporter "$@"
SH
chmod +x "$MNT/usr/local/bin/chromium"

echo "=== apt cache cleanup ==="
chroot "$MNT" /usr/bin/env DEBIAN_FRONTEND=noninteractive bash -c \
  'apt-get clean; rm -rf /var/lib/apt/lists/*'
rm -f "$MNT/etc/resolv.conf"; touch "$MNT/etc/resolv.conf"

cleanup
trap - EXIT
blkid "$RAW"

echo "=== raw -> dynamic VHDX ==="
VL=$W/gui.vhdx
qemu-img convert -O vhdx -o subformat=dynamic "$RAW" "$VL"
qemu-img info "$VL" | sed -n '1,5p'
cp "$VL" "$OUT"
rm -f "$RAW" "$VL"
ls -l "$OUT"
echo "HCS-GUI-ROOTFS-OK out=$OUT"
