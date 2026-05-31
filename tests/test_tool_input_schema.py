"""Tool inputSchema accepts string forms of booleans and integers."""

from __future__ import annotations

import jsonschema

from mcp_proxy.proxy_mcp import _coerce_bool_arg, build_meta_tool_list
from mcp_proxy.settings import Settings


def _search_tools_schema() -> dict:
    tools = build_meta_tool_list(["news", "default"], Settings())
    st = next(t for t in tools if t.name == "searchToolsForDomain")
    return st.inputSchema


def test_search_tools_schema_accepts_string_boolean() -> None:
    schema = _search_tools_schema()
    validator = jsonschema.Draft7Validator(schema)
    assert validator.is_valid(
        {"domain": "news", "query": "today", "listAll": "False", "offset": 0}
    )
    assert validator.is_valid(
        {"domain": "news", "query": "today", "listAll": False, "offset": "0"}
    )


def test_coerce_bool_string_false() -> None:
    assert _coerce_bool_arg("listAll", "False") is False
    assert _coerce_bool_arg("listAll", "true") is True
