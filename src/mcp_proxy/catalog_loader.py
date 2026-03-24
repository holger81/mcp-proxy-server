"""Load MCP catalog presets from package data and optional /data/config overlay."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from pydantic import TypeAdapter

from mcp_proxy.catalog_models import McpCatalogPreset

_PRESETS_ADAPTER = TypeAdapter(list[McpCatalogPreset])


def _load_json_array(raw: str, *, label: str) -> list[dict]:
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"{label}: root must be a JSON array")
    return data


def load_builtin_presets() -> list[McpCatalogPreset]:
    pkg = resources.files("mcp_proxy.catalog")
    path = pkg / "builtin_presets.json"
    raw = path.read_text(encoding="utf-8")
    items = _load_json_array(raw, label="builtin_presets.json")
    return _PRESETS_ADAPTER.validate_python(items)


def load_overlay_presets(data_dir: Path) -> list[McpCatalogPreset]:
    path = data_dir / "config" / "catalog_presets.json"
    if not path.is_file():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    items = _load_json_array(raw, label=str(path))
    return _PRESETS_ADAPTER.validate_python(items)


def list_merged_presets(data_dir: Path) -> list[McpCatalogPreset]:
    """Builtin entries overridden or extended by /data/config/catalog_presets.json (same id replaces)."""
    merged: dict[str, McpCatalogPreset] = {}
    order: list[str] = []

    for p in load_builtin_presets():
        merged[p.id] = p
        order.append(p.id)

    for p in load_overlay_presets(data_dir):
        if p.id not in merged:
            order.append(p.id)
        merged[p.id] = p

    return [merged[i] for i in order]


def presets_as_json(data_dir: Path) -> list[dict]:
    return [p.model_dump_public() for p in list_merged_presets(data_dir)]
