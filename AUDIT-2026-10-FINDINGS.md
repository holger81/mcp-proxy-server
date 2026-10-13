# Security & Correctness Audit — October 2026 (fresh pass)

Scope: `src/mcp_proxy` (proxy core + support modules), `servers/mcp-news-server`, infra
(Dockerfile, compose, entrypoint, `.github/workflows`, `patches/`, static admin UI).
Method: static review only (no interpreter in the audit container); key findings
independently re-verified against source, SDK v1.24.0 behavior, and test coverage.
Baseline: prior audit ledger F1–F40, PRs #1–#44 (all merged/deployed).

Regression status: **all prior fixes VERIFIED-OK** (shielded stdio cleanup 5.1,
isError semantics 5.2b, CAS `update_fields` 5.9, mtime cache + deep-copy 6.1, tool-list
TTL cache 6.2, disabled-tool normalization 4.1, CSP/esc() UI 7.1/7.2, SSRF guard,
`limited_get` #42 break-after-read, cancellable_proc group-kill, bearer fail-closed
bind, tool-name round-trip #39, pagination `_proxy.*` namespacing) — **except** two
policy-regression slips: SEC-4 (error-class leak reappears in news digest) and the
PR-#42 deadline class still open on non-RSS news paths (M-6).

---

## CRITICAL

### SEC-1 — `/mcp` auth bypass via any sub-path (`/mcp/anything`)
`src/mcp_proxy/security.py:184` · `src/mcp_proxy/app.py:122–140, 259` ·
`src/mcp_proxy/mcp_live_tracker_middleware.py:35–36` · SDK `StreamableHTTPSessionManager`/`StreamableHTTPServerTransport` (v1.24.0)

`AuthEnforcementMiddleware` gates MCP only on an **exact** match:
```python
if path.rstrip("/") == "/mcp":   # security.py:184
```
but the session manager is mounted with `app.mount("/mcp", _McpStreamableMount(...))`
(app.py:259). Starlette `Mount` matches `/mcp` **and every `/mcp/...` prefix**;
`_McpStreamableMount.__call__` hands the scope to `session_manager.handle_request`
which is **path-agnostic**, and the SDK transport's `handle_request` validates only
method/Accept/Content-Type/session headers — never `scope["path"]`.

Exploit (auth enabled, zero credentials):
`POST /mcp/x` (e.g. `/mcp/messages`, `/mcp/;`, `/mcp/a/b`) with an `initialize`
JSON-RPC body → middleware falls through to `call_next` (security.py:223), a full
session is created, then `tools/list` + `tools/call` work on the same session with any
path suffix.

Blast radius:
- **All upstream proxy tools callable anonymously** — tool calls execute against
  upstream servers with the proxy's stored credentials.
- Per-client policy bypass: an unbound session gets
  `resolve_current_api_client() → None` → `merge_client_settings(base, None)` = full
  base settings and **no disabled-tool filtering** (proxy_mcp.py:78–82).
- With `enforce_mcp_admin_tools=false` (**current default & live value**,
  settings.py:64–65, compose:59): proxy **admin** meta-tools (`addServer`,
  `updateServer`, `removeServer`, logs…) also run — `_assert_mcp_admin_allowed`
  warn-only branch for unbound identity (proxy_mcp.py:162–176). Turning the flag on
  closes only the admin tools, **not** upstream tool access.
- Effectively voids PR 38 *and* its warn-telemetry: warn logs only fire for requests
  that already passed the exact-`/mcp` gate, which is why "0 warn hits in prod" was
  observed.
- Test coverage gap: `tests/` contains **zero** requests to `/mcp/…` sub-paths
  (grep: no matches). The live-tracker already treats `/mcp/*` as MCP
  (`_is_mcp_http_path`, middleware L35–36) — the auth gate was simply never aligned.

Fix:
```python
# security.py — replace the exact match
if path == "/mcp" or path.startswith("/mcp/"):
```
(prefer a shared helper imported by both `security.py` and the live tracker so the two
can't drift). Regression tests: unauthenticated `POST /mcp/`, `POST /mcp/messages`,
`GET /mcp/sse` → 401/redirect, mirroring the existing `/mcp` tests; plus one
authenticated round-trip on `/mcp/` to prove clients still route.

Deploy note: this is remotely exploitable today on any non-loopback deployment; fix
before (or together with) the pending `MCP_PROXY_ENFORCE_MCP_ADMIN_TOOLS=1` flip — the
flip alone does not mitigate it.

---

## HIGH

### SEC-2 — Install children inherit the full proxy environment (secret theft via package lifecycle scripts)
`src/mcp_proxy/cancellable_proc.py:60–83` · `npm_install.py:288–294, 440–498` · `pypi_venv.py:98–132`

`run_process` never passes `env=` → every `npm`/`pip`/`node`/`setup.py` child inherits
the proxy environment including `MCP_PROXY_ADMIN_PASSWORD`, `MCP_PROXY_SESSION_SECRET`,
and all other `MCP_PROXY_*` settings. `--ignore-scripts` is added **only for
GitHub/tarball specs** (npm_install.py:441–446); a plain registry install runs package
`preinstall`/`postinstall` scripts, and `npm run build` (L491–495) runs the fetched
repo's build script — all with the full env. Contrast: stdio *upstream* spawns go
through the SDK's `get_default_environment()` allowlist scrub; installs are the one
unscrubbed `Popen` surface.

Preconditions (mitigating): `MCP_PROXY_ALLOW_NPM_INSTALL` /
`MCP_PROXY_ALLOW_PYPI_INSTALL` default **false** (settings.py:51–52, commented in
compose). Where enabled, even a *legitimate admin* installing a compromised package
leaks the admin password + session secret to attacker code.

Fix:
1. In `run_process`, always pass a scrubbed env:
   `env = {**get_default_environment(), **{k: v for k, v in os.environ.items() if k.startswith(("npm_config_", "PIP_"))}}`.
2. `--ignore-scripts` unconditionally for the registry install (GitHub path already
   builds explicitly); document for packages needing postinstall binaries.
3. Ops: prefer `MCP_PROXY_*_FILE` secret variants so values never sit in env.

### SEC-3 — Node.js inspector port 9229 published to all interfaces by default
`docker-compose.yml:12–15` · `stdio_client` injects `--inspect=0.0.0.0:9229` (per-server checkbox, `stdio_node_inspect`)

Any admin toggling "stdio node inspect" for a debug session exposes the **unauthenticated
DevTools protocol on the Docker host's LAN** = arbitrary code execution inside the
container holding every API key, `mcp_proxy.db` (client token digests + config), and
mail credentials. README advises a dedicated override publishing `127.0.0.1:9229`
only — but the base compose already publishes it unconditionally, making the
mitigation moot. Secondary: port is fixed, so two inspected servers collide.

Fix: remove `- "9229:9229/tcp"` from `docker-compose.yml`; ship it only in a
`docker-compose.debug.yml` as `127.0.0.1:9229:9229`; bind `--inspect=127.0.0.1` inside
the container unless an explicit per-server override is set.

### SEC-4 — News: SSRF guard is add-time only; unattended refresher fetches stored URLs unguarded (DNS-rebind TOCTOU)
`servers/mcp-news-server/src/mcp_news_server/server.py:510` · `fetchers.py:156` · `http_util.py:110–124`

`assert_safe_public_url` runs when a feed is **added**; the periodic digest refresher
and `live_fetch` call `limited_get(client, feed_url)` with **no first-hop validation**
(`_limited_get_once` validates redirect hops only, http_util.py:122). A feed added as
`https://attacker.example/rss` (passes: public IP) can re-point to
`169.254.169.254`/`127.0.0.1`/`10.x` (TTL 0) and then receive **unattended** requests
every refresh cycle from inside the network — persistent blind SSRF with partial
exfil (responses are parsed and titles/links surface in digest items). Out-of-band
edits of `feeds.yaml` are likewise never validated.

Fix: re-validate at fetch time — `await asyncio.to_thread(assert_safe_public_url, feed_url)`
at the top of `fetch_rss_via_http`/`fetch_page_metadata` (pairs with M-5); stronger:
pin the validated IP in a custom `httpx` transport (connect to checked IP, Host/SNI
from hostname) to close the resolve-then-connect race.

### SEC-5 — News: raw exception strings leak internal URLs in-band from digest local-SearXNG path
`servers/mcp-news-server/src/mcp_news_server/digest_cache.py:240–246`

```python
except Exception as e:
    errors.append({"source": f"searx:{q}", "error": str(e) or type(e).__name__})
```
Everything else honors the error-**class**-only policy (`classify_error` in
`fetchers.py:52`, `server.py:583/726/751`; `_brief_llm_error` in `llm_curator.py`).
An `httpx.HTTPStatusError` string embeds the internal SearXNG **hostname, port and
query**; it is persisted into the cached payload (`_finalize_payload`) and served in
`news_local` / cached `news_curate` responses to any MCP client. This is exactly the
surface the prior error-surface fix removed elsewhere → **policy regression**.
Untested (`test_error_surfaces.py` covers gather/curator only).

Fix: `"error": classify_error(e)` + mirror details to `log.warning`; add a test for
this path.

---

## MEDIUM

### M-1 — Schema-aware pagination peel is dead at runtime (hot-tool double resolution)
`src/mcp_proxy/proxy_mcp.py:1363–1398` · `tool_response_pagination.py`

The outer `call_tool` wrapper resolves the *original* tool, computes `declared` via
`_as_hot_call_tool_args`, discards it (`_declared`), and rewrites the request to
`name="callTool"`. `_call_tool_impl` re-runs `_as_hot_call_tool_args`, which then can
only match the hot key `callTool`, so `parse_call_tool_pagination` always receives
`declared_params=frozenset()` → the "don't peel params the upstream tool itself
declares" protection (PR 5.5) never applies on either the hot or the direct-`callTool`
path: an upstream tool with its own `responseOffset`/`responseCacheId` gets those
arguments peeled twice. `tests/test_pagination_collisions.py` only unit-tests
`peel_pagination_params`/`wrap_hot_tool_as_call_tool` directly — the end-to-end hole is
untested. Clients keep working via the namespaced `_proxy.*` spellings (always
protected). Fix: compute `declared` once from the original tool name and pass it into
`_call_tool_impl` (or stash it on the rewritten request); add an e2e test with an
upstream tool that declares `responseOffset`.

### M-2 — REST `/api/servers` round-trips full `env`/`headers` secrets
`src/mcp_proxy/api/servers.py:196–210, 377–384, 487–493` (raw `server.model_dump()`)

Any `can_admin` bearer (and the browser) receives complete upstream env maps incl.
API keys/passwords; secrets flow into LLM transcripts when an agent calls the admin
API, and through browser history/XSS surface. The MCP `listServers` path deliberately
omits env — inconsistent. Fix: mask values in non-interactive responses
(`"********"` sentinel + "unchanged on PUT" semantics).

### M-3 — REST `upgrade-stdio-package` read-modify-write race (CAS fix not applied to REST route)
`src/mcp_proxy/api/servers.py:406–473`

`store.get` → minutes-long install → mutate `server.command` → `store.update` writes
back a **stale snapshot**, clobbering concurrent edits — the exact race PR 5.9's CAS
`update_fields` fixed on the MCP `upgradeStdioServer` path. Fix: use
`store.update_fields` with expected old values for `command`/package meta.

### M-6 — News: caller-chosen URLs have no overall deadline (slow-drip handler stall)
`server.py:570–586` (`news_ingest_urls`), `737–758` (`extra_urls`), `554/714`
(searx), `llm_curator.py:269`

Per-feed `asyncio.wait_for` exists only on the RSS gather (`fetchers.py:41–48`).
A server trickling bytes under the per-operation timeout (or 20 redirect hops each
trickling) pins a tool handler open for minutes — same failure class that stalled
digest refreshes ~90 min in prod (PR #42 docstring, `http_util.py:33–38`).
Fix: wrap each fetch body in `asyncio.wait_for(..., feed_deadline_s())` or apply the
deadline inside `limited_get`.

### M-5 — News: blocking DNS + file IO on the event loop
`url_guard.py:101–105` (`socket.getaddrinfo`) called directly from async handlers
(`server.py:510, 545, 574, 668, 741`) and per redirect hop (`http_util.py:122`);
sync YAML store IO from async paths (`store.py:45/85/130–134` ← `server.py:498,
515–516, 675`, `digest_cache.py:186–195, 119–126`). A hostile `news_ingest_urls` batch
(40 URLs × 21 hops of DNS) stalls the whole stdio server. Fix:
`await asyncio.to_thread(...)` at these call sites (pattern already used at
`fetchers.py:157`).

### M-7 — `UpstreamToolListCache.get()` returns the internal list
`src/mcp_proxy/upstream_tool_cache.py:69` (vs. `put` at L76 which copies)

Callers mutate the returned list at their risk; any in-place sort/filter in
`proxy_mcp.py` consumers would silently corrupt the shared cache until TTL. Current
callers verified to only build fresh lists → latent. Fix: `return list(entry[2])`.

### M-8 — `MCP_PROXY_ENFORCE_MCP_ADMIN_TOOLS` still defaults false (pending flip is now urgent)
`settings.py:64–65` · `docker-compose.yml:59` · `proxy_mcp.py:142–189`

Documented warn-first rollout; PLAN.md records 0 warn hits and the remaining Portainer
stack-env step. Note: (a) per-client `can_admin` grants exist (PLAN.md:408), so
bearer-only deployments *can* flip safely — the field exists via
`PATCH /api/clients/{id}`; (b) the flip does **not** mitigate SEC-1; (c) consider
flipping the *default* to true in code so fresh installs are safe (opt-out with an
entrypoint warning).

### M-9 — Release-binary installs without checksum/signature; supply-chain looseness
`Dockerfile:46–61` · `docker/install-mail-mcp-release.sh:22–24` (also accepts
`latest`) · `Dockerfile:35–36` (`curl | bash` NodeSource) · caret-unpinned deps ·
mutable base image tag. A rotated/compromised GitHub release asset installs straight
into the secrets container. Fix: committed `MAIL_MCP_SHA256`/`PORTAINER_MCP_SHA256`
checked post-download; hashed lockfile (`uv lock` / `pip-compile --generate-hashes`);
pin base image by digest.

### M-10 — Most-privileged workflow pins a third-party action by mutable tag; deploys as root
`.github/workflows/deploy-ssh.yml:29` (`appleboy/ssh-action@v1.2.0`) · `:35–36`
(`ssh_user` default `root`). A moved tag runs attacker code on the runner **with**
`GH_PAT` + prod SSH access; root deploy = instant host takeover. Fix: pin full commit
SHA (Dependabot keeps it fresh); default to an unprivileged docker-group user.
Related: none of the 4 workflows declares `permissions:` (all need ≤ `contents: read`
+ ghcr push) → add least-privilege blocks (LOW but one-liner).

### M-11 — Admin-UI XSS guard tests have exploitable blind spots
`tests/test_admin_ui_escaping.py:23–58` (current code verified clean; the *guard* is weak)

1. Whole template literals are masked including interpolations → a future
   `innerHTML = \`<div>${server.name}</div>\`` refactor passes.
2. `innerHTML\s*=` misses `innerHTML +=`.
3. One `esc(` anywhere launders the statement (`esc(a) + userControlledUrl` passes).
4. No coverage of `insertAdjacentHTML`/`outerHTML`/`srcdoc`/`setAttribute(href|src)`/
   `javascript:`; hardcoded file list → new files silently unscanned.
Fix: glob `static/admin/`; match `(\+=|=)`; require every `${expr}` in a scanned
template to be an `esc()`/constant; extend sink list.

### M-12 — CORS `allow_origins=["*"]`
`src/mcp_proxy/app.py:244–253`. Harmless with credentials disabled, but in the
supported auth-disabled mode it lets any website's browser reconfigure servers
(= command execution). Fix: default empty origin list; require explicit opt-in for
`*`.

---

## LOW

- **L-1** `rate_limit.py:70–81`: `_streaks` never pruned/decayed (only `_attempts`/
  `_lockouts` are) → attacker-writable unbounded state + one old failure keeps doubling
  lockouts. Store `(streak, ts)`, decay, prune.
- **L-2** `upstream_inspect.py:216–236`: unbounded stderr capture during inspect
  (memory balloon from chatty upstream). Cap at ~64 KiB with truncation marker.
- **L-3** `cancellable_proc.py:70–76`: docstring claims "tree killed" on timeout, but
  the `cancel_event=None` path (used by the admin-UI install routes, `api/servers.py`)
  uses plain `subprocess.run` → only `npm`/`pip` dies, `node`/compiler grandchildren
  orphan. Drop the shortcut; always take the `Popen`+group-kill branch.
- **L-4** `npm_install.py:83–109`: `_package_lock_installed_dir` trusts lock-file keys
  (`target / matches[0]`) without containment check; a poisoned `package-lock.json`
  redirects follow-up installs outside the prefix. Add `relative_to(root)` + require
  `node_modules/` prefix (siblings at L419–422 / `pypi_venv.py:159–163` already do).
- **L-5** `client_store.py:202–203`: `update()` assigns `disabled_tools` bypassing the
  `normalize_disabled_tools` validator → non-canonical spellings returned to caller &
  briefly on disk (self-heals on next read). Set `validate_assignment=True` or call
  the validator.
- **L-6** `html_plain_text.py:113–121`: double entity decode (`convert_charrefs=True`
  + `html.unescape`) → forged markup in text previews; parser input unbounded
  (`max_chars` applied post-parse). Drop the second unescape; pre-truncate feed input.
- **L-7** `live_streamable_http_session_manager.py:228–241`: DELETE-terminated
  transports stay in `_server_instances` until shutdown (matches SDK v1.24.0 exactly —
  upstream behavior, not fork regression). Pop locally after `handle_request` when
  `transport.is_terminated`.
- **L-8** News `server.py:656–659`: `searx_queries` fan-out uncapped (only `extra_urls`
  capped at 40) → self-DoS against the operator's SearXNG. Cap like `extra_urls`.
- **L-9** News `url_guard.py:103`: malformed port raises bare `ValueError` (handlers
  catch only `UrlBlockedError`) → unclassified error text in-band. Wrap into
  `UrlBlockedError`.
- **L-10** News `fetchers.py:226`: `r.json()` on SearXNG responses without
  `JSONDecodeError` handling → SDK-echoed parse errors instead of `invalid_json`.
- **L-11** News `server.py:512, 547, 670`: guard rejections echo
  "does not resolve" vs "blocked internal address" → internal-DNS oracle for MCP
  clients. Generic in-band message; details to log.
- **L-12** News `default_feeds.yaml` is packaged (`pyproject.toml:31`) but never
  loaded — fresh installs don't get documented defaults (seeding is `feeds: []` +
  migrations). Seed via `importlib.resources` or delete + fix docstring.
- **L-13** News `llm_curator.py:22–25`: control-char sanitizer misses Unicode bidi
  isolates U+2066–U+2069 (Trojan-Source in item text/prompt). Extend `_CONTROL_RE`.
- **L-14** `patches/` mail-mcp patches are orphaned (referenced nowhere) and mutually
  conflicting (overlapping hunks, same base `17be138`). Delete or document precisely.
- **L-15** `docker-compose.yml:17` comment promises a healthcheck; none exists. Add
  `GET /api/health` check.
- **L-16** `tool_response_cache.py`: pagination cache ids are
  `secrets.token_urlsafe(12)` (unguessable) but not bound to client identity — a
  client that learns another's `responseCacheId` can page through its result. Bind
  cache entries to `client_id`/session.

## INFO (selected)

- Session cookie: HttpOnly + SameSite=Lax; **Secure only with `MCP_PROXY_SECURE_COOKIES=true`**
  (`settings.py:33`) → on plain-HTTP LAN the cookie travels clear; pair TLS with the flag.
  No CSRF tokens; defense = SameSite=Lax + all mutations on unsafe verbs (verified
  verb-by-verb); residual same-site localhost POST risk.
- Literal `"dev-no-auth-session-key-do-not-use"` when auth disabled (`app.py:221–225`)
  — harmless today (cookie never required), risky only if session claims are ever trusted.
- mail-mcp child runs as **root** (README:302–313); `docker-entrypoint.sh:29` chowns
  all of `/data` each start.
- `login` rate limiter keys peer IP (spoof-proof) — behind a reverse proxy without
  forwarded-header handling, all users share one bucket → collateral lockout.
  Deploy-time concern.
- News store: fixed `.tmp` name, no `fsync`, cross-process writers race on tmp
  (single-process design); `save()` without `load()` would wipe `migrations_applied`
  (all in-tree callers load first); refresh interval snapshotted at startup;
  `news_list_feeds` echoes `dataDir`; digest refresh lock can make a forced refresh
  wait ~2×(feed+LLM deadlines); digest refresh loop has no per-cycle timeout.
- `upstream_inspect.py:28`: module-level `mcp.os.posix` import breaks Windows
  portability; fallback JSON-RPC POST uses the whole 60 s budget
  (`upstream_inspect.py:376, 542–556`) — give it `min(15, timeout/3)`.
- `stdio_node_inspect.py:30` — fixed single port collides across two inspected
  servers; `0.0.0.0` bind (covered by SEC-3 fix).
- `admin_security_headers.py`: CSP `style-src 'unsafe-inline'` (CSS injection only);
  no HSTS/XFO (fine behind TLS-terminating proxy).
- `tool_response_pagination.py:179/199`: `int()` laxness (`"1_0"`, `1.9`, `True`);
  negatives still rejected → display oddity only.
- Stores: single fixed `.tmp` + no flock — fine for documented single-process model.
- `proxy_mcp.py` stale docstring in `apply_upstream_error_semantics` (says default
  off; 5.2b flipped it on). `_warned_mcp_admin_calls` grows with distinct client ids
  (bounded by client count; harmless).
- `live_mcp_tracker.latest_active_session_id` diagnostic fallback can mis-attribute a
  tool call to the wrong recent session (display-only).
- `news_digest_refresher`: no per-cycle timeout around refresh (hung fetch stalls cycle
  until stop()).

---

## Recommended order of action

1. **SEC-1** `/mcp` sub-path auth bypass — one-line gate fix + sub-path regression
   tests (ships today; remotely exploitable, no credentials).
2. **SEC-3** remove `9229` from base compose (one line) and **SEC-2** scrubbed install
   env (fail-closed defaults already half-mitigate SEC-2).
3. **M-8** complete the pending `MCP_PROXY_ENFORCE_MCP_ADMIN_TOOLS=1` Portainer flip —
   meaningful only *after* SEC-1 lands.
4. News server batch: **SEC-4/SEC-5** (fetch-time guard + `classify_error`), then
   M-5/M-6 (to_thread + deadlines), then L-8…L-13.
5. Proxy mediums: M-1 (peel plumbing + e2e test), M-2 (env masking), M-3 (REST CAS),
   M-7 (cache copy).
6. CI/supply chain: M-9/M-10 (+ workflow `permissions:`), M-11 guard hardening.

