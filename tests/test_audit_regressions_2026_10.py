"""Regression tests for the AUDIT-2026-10-FINDINGS.md fixes (Phases A–C).

Each test pins one repaired behavior:

- SEC-1: the auth gate covers every ``/mcp/...`` sub-path, not just the exact
  ``/mcp`` spelling (``Mount("/mcp")`` serves sub-paths and the streamable-HTTP
  session manager ignores them — pre-fix, ``POST /mcp/anything`` was an
  unauthenticated hole).
- SEC-2/L-3: install children get a scrubbed allow-list environment, never
  ``os.environ``.
- SEC-3: the injected Node ``--inspect`` flag binds loopback unless
  ``MCP_PROXY_NODE_INSPECT_HOST`` opts into a wider address.
- M-2: Bearer ``GET /api/servers`` responses mask ``env``/``headers`` values,
  and PUT restores those sentinels from the stored record.
- L-1: login-failure streaks decay after ``streak_reset_s`` instead of
  escalating lockouts forever from stale failures.
- L-16: pagination cache entries are owner-bound; another identity gets a miss.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from mcp_proxy.api.servers import _mask_secrets_for_bearer, _restore_masked_fields
from mcp_proxy.cancellable_proc import build_child_env
from mcp_proxy.client_store import ClientTokenStore
from mcp_proxy.live_mcp_tracker import current_mcp_api_client_id
from mcp_proxy.mcp_live_tracker_middleware import McpLiveTrackerMiddleware
from mcp_proxy.models import UpstreamServer
from mcp_proxy.rate_limit import LoginRateLimiter
from mcp_proxy.security import AuthEnforcementMiddleware, is_mcp_http_path
from mcp_proxy.settings import Settings
from mcp_proxy.stdio_node_inspect import stdio_effective_command
from mcp_proxy.tool_response_cache import ToolResponseCache


# -- SEC-1: /mcp sub-paths are gated -----------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/mcp", True),
        ("/mcp/", True),
        ("/mcp/anything", True),
        ("/mcp/session/init", True),
        ("/mcp-admin", False),
        ("/api/mcp", False),
        ("/", False),
        ("", False),
    ],
)
def test_is_mcp_http_path(path: str, expected: bool) -> None:
    assert is_mcp_http_path(path) is expected


async def _echo_ok(scope, receive, send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": b'{"ok": true}'})


def _authed_app(store) -> FastAPI:
    settings = Settings(admin_password="pw", session_secret="s3cret-s3cret-1234")
    app = FastAPI()
    app.state.settings = settings
    app.state.client_store = store
    app.add_middleware(AuthEnforcementMiddleware, settings=settings)
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.get("/api/health")
    async def _health() -> dict:
        return {"ok": True}

    # Mirrors production: app.mount("/mcp", streamable-HTTP ASGI app), which
    # matches every sub-path.
    app.mount("/mcp", _echo_ok)
    return app


_JSON = {"Accept": "application/json"}


def test_anonymous_subpath_requests_are_401(tmp_path: Path) -> None:
    client = TestClient(_authed_app(ClientTokenStore(tmp_path)))
    for path in ("/mcp/anything", "/mcp/", "/mcp", "/mcp/session/init"):
        r = client.post(path, headers=_JSON)
        assert r.status_code == 401, path


def test_anonymous_get_json_accept_is_401_browser_redirects(tmp_path: Path) -> None:
    client = TestClient(_authed_app(ClientTokenStore(tmp_path)))
    assert (
        client.get("/mcp/anything", headers=_JSON).status_code == 401
    )  # JSON clients: clean 401
    r = client.get("/mcp/anything", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code in (302, 307)  # browsers: login redirect
    assert "/login" in r.headers["location"]


def test_valid_bearer_reaches_mcp_subpath(tmp_path: Path) -> None:
    store = ClientTokenStore(tmp_path)
    _rec, plain = store.create("ops")
    client = TestClient(_authed_app(store))
    r = client.post(
        "/mcp/deep/path", headers={"Authorization": f"Bearer {plain}", **_JSON}
    )
    assert r.status_code == 200


def test_health_stays_public(tmp_path: Path) -> None:
    client = TestClient(_authed_app(ClientTokenStore(tmp_path)))
    assert client.get("/api/health").status_code == 200


def test_auth_disabled_passes_subpaths_through(tmp_path: Path) -> None:
    app = FastAPI()
    app.state.settings = Settings()  # no password → auth_enabled False
    app.state.client_store = ClientTokenStore(tmp_path)
    app.add_middleware(AuthEnforcementMiddleware, settings=app.state.settings)
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.mount("/mcp", _echo_ok)
    client = TestClient(app)
    assert client.post("/mcp/anything", headers=_JSON).status_code == 200


class _Tracker:
    async def touch(self, **kwargs) -> None:  # pragma: no cover - unused
        pass


async def _echo_cid(scope, receive, send) -> None:
    body = json.dumps({"cid": current_mcp_api_client_id.get()}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def test_live_tracker_binds_client_on_subpath(tmp_path: Path) -> None:
    """Per-client policy needs the identity bound on sub-paths too."""
    store = ClientTokenStore(tmp_path)
    rec, plain = store.create("ops")
    mw = McpLiveTrackerMiddleware(_echo_cid, _Tracker(), store)
    client = TestClient(mw)
    r = client.get("/mcp/anything", headers={"Authorization": f"Bearer {plain}"})
    assert r.status_code == 200
    assert r.json()["cid"] == rec.id


# -- SEC-2 / L-3: scrubbed child environment -----------------------------------


def test_build_child_env_scrubs_proxy_secrets(monkeypatch) -> None:
    monkeypatch.setenv("MCP_PROXY_ADMIN_PASSWORD", "hunter2")
    monkeypatch.setenv("MCP_PROXY_SESSION_SECRET", "sess")
    monkeypatch.setenv("MCP_PROXY_ENFORCE_MCP_ADMIN_TOOLS", "1")
    monkeypatch.setenv("SOME_UPSTREAM_TOKEN", "tok")
    monkeypatch.setenv("HOME", "/home/app")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = build_child_env()
    for leaked in (
        "MCP_PROXY_ADMIN_PASSWORD",
        "MCP_PROXY_SESSION_SECRET",
        "MCP_PROXY_ENFORCE_MCP_ADMIN_TOOLS",
        "SOME_UPSTREAM_TOKEN",
    ):
        assert leaked not in env
    # what npm/pip genuinely needs survives
    assert env["HOME"] == "/home/app"
    assert env["PATH"] == "/usr/bin"


def test_build_child_env_has_usable_defaults(monkeypatch) -> None:
    for key in ("PATH", "TMPDIR"):
        monkeypatch.delenv(key, raising=False)
    env = build_child_env()
    assert env["PATH"] and env["TMPDIR"] == "/tmp"


# -- SEC-3: Node inspector binds loopback by default ---------------------------


def _inspect_server() -> UpstreamServer:
    return UpstreamServer(
        id="nodey", type="stdio", command=["node", "server.js"], stdio_node_inspect=True
    )


def test_inspect_flag_defaults_to_loopback(monkeypatch) -> None:
    monkeypatch.delenv("MCP_PROXY_NODE_INSPECT_HOST", raising=False)
    argv = stdio_effective_command(_inspect_server())
    assert argv[:2] == ["node", "--inspect=127.0.0.1:9229"]


def test_inspect_flag_honors_env_override(monkeypatch) -> None:
    monkeypatch.setenv("MCP_PROXY_NODE_INSPECT_HOST", "0.0.0.0")
    argv = stdio_effective_command(_inspect_server())
    assert argv[1] == "--inspect=0.0.0.0:9229"


def test_inspect_brk_uses_same_host(monkeypatch) -> None:
    monkeypatch.setenv("MCP_PROXY_NODE_INSPECT_HOST", "0.0.0.0")
    srv = _inspect_server()
    srv.stdio_node_inspect_brk = True
    argv = stdio_effective_command(srv)
    assert argv[1] == "--inspect-brk=0.0.0.0:9229"


# -- M-2: bearer masking + sentinel restore -------------------------------------


def _req(
    *,
    bearer: str | None = None,
    session_admin: bool = False,
    store=None,
) -> Request:
    headers = []
    if bearer:
        headers.append((b"authorization", f"Bearer {bearer}".encode()))
    scope = {
        "type": "http",
        "headers": headers,
        "session": {"admin": True} if session_admin else {},
        "app": SimpleNamespace(state=SimpleNamespace(server_store=store)),
    }
    return Request(scope)


_DUMPED = {
    "id": "srv",
    "type": "stdio",
    "env": {"TOKEN": "s3cret", "EMPTY": ""},
    "headers": {"Authorization": "Basic zzz"},
}


def test_bearer_get_masks_secret_maps() -> None:
    out = _mask_secrets_for_bearer(_req(bearer="t"), dict(_DUMPED))
    assert out["env"] == {"TOKEN": "********", "EMPTY": ""}
    assert out["headers"] == {"Authorization": "********"}


def test_session_and_authless_responses_keep_real_values() -> None:
    # Admin-UI cookie session keeps real values (its edit dialog needs them).
    out = _mask_secrets_for_bearer(_req(session_admin=True), dict(_DUMPED))
    assert out["env"]["TOKEN"] == "s3cret"
    # Auth disabled (no bearer presented) → untouched.
    out2 = _mask_secrets_for_bearer(_req(), dict(_DUMPED))
    assert out2["headers"]["Authorization"] == "Basic zzz"


class _FakeServerStore:
    def __init__(self, existing: UpstreamServer | None) -> None:
        self._existing = existing

    def get(self, server_id: str) -> UpstreamServer | None:
        return self._existing


def test_put_restores_masked_sentinels_from_store() -> None:
    # stdio servers carry env (the model validator clears headers); http
    # servers carry headers — exercise each field on its own transport.
    stored = UpstreamServer(
        id="srv", type="stdio", command=["x"], env={"TOKEN": "real"}
    )
    body = UpstreamServer(
        id="srv",
        type="stdio",
        command=["x"],
        env={"TOKEN": "********", "NEW": "plain"},
    )
    req = _req(bearer="t", store=_FakeServerStore(stored))
    merged = _restore_masked_fields(req, body)
    assert merged.env == {"TOKEN": "real", "NEW": "plain"}

    stored_h = UpstreamServer(
        id="srv",
        type="http",
        url="http://internal:1/mcp",
        headers={"H": "keep-me"},
    )
    body_h = UpstreamServer(
        id="srv",
        type="http",
        url="http://internal:1/mcp",
        headers={"H": "********"},
    )
    req_h = _req(bearer="t", store=_FakeServerStore(stored_h))
    merged_h = _restore_masked_fields(req_h, body_h)
    assert merged_h.headers == {"H": "keep-me"}


def test_put_restore_leaves_session_payload_alone() -> None:
    stored = UpstreamServer(
        id="srv", type="stdio", command=["x"], env={"TOKEN": "real"}
    )
    body = UpstreamServer(
        id="srv", type="stdio", command=["x"], env={"TOKEN": "********"}
    )
    req = _req(session_admin=True, store=_FakeServerStore(stored))
    merged = _restore_masked_fields(req, body)
    assert merged.env == {"TOKEN": "********"}  # session sends real values; no restore


def test_put_restore_unknown_server_is_passthrough() -> None:
    body = UpstreamServer(
        id="fresh", type="stdio", command=["x"], env={"TOKEN": "********"}
    )
    req = _req(bearer="t", store=_FakeServerStore(None))
    merged = _restore_masked_fields(req, body)
    assert merged.env == {"TOKEN": "********"}


# -- L-1: login streak decay -----------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def test_streak_decays_after_reset_window() -> None:
    clock = FakeClock()
    lim = LoginRateLimiter(
        clock=clock, max_attempts=3, lockout_base_s=10, lockout_max_s=1000
    )
    lim.record_failure("ip")
    lim.record_failure("ip")  # streak 2, below threshold
    clock.advance(901)  # past default streak_reset_s=900
    lim.record_failure("ip")
    lim.record_failure("ip")  # NEW streak → still 2, no lockout
    assert lim.hit("ip") is None

    # Third failure of the new streak locks out with the *base* penalty only;
    # pre-fix the streak was cumulative (5) and doubled it past lockout_base_s.
    lim.record_failure("ip")
    retry = lim.hit("ip")
    assert retry is not None and 0 < retry <= 10


def test_streak_within_window_still_escalates() -> None:
    clock = FakeClock()
    lim = LoginRateLimiter(
        clock=clock, max_attempts=3, lockout_base_s=10, lockout_max_s=1000
    )
    for _ in range(3):
        lim.record_failure("ip")
    retry = lim.hit("ip")
    assert retry is not None
    clock.advance(11)
    lim.record_failure("ip")  # streak 4 → 2× penalty escalation still applies
    assert lim.hit("ip") == pytest.approx(20)


# -- L-16: owner-bound pagination cache ------------------------------------------


def test_cache_entry_only_replays_for_its_owner() -> None:
    cache = ToolResponseCache()
    cache_id = cache.put("srv__tool", "private payload", owner="client-a")
    assert cache.get(cache_id, owner="client-a") is not None
    assert cache.get(cache_id, owner="client-b") is None
    assert cache.get(cache_id, owner=None) is None  # anonymous/session cannot replay


def test_unbound_entries_still_replay_for_anyone() -> None:
    cache = ToolResponseCache()
    cache_id = cache.put("srv__tool", "payload")  # owner defaults to None
    assert cache.get(cache_id, owner=None) is not None
    assert cache.get(cache_id, owner="client-a") is None
