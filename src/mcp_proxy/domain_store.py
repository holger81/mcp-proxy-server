"""Persisted domain labels for grouping upstream MCP servers (admin + MCP tool enums)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from mcp_proxy.models import validate_slug_id


class DomainRecord(BaseModel):
    id: str = Field(min_length=1, max_length=63, description="Unique slug, e.g. smart-home")
    label: str = Field(min_length=1, max_length=120, description="Human label for dropdowns")

    @field_validator("id")
    @classmethod
    def id_slug(cls, v: str) -> str:
        return validate_slug_id(v)

    @field_validator("label", mode="before")
    @classmethod
    def strip_label(cls, v: object) -> str:
        s = str(v).strip() if v is not None else ""
        if not s:
            raise ValueError("label is required")
        return s


class DomainListFile(BaseModel):
    domains: list[DomainRecord] = Field(default_factory=list)


class DomainStore:
    """Thread-safe JSON store under /data/config/domains.json."""

    def __init__(self, data_dir: Path) -> None:
        self._config_dir = data_dir / "config"
        self._path = self._config_dir / "domains.json"
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> DomainListFile:
        if not self._path.is_file():
            return DomainListFile()
        text = self._path.read_text(encoding="utf-8")
        if not text.strip():
            return DomainListFile()
        return DomainListFile.model_validate(json.loads(text))

    def _write_unlocked(self, doc: DomainListFile) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(doc.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def ensure_default_domain(self) -> None:
        """If no domains file or empty list, create default domain."""
        with self._lock:
            doc = self._read()
            if doc.domains:
                return
            doc.domains.append(DomainRecord(id="default", label="Default"))
            self._write_unlocked(doc)

    def list_public(self) -> list[dict[str, Any]]:
        with self._lock:
            doc = self._read()
        return [d.model_dump(mode="json") for d in doc.domains]

    def list_records(self) -> list[DomainRecord]:
        with self._lock:
            return list(self._read().domains)

    def id_set(self) -> set[str]:
        return {d.id for d in self.list_records()}

    def get(self, domain_id: str) -> DomainRecord | None:
        did = validate_slug_id(domain_id)
        with self._lock:
            for d in self._read().domains:
                if d.id == did:
                    return d
        return None

    def add(self, record: DomainRecord) -> None:
        with self._lock:
            doc = self._read()
            if any(d.id == record.id for d in doc.domains):
                raise ValueError(f"domain id already exists: {record.id}")
            doc.domains.append(record)
            self._write_unlocked(doc)

    def remove(self, domain_id: str) -> bool:
        did = validate_slug_id(domain_id)
        if did == "default":
            raise ValueError("cannot remove the default domain")
        with self._lock:
            doc = self._read()
            before = len(doc.domains)
            doc.domains = [d for d in doc.domains if d.id != did]
            if len(doc.domains) == before:
                return False
            self._write_unlocked(doc)
            return True
