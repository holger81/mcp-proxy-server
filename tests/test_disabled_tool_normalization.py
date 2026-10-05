"""PR 4.1: disabled-tool checks compare decoded names, not raw strings.

On main, `disabled_tools` entries and wire tool names were compared verbatim,
so disabling `news-server/news_curate` (legacy spelling) still allowed calls
as `news_server__news_curate` — and vice versa. All spellings of the same
(server, tool) pair now canonicalize to one key.
"""

from __future__ import annotations

import pytest

from mcp_proxy.client_policy import client_disabled_tools, is_tool_disabled
from mcp_proxy.client_store import ApiClientRecord
from mcp_proxy.tool_names import canonical_tool_key, encode_proxy_tool_name

# Same tool, both wire spellings (safe_tool_names on/off).
SAFE_SPELLINGS = ["news-server/news_curate", "news_server__news_curate"]

# A tool name that is not safely representable: legacy form keeps it raw,
# the safe encoder falls back to `srv__p__<hex>`.
UNSAFE_TOOL = "news curate!!"
UNSAFE_SPELLINGS = [
    f"news-server/{UNSAFE_TOOL}",
    encode_proxy_tool_name("news-server", UNSAFE_TOOL),
]


def test_spellings_canonicalize_equal() -> None:
    assert len({canonical_tool_key(s) for s in SAFE_SPELLINGS}) == 1
    assert len({canonical_tool_key(s) for s in UNSAFE_SPELLINGS}) == 1
    assert canonical_tool_key(SAFE_SPELLINGS[0]) != canonical_tool_key(
        UNSAFE_SPELLINGS[0]
    )


@pytest.mark.parametrize("stored", SAFE_SPELLINGS)
def test_one_stored_entry_blocks_every_spelling(stored: str) -> None:
    client = ApiClientRecord(
        id="c1",
        label="t",
        created_at="2026-01-01T00:00:00Z",
        token_sha256_hex="0" * 64,
        disabled_tools=[stored],
    )
    disabled = client_disabled_tools(client)
    for wire in SAFE_SPELLINGS:
        assert is_tool_disabled(wire, disabled), wire
    assert not is_tool_disabled("news_server__news_list_feeds", disabled)
    assert not is_tool_disabled("other_server__news_curate", disabled)


@pytest.mark.parametrize("stored", UNSAFE_SPELLINGS)
def test_hex_fallback_tool_blocked_whichever_way(stored: str) -> None:
    client = ApiClientRecord(
        id="c1",
        label="t",
        created_at="2026-01-01T00:00:00Z",
        token_sha256_hex="0" * 64,
        disabled_tools=[stored],
    )
    disabled = client_disabled_tools(client)
    for wire in UNSAFE_SPELLINGS:
        assert is_tool_disabled(wire, disabled), wire


def test_meta_tool_names_stay_raw() -> None:
    client = ApiClientRecord(
        id="c1",
        label="t",
        created_at="2026-01-01T00:00:00Z",
        token_sha256_hex="0" * 64,
        disabled_tools=["callTool"],
    )
    disabled = client_disabled_tools(client)
    assert is_tool_disabled("callTool", disabled)
    assert not is_tool_disabled("callTool_x", disabled)
    # A composite upstream tool that happens to be named "callTool" is
    # a different key and stays allowed.
    assert not is_tool_disabled("news_server__callTool", disabled)


def test_validator_canonicalizes_and_dedupes() -> None:
    client = ApiClientRecord(
        id="c1",
        label="t",
        created_at="2026-01-01T00:00:00Z",
        token_sha256_hex="0" * 64,
        disabled_tools=[
            "news-server/news_curate",
            "news_server__news_curate",  # same tool, other spelling
            "callTool",
            "  ",
        ],
    )
    assert client.disabled_tools == ["news_server__news_curate", "callTool"]
