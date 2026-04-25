from __future__ import annotations

import json
import threading
from pathlib import Path

_LOCK = threading.Lock()


def _meta_path(data_dir: Path) -> Path:
    return data_dir / "config" / "stdio-packages.json"


def _load_unlocked(data_dir: Path) -> dict[str, dict[str, str]]:
    path = _meta_path(data_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for sid, meta in raw.items():
        if not isinstance(sid, str) or not isinstance(meta, dict):
            continue
        ecosystem = str(meta.get("ecosystem", "")).strip()
        package_spec = str(meta.get("package_spec", "")).strip()
        if ecosystem in {"pypi", "npm"} and package_spec:
            out[sid] = {"ecosystem": ecosystem, "package_spec": package_spec}
    return out


def _save_unlocked(data_dir: Path, doc: dict[str, dict[str, str]]) -> None:
    path = _meta_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def get_stdio_meta(data_dir: Path, server_id: str) -> dict[str, str] | None:
    with _LOCK:
        return _load_unlocked(data_dir).get(server_id)


def set_stdio_meta(
    data_dir: Path, server_id: str, ecosystem: str, package_spec: str
) -> None:
    with _LOCK:
        doc = _load_unlocked(data_dir)
        doc[server_id] = {"ecosystem": ecosystem, "package_spec": package_spec}
        _save_unlocked(data_dir, doc)


def remove_stdio_meta(data_dir: Path, server_id: str) -> None:
    with _LOCK:
        doc = _load_unlocked(data_dir)
        if server_id in doc:
            doc.pop(server_id, None)
            _save_unlocked(data_dir, doc)
