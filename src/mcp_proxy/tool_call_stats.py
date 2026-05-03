"""Persistent counters for composite tool names (`server/tool`) invoked via callTool."""

from __future__ import annotations

import json
import threading
from pathlib import Path

_HOT_TOOL_SLOTS = 3


class ToolCallStatsStore:
    """Thread-safe counts persisted under ``data_dir / tool_call_stats.json``."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "tool_call_stats.json"
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._load()

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

    def _persist_unlocked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        payload = {"counts": dict(sorted(self._counts.items()))}
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    def record_success(self, composite_tool_name: str) -> None:
        key = composite_tool_name.strip()
        if not key or "/" not in key:
            return
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            self._persist_unlocked()

    def top_keys(self, n: int = _HOT_TOOL_SLOTS) -> list[str]:
        with self._lock:
            items = sorted(self._counts.items(), key=lambda kv: (-kv[1], kv[0]))
            return [k for k, _ in items[: max(0, n)]]
