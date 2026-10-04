#!/usr/bin/env python3
"""End-to-end smoke test for the MCP proxy.

Boots the real proxy (uvicorn, temp data dir, auth off, installs off),
registers a stdio fixture upstream, then drives the full LLM path over MCP:
initialize -> tools/list -> searchToolsForDomain -> callTool (composite and
legacy spellings) -> cached response paging. Exits non-zero on any failure,
printing the proxy log tail. This is the regression gate for the audit
remediation PRs (see PLAN.md); it only asserts behavior that is correct on
main today.

Run: python scripts/smoke_test.py   (from any cwd; needs the repo installed)
Env: SMOKE_KEEP_LOGS=1 keeps the temp data dir and prints its path.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "scripts" / "smoke_fixture" / "echo_server.py"
META_TOOLS = {"searchToolsForDomain", "searchTool", "callTool", "htmlToPlainText"}
PROXY_READY_TIMEOUT_S = 30.0
GLOBAL_TIMEOUT_S = 120.0


class SmokeFailure(Exception):
    """Raised by check() on the first failed assertion."""


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"PASS: {name}")
        return
    print(f"FAIL: {name}" + (f" — {detail}" if detail else ""))
    raise SmokeFailure(name)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def text_of(result) -> str:
    return "".join(b.text for b in result.content if getattr(b, "type", None) == "text")


class Proxy:
    def __init__(self, port: int, data_dir: Path) -> None:
        self.port = port
        self.data_dir = data_dir
        self.log_path = data_dir / "proxy.log"
        self._proc: subprocess.Popen | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        env = {
            **os.environ,
            "MCP_PROXY_HOST": "127.0.0.1",
            "MCP_PROXY_PORT": str(self.port),
            "MCP_PROXY_DATA_DIR": str(self.data_dir),
            "MCP_PROXY_STATIC_ROOT": str(REPO / "static"),
            "MCP_PROXY_NEWS_DIGEST_REFRESH_ENABLED": "false",
            "MCP_PROXY_ALLOW_PYPI_INSTALL": "false",
            "MCP_PROXY_ALLOW_NPM_INSTALL": "false",
        }
        log = self.log_path.open("w", encoding="utf-8")
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "mcp_proxy.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )

    async def wait_ready(self) -> None:
        deadline = asyncio.get_running_loop().time() + PROXY_READY_TIMEOUT_S
        async with httpx.AsyncClient() as c:
            while True:
                if self._proc is not None and self._proc.poll() is not None:
                    raise SmokeFailure(f"proxy exited early with code {self._proc.returncode}")
                try:
                    r = await c.get(f"{self.base}/api/health", timeout=2.0)
                    if r.status_code == 200:
                        return
                except Exception:
                    pass
                if asyncio.get_running_loop().time() > deadline:
                    raise SmokeFailure("proxy did not become healthy in time")
                await asyncio.sleep(0.25)

    def stop(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass

    def log_tail(self, chars: int = 4000) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-chars:]
        except OSError as e:
            return f"<could not read proxy log: {e}>"


async def drive_mcp_session(proxy: Proxy) -> None:
    """All LLM-facing assertions, inside one MCP client session."""
    async with streamable_http_client(f"{proxy.base}/mcp") as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()

            tools = await s.list_tools()
            names = {t.name for t in tools.tools}
            check("tools/list contains meta-tools", META_TOOLS <= names, f"got {sorted(names)[:10]}")

            res = await s.call_tool(
                "searchToolsForDomain",
                {"domain": "default", "listAll": True, "limit": 100},
            )
            body = text_of(res)
            check("searchToolsForDomain finds fixture tool", "smoke_echo__echo" in body, body[:300])

            res = await s.call_tool(
                "callTool",
                {"toolName": "smoke_echo__echo", "arguments": {"text": "hi"}},
            )
            check("callTool composite spelling round-trips", text_of(res) == "echo:hi", text_of(res)[:200])

            res = await s.call_tool(
                "callTool",
                {"toolName": "smoke-echo/echo", "arguments": {"text": "legacy"}},
            )
            check("callTool legacy spelling round-trips", text_of(res) == "echo:legacy", text_of(res)[:200])

            # Cached paging: response longer than the page size (default 5000)
            # is sliced; continuation goes through pagination.responseCacheId.
            long_text = "".join(f"{i % 10}abcdefghij" for i in range(600))  # 6000 chars
            res = await s.call_tool(
                "callTool",
                {
                    "toolName": "smoke_echo__echo",
                    "arguments": {"text": long_text},
                    "responseLimit": 1000,
                },
            )
            page1_raw = text_of(res)
            try:
                page1 = json.loads(page1_raw)
            except json.JSONDecodeError:
                page1 = {}
            pag = page1.get("pagination") or {}
            cache_id = pag.get("responseCacheId")
            check(
                "paged response exposes pagination.responseCacheId + hasMore",
                bool(cache_id) and pag.get("hasMore") is True and len(page1.get("text", "")) == 1000,
                page1_raw[:300],
            )
            joined = page1.get("text", "")
            page, offset = page1, pag.get("returnedChars", 0)
            hops = 0
            while (page.get("pagination") or {}).get("hasMore") and hops < 20:
                res = await s.call_tool(
                    "callTool",
                    {
                        "toolName": "smoke_echo__echo",
                        "arguments": {},
                        "responseCacheId": cache_id,
                        "responseOffset": offset,
                        "responseLimit": 1000,
                    },
                )
                page = json.loads(text_of(res))
                joined += page.get("text", "")
                offset += (page.get("pagination") or {}).get("returnedChars", 0)
                hops += 1
            check(
                "cached paging reassembles full upstream text",
                joined == f"echo:{long_text}",
                f"joined {len(joined)} of {len(long_text) + 5} chars: {joined[:80]!r}",
            )


async def run() -> None:
    data_dir = Path(tempfile.mkdtemp(prefix="mcp-smoke-"))
    proxy = Proxy(_free_port(), data_dir)
    proxy.start()
    failed = False
    try:
        await proxy.wait_ready()
        print("PASS: health endpoint responds")

        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{proxy.base}/api/servers",
                json={
                    "id": "smoke-echo",
                    "type": "stdio",
                    "domain": "default",
                    "display_name": "Smoke Echo",
                    "command": [sys.executable, str(FIXTURE)],
                },
                timeout=10.0,
            )
        check("register stdio upstream", r.status_code == 201, f"{r.status_code} {r.text[:200]}")

        await drive_mcp_session(proxy)
    except BaseException:
        failed = True
        print("---- proxy log tail ----", file=sys.stderr)
        print(proxy.log_tail(), file=sys.stderr)
        raise
    finally:
        proxy.stop()
        # No orphaned stdio fixture children may outlive the proxy.
        await asyncio.sleep(1.0)
        try:
            stray = subprocess.run(["pgrep", "-f", str(FIXTURE)], capture_output=True, text=True)
            orphans = stray.stdout.split()
        except FileNotFoundError:  # no pgrep on this platform
            orphans = []
        if orphans:
            print(f"FAIL: no orphaned upstream fixture processes — pids: {orphans}")
            failed = True
        else:
            print("PASS: no orphaned upstream fixture processes")
        if os.environ.get("SMOKE_KEEP_LOGS") or failed:
            print(f"(proxy data dir kept at {data_dir})")
        else:
            shutil.rmtree(data_dir, ignore_errors=True)
    if failed:
        raise SmokeFailure("smoke assertions failed (see FAIL lines above)")


async def main() -> None:
    async with asyncio.timeout(GLOBAL_TIMEOUT_S):
        await run()
    print("SMOKE OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (SmokeFailure, TimeoutError) as e:
        print(f"SMOKE FAILED: {e}", file=sys.stderr)
        sys.exit(1)
