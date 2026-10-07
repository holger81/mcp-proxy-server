"""PR 5.9: pip/npm installs must be cancellable; config edits must not clobber.

On main, installs ran in ``anyio.to_thread.run_sync`` with the default
``abandon_on_cancel=False``: a cancelled MCP tool call (client disconnect,
session idle timeout, shutdown) could not return until the whole install
finished — up to 15 minutes — and the pip/npm child ran on regardless. And
``setServerEnabled`` / ``upgradeStdioServer`` did ``get()`` → mutate →
``update()`` (full-record write), so a concurrent edit to a *different* field
could be silently rolled back (lost update).

Fixes: ``cancellable_proc.run_process`` (kills the child's process group when
a ``threading.Event`` fires), ``_install_in_thread`` (abandons the worker on
cancellation and signals the event), and ``ServerConfigStore.update_fields``
(atomic read-modify-write under the lock).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from types import SimpleNamespace

import anyio
from mcp import types as mcp_types

import mcp_proxy.proxy_mcp as pm
from mcp_proxy.config_store import ServerConfigStore
from mcp_proxy.models import UpstreamServer
from mcp_proxy.proxy_mcp import build_proxy_mcp_server
from mcp_proxy.settings import Settings
from mcp_proxy.tool_call_stats import ToolCallStatsStore

_SLEEPER = (
    "import os, sys, time\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "time.sleep(120)\n"
)
_PARENT_WITH_CHILD = (
    "import os, subprocess, sys, time\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "gp = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "open(sys.argv[2], 'w').write(str(gp.pid))\n"
    "time.sleep(120)\n"
)


def _pid_alive(pid: int) -> bool:
    """Live (not merely an unreaped zombie) check.

    A group-killed grandchild is orphaned to PID 1, and this container's PID 1
    (``opencode serve``) does not reap foreign orphans — the zombie would keep
    answering ``kill(pid, 0)``. State ``Z`` means the process is dead.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            state = fh.read().rsplit(b") ", 1)[1].split()[0]
    except (FileNotFoundError, IndexError):
        return False
    return state != b"Z"


async def _await_pidfile(path, timeout_s: float = 10.0) -> int:
    deadline = time.monotonic() + timeout_s
    while not path.is_file():
        if time.monotonic() > deadline:
            raise AssertionError("child never started")
        await anyio.sleep(0.02)
    return int(path.read_text())


async def _assert_dead(pid: int) -> None:
    deadline = time.monotonic() + 5.0
    while _pid_alive(pid) and time.monotonic() < deadline:
        await anyio.sleep(0.05)
    assert not _pid_alive(pid), f"pid {pid} survived"


# -- run_process -----------------------------------------------------------------


def test_run_process_without_event_behaves_like_subprocess_run():
    from mcp_proxy.cancellable_proc import run_process

    cp = run_process([sys.executable, "-c", "print('hi')"], timeout_s=30)
    assert cp.returncode == 0
    assert cp.stdout.strip() == "hi"


async def test_run_process_kills_child_on_cancel(tmp_path):
    from mcp_proxy.cancellable_proc import InstallCancelled, run_process

    pidfile = tmp_path / "child.pid"
    ev = threading.Event()
    outcome: list[str] = []

    def worker():
        try:
            run_process(
                [sys.executable, "-c", _SLEEPER, str(pidfile)],
                timeout_s=120,
                cancel_event=ev,
            )
        except InstallCancelled:
            outcome.append("cancelled")

    with anyio.fail_after(15):
        async with anyio.create_task_group() as tg:
            tg.start_soon(anyio.to_thread.run_sync, worker)
            pid = await _await_pidfile(pidfile)
            t0 = time.monotonic()
            ev.set()
    assert outcome == ["cancelled"]
    # On main (no mechanism) the child would sleep on for 120s and the worker
    # thread would never report "cancelled".
    assert time.monotonic() - t0 < 10
    await _assert_dead(pid)


async def test_run_process_kills_whole_process_group(tmp_path):
    from mcp_proxy.cancellable_proc import InstallCancelled, run_process

    pidfile = tmp_path / "p.pid"
    gpidfile = tmp_path / "g.pid"
    ev = threading.Event()
    outcome: list[str] = []

    def worker():
        try:
            run_process(
                [sys.executable, "-c", _PARENT_WITH_CHILD, str(pidfile), str(gpidfile)],
                timeout_s=120,
                cancel_event=ev,
            )
        except InstallCancelled:
            outcome.append("cancelled")

    with anyio.fail_after(15):
        async with anyio.create_task_group() as tg:
            tg.start_soon(anyio.to_thread.run_sync, worker)
            pid = await _await_pidfile(pidfile)
            gpid = await _await_pidfile(gpidfile)
            ev.set()
    assert outcome == ["cancelled"]
    await _assert_dead(pid)
    # Grandchild inherited the child's (new-session) process group, so the
    # group kill gets it too — a bare proc.terminate() would not.
    await _assert_dead(gpid)


# -- proxy-side plumbing ---------------------------------------------------------


async def test_install_in_thread_abandons_and_signals_event():
    seen = threading.Event()
    started = threading.Event()

    def slow_install(*_args, cancel_event=None):
        assert cancel_event is not None
        started.set()
        while not cancel_event.wait(0.02):
            pass
        seen.set()

    with anyio.fail_after(8):  # main: cancel deferred -> hangs to this guard
        async with anyio.create_task_group() as tg:

            async def drive():
                await pm._install_in_thread(slow_install, "a", "b", "c")

            tg.start_soon(drive)
            await anyio.sleep(0.2)
            assert started.is_set()
            t0 = time.monotonic()
            tg.cancel_scope.cancel()
        assert time.monotonic() - t0 < 3, "await ignored cancellation (not abandoned)"
    assert seen.wait(3), "worker never received the cancel event"


async def test_register_stdio_server_install_cancellable(tmp_path, monkeypatch):
    """Cancelling the *tool call* mid-install returns promptly and stops pip."""
    from mcp_proxy.cancellable_proc import InstallCancelled

    started = threading.Event()
    seen = threading.Event()

    def fake_install(data_dir, sid, spec, cancel_event=None):
        assert cancel_event is not None
        started.set()
        while not cancel_event.wait(0.02):
            pass
        seen.set()
        raise InstallCancelled("interrupted")  # abandoned thread's result discarded

    monkeypatch.setattr(pm, "install_into_venv", fake_install)

    store = ServerConfigStore(tmp_path)
    settings = Settings(data_dir=tmp_path)
    stats = ToolCallStatsStore(tmp_path, flush_interval_s=3600.0)
    domain_store = SimpleNamespace(
        list_records=lambda: [], id_set=lambda: {"default"}
    )
    srv = build_proxy_mcp_server(store, domain_store, settings, stats)
    handler = srv.request_handlers[mcp_types.CallToolRequest]

    async def drive():
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name="registerStdioServer",
                arguments={
                    "ecosystem": "pypi",
                    "serverId": "demo",
                    "domain": "default",
                    "package": "some-pkg",
                },
            ),
        )
        await handler(req)

    with anyio.fail_after(15):
        async with anyio.create_task_group() as tg:
            tg.start_soon(drive)
            deadline = time.monotonic() + 5
            while not started.is_set():
                if time.monotonic() > deadline:
                    raise AssertionError("install never started")
                await anyio.sleep(0.02)
            t0 = time.monotonic()
            tg.cancel_scope.cancel()
        assert time.monotonic() - t0 < 3, "tool call outlived the cancellation"
    assert seen.wait(3), "cancel event never reached the installer"
    assert store.get("demo") is None  # half-installed server not registered


# -- ServerConfigStore CAS ---------------------------------------------------------


def _seed(tmp_path) -> ServerConfigStore:
    store = ServerConfigStore(tmp_path)
    store.add(
        UpstreamServer(id="srv", domain="default", type="stdio", command=["run"])
    )
    return store


def test_update_fields_persists(tmp_path):
    store = _seed(tmp_path)
    out = store.update_fields(
        "srv", lambda s: s.model_copy(update={"enabled": False})
    )
    assert out.enabled is False
    assert store.get("srv").enabled is False


def test_update_fields_concurrent_edits_are_all_kept(tmp_path):
    store = _seed(tmp_path)
    barrier = threading.Barrier(3)

    def edit(update: dict):
        barrier.wait(timeout=10)
        store.update_fields("srv", lambda s: s.model_copy(update=update))

    threads = [
        threading.Thread(target=edit, args=({"enabled": False},)),
        threading.Thread(target=edit, args=({"llm_context": "ctx"},)),
        threading.Thread(target=edit, args=({"display_name": "D"},)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = store.get("srv")
    # Full-record get/update would let the last writer win over the others.
    assert final.enabled is False
    assert final.llm_context == "ctx"
    assert final.display_name == "D"


def test_update_fields_unknown_id_raises_keyerror(tmp_path):
    store = _seed(tmp_path)
    try:
        store.update_fields("nope", lambda s: s)
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")


def test_update_fields_rejects_id_change(tmp_path):
    store = _seed(tmp_path)
    try:
        store.update_fields(
            "srv", lambda s: s.model_copy(update={"id": "other"})
        )
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


async def test_set_server_enabled_tool_uses_cas(tmp_path):
    store = _seed(tmp_path)
    settings = Settings(data_dir=tmp_path)
    stats = ToolCallStatsStore(tmp_path, flush_interval_s=3600.0)
    domain_store = SimpleNamespace(list_records=lambda: [], id_set=lambda: {"default"})
    srv = build_proxy_mcp_server(store, domain_store, settings, stats)
    handler = srv.request_handlers[mcp_types.CallToolRequest]

    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(
            name="setServerEnabled",
            arguments={"serverId": "srv", "enabled": False},
        ),
    )
    result = (await handler(req)).root
    assert not result.isError, result.content
    assert store.get("srv").enabled is False

    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(
            name="setServerEnabled",
            arguments={"serverId": "ghost", "enabled": True},
        ),
    )
    result = (await handler(req)).root
    assert result.isError
    text = "".join(getattr(c, "text", "") for c in result.content)
    assert "Unknown server id" in text
