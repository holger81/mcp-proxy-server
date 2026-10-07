"""Cancellation-aware subprocess runs for long installs (PR 5.9).

pip/npm installs execute in ``anyio.to_thread.run_sync``. With the default
``abandon_on_cancel=False`` a cancelled MCP tool call (client disconnect,
session idle timeout, shutdown) could not return until the whole install
finished — up to 10 minutes — holding the session's cancel scope and lifespan
shutdown hostage; and with abandonment the pip/npm child would keep running
regardless.

``run_process`` mirrors ``subprocess.run`` but watches a caller-supplied
``threading.Event`` and, when it fires, terminates the child's whole process
group (the child is spawned in its own session). Without an event it degrades
to a plain ``subprocess.run``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

_KILL_GRACE_S = 2.0
_POLL_S = 0.05


class InstallCancelled(RuntimeError):
    """Raised by :func:`run_process` when the cancel event killed the child."""


def _terminate_group(proc: subprocess.Popen, grace_s: float = _KILL_GRACE_S) -> None:
    """Terminate the child (and, on POSIX, its whole process group)."""
    if os.name != "posix":
        proc.terminate()
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            proc.kill()
        return
    try:
        # start_new_session=True made the child its own group leader, so the
        # group id equals its pid and pip/npm's helpers die with it.
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(_POLL_S)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_process(
    argv: list[str],
    *,
    timeout_s: float,
    cancel_event: threading.Event | None = None,
    **popen_kwargs,
) -> subprocess.CompletedProcess:
    """``subprocess.run(capture_output=True, text=True)`` with a kill switch.

    When ``cancel_event`` is set while the child runs, the child's process
    group is terminated and :class:`InstallCancelled` is raised. Timeouts
    behave like ``subprocess.run`` (tree killed, ``TimeoutExpired`` raised).
    """
    if cancel_event is None:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, **popen_kwargs
        )

    popen_kwargs.setdefault("stdout", subprocess.PIPE)
    popen_kwargs.setdefault("stderr", subprocess.PIPE)
    popen_kwargs.setdefault("text", True)
    if os.name == "posix":
        popen_kwargs.setdefault("start_new_session", True)
    proc = subprocess.Popen(argv, **popen_kwargs)

    killed = False

    def _watch() -> None:
        nonlocal killed
        while proc.poll() is None:
            if cancel_event.is_set():
                killed = True
                _terminate_group(proc)
                break
            time.sleep(_POLL_S)

    watcher = threading.Thread(
        target=_watch, name=f"proc-cancel-{proc.pid}", daemon=True
    )
    watcher.start()
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _terminate_group(proc)
        raise
    finally:
        watcher.join(timeout=_KILL_GRACE_S + 1.0)
    if killed:
        raise InstallCancelled(
            f"install interrupted; {argv[0] if argv else 'child'} was terminated"
        )
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
