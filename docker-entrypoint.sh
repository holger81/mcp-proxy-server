#!/bin/sh
set -e
# Fresh Docker volumes (and many Portainer mounts) are root-owned; the app runs as appuser (uid 1000).
# When started as root, fix ownership of /data then drop privileges.
if [ "$(id -u)" = "0" ]; then
  mkdir -p /data/config /data/mcp-news
  MCP_PROXY_DATA_DIR="${MCP_PROXY_DATA_DIR:-/data}"
  export MCP_PROXY_DATA_DIR
  export MCP_NEWS_DEFAULT_FEEDS="${MCP_NEWS_DEFAULT_FEEDS:-/app/mcp-news-default-feeds.yaml}"
  python3 /app/docker/seed_mcp_news.py
  chown -R appuser:appuser /data
  exec gosu appuser "$@"
fi
exec "$@"
