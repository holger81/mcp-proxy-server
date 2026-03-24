# mcp-proxy-server

FastAPI app that aggregates registered MCP upstreams:

- `GET/POST /mcp` — **Streamable HTTP** MCP endpoint. Clients only see three tools: **`searchToolsForDomain`**, **`searchTool`**, and **`callTool`**. Upstream tools are listed or invoked via those (composite name `<server-id>/<tool-name>`). Domains group servers; each server has a **domain** id configured in the admin UI. When auth is enabled, send `Authorization: Bearer <api-client-token>` (same as `/api/*`).
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

By default **no admin password** is configured: the admin UI does not ask for a password until you set **`MCP_PROXY_ADMIN_PASSWORD`** and **`MCP_PROXY_SESSION_SECRET`** (see [Authentication](#authentication) below).

On **Admin**, **Install stdio MCP** runs **`pip`** (PyPI) or **`npm install`** (npm) inside the container, detects a CLI binary, and **`POST /api/servers/register-stdio-package`** creates or updates the stdio entry in **`/data/config/servers.json`** (install roots: **`/data/venvs/<id>`**, **`/data/npm/<id>`**). **Add remote MCP (HTTP)** adds streamable or legacy SSE upstreams. Optional catalog overlays: mount **`/data/config/catalog_presets.json`** and use **`GET /api/catalog/presets`** (builtin list is empty; **`{DATA_DIR}`** in preset **`command`** / **`cwd`** expands to **`MCP_PROXY_DATA_DIR`**). API: **`GET/POST /api/servers`**, **`PUT /api/servers/{id}`**, **`DELETE /api/servers/{id}`**, **`POST /api/servers/register-stdio-package`**, **`GET /api/servers/{id}/inspect?kind=…`**. HTTP transport: **`streamable-http`** (default) or **`sse`**.

## Use with Portainer

Variables you type under **Environment** in a Portainer stack are **not** visible inside the container unless **`docker-compose.yml` maps them** into `services.*.environment` (this repo’s Compose file uses `${MCP_PROXY_ADMIN_PASSWORD:-}` etc.). After changing the stack file from Git, **redeploy** the stack so the container is recreated.

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
| `MCP_PROXY_ADMIN_PASSWORD` | *(empty)* | If set, enables auth: admin UI login + protected API |
| `MCP_PROXY_ADMIN_PASSWORD_FILE` | *(empty)* | If set, password is read from this file (overrides `MCP_PROXY_ADMIN_PASSWORD`). Use with Docker/Portainer **secrets**. |
| `MCP_PROXY_SESSION_SECRET` | *(empty)* | Required when admin password is set; use a long random string (≥16 chars) |
| `MCP_PROXY_SESSION_SECRET_FILE` | *(empty)* | If set, session secret is read from this file (overrides `MCP_PROXY_SESSION_SECRET`). |
| `MCP_PROXY_SECURE_COOKIES` | `false` | Set `true` when serving over HTTPS so session cookies are `Secure` |

Domains are stored in **`/data/config/domains.json`** (bootstrap includes **`default`**). Manage them in **Admin → Domains** (requires admin session when auth is on). Each server has a **`domain`** field in **`servers.json`**. API: **`GET/POST /api/domains`**, **`DELETE /api/domains/{id}`** (admin session only).

### Admin: logs, LLM preview, and per-server LLM notes

In **Admin → Logs**, the UI loads **`GET /api/logs?limit=…`** (default 500 lines, max 2000): recent formatted lines from an in-memory ring buffer (`mcp_proxy` and Uvicorn loggers). **Admin → LLM preview** calls **`GET /api/mcp-llm-preview`** and shows the JSON snapshot of merged **`instructions`**, the three meta-tools (names, descriptions, **`inputSchema`**), and short **`extras`** notes—i.e. what MCP exposes at the session tool layer (upstream tools still appear only via search).

Each server may include **`llm_context`** (persisted in **`servers.json`**, max 12k chars). That text is appended into the proxy’s MCP **`instructions`** (under per-server headings) and, when non-empty, duplicated on each row in **`searchToolsForDomain`** / **`searchTool`** results as **`serverLlmContext`**. Configure it in the admin **Register server** wizard, **Add remote MCP (HTTP)**, or **Edit server**. **`POST /api/servers/register-stdio-package`** accepts **`llm_context`** as well.

These two endpoints require an **admin session** (same as **API clients** and **Domains**), not only a bearer API token.

### Authentication

When a non-empty admin password is loaded (from **`MCP_PROXY_ADMIN_PASSWORD`** or **`MCP_PROXY_ADMIN_PASSWORD_FILE`**), a session secret of at least 16 characters must also be loaded (from **`MCP_PROXY_SESSION_SECRET`** or **`MCP_PROXY_SESSION_SECRET_FILE`**). On startup the process logs either **“Authentication is enabled”** or **“Authentication is disabled”** — if you expected a password but see “disabled”, the variables are not reaching the container (wrong service, typo, or secrets only mounted as files without `*_FILE`).

**Docker Compose secrets** usually appear as files under **`/run/secrets/...`**. Point the app at them, for example:

```yaml
environment:
  MCP_PROXY_ADMIN_PASSWORD_FILE: /run/secrets/mcp_admin_password
  MCP_PROXY_SESSION_SECRET_FILE: /run/secrets/mcp_session_secret
secrets:
  mcp_admin_password:
    file: ./secrets/admin_password.txt
  mcp_session_secret:
    file: ./secrets/session_secret.txt
```

When authentication is enabled:

- **`GET /api/health`** and **`/api/auth/*`** stay public.
- **Admin UI** (`/admin/`, except **`/admin/login.html`**) requires signing in with the admin password (session cookie).
- **`/mcp`** (Streamable HTTP for Cursor and other MCP clients) uses the same rule: admin session cookie **or** **`Authorization: Bearer <api-client-token>`**.
- **API** (`/api/servers`, catalog, inspect, install, etc.) accepts either that session **or** **`Authorization: Bearer <token>`** from an API client created in the admin **API clients** tab.
- **Client management** (`GET/POST/DELETE /api/clients`) is only available with an **admin session** (not with a client bearer token).
- OpenAPI **`/docs`** and **`/openapi.json`** require an admin session when auth is on.

Create tokens in **Admin → API clients**; each token is shown once. Revoking removes access immediately.

## Local development (optional)

Requires Python 3.12+ on the host:

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .
uvicorn mcp_proxy.app:app --reload --host 0.0.0.0 --port 8080
```

Prefer Docker if you want no local toolchain.
