"""Short-lived cache of large callTool text responses for pagination."""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class CachedToolResponse:
    tool_name: str
    text: str
    created_at: float
    # Audit L-16: the cached body may carry secrets fetched on behalf of one
    # caller; ``owner`` (API client id / session sentinel) bounds replay.
    owner: str | None = None


class ToolResponseCache:
    """In-memory LRU cache with TTL (per proxy process)."""

    def __init__(self, *, max_entries: int = 64, ttl_s: float = 600.0) -> None:
        self._max_entries = max(1, max_entries)
        self._ttl_s = max(30.0, ttl_s)
        self._lock = Lock()
        self._entries: OrderedDict[str, CachedToolResponse] = OrderedDict()

    def put(self, tool_name: str, text: str, *, owner: str | None = None) -> str:
        cache_id = secrets.token_urlsafe(12)
        entry = CachedToolResponse(
            tool_name=tool_name,
            text=text,
            created_at=time.monotonic(),
            owner=owner,
        )
        with self._lock:
            self._purge_expired_unlocked()
            self._entries[cache_id] = entry
            self._entries.move_to_end(cache_id)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return cache_id

    def get(
        self, cache_id: str, *, owner: str | None = None
    ) -> CachedToolResponse | None:
        with self._lock:
            self._purge_expired_unlocked()
            entry = self._entries.get(cache_id)
            if entry is None:
                return None
            # Audit L-16: replay by a different owner is a miss (callers
            # already surface ids as "missing or expired"); ids are random,
            # but they land in LLM transcripts where leakage must be bounded.
            if entry.owner != owner:
                return None
            self._entries.move_to_end(cache_id)
            return entry

    def _purge_expired_unlocked(self) -> None:
        now = time.monotonic()
        expired = [
            cid
            for cid, e in self._entries.items()
            if now - e.created_at > self._ttl_s
        ]
        for cid in expired:
            del self._entries[cid]
