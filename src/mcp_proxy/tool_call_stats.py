"""Persistent counters for composite tool names (legacy `server/tool`, safe `srv__tool`, etc.)."""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

HOT_TOOL_SLOTS = 3


class ToolCallStatsStore:
    """Thread-safe counts persisted under ``data_dir / tool_call_stats.json``.

    PR 5.4: mutations only touch memory and schedule a debounced background
    flush (at most one file write per ``flush_interval_s``); a hot proxy used
    to rewrite the whole JSON on every successful tool call. ``flush()``
    forces a pending write (also registered via ``atexit``). Key count is
    capped so removed/renamed upstreams cannot grow the file forever.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        flush_interval_s: float = 5.0,
        max_keys: int = 1000,
    ) -> None:
        self._path = data_dir / "tool_call_stats.json"
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._flush_interval_s = max(0.001, flush_interval_s)
        self._max_keys = max(1, max_keys)
        self._dirty = False
        self._timer: threading.Timer | None = None
        self._load()
        atexit.register(self.flush)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw_txt = self._path.read_text(encoding="utf-8")
            raw = json.loads(raw_txt)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        counts = raw.get("counts")
        if not isinstance(counts, dict):
            return
        out: dict[str, int] = {}
        for k, v in counts.items():
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if n > 0:
                out[str(k)] = n
        self._counts = out
        self._cap_unlocked()

    def _cap_unlocked(self) -> None:
        """Drop least-popular keys beyond ``max_keys`` (ties: name order)."""
        overflow = len(self._counts) - self._max_keys
        if overflow <= 0:
            return
        for key, _ in sorted(self._counts.items(), key=lambda kv: (kv[1], kv[0]))[
            :overflow
        ]:
            del self._counts[key]

    def _persist_unlocked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        payload = {"counts": dict(sorted(self._counts.items()))}
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    def _schedule_flush_unlocked(self) -> None:
        if self._timer is not None:
            return  # one pending write is enough; it picks up the latest state
        timer = threading.Timer(self._flush_interval_s, self._timer_flush)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _timer_flush(self) -> None:
        with self._lock:
            self._timer = None
            self._flush_dirty_unlocked()

    def _flush_dirty_unlocked(self) -> None:
        if not self._dirty:
            return
        try:
            self._persist_unlocked()
        except OSError:
            # Keep _dirty set: the next flush (timer, call, or atexit) retries.
            log.warning("tool_call_stats: persisting %s failed", self._path, exc_info=True)
            return
        self._dirty = False

    def flush(self) -> None:
        """Force a pending write (best effort; used at shutdown)."""
        with self._lock:
            self._flush_dirty_unlocked()

    def record_success(self, composite_tool_name: str) -> None:
        key = composite_tool_name.strip()
        # Composite keys: legacy `srv/tool`, hex fallback `srv__p__…`, or descriptive `srv__tool`.
        if not key:
            return
        if "/" not in key and "__p__" not in key:
            if "__" not in key:
                return
            left, _, right = key.partition("__")
            if not left or not right:
                return
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            self._cap_unlocked()
            self._dirty = True
            self._schedule_flush_unlocked()

    def remove(self, composite_tool_name: str) -> bool:
        """Drop a composite key from stats (e.g. tool removed upstream)."""
        key = composite_tool_name.strip()
        if not key:
            return False
        with self._lock:
            if key not in self._counts:
                return False
            del self._counts[key]
            self._dirty = True
            self._schedule_flush_unlocked()
            return True

    def ranked_keys(self) -> list[str]:
        """All composite keys ordered by call count (desc), then name."""
        with self._lock:
            # Readers (admin UI, tool-list build) double as a flush point so
            # the file is never older than one interval *plus* traffic pause.
            if self._dirty:
                self._schedule_flush_unlocked()
            items = sorted(self._counts.items(), key=lambda kv: (-kv[1], kv[0]))
            return [k for k, _ in items]

    def top_keys(self, n: int = HOT_TOOL_SLOTS) -> list[str]:
        return self.ranked_keys()[: max(0, n)]
