from __future__ import annotations

import json
import threading
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

    def _write_unlocked(self, doc: ServerListFile) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        payload = doc.model_dump(mode="json")
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)
