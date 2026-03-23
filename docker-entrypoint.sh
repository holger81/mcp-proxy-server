#!/bin/sh
set -e
# Fresh Docker volumes (and many Portainer mounts) are root-owned; the app runs as appuser (uid 1000).
# When started as root, fix ownership of /data then drop privileges.
if [ "$(id -u)" = "0" ]; then
  mkdir -p /data/config
  chown -R appuser:appuser /data
  exec gosu appuser "$@"
fi
exec "$@"
