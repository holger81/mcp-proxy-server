# mcp-proxy-server

FastAPI app that will aggregate MCP upstreams. Scaffold includes:

- `GET/POST /mcp` — placeholders (Streamable HTTP to be implemented)
- `GET /api/health` — liveness
- `GET /admin/` — static admin shell

## Run with Docker

The image starts as **root** only to `mkdir` **`/data/config`** and **`chown`** the **`/data`** volume to **`appuser` (uid 1000)**, then runs Uvicorn as that user. This avoids `PermissionError` on empty named volumes (e.g. Portainer).

```bash
docker build -t mcp-proxy .
docker run --rm -p 2222:8080 -v mcp-proxy-data:/data mcp-proxy
```

If you force **`docker run --user`** and the volume is not writable by that uid, startup may still fail—use the default entrypoint or pre-chown **`/data`** on the host.

Or with Compose:

```bash
docker compose up -d --build
```

Open http://localhost:2222/admin/ and http://localhost:2222/api/health .

On **Admin**, you can add **HTTP** (remote MCP URL) or **stdio** (command line) servers; they are saved to **`/data/config/servers.json`** on the container volume. JSON API: **`GET/POST /api/servers`**, **`DELETE /api/servers/{id}`**. For each server, **`GET /api/servers/{id}/inspect?kind=tools|resources|prompts|capabilities`** runs a short MCP session and returns **`list_tools`**, **`list_resources`**, **`list_prompts`**, or the **`initialize`** result. HTTP upstreams can use **`http_transport`: `streamable-http`** (default) or **`sse`** (legacy).

## Use with Portainer

1. Commit and push this repo to Git (e.g. GitHub).
2. In **Portainer** → **Stacks** → **Add stack** → **Repository** (or **Web editor**):
   - Point **Repository URL** at your repo and set the **Compose path** to `docker-compose.yml`, **or** paste the contents of `docker-compose.yml`.
3. **Deploy / update via build**, not a registry pull: **`mcp-proxy:latest`** is only a **local** tag after `docker compose build`. The stack sets **`pull_policy: never`** so Compose should not try to pull it from Docker Hub. In Portainer, avoid relying on **“Pull”** alone for this service—use **redeploy with rebuild** (or equivalent) so the image is built from the Dockerfile. The app is on host port **2222** (container **8080**). Data persists in the **`mcp-proxy-data`** volume.

If your Portainer environment cannot build from Git, build the image elsewhere, push to a registry, and replace `build: .` in the stack with `image: your-registry/mcp-proxy:tag` (and remove or change **`pull_policy`** as needed).

## Configuration (environment)

| Variable | Default | Meaning |
|----------|---------|---------|
| `MCP_PROXY_HOST` | `0.0.0.0` | Bind address |
| `MCP_PROXY_PORT` | `8080` | Port |
| `MCP_PROXY_DATA_DIR` | `/data` | Persisted config / venvs |
| `MCP_PROXY_STATIC_ROOT` | `/app/static` | Static files root (set in image) |

## Local development (optional)

Requires Python 3.12+ on the host:

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .
uvicorn mcp_proxy.app:app --reload --host 0.0.0.0 --port 8080
```

Prefer Docker if you want no local toolchain.
