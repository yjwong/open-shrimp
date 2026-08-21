#!/bin/bash
#
# Generate the signed Sparkle appcast for a release DMG.
#
# The feed is published as a release asset named `appcast.xml`, which GitHub
# also serves from a URL that does not name a tag — so the URL every shipped
# app carries is stable, and no second deploy path or commit to master exists
# to keep it fed.
#
# `generate_appcast` operates on a directory, not a file: it extracts every
# archive it finds, reads the version out of the app inside, signs the archive,
# and merges the result into an appcast already sitting in that directory.  The
# DMG is copied into a scratch directory of its own because the tool moves
# superseded archives aside as it runs, and the release's real DMG is not
# something to hand it.
#
# Usage: make-appcast.sh <app> <dmg> <tag> <output>
#
# Reads SPARKLE_ED_PRIVATE_KEY from the environment: the base64 seed of the
# EdDSA key whose public half is SUPublicEDKey in the bundle's Info.plist.
set -euo pipefail

APP="$1"
DMG="$2"
TAG="$3"
OUT="$4"

if [ -z "${SPARKLE_ED_PRIVATE_KEY:-}" ]; then
    echo "::error::SPARKLE_ED_PRIVATE_KEY is not set — the feed would be unsigned"
    exit 1
fi

GENERATE_APPCAST="$(dirname "$0")/../vendor/bin/generate_appcast"
if [ ! -x "$GENERATE_APPCAST" ]; then
    echo "::error::$GENERATE_APPCAST is missing — run \`make sparkle\`"
    exit 1
fi

# Checked up front: the tests at the end are all that stops a broken feed from
# being published, and a missing xmllint would otherwise show up part-way
# through verifying as some other failure instead of a missing tool.
if ! command -v xmllint >/dev/null; then
    echo "::error::xmllint is required to verify the generated feed"
    exit 1
fi

# Read the feed off the bundle that is about to ship rather than stating it a
# second time.  The feed and the enclosures inside it have to name the same
# repository: a feed whose downloads point elsewhere verifies, offers an
# update, and then hands every install a 404.
FEED_URL="$(/usr/libexec/PlistBuddy -c "Print :SUFeedURL" "$APP/Contents/Info.plist")"
SUFFIX="/releases/latest/download/appcast.xml"
case "$FEED_URL" in
    *"$SUFFIX") REPO_URL="${FEED_URL%"$SUFFIX"}" ;;
    *)
        echo "::error::SUFeedURL is not a GitHub latest-asset URL: $FEED_URL"
        exit 1
        ;;
esac
DOWNLOAD_PREFIX="$REPO_URL/releases/download/$TAG/"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp "$DMG" "$WORK/"

# Seed with the published feed so the older entries survive.  An install would
# not notice their loss, because Sparkle offers the newest item and no other,
# so the first release with no feed to fetch yet is not an error.  A transport
# failure is an error: it would narrow the feed to a single item for a reason
# nobody would go looking for.
STATUS="$(curl -sSL -w '%{http_code}' -o "$WORK/seed.xml" "$FEED_URL")" || {
    echo "::error::could not reach the published feed at $FEED_URL"
    exit 1
}
case "$STATUS" in
    200)
        # A published feed that does not parse is broken, not missing.
        # Starting a fresh one over the top of it would republish under the
        # same URL and bury whatever went wrong.
        xmllint --noout "$WORK/seed.xml"
        mv "$WORK/seed.xml" "$WORK/appcast.xml"
        ;;
    404)
        echo "no feed published yet; starting one"
        rm -f "$WORK/seed.xml"
        ;;
    *)
        echo "::error::unexpected HTTP $STATUS fetching $FEED_URL"
        exit 1
        ;;
esac

# --link is the product website, not the download: Sparkle offers it when it
# cannot install an update from inside the app.  The download URL comes from
# --download-url-prefix, and there is no flag for the feed's own address.
printf '%s\n' "$SPARKLE_ED_PRIVATE_KEY" | "$GENERATE_APPCAST" \
    --ed-key-file - \
    --download-url-prefix "$DOWNLOAD_PREFIX" \
    --link "https://shrimp.wong.place" \
    --maximum-versions 10 \
    -o "$WORK/appcast.xml" \
    "$WORK"

# Fail the release rather than publish a feed nobody can install from.  Parsing
# is the weak check; the entry for this release also has to be present, signed,
# and pointing at the asset this run uploads.
#
# If the signing key does not match SUPublicEDKey in the bundle,
# generate_appcast prints a warning, leaves the signature off the enclosure,
# and exits 0.  A wrong key would then ship a green release whose feed every
# install refuses.
#
# `sparkle:edSignature` is namespaced, so it is matched by local name.  The
# query asks for a boolean so that xmllint prints "true" or "false", rather
# than a number whose formatting is libxml2's choice.
xmllint --noout "$WORK/appcast.xml"
ENTRY="$TAG/$(basename "$DMG")"
FOUND="$(xmllint --xpath "boolean(//item/enclosure[contains(@url, '$ENTRY') and string-length(@*[local-name()='edSignature']) > 0])" "$WORK/appcast.xml" 2>/dev/null || echo false)"
if [ "$FOUND" != "true" ]; then
    echo "::error::appcast has no signed entry for $ENTRY"
    xmllint --format "$WORK/appcast.xml" || cat "$WORK/appcast.xml"
    exit 1
fi

mkdir -p "$(dirname "$OUT")"
cp "$WORK/appcast.xml" "$OUT"
echo "wrote $OUT"
