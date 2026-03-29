#!/bin/bash
# Entrypoint for computer-use containers.
# Starts a headless Wayland compositor (labwc), VNC server (wayvnc),
# and a default browser (Firefox ESR).
set -eu

MY_UID=$(id -u)

# Wayland / wlroots environment.
export XDG_RUNTIME_DIR="/tmp/runtime-${MY_UID}"
mkdir -p "$XDG_RUNTIME_DIR"
export WLR_BACKENDS=headless
export WLR_RENDERER=pixman
export WAYLAND_DISPLAY=wayland-0

# Fixed resolution for first iteration.
export WLR_HEADLESS_OUTPUTS=1

# Ensure screenshot directory exists.
mkdir -p /tmp/screenshots

# Start labwc (headless Wayland compositor).
labwc &
LABWC_PID=$!

# Wait for the Wayland socket to appear (up to 15s).
for _i in $(seq 1 75); do
    if [ -S "${XDG_RUNTIME_DIR}/${WAYLAND_DISPLAY}" ]; then
        break
    fi
    sleep 0.2
done

if [ ! -S "${XDG_RUNTIME_DIR}/${WAYLAND_DISPLAY}" ]; then
    echo "ERROR: labwc did not create Wayland socket after 15s" >&2
    exit 1
fi

echo "labwc ready (PID ${LABWC_PID})"

# Start persistent virtual keyboard so the seat advertises keyboard
# capability.  Without this, Firefox (native Wayland) never creates a
# wl_keyboard listener and silently drops all wlrctl keyboard input.
seat-keyboard &
echo "seat-keyboard started"

# Start wayvnc for observability (optional, non-fatal if it fails).
wayvnc --output=HEADLESS-1 0.0.0.0 5900 &
echo "wayvnc started on port 5900"

# Launch Firefox ESR with the pre-configured profile.
firefox-esr --no-remote -P default &
echo "Firefox ESR started"

# Keep the container alive.  CLI invocations arrive via `docker exec`.
exec sleep infinity
