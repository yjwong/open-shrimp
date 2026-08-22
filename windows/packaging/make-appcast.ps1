#!/usr/bin/env pwsh
#
# Generate the signed Sparkle appcast for a release MSI.
#
# The feed ships as a release asset named `appcast-windows.xml`.  GitHub serves
# release assets from a URL that names no tag, so the URL compiled into every
# tray stays valid without a second deploy path or a commit to master.  The
# name differs from the macOS `appcast.xml` because both are assets of the same
# release.
#
# The generator is handed exactly one MSI, so the feed it writes holds exactly
# one item, and Sparkle offers the newest item and no other.  macOS seeds from
# the published feed first only because its generator rebuilds the whole feed
# out of a directory of archives.
#
# ASCII only, deliberately.  This file carries no byte-order mark, and Windows
# PowerShell reads a script without one in the machine's ANSI codepage: an em
# dash decodes there as three characters, one of which is a curly quote that
# closes whatever string it lands in, and the whole file then fails to parse.
# The shebang asks for pwsh, which would read it as UTF-8, but a caller that
# says `shell: powershell` is one word away.
#
# Usage: make-appcast.ps1 -Msi <path> -Version <x.y.z> -BaseUrl <prefix/> -OutputDirectory <dir>
#
# Reads SPARKLE_PRIVATE_KEY and SPARKLE_PUBLIC_KEY from the environment: the
# base64 seed of the EdDSA key the release signs with, and the base64 public
# half the tray verifies against.  The same pair macOS uses, under the variable
# names this generator reads.

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Msi,
    [Parameter(Mandatory)][string]$Version,
    [Parameter(Mandatory)][string]$BaseUrl,
    [Parameter(Mandatory)][string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$SparkleNs = 'http://www.andymatuschak.org/xml-namespaces/sparkle'
$Generator = 'netsparkle-generate-appcast'

function Fail([string]$message) {
    Write-Host "::error::$message"
    exit 1
}

if ([string]::IsNullOrWhiteSpace($env:SPARKLE_PRIVATE_KEY)) {
    Fail 'SPARKLE_PRIVATE_KEY is not set: the feed would be unsigned'
}

# The public key is not a secret, and it still has to be set: the generator
# verifies the feed's own detached signature against it before writing it, and
# `--verify` below reports `Signature invalid` when either key is absent, so an
# unset public key looks exactly like a wrong one.
if ([string]::IsNullOrWhiteSpace($env:SPARKLE_PUBLIC_KEY)) {
    Fail 'SPARKLE_PUBLIC_KEY is not set: the signature could not be verified'
}

if (-not (Get-Command $Generator -ErrorAction SilentlyContinue)) {
    Fail "$Generator is missing: install NetSparkleUpdater.Tools.AppCastGenerator"
}

# The enclosure URL is the prefix concatenated with the file name; the
# generator neither adds a separator nor complains about a missing one, so a
# prefix without a trailing slash yields a URL that is well-formed and 404s.
if (-not $BaseUrl.EndsWith('/')) {
    Fail "BaseUrl must end with a slash: $BaseUrl"
}

$msiFile = Get-Item -LiteralPath $Msi
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$outDir = (Resolve-Path -LiteralPath $OutputDirectory).Path
$feedPath = Join-Path $outDir 'appcast-windows.xml'
$feedSignaturePath = "$feedPath.signature"

# `--file-version` supplies the only version this item will ever have.  Left to
# itself the generator reads one with `FileVersionInfo`, which needs a PE
# version resource; an MSI is an OLE compound document and has none, so the
# read returns null and the whole file is skipped.
#
# `--single-file` guarantees there is exactly one binary for that pinned
# version to apply to.  `--use-ed25519-signature-attribute` spells the
# enclosure attribute `sparkle:edSignature`, the way Sparkle does.
& $Generator `
    --single-file $msiFile.FullName `
    --file-version $Version `
    --appcast-output-directory $outDir `
    --output-file-name appcast-windows `
    --base-url $BaseUrl `
    --use-ed25519-signature-attribute `
    --product-name OpenShrimp `
    --os windows `
    --human-readable
if ($LASTEXITCODE -ne 0) {
    Fail "$Generator exited $LASTEXITCODE"
}

# Everything below is what stops a broken feed from being published, and every
# check answers a way this generator fails while exiting 0:
#
#   - No signing key writes a complete-looking feed whose enclosure carries no
#     signature attribute at all.
#   - No version for the binary writes a valid feed holding zero items.  An
#     empty feed parses, uploads, and tells every tray it is up to date
#     forever.
#   - A private key that is not the one the tray verifies against writes the
#     feed and no `.signature` beside it: the generator checks that one itself
#     and declines to write what did not verify.
foreach ($path in @($feedPath, $feedSignaturePath)) {
    if (-not (Test-Path -LiteralPath $path)) {
        Fail "$Generator wrote no $(Split-Path -Leaf $path)"
    }
}

# Strict mode fetches `<feed>.signature` as a separate request and refuses the
# feed if it does not check out, so an empty one is a feed nobody can read.
$feedSignature = (Get-Content -LiteralPath $feedSignaturePath -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($feedSignature)) {
    Fail 'the feed signature file is empty'
}

try {
    $feed = [xml](Get-Content -LiteralPath $feedPath -Raw)
} catch {
    Fail "the generated feed is not well-formed XML: $_"
}

$ns = New-Object System.Xml.XmlNamespaceManager($feed.NameTable)
$ns.AddNamespace('sparkle', $SparkleNs)

$items = $feed.SelectNodes('/rss/channel/item')
if ($items.Count -ne 1) {
    Fail "expected exactly one item in the feed, found $($items.Count)"
}
$item = $items[0]

# Compared against `TrayVersion.Current`, which is three-part by construction.
# A four-field version here would mean the version read succeeded after all and
# `--file-version` was ignored, which it is silently.
$itemVersion = $item.SelectSingleNode('sparkle:version', $ns)
if ($null -eq $itemVersion -or $itemVersion.InnerText -ne $Version) {
    Fail "feed version is '$(if ($itemVersion) { $itemVersion.InnerText } else { 'absent' })', expected '$Version'"
}

$enclosure = $item.SelectSingleNode('enclosure')
if ($null -eq $enclosure) {
    Fail 'the feed item has no enclosure'
}

$expectedUrl = "$BaseUrl$($msiFile.Name)"
if ($enclosure.GetAttribute('url') -ne $expectedUrl) {
    Fail "enclosure points at $($enclosure.GetAttribute('url')), expected $expectedUrl"
}

if ([long]$enclosure.GetAttribute('length') -ne $msiFile.Length) {
    Fail "enclosure length is $($enclosure.GetAttribute('length')), expected $($msiFile.Length)"
}

$signature = $enclosure.GetAttribute('edSignature', $SparkleNs)
if ([string]::IsNullOrWhiteSpace($signature)) {
    Fail 'the enclosure carries no sparkle:edSignature'
}

# The two signatures are not equally exposed.  The generator checks the feed's
# own detached signature as it writes it, so a wrong private key shows up as
# the absent `.signature` the existence check above catches.  Nothing at
# generation time looks at the enclosure signature over the MSI bytes, which is
# written from the private key alone, so the enclosure call below is the only
# check it meets before the release ships.  Both checks read
# SPARKLE_PUBLIC_KEY, which is the same constant the tray compiles in.
#
# `--verify` reports its verdict on stdout and exits 0 either way, so the
# verdict is the only thing worth reading.
function Confirm-Signature([string]$path, [string]$signature, [string]$what) {
    $output = & $Generator --verify $path --signature $signature 2>&1
    if (($output -join "`n") -notmatch '(?m)^Signature valid$') {
        Write-Host ($output -join "`n")
        Fail "$what does not verify against SPARKLE_PUBLIC_KEY"
    }
}

Confirm-Signature $msiFile.FullName $signature 'the enclosure signature'
Confirm-Signature $feedPath $feedSignature 'the feed signature'

Write-Host "wrote $feedPath and $feedSignaturePath"
