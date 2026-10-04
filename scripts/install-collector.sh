#!/bin/sh
# INSTALL_DIR overrides the per-user destination; no root or shell-profile edits.
set -eu

fail() {
    printf '%s\n' "install-collector: $*" >&2
    exit 1
}

for command in uname curl tar shasum awk mkdir chmod mv rm; do
    command -v "$command" >/dev/null 2>&1 || fail "missing prerequisite: $command"
done

[ "$(uname -s)" = Darwin ] || fail "unsupported platform (macOS required)"
case "$(uname -m)" in
    arm64) arch=arm64 ;;
    x86_64|amd64) arch=amd64 ;;
    *) fail "unsupported architecture (arm64 or amd64 required)" ;;
esac

version=0.162.0
asset="otelcol-contrib_${version}_darwin_${arch}.tar.gz"
base="https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${version}"
destination=${INSTALL_DIR:-"${HOME:?HOME must be set}/.local/bin"}
[ -n "$destination" ] || fail "INSTALL_DIR must not be empty"
mkdir -p "$destination"
destination=$(cd "$destination" && pwd -P)

# Stage on the destination filesystem for atomic replacement. mkdir is exclusive.
staging="$destination/.otelcol-download.$$"
mkdir "$staging" || fail "cannot create private staging directory"
trap 'rm -rf "$staging"' 0
trap 'exit 1' HUP INT TERM
chmod 700 "$staging"

curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 30 --max-time 300 \
    "$base/$asset" -o "$staging/$asset" || fail "Collector download failed"
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 30 --max-time 300 \
   "$base/$asset.sha256" -o "$staging/checksums.txt" \
   || fail "checksum download failed"

expected=$(awk '
   NF != 1 { invalid=1 }
   { count++; digest=$1 }
   END { if (invalid || count != 1) exit 1; print digest }
' "$staging/checksums.txt") || fail "missing or ambiguous checksum for $asset"
case "$expected" in
    *[!0-9a-fA-F]*|'') fail "invalid checksum for $asset" ;;
esac
[ "${#expected}" -eq 64 ] || fail "invalid checksum for $asset"
actual=$(shasum -a 256 "$staging/$asset" | awk '{print $1}')
[ "$actual" = "$expected" ] || fail "checksum mismatch for $asset"

# Extract only the expected file, never arbitrary paths from the archive.
tar -xzf "$staging/$asset" -C "$staging" otelcol-contrib
[ -f "$staging/otelcol-contrib" ] && [ ! -L "$staging/otelcol-contrib" ] \
    || fail "archive does not contain a regular Collector binary"
chmod 755 "$staging/otelcol-contrib"
"$staging/otelcol-contrib" --version
[ ! -d "$destination/otelcol-contrib" ] || fail "destination is a directory"
mv -f "$staging/otelcol-contrib" "$destination/otelcol-contrib"
printf 'Installed Collector v%s at %s\n' "$version" "$destination/otelcol-contrib"
