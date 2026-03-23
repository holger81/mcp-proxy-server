# mcp-proxy-server

FastAPI app that will aggregate MCP upstreams. Scaffold includes:

- `GET/POST /mcp` — placeholders (Streamable HTTP to be implemented)
- `GET /api/health` — liveness
- `GET /admin/` — static admin shell

## Run with Docker

```bash
docker build -t mcp-proxy .
docker run --rm -p 2222:8080 -v mcp-proxy-data:/data mcp-proxy
```

Or with Compose:

```bash
docker compose up -d --build
```

Open http://localhost:2222/admin/ and http://localhost:2222/api/health .

## Use with Portainer

1. Commit and push this repo to Git (e.g. GitHub).
2. In **Portainer** → **Stacks** → **Add stack** → **Repository** (or **Web editor**):
   - Point **Repository URL** at your repo and set the **Compose path** to `docker-compose.yml`, **or** paste the contents of `docker-compose.yml`.
3. Deploy. The stack publishes the app on host port **2222** (container still listens on **8080**). Data persists in the **`mcp-proxy-data`** Docker volume (config/venvs once you add those features).

If your Portainer environment cannot build from Git, build the image elsewhere, push to a registry, and replace `build: .` in the stack with `image: your-registry/mcp-proxy:tag`.

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
