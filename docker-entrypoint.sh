#!/bin/sh
set -e
# Fresh Docker volumes (and many Portainer mounts) are root-owned; the app runs as appuser (uid 1000).
# When started as root, fix ownership of /data then drop privileges.
if [ "$(id -u)" = "0" ]; then
  mkdir -p /data/config /data/mcp-news /data/mail-mcp /data/extra-ca
  # Private CA / server certs (bind-mount ./docker/extra-ca → /data/extra-ca in compose).
  # Installs into Debian trust store so mail-mcp / Rust TLS trusts your IMAP/SMTP endpoint.
  if [ -d /data/extra-ca ]; then
    found=0
    for f in /data/extra-ca/*; do
      [ -f "$f" ] || continue
      case "$f" in
        *.pem|*.crt) ;;
        *) continue ;;
      esac
      found=1
      bn=$(basename "$f")
      case "$bn" in
        *.pem) cp "$f" "/usr/local/share/ca-certificates/${bn%.pem}.crt" ;;
        *.crt) cp "$f" "/usr/local/share/ca-certificates/$bn" ;;
      esac
    done
    if [ "$found" = "1" ]; then
      update-ca-certificates >/dev/null
    fi
  fi
  MCP_PROXY_DATA_DIR="${MCP_PROXY_DATA_DIR:-/data}"
  export MCP_PROXY_DATA_DIR
  export MCP_NEWS_DEFAULT_FEEDS="${MCP_NEWS_DEFAULT_FEEDS:-/app/mcp-news-default-feeds.yaml}"
  python3 /app/docker/seed_mcp_news.py
  chown -R appuser:appuser /data
  exec gosu appuser "$@"
fi
exec "$@"
