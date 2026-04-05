from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NewsItem:
    title: str
    url: str
    summary: str | None = None
    published: str | None = None
    source_type: str = "unknown"
    source_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "published": self.published,
            "sourceType": self.source_type,
            "sourceName": self.source_name,
        }
        if self.extra:
            d["extra"] = self.extra
        return d


@dataclass
class FeedEntry:
    url: str
    label: str = ""
    enabled: bool = True

    def to_json_dict(self) -> dict[str, Any]:
        return {"url": self.url, "label": self.label or "", "enabled": self.enabled}
