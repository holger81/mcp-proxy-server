"""Optional LLM pass to pick top headlines and write a briefing (OpenAI-compatible API)."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class LlmCuratorConfig:
    base_url: str
    api_key: str | None
    model: str
    top_n: int
    input_max: int
    summary_max_chars: int
    timeout_s: float
    digests: frozenset[str]

    @classmethod
    def from_env(cls) -> LlmCuratorConfig | None:
        model = os.environ.get("NEWS_MCP_LLM_MODEL", "").strip()
        if not model:
            return None
        raw_enabled = os.environ.get("NEWS_MCP_LLM_ENABLED", "").strip().lower()
        if raw_enabled in ("0", "false", "no", "off"):
            return None
        base = (
            os.environ.get("NEWS_MCP_LLM_BASE_URL", "").strip()
            or "https://api.openai.com/v1"
        ).rstrip("/")
        api_key = os.environ.get("NEWS_MCP_LLM_API_KEY", "").strip() or None
        top_n = _int_env("NEWS_MCP_LLM_TOP_N", 12, lo=3, hi=30)
        input_max = _int_env("NEWS_MCP_LLM_INPUT_MAX", 40, lo=10, hi=80)
        # Per-headline RSS excerpt sent to the model (after HTML strip). 0 = no cap.
        summary_max_chars = _int_env("NEWS_MCP_LLM_SUMMARY_MAX_CHARS", 1200, lo=0, hi=8000)
        timeout_s = _float_env("NEWS_MCP_LLM_TIMEOUT_S", 90.0, lo=10.0, hi=300.0)
        digest_raw = os.environ.get("NEWS_MCP_LLM_DIGESTS", "today,germany,local").strip()
        digests = frozenset(
            d.strip().lower()
            for d in digest_raw.split(",")
            if d.strip().lower() in ("today", "germany", "local")
        ) or frozenset({"today"})
        return cls(
            base_url=base,
            api_key=api_key,
            model=model,
            top_n=top_n,
            input_max=input_max,
            summary_max_chars=summary_max_chars,
            timeout_s=timeout_s,
            digests=digests,
        )


def _int_env(key: str, default: int, *, lo: int, hi: int) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default


def _float_env(key: str, default: float, *, lo: float, hi: float) -> float:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(hi, float(raw)))
    except ValueError:
        return default


def _plain_summary(raw: str | None, max_chars: int) -> str:
    """RSS description with HTML removed; optional cap (0 = send full excerpt)."""
    if not raw:
        return ""
    text = _HTML_TAG_RE.sub(" ", raw)
    text = _WS_RE.sub(" ", text).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    # Prefer breaking at a sentence when truncating (token safety valve only).
    cut = text[:max_chars]
    for sep in (". ", "! ", "? "):
        pos = cut.rfind(sep)
        if pos >= max_chars // 2:
            return cut[: pos + 1].strip()
    return cut.rstrip() + "…"


def _headline_lines(
    items: list[dict[str, Any]], limit: int, summary_max_chars: int
) -> list[str]:
    lines: list[str] = []
    for i, item in enumerate(items[:limit], start=1):
        title = str(item.get("title") or "").strip() or "(no title)"
        source = str(item.get("sourceName") or item.get("sourceType") or "").strip()
        summary = _plain_summary(str(item.get("summary") or ""), summary_max_chars)
        bit = f"{i}. [{source}] {title}" if source else f"{i}. {title}"
        if summary:
            bit += f"\n   {summary}"
        lines.append(bit)
    return lines


def _parse_llm_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        raise ValueError("LLM response did not contain JSON")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM JSON root must be an object")
    return data


def _apply_selection(
    items: list[dict[str, Any]], parsed: dict[str, Any], top_n: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (curated items, selection notes)."""
    selected_raw = parsed.get("selected")
    if not isinstance(selected_raw, list):
        raise ValueError("LLM JSON missing 'selected' array")

    curated: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    seen: set[int] = set()
    for entry in selected_raw:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if idx < 1 or idx > len(items) or idx in seen:
            continue
        seen.add(idx)
        item = dict(items[idx - 1])
        why = str(entry.get("importance") or entry.get("why") or "").strip()
        if why:
            item["importance"] = why
        curated.append(item)
        notes.append({"index": idx, "importance": why or None})
        if len(curated) >= top_n:
            break

    if not curated:
        raise ValueError("LLM selected no valid headlines")

    return curated, notes


async def maybe_curate_digest_payload(
    payload: dict[str, Any],
    *,
    digest: str,
    client: httpx.AsyncClient,
    config: LlmCuratorConfig | None = None,
) -> dict[str, Any]:
    """If LLM is configured for this digest, add briefing and reorder items."""
    cfg = config or LlmCuratorConfig.from_env()
    if cfg is None or digest not in cfg.digests:
        return payload

    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return payload

    lines = _headline_lines(items, cfg.input_max, cfg.summary_max_chars)
    if not lines:
        return payload

    system = (
        "You are a senior news editor. From the numbered headline list (title plus RSS excerpt "
        "per story), pick the stories that matter most to a well-informed general reader and "
        f"write a thematic briefing. Choose up to {cfg.top_n} distinct stories (fewer if the list "
        "is thin). Avoid duplicate angles on the same event. The excerpts are from RSS, not full "
        "articles — synthesize themes from what is provided.\n\n"
        "Respond with JSON only:\n"
        "{\n"
        '  "briefing": "2-4 short paragraphs in markdown summarizing the day\'s most important themes",\n'
        '  "selected": [\n'
        '    {"index": 1, "importance": "one sentence on why this matters"}\n'
        "  ]\n"
        "}"
    )
    user = (
        f"Digest: {digest}\n"
        f"Headlines ({len(lines)} shown, {payload.get('itemCount', len(items))} total after dedupe):\n"
        + "\n".join(lines)
    )

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }

    url = f"{cfg.base_url}/chat/completions"
    try:
        resp = await client.post(url, headers=headers, json=body, timeout=cfg.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("LLM response missing choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ValueError("LLM response missing message content")
        parsed = _parse_llm_json(content)
        briefing = str(parsed.get("briefing") or "").strip()
        if not briefing:
            raise ValueError("LLM JSON missing briefing")
        curated, selection = _apply_selection(items, parsed, cfg.top_n)
    except Exception as e:
        log.warning("LLM digest curation failed for %s: %s", digest, e)
        meta = dict(payload.get("meta") or {})
        meta["llmCurated"] = False
        meta["llmError"] = str(e) or type(e).__name__
        out = dict(payload)
        out["meta"] = meta
        return out

    meta = dict(payload.get("meta") or {})
    meta["llmCurated"] = True
    meta["llmModel"] = cfg.model
    meta["llmCuratedAt"] = meta.get("updatedAt")
    meta["rawItemCount"] = payload.get("itemCount", len(items))

    out = dict(payload)
    out["briefing"] = briefing
    out["items"] = curated
    out["itemCount"] = len(curated)
    out["selection"] = selection
    out["meta"] = meta
    log.info(
        "LLM curated digest %s: %s -> %s items",
        digest,
        meta.get("rawItemCount"),
        len(curated),
    )
    return out
