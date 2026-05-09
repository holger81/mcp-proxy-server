#!/bin/sh
# Prefer operator-installed binary on /data (survives image upgrades); fallback to image-baked copy.
set -e
DATA_BIN="${MAIL_MCP_DATA_BIN:-/data/mail-mcp/mail-mcp}"
IMAGE_BIN="${MAIL_MCP_IMAGE_BIN:-/opt/mail-mcp/mail-mcp}"
if [ -x "$DATA_BIN" ]; then
  exec "$DATA_BIN" "$@"
fi
if [ -x "$IMAGE_BIN" ]; then
  exec "$IMAGE_BIN" "$@"
fi
echo "mail-mcp: no executable at $DATA_BIN or $IMAGE_BIN" >&2
exit 127
