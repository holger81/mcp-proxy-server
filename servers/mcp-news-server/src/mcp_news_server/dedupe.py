from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from mcp_news_server.models import NewsItem

_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "_ga",
        "ref",
        "ref_src",
        "si",
        "spm",
    }
)


def canonical_url(url: str) -> str:
    """Normalize URL for equality: scheme/host lowercased, strip fragment, drop common tracking query keys."""
    raw = (url or "").strip()
    if not raw:
        return ""
    p = urlparse(raw)
    if not p.scheme or not p.netloc:
        return raw
    scheme = p.scheme.lower()
    netloc = p.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    q_pairs = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    q_pairs.sort(key=lambda kv: kv[0].lower())
    query = urlencode(q_pairs, doseq=True)
    path = p.path or "/"
    return urlunparse((scheme, netloc, path, "", query, ""))


def title_fingerprint(title: str) -> str:
    s = (title or "").lower()
    s = re.sub(r"[^\w\s]+", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s, flags=re.UNICODE).strip()
    return s


def _published_sort_key(it: NewsItem) -> tuple[int, str]:
    """Tuple for sorting: dated items first, then by ISO date string (use reverse=True for newest-first)."""
    p = (it.published or "").strip()
    return (0, p) if p else (1, "")


def _prefer_item(a: NewsItem, b: NewsItem) -> bool:
    """True if a should replace b for the same canonical URL (newer / richer wins)."""
    pa, pb = (a.published or "").strip(), (b.published or "").strip()
    if pa and pb and pa != pb:
        return pa > pb
    if pa and not pb:
        return True
    if not pa and pb:
        return False
    return len(a.summary or "") > len(b.summary or "")


def dedupe_news_items(
    items: list[NewsItem],
    *,
    dedupe_urls: bool = True,
    dedupe_titles: bool = True,
    min_title_fingerprint_len: int = 24,
) -> list[NewsItem]:
    """Drop duplicate links, then drop duplicate stories by normalized title (syndication)."""
    working = [it for it in items if (it.url or "").strip()]
    if dedupe_urls:
        by_url: dict[str, NewsItem] = {}
        for it in working:
            key = canonical_url(it.url)
            if not key:
                key = it.url.strip()
            if key not in by_url or _prefer_item(it, by_url[key]):
                by_url[key] = it
        working = list(by_url.values())

    working.sort(key=_published_sort_key, reverse=True)
    if not dedupe_titles:
        return working

    seen_fp: set[str] = set()
    out: list[NewsItem] = []
    for it in working:
        fp = title_fingerprint(it.title)
        if len(fp) >= min_title_fingerprint_len and fp in seen_fp:
            continue
        if len(fp) >= min_title_fingerprint_len:
            seen_fp.add(fp)
        out.append(it)
    return out
