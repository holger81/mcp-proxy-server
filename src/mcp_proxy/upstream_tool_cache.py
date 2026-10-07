"""Per-server TTL cache for upstream ``list_tools`` results (PLAN 6.2).

Every discovery flow (meta tools, hot-tool enrichment, admin catalog) used
to open a full MCP session (connect + initialize + ``tools/list``) against
each enabled upstream *per request*. This cache stores the tool list per
server for ``tool_list_cache_ttl_s`` seconds (0 disables).

Invalidation:

- **Config edits self-invalidate**: the cache key is the server id *plus a
  fingerprint of the whole config record*, so changing url/command/headers
  (or anything else) misses on the next discovery — no store hooks needed.
- **Fetch errors invalidate** (see ``_list_upstream_tools_cached`` in
  ``proxy_mcp``), so a recovered upstream is re-queried immediately.
- **Package installs/upgrades invalidate explicitly** (stdio contents can
  change while the command string stays identical).

Entries are bounded (LRU-ish eviction by age). Cached ``Tool`` objects are
shared read-only — discovery callers must not mutate them.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

from mcp_proxy.models import UpstreamServer


def server_fingerprint(server: UpstreamServer) -> str:
    """Cheap identity of a server's *config* (change ⇒ cached tools invalid).

    Duck-typed so tests can pass lightweight stand-ins.
    """
    try:
        data = server.model_dump(mode="json")
    except AttributeError:
        data = getattr(server, "__dict__", None) or repr(server)
    payload = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class UpstreamToolListCache:
    def __init__(self, ttl_s: float = 30.0, max_entries: int = 128) -> None:
        self._ttl_s = max(0.0, float(ttl_s))
        self._max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        # server_id -> (fingerprint, stored_at_monotonic, tools)
        self._entries: dict[str, tuple[str, float, list[Any]]] = {}

    @property
    def enabled(self) -> bool:
        return self._ttl_s > 0

    def get(self, server: UpstreamServer) -> list[Any] | None:
        if not self.enabled:
            return None
        fp = server_fingerprint(server)
        with self._lock:
            entry = self._entries.get(server.id)
            if entry is None or entry[0] != fp:
                return None
            if time.monotonic() - entry[1] >= self._ttl_s:
                del self._entries[server.id]
                return None
            return entry[2]

    def put(self, server: UpstreamServer, tools: list[Any]) -> None:
        if not self.enabled:
            return
        fp = server_fingerprint(server)
        with self._lock:
            self._entries[server.id] = (fp, time.monotonic(), list(tools))
            while len(self._entries) > self._max_entries:
                oldest = min(self._entries, key=lambda k: self._entries[k][1])
                del self._entries[oldest]

    def invalidate(self, server_id: str | None = None) -> None:
        with self._lock:
            if server_id is None:
                self._entries.clear()
            else:
                self._entries.pop(server_id, None)
