"""Persisted API client records (bearer tokens; hashes only on disk)."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from mcp_proxy.models import validate_slug_id


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ApiClientRecord(BaseModel):
    id: str = Field(min_length=1, max_length=63)
    label: str = Field(min_length=1, max_length=200)
    created_at: str
    token_sha256_hex: str = Field(min_length=64, max_length=64)

    @field_validator("id")
    @classmethod
    def id_slug(cls, v: str) -> str:
        return validate_slug_id(v)


class ApiClientListFile(BaseModel):
    clients: list[ApiClientRecord] = Field(default_factory=list)


class ClientTokenStore:
    """Thread-safe JSON store under /data/config/api_clients.json."""

    def __init__(self, data_dir: Path) -> None:
        self._config_dir = data_dir / "config"
        self._path = self._config_dir / "api_clients.json"
        self._lock = threading.Lock()

    def _read(self) -> ApiClientListFile:
        if not self._path.is_file():
            return ApiClientListFile()
        text = self._path.read_text(encoding="utf-8")
        if not text.strip():
            return ApiClientListFile()
        return ApiClientListFile.model_validate(json.loads(text))

    def _write_unlocked(self, doc: ApiClientListFile) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(doc.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def list_public(self) -> list[dict[str, Any]]:
        with self._lock:
            doc = self._read()
        return [
            {"id": c.id, "label": c.label, "created_at": c.created_at}
            for c in doc.clients
        ]

    def create(self, label: str) -> tuple[ApiClientRecord, str]:
        """Return (stored record with hash, plaintext bearer token shown once)."""
        label = label.strip()
        if not label:
            raise ValueError("label is required")
        cid = validate_slug_id("c" + secrets.token_hex(8))
        plain = "mcp_" + secrets.token_urlsafe(32)
        digest = _token_digest(plain)
        created = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        record = ApiClientRecord(
            id=cid,
            label=label[:200],
            created_at=created,
            token_sha256_hex=digest,
        )
        with self._lock:
            doc = self._read()
            doc.clients.append(record)
            self._write_unlocked(doc)
        return record, plain

    def verify_bearer(self, token: str) -> bool:
        if not token:
            return False
        digest = _token_digest(token)
        with self._lock:
            doc = self._read()
            return any(
                secrets.compare_digest(digest, c.token_sha256_hex) for c in doc.clients
            )

    def remove(self, client_id: str) -> bool:
        cid = validate_slug_id(client_id)
        with self._lock:
            doc = self._read()
            before = len(doc.clients)
            doc.clients = [c for c in doc.clients if c.id != cid]
            if len(doc.clients) == before:
                return False
            self._write_unlocked(doc)
            return True
