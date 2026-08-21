#!/bin/bash
#
# Seal OpenShrimp.app, nested code first.
#
# One `codesign` over the bundle seals it but does not sign the code inside it.
# Sparkle brings a framework carrying a helper .app, a standalone Autoupdate
# executable and two XPC services, and every one of them needs a signature of
# its own, applied before the enclosure that records its hash.  Notarization
# rejects a submission that leaves one unsigned, and resigning the enclosure
# afterwards does not repair it.
#
# The nested set is discovered rather than listed: a Sparkle version that moves
# a helper is signed anyway instead of quietly leaving one out.
#
# Usage: sign-bundle.sh <identity> <app> [entitlements]
set -euo pipefail

IDENTITY="$1"
APP="$2"
ENTITLEMENTS="${3:-}"

# Ad-hoc is the local build's identity, and takes neither a timestamp — there
# is no certificate to timestamp against — nor the hardened runtime, which
# would turn on library validation over an app and a framework that have no
# Team ID to match each other with.
COMMON=(--force --sign "$IDENTITY")
if [ "$IDENTITY" != "-" ]; then
    COMMON+=(--timestamp --options runtime)
fi

# `find -depth` walks post-order: everything inside a directory is listed
# before the directory itself, which is the order codesign requires.  Symlinks
# are skipped rather than followed — a framework's top level is nothing but
# links into Versions/, and signing through one signs the same file twice
# under a name that is not where it lives.
if [ -d "$APP/Contents/Frameworks" ]; then
    while IFS= read -r path; do
        [ -L "$path" ] && continue
        case "$path" in
            *.app | *.xpc | *.framework | *.bundle | *.appex)
                [ -d "$path" ] || continue
                ;;
            *)
                # A loose executable inside a bundle is sealed as a resource
                # rather than signed by the enclosing pass, so it has to be
                # named here.  Anything that is not Mach-O is a resource and
                # is left to the seal.
                [ -f "$path" ] && [ -x "$path" ] || continue
                case "$(file -b "$path")" in
                    Mach-O*) ;;
                    *) continue ;;
                esac
                ;;
        esac
        echo "signing $path"
        codesign "${COMMON[@]}" "$path"
    done < <(find "$APP/Contents/Frameworks" -depth)
fi

# The identifier is stated rather than derived so that an executable run
# outside a bundle, and a bundle whose plist has not been substituted yet,
# still sign as the app the login item registered.
APP_ARGS=("${COMMON[@]}" --identifier com.openshrimp.app)
if [ -n "$ENTITLEMENTS" ]; then
    APP_ARGS+=(--entitlements "$ENTITLEMENTS")
fi
echo "signing $APP"
codesign "${APP_ARGS[@]}" "$APP"

# `--deep` verification has something to say now that there is nested code, and
# an unsigned XPC service is caught here rather than by the notary service
# twenty minutes later.
codesign --verify --strict --deep --verbose=2 "$APP"
