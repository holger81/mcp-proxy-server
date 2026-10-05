"""PR 5.1: timeout during a stdio upstream must not leak the child process tree.

The stdio transport is spawned with ``start_new_session=True`` so a hung tree
can be SIGTERM'd wholesale via ``terminate_posix_process_tree``. But that
cleanup lives in a ``finally`` that routinely runs inside the *already
cancelled* ``upstream_timeout_s`` scope: without a shield every await in it
re-raises Cancelled, the tree termination is skipped, and the grandchildren
of the direct child (which anyio's teardown SIGKILLs) sleep forever.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import anyio
import pytest
from mcp.client.stdio import StdioServerParameters

from mcp_proxy.upstream_inspect import _stdio_client_piped_stderr_capture

GRANDCHILD = (
    "import os, sys, time\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "time.sleep(3600)\n"
)

CHILD = (
    "import os, subprocess, sys, time\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "subprocess.Popen([sys.executable, sys.argv[3], sys.argv[2]])\n"
    "time.sleep(3600)\n"
)


def _process_gone(pid: int, direct_child: bool) -> bool:
    """True once `pid` is dead (or a zombie we reaped/can never affect again)."""
    if direct_child:
        try:
            reaped, _ = os.waitpid(pid, os.WNOHANG)
            if reaped == pid:
                return True
        except ChildProcessError:
            return True  # already reaped by the transport
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    # Alive (or zombie we cannot reap, e.g. reparented grandchild): check state.
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            state = fh.read().rsplit(b") ", 1)[1].split(b" ", 1)[0]
        return state == b"Z"
    except (FileNotFoundError, IndexError):
        return True


def _await_gone(pid: int, label: str, *, direct_child: bool = False) -> None:
    deadline = time.monotonic() + 10.0
    while not _process_gone(pid, direct_child):
        if time.monotonic() > deadline:
            pytest.fail(f"{label} pid {pid} survived timeout cleanup (leak)")
        time.sleep(0.05)


def _read_pid(path, label: str) -> int:
    deadline = time.monotonic() + 5.0
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"{label} never wrote its pid file (spawn failed?)")
        time.sleep(0.05)
    return int(path.read_text())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-tree semantics")
async def test_timeout_cleanup_terminates_hanging_stdio_tree(tmp_path) -> None:
    child_pidfile = tmp_path / "child.pid"
    grandchild_pidfile = tmp_path / "grandchild.pid"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(GRANDCHILD)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", CHILD, str(child_pidfile), str(grandchild_pidfile), str(grandchild)],
    )
    sink: list[str] = []
    child_pid = grandchild_pid = -1

    try:
        with pytest.raises(TimeoutError):
            with anyio.fail_after(0.5):  # like settings.upstream_timeout_s
                async with _stdio_client_piped_stderr_capture(params, sink):
                    await anyio.sleep(30)  # cancelled mid-session → TimeoutError

        child_pid = _read_pid(child_pidfile, "child")
        grandchild_pid = _read_pid(grandchild_pidfile, "grandchild")
        _await_gone(child_pid, "child", direct_child=True)
        _await_gone(grandchild_pid, "grandchild")  # ← the leak this PR fixes
    finally:  # test hygiene: never leave the hanging tree behind
        for pid in (grandchild_pid, child_pid):
            if pid > 0:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
