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

On **Admin**, **Install stdio MCP** runs **`pip`** (PyPI) or **`npm install`** (npm) inside the container, detects a CLI binary, and **`POST /api/servers/register-stdio-package`** creates or updates the stdio entry in **`/data/config/servers.json`** (install roots: **`/data/venvs/<id>`**, **`/data/npm/<id>`**). **Add remote MCP (HTTP)** adds streamable or legacy SSE upstreams. Optional catalog overlays: mount **`/data/config/catalog_presets.json`** and use **`GET /api/catalog/presets`** (builtin list is empty; **`{DATA_DIR}`** in preset **`command`** / **`cwd`** expands to **`MCP_PROXY_DATA_DIR`**). API: **`GET/POST /api/servers`**, **`PUT /api/servers/{id}`**, **`DELETE /api/servers/{id}`**, **`POST /api/servers/register-stdio-package`**, **`GET /api/servers/{id}/inspect?kind=…`**. HTTP transport: **`streamable-http`** (default) or **`sse`**.

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
| `MCP_PROXY_DATA_DIR` | `/data` | Config, **`venvs/`** (PyPI), **`npm/`** (npm) |
| `MCP_PROXY_ALLOW_PYPI_INSTALL` | `true` | Allow pip in **register-stdio-package** (`pypi`). Set `false` if the admin UI is untrusted. |
| `MCP_PROXY_ALLOW_NPM_INSTALL` | `true` | Allow npm in **register-stdio-package** (`npm`). Requires Node in the image. |
| `MCP_PROXY_STATIC_ROOT` | `/app/static` | Static files root (set in image) |

## Local development (optional)

Requires Python 3.12+ on the host:

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .
uvicorn mcp_proxy.app:app --reload --host 0.0.0.0 --port 8080
```

Prefer Docker if you want no local toolchain.
