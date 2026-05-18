#!/bin/sh
# Download portainer-mcp-enhanced Linux release from GitHub into /data/portainer-mcp/.
# Does not use api.github.com (avoids unauthenticated rate limits in Docker).
# Usage:
#   PORTAINER_MCP_VERSION=v0.8.0 ./install-portainer-mcp-release.sh
#   PORTAINER_MCP_VERSION=latest ./install-portainer-mcp-release.sh
# Run inside the container (as root or appuser with write access to DEST).

set -e
VERSION="${PORTAINER_MCP_VERSION:?Set PORTAINER_MCP_VERSION e.g. v0.8.0 or latest}"
DEST="${PORTAINER_MCP_INSTALL_DEST:-/data/portainer-mcp}"
TMP="${TMPDIR:-/tmp}/portainer-mcp-install-$$"
REPO="jmrplens/portainer-mcp-enhanced"

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64 | amd64) ARCH_SUFFIX="linux_amd64" ;;
  aarch64 | arm64) ARCH_SUFFIX="linux_arm64" ;;
  *)
    echo "portainer-mcp-enhanced: no Linux binary for machine ${ARCH} (upstream ships amd64/arm64)." >&2
    exit 1
    ;;
esac

mkdir -p "$DEST" "$TMP"
trap 'rm -rf "$TMP"' EXIT

if [ "$VERSION" = "latest" ]; then
  TAG_URL="$(curl -fsSI -o /dev/null -w '%{url_effective}' \
    "https://github.com/${REPO}/releases/latest" 2>/dev/null || true)"
  TAG="${TAG_URL##*/}"
  if [ -z "$TAG" ] || [ "$TAG" = "latest" ]; then
    echo "portainer-mcp-enhanced: could not resolve latest release tag from GitHub redirect." >&2
    echo "Use PORTAINER_MCP_VERSION=v0.8.0 (or another tag) instead." >&2
    exit 1
  fi
  echo "Resolved latest release tag: ${TAG}"
else
  case "$VERSION" in
    v*) TAG="$VERSION" ;;
    *) TAG="v${VERSION}" ;;
  esac
fi

VER="${TAG#v}"
ARCHIVE="portainer-mcp-enhanced_${VER}_${ARCH_SUFFIX}.tar.gz"
URL="https://github.com/${REPO}/releases/download/${TAG}/${ARCHIVE}"

echo "Downloading ${ARCHIVE} (${TAG})…"
if ! curl -fsSL "$URL" -o "$TMP/$ARCHIVE"; then
  echo "portainer-mcp-enhanced: download failed for ${URL}" >&2
  exit 1
fi

tar -xzf "$TMP/$ARCHIVE" -C "$TMP"
install -m 0755 "$TMP/portainer-mcp-enhanced" "$DEST/portainer-mcp-enhanced"
echo "Installed portainer-mcp-enhanced (${TAG}) -> ${DEST}/portainer-mcp-enhanced"
