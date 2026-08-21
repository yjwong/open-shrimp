#!/bin/bash
#
# Submit to the notary service, wait for a verdict, and staple the ticket.
#
# Two things are notarized per release and neither stands in for the other: the
# DMG the user downloads, and the .app inside it.  Sparkle extracts the .app
# from an update and validates that on its own, so an unstapled app inside a
# stapled DMG installs only while the machine can reach Apple — offline, the
# update fails with nothing to explain it.
#
# A rejection prints the notary log before failing.  Without it the failure is
# one line saying "Invalid" and the reason lives behind a submission ID that
# the runner has already thrown away.
#
# Usage: notarize.sh <submit> [staple]
#
# `submit` is the archive handed to the notary service.  `staple` is what the
# ticket is attached to, which is a different path when a .app has to be zipped
# to be submitted at all; it defaults to `submit`.
#
# Reads API_KEY_B64, API_KEY_ID and API_ISSUER_ID from the environment.
set -uo pipefail

SUBMIT="$1"
STAPLE="${2:-$1}"

KEY="$(mktemp -t notary_key)"
trap 'rm -f "$KEY"' EXIT
printf '%s' "$API_KEY_B64" | base64 --decode -o "$KEY"

SUBMIT_OUT=$(xcrun notarytool submit "$SUBMIT" \
    --key "$KEY" --key-id "$API_KEY_ID" --issuer "$API_ISSUER_ID" \
    --wait --timeout 30m 2>&1)
SUBMIT_RC=$?
echo "$SUBMIT_OUT"

# The service can accept the submission and reject the contents, so the exit
# status alone is not the verdict.
if [ "$SUBMIT_RC" -ne 0 ] || ! printf '%s\n' "$SUBMIT_OUT" | grep -q 'status: Accepted'; then
    SUBMIT_ID=$(printf '%s\n' "$SUBMIT_OUT" | awk '/^[[:space:]]*id:/ {print $2; exit}')
    if [ -n "$SUBMIT_ID" ]; then
        echo "::group::Notary log for $SUBMIT_ID"
        xcrun notarytool log "$SUBMIT_ID" \
            --key "$KEY" --key-id "$API_KEY_ID" --issuer "$API_ISSUER_ID" || true
        echo "::endgroup::"
    fi
    exit 1
fi

set -e
xcrun stapler staple "$STAPLE"
xcrun stapler validate "$STAPLE"
