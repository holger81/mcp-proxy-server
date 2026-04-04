"""In-memory ring buffer of recent log lines for the admin Logs tab."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime


class _LocalTimeFormatter(logging.Formatter):
    """Format ``asctime`` using the process wall clock (honours ``TZ`` and ``/etc/localtime`` on Unix)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created)
        fmt = datefmt or "%Y-%m-%d %H:%M:%S"
        return dt.strftime(fmt)


class RingLogHandler(logging.Handler):
    """Keeps the last N formatted log lines (thread-safe)."""

    def __init__(self, capacity: int = 1000) -> None:
        super().__init__()
        self._capacity = capacity
        self._buf: deque[str] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self.setFormatter(
            _LocalTimeFormatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            with self._lock:
                self._buf.append(msg)
        except Exception:
            self.handleError(record)

    def get_lines(self, limit: int | None = None) -> list[str]:
        with self._lock:
            lines = list(self._buf)
        if limit is not None and limit > 0:
            lines = lines[-limit:]
        return lines


_ring_handler = RingLogHandler(1000)


def get_ring_handler() -> RingLogHandler:
    return _ring_handler


def attach_ring_logging() -> None:
    """Attach the shared ring handler to app and uvicorn loggers (idempotent per logger)."""
    # Reload libc timezone (Unix): picks up ``TZ`` and ``/etc/localtime`` (e.g. Docker bind-mount).
    if os.name != "nt":
        try:
            time.tzset()
        except Exception:
            pass
    h = _ring_handler
    for name in ("mcp_proxy", "uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        if not any(type(x) is RingLogHandler for x in lg.handlers):
            lg.addHandler(h)
