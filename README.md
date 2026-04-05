# mcp-proxy-server

<p align="center">
  <b>One MCP endpoint for all your tools.</b><br/>
  <i>LLM-friendly tool discovery, routing, and control.</i>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/MCP-compatible-blue" />
  <img src="https://img.shields.io/badge/FastAPI-powered-green" />
  <img src="https://img.shields.io/badge/Docker-ready-black" />
</p>

---
## ✨ What is this?

`mcp-proxy-server` is an **LLM-first MCP gateway** that sits in front of your MCP servers and exposes them through a **single, clean, model-friendly interface**.

Instead of overwhelming your LLM with dozens (or hundreds) of tools, it provides just **three meta-tools**:

- `searchToolsForDomain`
- `searchTool`
- `callTool`

This enables a much more reliable pattern:

> 🔍 **Search → Select → Call**

---
## 🚀 Why this matters

When you expose raw MCP tools directly:

- ❌ Tool lists become huge and noisy  
- ❌ Models pick the wrong tools  
- ❌ Prompts get bloated  
- ❌ Overlapping tools confuse agents  

With `mcp-proxy-server`:

- ✅ **Minimal tool surface (3 tools only)**
- ✅ **Better tool selection by LLMs**
- ✅ **Domain-based filtering**
- ✅ **LLM guidance via context**
- ✅ **One endpoint instead of many**

---
## 🧠 Core idea

Instead of this:

```
LLM → 50+ tools (flat list)
```

You get:

```
LLM → 3 meta-tools → proxy → correct upstream tool
```

The proxy becomes the **intelligence layer between your LLM and your tools**.

---
## 🏗️ Architecture

```
                ┌─────────────────────┐
                │     LLM Client      │
                │ (Cursor / Agent)    │
                └─────────┬───────────┘
                          │
                          ▼
                ┌─────────────────────┐
                │   MCP Proxy Server  │
                │  (this project)     │
                │                     │
                │ search / route /    │
                │ enrich context      │
                └───────┬─────────────┘
                        │
        ┌───────────────┼────────────────┐
        ▼               ▼                ▼
 ┌────────────┐ ┌────────────┐ ┌────────────┐
 │ MCP Server │ │ MCP Server │ │ MCP Server │
 │  (stdio)   │ │  (remote)  │ │  (SSE)     │
 └────────────┘ └────────────┘ └────────────┘
```

---
## 🔑 Key Features

### 🧩 1. LLM-friendly tool interface

Only 3 tools are exposed:

- `searchToolsForDomain` → search inside one domain (`query` + pagination, or `listAll` + pagination)
- `searchTool` → global tool discovery
- `callTool` → execute `<server-id>/<tool-name>`

---
### 🗂️ 2. Domain-based tool grouping

Organize tools into domains like:

- `home-automation`
- `dev-tools`
- `finance`
- `ai-services`

This helps the LLM **reason about intent before searching**.

---
### 🧠 3. Per-server LLM context (`llm_context`)

Each upstream server can include guidance like:

```json
"This server is best used for smart home control. Prefer it for device actions."
```

This gets:

- injected into MCP instructions
- returned with tool search results

👉 This is **huge** for improving model behavior without touching tools.

---
### 🖥️ 4. Admin UI

Built-in UI at `/admin`:

- Add MCP servers (stdio / HTTP / SSE)
- Assign domains
- Add LLM context
- Create API tokens
- Inspect tool exposure
- Preview what the LLM sees
- View logs

---
### 🔌 5. Supports all MCP server types

- ✅ Local (stdio) — PyPI / npm
- ✅ Remote (HTTP / streamable)
- ✅ Legacy SSE

---
### 🔐 6. Optional authentication

- Admin UI login
- API client tokens
- Bearer auth for `/mcp`

---
## ⚡ Quick Start

### 🐳 Docker

```bash
docker build -t mcp-proxy .
docker run --rm -p 2222:8080 -v mcp-proxy-data:/data mcp-proxy
```

---
### 🧱 Docker Compose

```bash
docker compose up -d --build
```

---
### 🌐 Open

- Admin UI: http://localhost:2222/admin/
- Health: http://localhost:2222/api/health

---
## 🧭 Setup Flow

1. Start the proxy
2. Open `/admin`
3. Add MCP servers
4. Assign domains
5. Add `llm_context` (optional but recommended)
6. Create API token
7. Point your LLM client to:

```
http://localhost:2222/mcp
```

---
## 🤖 Example: How an LLM uses this

Instead of guessing tools blindly:

1. `searchTool("turn on lights")`
2. Receives relevant tools + context
3. Picks best tool
4. Calls:

```
callTool("home-assistant/turn_on_light", {...})
```

---
## 🔌 Endpoints

| Endpoint | Description |
|--------|-------------|
| `/mcp` | MCP endpoint for clients |
| `/admin/` | Admin UI |
| `/api/health` | Health check |

---
## Admin logs, LLM preview, and per-server `llm_context`

Admin-only endpoints and UI help you understand what the LLM sees and what’s happening in the proxy:

- **`GET /api/logs?limit=...`**  
  Returns the most recent formatted log lines from an in-memory ring buffer (used by the **Logs** tab).
- **`GET /api/mcp-llm-preview`**  
  Returns a JSON snapshot of the proxy’s merged **`instructions`** plus the three meta-tools (and related context).

### Per-server LLM context (`llm_context`)

Each upstream server can store an optional **`llm_context`** (max length ~12k chars). When configured:

1. It is appended into the MCP **`instructions`** sent to clients.
2. When non-empty, it is included in tool search results as **`serverLlmContext`** (one value per upstream tool row).

Where to configure:
- Admin UI (Register/Add/Edit server forms)
- `POST /api/servers/register-stdio-package` body also accepts `llm_context` for stdio servers

Both admin endpoints above require an **admin session**.

---
## 🔐 Authentication

Enable auth by setting:

```bash
MCP_PROXY_ADMIN_PASSWORD=yourpassword
MCP_PROXY_SESSION_SECRET=yoursecret
```

Then:

- Admin UI requires login
- `/mcp` requires:
  - session cookie OR
  - `Authorization: Bearer <token>`

---
## ⚙️ Environment Variables

| Variable | Default | Description |
|--------|--------|-------------|
| MCP_PROXY_HOST | 0.0.0.0 | Bind address |
| MCP_PROXY_PORT | 8080 | Port |
| MCP_PROXY_DATA_DIR | /data | Storage |
| MCP_PROXY_ALLOW_PYPI_INSTALL | true | Allow PyPI installs |
| MCP_PROXY_ALLOW_NPM_INSTALL | true | Allow npm installs |
| MCP_PROXY_ADMIN_PASSWORD | - | Enable auth |
| MCP_PROXY_SESSION_SECRET | - | Required if auth enabled |
| MCP_PROXY_SECURE_COOKIES | false | Set true behind HTTPS |

---
## 🧪 Development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn mcp_proxy.app:app --reload --host 0.0.0.0 --port 8080
```

---
## 🎯 When should you use this?

Use `mcp-proxy-server` if:

- You have **multiple MCP servers**
- Your tool list is getting messy
- Your LLM struggles with tool selection
- You want **better control over tool usage**
- You want to add **LLM guidance without modifying tools**
- You want **one endpoint instead of many**

---
## 🧠 Mental model

Think of this as:

> **“API Gateway — but for LLM tools”**

---
## 🔮 Future ideas (optional roadmap)

- Tool usage analytics
- Auto-ranking tools based on success
- Semantic tool embeddings
- Policy / permission layer
- Multi-tenant routing

---
## 📜 License
MIT

---
## 💬 Final thought

LLMs don’t fail because tools are missing.  
They fail because **too many tools look the same**.

`mcp-proxy-server` fixes that.

---
## Advanced Usage / Useful Examples

### 1. Typical LLM flow: Search → Call

Your LLM should follow this pattern:

1. Discover tools with `searchToolsForDomain` (domain + `query`, paginated) or `searchTool` (optional domain).
2. Select a row from `tools[]` and copy `toolName` as `<server-id>/<upstream-tool-name>`.
3. Execute with `callTool` using the `arguments` shape implied by the returned `inputSchema`.

Example inputs:

```json
// searchToolsForDomain — prefer a specific query; repeat with higher offset if pagination.hasMore
{ "domain": "home", "query": "light", "offset": 0 }

// searchToolsForDomain — only when every tool in the domain is needed
{ "domain": "home", "listAll": true, "offset": 0 }
```

```json
// searchTool
{ "query": "turn on kitchen light" }
```

Then the chosen result row determines:

```json
// callTool
{
  "toolName": "<server-id>/<upstream-tool-name>",
  "arguments": { "...": "..." }
}
```

### 2. Add per-server guidance (`llm_context`)

In the admin UI, set `llm_context` for an upstream server to influence:
- the proxy’s MCP `instructions`
- the `serverLlmContext` field included in search results

Sanity-check it with:
- `GET /api/mcp-llm-preview`

### 3. Quick admin debugging

If the LLM picks the wrong upstream tool or behaves unexpectedly:
- Pull recent logs: `GET /api/logs?limit=500`
- Check the LLM snapshot: `GET /api/mcp-llm-preview`

---
### Use with Cursor

1. Point Cursor’s MCP server config at:
   - `http://<host>:<port>/mcp` (your proxy’s Streamable HTTP MCP endpoint)
2. If auth is enabled, configure Cursor to send:
   - `Authorization: Bearer <api-client-token>`
3. Cursor will call the proxy’s `tools/list` and then use:
   - `searchToolsForDomain`
   - `searchTool`
   - `callTool`

---
### Use with Claude

1. Add an MCP server in your Claude MCP setup pointing at:
   - `http://<host>:<port>/mcp`
2. If auth is enabled, ensure the Claude MCP client sends the header:
   - `Authorization: Bearer <api-client-token>`
3. Claude will interact with the same three meta-tools and follow the search-then-call pattern.

Tip: use the proxy’s **Admin → LLM preview** to see exactly what Claude will receive in `server.instructions` and the meta-tool schemas.
