"""Composite tool-name encoding: round-trip guarantees and self-collision cases.

Auditor follow-up (PR 39): the `serverid__p__<hex>` fallback used to collide
with upstream tools literally named like the fallback (e.g. `p__41`) and with
server ids whose hyphen form contains `__` (e.g. `a--p`). The encoder now
validates every candidate wire name decodes back to the exact pair, so no
input can silently decode to a phantom (server, tool).
"""

from __future__ import annotations

import pytest

from mcp_proxy.tool_names import (
    canonical_tool_key,
    decode_proxy_tool_name,
    encode_proxy_tool_name,
)

ROUND_TRIP_CASES = [
    # plain safe encoding
    ("srv", "tool"),
    ("mcp-tools-admin", "searchTool"),
    ("srv", "UPPER_123"),
    ("srv", "_leading"),
    # names that mimic the hex fallback (the self-collision class)
    ("srv", "p__41"),
    ("srv", "p__deadbeef"),
    ("srv", "p__"),
    ("srv", "p__z"),
    ("srv", "__p__"),
    ("srv", "x__p__6869"),
    ("srv", "41"),
    ("srv", "n__p"),
    # server ids whose underscore form contains "__" or mimics the marker
    ("a--b", "tool"),
    ("a--p", "41"),
    ("a--p", "p__41"),
    ("x--p--y", "tool"),
    # unsafe tool characters force the hex branch
    ("srv", "tool/name"),
    ("srv", "tool with space"),
    ("srv", "wörk"),
    ("srv", "🔧 wrench"),
    ("srv", ""),
]


@pytest.mark.parametrize(("server_id", "tool_name"), ROUND_TRIP_CASES)
def test_encode_decode_round_trip(server_id: str, tool_name: str) -> None:
    wire = encode_proxy_tool_name(server_id, tool_name)
    assert wire == wire.strip()
    assert decode_proxy_tool_name(wire) == (server_id, tool_name)


def test_colliding_pairs_get_distinct_wires() -> None:
    # Before the fix, ("a", "p__41"), ("a--p", "41") and ("a--p", "p__41")
    # all produced `a__p__...` wires that decoded to a phantom pair.
    wires = {
        encode_proxy_tool_name(sid, tool) for sid, tool in ROUND_TRIP_CASES
    }
    assert len(wires) == len(set(ROUND_TRIP_CASES))


def test_hex_lookalike_tool_is_not_read_as_hex_fallback() -> None:
    wire = encode_proxy_tool_name("a", "p__41")
    # Must NOT be the safe form `a__p__41`: that decodes via the hex branch
    # to ("a", "A"). The encoder falls back to the hex of the whole name.
    assert wire == "a__p__" + "p__41".encode("utf-8").hex()
    assert decode_proxy_tool_name(wire) == ("a", "p__41")


def test_server_id_mimicking_the_marker_still_round_trips() -> None:
    # sid "a--p" -> prefix "a__p", so the hex wire carries a second "__p__"
    # right next to the marker. The decoder skips the lookalike (its right
    # side still contains "_") and the hex form round-trips.
    wire = encode_proxy_tool_name("a--p", "41")
    assert wire == "a__p__p__3431"
    assert decode_proxy_tool_name(wire) == ("a--p", "41")


def test_stable_for_normal_names() -> None:
    # Existing production wire names must not change.
    assert encode_proxy_tool_name("news", "curate") == "news__curate"
    assert encode_proxy_tool_name("mcp-tools-admin", "list_tools") == "mcp_tools_admin__list_tools"
    assert encode_proxy_tool_name("srv", "bad name") == "srv__p__" + "bad name".encode().hex()


def test_legacy_spellings_still_decode() -> None:
    # Decoder behaviour is frozen for names already out in the wild.
    assert decode_proxy_tool_name("srv/tool") == ("srv", "tool")
    assert decode_proxy_tool_name("srv__tool") == ("srv", "tool")
    assert decode_proxy_tool_name("a__p__41") == ("a", "A")


def test_encode_raises_when_no_round_trip_form_exists(monkeypatch) -> None:
    """Last-resort legacy form must not emit undecodable wires (empty tool)."""
    import mcp_proxy.tool_names as tn

    def never_round_trips(wire: str, server_id: str, tool_name: str) -> bool:
        return False

    monkeypatch.setattr(tn, "_decodes_exactly", never_round_trips)
    with pytest.raises(ValueError, match="cannot encode"):
        encode_proxy_tool_name("srv", "")


@pytest.mark.parametrize(("server_id", "tool_name"), ROUND_TRIP_CASES)
def test_canonical_key_unifies_spellings(server_id: str, tool_name: str) -> None:
    canonical = canonical_tool_key(encode_proxy_tool_name(server_id, tool_name))
    if tool_name:
        # the legacy "/" spelling can't represent an empty tool name at all
        assert canonical == canonical_tool_key(f"{server_id}/{tool_name}")
    assert canonical_tool_key(canonical) == canonical
