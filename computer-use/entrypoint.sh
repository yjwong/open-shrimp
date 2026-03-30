#!/bin/bash
# Entrypoint for computer-use containers.
# Starts a headless Wayland compositor (labwc), VNC server (wayvnc),
# and a default browser (Chromium).
#
# When ENABLE_DIND=1, also starts a rootless Docker daemon before the
# compositor — used when both computer_use and docker_in_docker are
# enabled for a context.
set -eu

MY_UID=$(id -u)
MY_GID=$(id -g)

# --- Optional: Docker-in-Docker setup ---
if [ "${ENABLE_DIND:-0}" = "1" ]; then
    # Ensure the current uid exists in /etc/passwd — rootless Docker's
    # newuidmap/newgidmap require a valid passwd entry.
    if ! getent passwd "$MY_UID" > /dev/null 2>&1; then
        echo "claude:x:${MY_UID}:${MY_GID}::/home/claude:/bin/bash" >> /etc/passwd
    fi
    if ! getent group "$MY_GID" > /dev/null 2>&1; then
        echo "claude:x:${MY_GID}:" >> /etc/group
    fi

    # Register subordinate uid/gid ranges for the current (non-root) user.
    echo "claude:100000:65536" > /etc/subuid
    echo "claude:100000:65536" > /etc/subgid

    # Ensure XDG_RUNTIME_DIR exists before starting dockerd-rootless.sh,
    # which checks for a writable runtime dir on startup.
    export XDG_RUNTIME_DIR="/tmp/runtime-${MY_UID}"
    mkdir -p "$XDG_RUNTIME_DIR"

    # Patch dockerd-rootless.sh to tolerate sysctl failures (ip_forward is
    # already set via the container's --sysctl flag).
    sed 's/sysctl -w \(.*\)$/sysctl -w \1 || true/' /usr/bin/dockerd-rootless.sh \
        > /tmp/dockerd-rootless.sh
    chmod +x /tmp/dockerd-rootless.sh

    # Disable slirp4netns's internal sandbox and seccomp — these try to
    # create mount namespaces/apply seccomp filters which are blocked by the
    # outer container's security profile.
    export DOCKERD_ROOTLESS_ROOTLESSKIT_SLIRP4NETNS_SANDBOX=false
    export DOCKERD_ROOTLESS_ROOTLESSKIT_SLIRP4NETNS_SECCOMP=false

    # Start rootless Docker daemon (no iptables in nested containers).
    SKIP_IPTABLES=1 /tmp/dockerd-rootless.sh --iptables=false \
        > /tmp/dockerd.log 2>&1 &

    # Wait for Docker to be ready (up to 30s).
    export DOCKER_HOST="unix:///tmp/runtime-${MY_UID}/docker.sock"
    for _i in $(seq 1 30); do
        if docker info > /dev/null 2>&1; then
            echo "Docker-in-Docker daemon ready"
            break
        fi
        sleep 1
    done

    # Create a Docker context so that `docker exec` sessions (which don't
    # inherit runtime env vars) can find the daemon without DOCKER_HOST.
    docker context create rootless --docker "host=unix:///tmp/runtime-${MY_UID}/docker.sock" 2>/dev/null || true
    docker context use rootless 2>/dev/null || true
    unset DOCKER_HOST

    # Add masquerade rules for container outbound networking.
    # rootless dockerd runs with --iptables=false (required in nested containers),
    # so we must manually add NAT rules for all bridge subnets (docker0 + any
    # docker-compose br-* networks created later).  Run in a background loop so
    # dynamically-created networks get rules too.
    _nsenter() {
        CHILD_PID=$(cat "${XDG_RUNTIME_DIR}/dockerd-rootless/child_pid" 2>/dev/null)
        [ -z "$CHILD_PID" ] && return 1
        nsenter --preserve-credentials -U -n -t "$CHILD_PID" "$@"
    }

    ensure_masquerade() {
        _nsenter true 2>/dev/null || return
        for BRIDGE in $(_nsenter ip -o link show type bridge 2>/dev/null \
                | grep -oP '(?<=: )\S+(?=:)'); do
            SUBNET=$(_nsenter ip -4 addr show "$BRIDGE" 2>/dev/null \
                | grep -oP 'inet \K[\d./]+')
            if [ -n "$SUBNET" ]; then
                _nsenter iptables -t nat -C POSTROUTING \
                    -s "$SUBNET" ! -o "$BRIDGE" -j MASQUERADE 2>/dev/null || \
                _nsenter iptables -t nat -A POSTROUTING \
                    -s "$SUBNET" ! -o "$BRIDGE" -j MASQUERADE 2>/dev/null || true
            fi
        done
    }

    cleanup_masquerade() {
        _nsenter true 2>/dev/null || return
        BRIDGES=$(_nsenter ip -o link show type bridge 2>/dev/null \
            | grep -oP '(?<=: )\S+(?=:)')
        _nsenter iptables -t nat -S POSTROUTING 2>/dev/null \
            | grep 'MASQUERADE' | while read -r RULE; do
            RULE_BRIDGE=$(echo "$RULE" | sed -n 's/.* -o \([^ ]*\).*/\1/p')
            RULE_SUBNET=$(echo "$RULE" | sed -n 's/.*-s \([^ ]*\).*/\1/p')
            [ -z "$RULE_BRIDGE" ] || [ -z "$RULE_SUBNET" ] && continue
            if ! echo "$BRIDGES" | grep -qxF "$RULE_BRIDGE"; then
                _nsenter iptables -t nat -D POSTROUTING \
                    -s "$RULE_SUBNET" ! -o "$RULE_BRIDGE" -j MASQUERADE \
                    2>/dev/null || true
            fi
        done
    }

    ensure_masquerade
    docker events --filter type=network --format '{{.Action}}' 2>/dev/null | \
        while read -r event; do
            case "$event" in
                create|connect) ensure_masquerade ;;
                destroy) cleanup_masquerade ;;
            esac
        done &
fi

# --- Wayland / wlroots environment ---
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
# capability.  Without this, Chromium (native Wayland) never creates a
# wl_keyboard listener and silently drops all wlrctl keyboard input.
seat-keyboard &
echo "seat-keyboard started"

# Start wayvnc for observability (optional, non-fatal if it fails).
wayvnc --output=HEADLESS-1 0.0.0.0 5900 &
echo "wayvnc started on port 5900"

# Launch Chromium (installed by Playwright) with Wayland native mode and
# a CDP debugging port so Playwright MCP can attach to it.
CHROMIUM_BIN=$(find /home/claude/.cache/ms-playwright -name 'chromium' -o -name 'chrome' 2>/dev/null | grep -E '/(chromium|chrome)$' | head -1)
if [ -z "$CHROMIUM_BIN" ]; then
    echo "WARNING: Chromium binary not found in Playwright cache" >&2
else
    "$CHROMIUM_BIN" \
        --no-first-run \
        --no-default-browser-check \
        --no-sandbox \
        --disable-dev-shm-usage \
        --disable-background-networking \
        --ozone-platform=wayland \
        --user-data-dir=/home/claude/.config/chromium \
        --remote-debugging-port=9222 &
    echo "Chromium started with CDP on port 9222"
fi

# Keep the container alive.  CLI invocations arrive via `docker exec`.
exec sleep infinity
