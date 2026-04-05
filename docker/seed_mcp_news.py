#!/usr/bin/env python3
"""Idempotent bootstrap for Docker: news domain, bundled MCP news stdio server, default RSS feeds."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

DATA = Path(os.environ.get("MCP_PROXY_DATA_DIR", "/data")).resolve()
CONFIG = DATA / "config"
NEWS_DATA = DATA / "mcp-news"
DEFAULT_FEEDS_SRC = Path(
    os.environ.get("MCP_NEWS_DEFAULT_FEEDS", "/app/mcp-news-default-feeds.yaml")
).resolve()

DEFAULT_DOMAIN = {"id": "default", "label": "Default"}
NEWS_DOMAIN = {"id": "news", "label": "News & current events"}

NEWS_SERVER = {
    "id": "mcp-news",
    "domain": "news",
    "enabled": True,
    "type": "stdio",
    "display_name": "News curation (RSS, web, SearXNG)",
    "llm_context": (
        "Curates headlines from configured RSS feeds, arbitrary article URLs (Open Graph / title), and optional "
        "SearXNG search. Tools: news_list_feeds, news_add_rss_feed, news_remove_rss_feed, news_searx_search, "
        "news_ingest_urls, news_curate. For a deduplicated multi-source briefing, call news_curate. "
        "Feed list persists under NEWS_MCP_DATA_DIR (set below)."
    ),
    "command": ["mcp-news-server"],
    "cwd": None,
    "env": {"NEWS_MCP_DATA_DIR": str(NEWS_DATA)},
}


def _load_json(path: Path, default):
    if not path.is_file():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def _atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _ensure_domains() -> bool:
    """Ensure `default` (for other MCP servers) and `news` exist; idempotent."""
    path = CONFIG / "domains.json"
    doc = _load_json(path, {"domains": []})
    domains = doc.get("domains")
    if not isinstance(domains, list):
        domains = []
    ids = {d.get("id") for d in domains if isinstance(d, dict)}
    changed = False
    if "default" not in ids:
        domains.insert(0, dict(DEFAULT_DOMAIN))
        ids.add("default")
        changed = True
        print("seed_mcp_news: added domain 'default'", file=sys.stderr)
    if "news" not in ids:
        domains.append(dict(NEWS_DOMAIN))
        changed = True
        print("seed_mcp_news: added domain 'news'", file=sys.stderr)
    if not changed:
        return False
    doc["domains"] = domains
    _atomic_write_json(path, doc)
    return True


def _ensure_server() -> bool:
    path = CONFIG / "servers.json"
    doc = _load_json(path, {"servers": []})
    servers = doc.get("servers")
    if not isinstance(servers, list):
        servers = []
    if any(isinstance(s, dict) and s.get("id") == "mcp-news" for s in servers):
        return False
    servers.append(dict(NEWS_SERVER))
    doc["servers"] = servers
    _atomic_write_json(path, doc)
    print("seed_mcp_news: registered stdio server 'mcp-news'", file=sys.stderr)
    return True


def _ensure_default_feeds() -> bool:
    NEWS_DATA.mkdir(parents=True, exist_ok=True)
    dest = NEWS_DATA / "feeds.yaml"
    if dest.exists():
        return False
    if not DEFAULT_FEEDS_SRC.is_file():
        print(
            f"seed_mcp_news: warning: default feeds missing at {DEFAULT_FEEDS_SRC}",
            file=sys.stderr,
        )
        return False
    shutil.copyfile(DEFAULT_FEEDS_SRC, dest)
    print(f"seed_mcp_news: installed default feeds -> {dest}", file=sys.stderr)
    return True


def main() -> int:
    CONFIG.mkdir(parents=True, exist_ok=True)
    _ensure_domains()
    _ensure_server()
    _ensure_default_feeds()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
