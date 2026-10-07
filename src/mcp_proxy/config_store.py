from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

from mcp_proxy.models import ServerListFile, UpstreamServer


class ServerConfigStore:
    """Thread-safe JSON persistence for upstream MCP server definitions."""

    def __init__(self, data_dir: Path) -> None:
        self._config_dir = data_dir / "config"
        self._path = self._config_dir / "servers.json"
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _read_raw(self) -> ServerListFile:
        if not self._path.is_file():
            return ServerListFile()
        text = self._path.read_text(encoding="utf-8")
        if not text.strip():
            return ServerListFile()
        data = json.loads(text)
        return ServerListFile.model_validate(data)

    def list_servers(self) -> list[UpstreamServer]:
        with self._lock:
            return list(self._read_raw().servers)

    def get(self, server_id: str) -> UpstreamServer | None:
        with self._lock:
            for s in self._read_raw().servers:
                if s.id == server_id:
                    return s
        return None

    def add(self, server: UpstreamServer) -> None:
        with self._lock:
            doc = self._read_raw()
            if any(s.id == server.id for s in doc.servers):
                raise ValueError(f"server id already exists: {server.id}")
            doc.servers.append(server)
            self._write_unlocked(doc)

    def remove(self, server_id: str) -> bool:
        with self._lock:
            doc = self._read_raw()
            before = len(doc.servers)
            doc.servers = [s for s in doc.servers if s.id != server_id]
            if len(doc.servers) == before:
                return False
            self._write_unlocked(doc)
            return True

    def update(self, server_id: str, server: UpstreamServer) -> None:
        if server.id != server_id:
            raise ValueError("server id in body must match URL path")
        with self._lock:
            doc = self._read_raw()
            for i, s in enumerate(doc.servers):
                if s.id == server_id:
                    doc.servers[i] = server
                    self._write_unlocked(doc)
                    return
            raise KeyError(server_id)

    def update_fields(
        self, server_id: str, mutate: "Callable[[UpstreamServer], UpstreamServer]"
    ) -> UpstreamServer:
        """Atomic read-modify-write of one server (PR 5.9 CAS).

        ``mutate`` receives the *current* server (freshly read under the lock)
        and returns the modified copy. Because read + write happen while the
        lock is held, two concurrent single-field updates (e.g. toggling
        ``enabled`` vs rewriting ``command`` after an upgrade) can never
        clobber each other the way a ``get()`` then ``update()`` would, where
        an intervening write is silently overwritten (lost update).

        Raises ``KeyError`` if the server id is missing.
        """
        with self._lock:
            doc = self._read_raw()
            for i, s in enumerate(doc.servers):
                if s.id == server_id:
                    updated = mutate(s)
                    if updated.id != server_id:
                        raise ValueError("mutate must not change the server id")
                    doc.servers[i] = updated
                    self._write_unlocked(doc)
                    return updated
            raise KeyError(server_id)

    def _write_unlocked(self, doc: ServerListFile) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        payload = doc.model_dump(mode="json")
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)
