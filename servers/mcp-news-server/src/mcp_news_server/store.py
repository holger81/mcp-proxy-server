from __future__ import annotations

import os
from pathlib import Path

import yaml

from mcp_news_server.dedupe import canonical_url
from mcp_news_server.models import FeedEntry

_DEFAULT_REL = Path(".local/share/mcp-news-server")


def default_data_dir() -> Path:
    env = os.environ.get("NEWS_MCP_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / _DEFAULT_REL


class FeedStore:
    """YAML-backed RSS feed list."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self._path = self.data_dir / "feeds.yaml"

    def _ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("feeds: []\n", encoding="utf-8")

    def load(self) -> list[FeedEntry]:
        self._ensure()
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        feeds = raw.get("feeds") or []
        out: list[FeedEntry] = []
        if not isinstance(feeds, list):
            return out
        for row in feeds:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url", "")).strip()
            if not url:
                continue
            label = str(row.get("label", "") or "").strip()
            enabled = bool(row.get("enabled", True))
            out.append(FeedEntry(url=url, label=label, enabled=enabled))
        return out

    def save(self, feeds: list[FeedEntry]) -> None:
        self._ensure()
        payload = {
            "feeds": [
                {"url": f.url, "label": f.label, "enabled": f.enabled} for f in feeds
            ]
        }
        self._path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    def add(self, url: str, label: str = "") -> list[FeedEntry]:
        feeds = self.load()
        canon = canonical_url(url) or url.strip()
        for f in feeds:
            if canonical_url(f.url) == canon or f.url.strip() == url.strip():
                return feeds
        feeds.append(FeedEntry(url=url.strip(), label=label.strip(), enabled=True))
        self.save(feeds)
        return feeds

    def remove(self, url: str) -> list[FeedEntry]:
        feeds = self.load()
        target = canonical_url(url) or url.strip()
        kept = [
            f
            for f in feeds
            if canonical_url(f.url) != target and f.url.strip() != url.strip()
        ]
        self.save(kept)
        return kept
