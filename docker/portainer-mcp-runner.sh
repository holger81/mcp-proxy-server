#!/bin/sh
# Prefer volume-installed binary; optional image-baked fallback under /opt/portainer-mcp/.
set -e
DATA_BIN="${PORTAINER_MCP_DATA_BIN:-/data/portainer-mcp/portainer-mcp-enhanced}"
IMAGE_BIN="${PORTAINER_MCP_IMAGE_BIN:-/opt/portainer-mcp/portainer-mcp-enhanced}"
if [ -x "$DATA_BIN" ]; then
  exec "$DATA_BIN" "$@"
fi
if [ -x "$IMAGE_BIN" ]; then
  exec "$IMAGE_BIN" "$@"
fi
echo "portainer-mcp-enhanced: no executable at $DATA_BIN or $IMAGE_BIN" >&2
echo "Install: Admin → Explore servers → Update portainer-mcp binary, or POST /api/portainer-mcp/update" >&2
exit 1
