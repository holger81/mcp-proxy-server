#!/bin/sh
# Download portainer-mcp-enhanced Linux release from GitHub into /data/portainer-mcp/.
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
export PORTAINER_MCP_ARCH_SUFFIX="$ARCH_SUFFIX"

mkdir -p "$DEST" "$TMP"
trap 'rm -rf "$TMP"' EXIT
export TMP DEST

python3 <<'PY'
import json
import os
import shutil
import sys
import urllib.error
import urllib.request

version = os.environ["PORTAINER_MCP_VERSION"].strip()
arch_suffix = os.environ["PORTAINER_MCP_ARCH_SUFFIX"]
repo = os.environ.get("PORTAINER_MCP_REPO", "jmrplens/portainer-mcp-enhanced")
tmp = os.environ["TMP"]
dest = os.environ["DEST"]

if version == "latest":
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
else:
    api_url = f"https://api.github.com/repos/{repo}/releases/tags/{version}"

req = urllib.request.Request(
    api_url,
    headers={"Accept": "application/vnd.github+json", "User-Agent": "mcp-proxy-install"},
)
try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        release = json.load(resp)
except urllib.error.HTTPError as e:
    print(f"GitHub API error: {e.code} {e.reason} for {api_url}", file=sys.stderr)
    sys.exit(1)

tag = release.get("tag_name") or version
assets = release.get("assets") or []
needle = f"_{arch_suffix}.tar.gz"
match = None
for a in assets:
    name = a.get("name") or ""
    if name.endswith(needle) and "portainer-mcp-enhanced" in name:
        match = a
        break
if not match:
    names = ", ".join(sorted(a.get("name", "") for a in assets))
    print(
        f"No asset matching *{needle} in release {tag}. Assets: {names}",
        file=sys.stderr,
    )
    sys.exit(1)

url = match["browser_download_url"]
archive_name = match["name"]
archive_path = os.path.join(tmp, archive_name)

print(f"Downloading {archive_name} ({tag})…")
with urllib.request.urlopen(url, timeout=300) as resp:
    data = resp.read()
with open(archive_path, "wb") as f:
    f.write(data)

import tarfile

with tarfile.open(archive_path, "r:gz") as tf:
    tf.extractall(tmp, filter="data")

bin_name = "portainer-mcp-enhanced"
src = os.path.join(tmp, bin_name)
if not os.path.isfile(src):
    print(f"Expected binary {bin_name} in archive", file=sys.stderr)
    sys.exit(1)

out = os.path.join(dest, bin_name)
# copy2 (not rename from /tmp): /data is often a Docker volume on another filesystem.
shutil.copy2(src, out)
os.chmod(out, 0o755)
print(f"Installed portainer-mcp-enhanced ({tag}) -> {out}")
PY
