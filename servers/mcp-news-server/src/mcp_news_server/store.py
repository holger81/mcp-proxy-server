from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml

from mcp_news_server.dedupe import canonical_url
from mcp_news_server.feed_migrations import migrate_feeds
from mcp_news_server.models import FeedEntry

log = logging.getLogger(__name__)

_DEFAULT_REL = Path(".local/share/mcp-news-server")

# One lock for all FeedStore instances: tool handlers run concurrently in the
# server's thread pool, so the load-modify-save cycle and the atomic replace
# below must not interleave. RLock because add()/remove() nest load() and save().
_STORE_LOCK = threading.RLock()


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
        # Migration IDs already recorded on disk (refreshed by load());
        # persisted again by save() so each one-shot migration runs only once.
        self._applied: list[str] = []

    def _ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("feeds: []\n", encoding="utf-8")

    def load(self) -> list[FeedEntry]:
        with _STORE_LOCK:
            self._ensure()
            raw = self._read_yaml_tolerant()
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
            disk_applied = [
                x
                for x in raw.get("migrations_applied") or []
                if isinstance(x, str)
            ]
            migrated, recorded, changed = migrate_feeds(out, disk_applied)
            self._applied = disk_applied + [
                rid for rid in recorded if rid not in disk_applied
            ]
            if changed or recorded:
                self.save(migrated)
            return migrated

    def _read_yaml_tolerant(self) -> dict:
        """Parse feeds.yaml; never raise on bad content.

        Unparseable YAML is moved aside to ``feeds.yaml.corrupt-<UTC ts>`` so
        the evidence survives but the server can start from the seeded defaults.
        Callers hold _STORE_LOCK.
        """
        try:
            parsed = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except OSError as e:
            log.error("Could not read %s: %s", self._path, e)
            return {}
        except yaml.YAMLError as e:
            log.error(
                "feeds.yaml is not valid YAML (%s); moved to %s, starting fresh",
                e,
                self._quarantine_corrupt(),
            )
            return {}
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            log.warning(
                "Ignoring unexpected feeds.yaml top-level type %s",
                type(parsed).__name__,
            )
            return {}
        return parsed

    def _quarantine_corrupt(self) -> Path:
        """Rename the current feeds.yaml aside with a UTC timestamp suffix."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bad = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        try:
            self._path.replace(bad)
        except OSError as e:
            log.error("Could not move corrupt %s aside: %s", self._path, e)
            return self._path
        return bad

    def save(self, feeds: list[FeedEntry]) -> None:
        with _STORE_LOCK:
            self._ensure()
            payload = {
                "feeds": [
                    {"url": f.url, "label": f.label, "enabled": f.enabled}
                    for f in feeds
                ],
                "migrations_applied": list(self._applied),
            }
            # Write-then-replace so a crash mid-write can never leave a
            # truncated feeds.yaml behind.
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(
                yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)

    def add(self, url: str, label: str = "") -> list[FeedEntry]:
        with _STORE_LOCK:
            feeds = self.load()
            canon = canonical_url(url) or url.strip()
            for f in feeds:
                if canonical_url(f.url) == canon or f.url.strip() == url.strip():
                    return feeds
            feeds.append(
                FeedEntry(url=url.strip(), label=label.strip(), enabled=True)
            )
            self.save(feeds)
            return feeds

    def remove(self, url: str) -> list[FeedEntry]:
        with _STORE_LOCK:
            feeds = self.load()
            target = canonical_url(url) or url.strip()
            kept = [
                f
                for f in feeds
                if canonical_url(f.url) != target and f.url.strip() != url.strip()
            ]
            self.save(kept)
            return kept
