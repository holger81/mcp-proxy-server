#!/bin/sh
# Download mail-mcp Linux amd64 release tarball from GitHub and install into /data/mail-mcp/.
# Usage: MAIL_MCP_VERSION=v0.4.5 ./install-mail-mcp-release.sh
# Run inside the container (appuser) or on host with DATA_ROOT=/path/to/volume.

set -e
VERSION="${MAIL_MCP_VERSION:?Set MAIL_MCP_VERSION e.g. v0.4.5}"
DEST="${MAIL_MCP_INSTALL_DEST:-/data/mail-mcp}"
TMP="${TMPDIR:-/tmp}/mail-mcp-install-$$"
ARCHIVE="mail-mcp-x86_64-unknown-linux-gnu.tar.xz"
URL="https://github.com/tecnologicachile/mail-mcp/releases/download/${VERSION}/${ARCHIVE}"

mkdir -p "$DEST" "$TMP"
trap 'rm -rf "$TMP"' EXIT

curl -fsSL "$URL" -o "$TMP/$ARCHIVE"
tar -xJf "$TMP/$ARCHIVE" -C "$TMP"
install -m 0755 "$TMP/mail-mcp-x86_64-unknown-linux-gnu/mail-mcp" "$DEST/mail-mcp"
echo "Installed mail-mcp ${VERSION} -> $DEST/mail-mcp"
