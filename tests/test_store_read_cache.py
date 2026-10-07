"""PLAN 6.1: mtime-invalidated parse cache in the three config stores.

Hot paths (bearer resolution per HTTP request, MCP ``tools/list``) must not
re-read + re-parse the JSON files every call, while external edits and store
writes must still become visible (revocation freshness).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mcp_proxy.client_store import ClientTokenStore
from mcp_proxy.config_store import ServerConfigStore
from mcp_proxy.domain_store import DomainRecord, DomainStore
from mcp_proxy.models import UpstreamServer


def _count_parses(store) -> list[Path]:
    """Instrument a store's cache: list of paths passed to ``parse``."""
    cache = store._cache  # AttributeError on main -> these tests are new-API
    original = cache._parse
    calls: list[Path] = []

    def parse(path: Path):
        calls.append(path)
        return original(path)

    cache._parse = parse
    return calls


def _touch_later(path: Path, text: str) -> None:
    """Rewrite file content and force a fresh mtime signature."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


# --- cache mechanics -------------------------------------------------------


def test_repeat_reads_parse_only_once(tmp_path):
    store = ServerConfigStore(tmp_path)
    store.add(UpstreamServer(id="a", type="stdio", command=["x"]))
    calls = _count_parses(store)
    store.list_servers()
    store.list_servers()
    store.get("a")
    assert len(calls) == 1


def test_external_edit_is_detected(tmp_path):
    store = ServerConfigStore(tmp_path)
    store.add(UpstreamServer(id="a", type="stdio", command=["x"]))
    assert len(store.list_servers()) == 1
    _touch_later(
        store.path,
        json.dumps(
            {
                "servers": [
                    {"id": "a", "type": "stdio", "command": ["x"]},
                    {"id": "b", "type": "http", "url": "http://h/mcp"},
                ]
            }
        ),
    )
    assert {s.id for s in store.list_servers()} == {"a", "b"}


def test_missing_file_then_created(tmp_path):
    store = ServerConfigStore(tmp_path)
    assert store.list_servers() == []
    assert store.list_servers() == []  # "missing" state cached, still cheap
    _touch_later(
        store.path,
        json.dumps({"servers": [{"id": "a", "type": "stdio", "command": ["x"]}]}),
    )
    assert [s.id for s in store.list_servers()] == ["a"]


def test_empty_file_still_means_empty_doc(tmp_path):
    store = ServerConfigStore(tmp_path)
    _touch_later(store.path, "   ")
    assert store.list_servers() == []


def test_corrupt_json_is_not_cached(tmp_path):
    """Fail closed: a broken file keeps raising until it is fixed (PLAN 4.3)."""
    store = ServerConfigStore(tmp_path)
    _touch_later(store.path, "{not json")
    with pytest.raises(json.JSONDecodeError):
        store.list_servers()
    with pytest.raises(json.JSONDecodeError):
        store.list_servers()
    _touch_later(store.path, json.dumps({"servers": []}))
    assert store.list_servers() == []


# --- invalidation on own writes -------------------------------------------


def test_store_writes_invalidate_config_store(tmp_path):
    store = ServerConfigStore(tmp_path)
    calls = _count_parses(store)
    store.add(UpstreamServer(id="a", type="stdio", command=["x"]))
    assert len(store.list_servers()) == 1
    store.add(UpstreamServer(id="b", type="stdio", command=["y"]))
    assert len(store.list_servers()) == 2
    # parses: add(a) [missing], list after create-write, final list after
    # add(b)'s write — add(b)'s own read hits the cache add(a)'s list primed:
    assert len(calls) == 3


def test_bearer_revocation_via_external_edit(tmp_path):
    """Security-relevant freshness: removing a client externally must bite."""
    store = ClientTokenStore(tmp_path)
    _record, token = store.create("alpha")
    assert store.resolve_bearer(token) is not None
    _count_parses(store)  # reset instrumentation after create's internal reads
    _touch_later(store._path, json.dumps({"clients": []}))
    assert store.resolve_bearer(token) is None


def test_client_store_writes_invalidate(tmp_path):
    store = ClientTokenStore(tmp_path)
    _record, token = store.create("alpha")
    assert store.update("missing", label="x") is None
    _count_parses(store)
    assert store.resolve_bearer(token) is not None
    assert store.remove(_record.id) is True
    assert store.resolve_bearer(token) is None


# --- handed-out records are isolated from the cached document -------------


def test_config_store_records_are_copies(tmp_path):
    store = ServerConfigStore(tmp_path)
    store.add(UpstreamServer(id="a", type="stdio", command=["x"], enabled=True))
    got = store.get("a")
    assert got is not None
    got.enabled = False
    assert store.get("a").enabled is True
    listed = store.list_servers()[0]
    listed.enabled = False
    assert store.get("a").enabled is True


def test_domain_store_records_are_copies(tmp_path):
    store = DomainStore(tmp_path)
    store.add(DomainRecord(id="smart-home", label="Smart Home"))
    got = store.get("smart-home")
    assert got is not None
    got.label = "Mutated"
    assert store.get("smart-home").label == "Smart Home"
    assert [d.label for d in store.list_records()] == ["Smart Home"]


def test_client_store_records_are_copies(tmp_path):
    store = ClientTokenStore(tmp_path)
    record, token = store.create("alpha")
    got = store.resolve_bearer(token)
    assert got is not None
    got.label = "Mutated"
    got.can_admin = True
    fresh = store.get(record.id)
    assert fresh is not None
    assert fresh.label == "alpha"
    assert fresh.can_admin is False


def test_update_fields_mutates_a_copy(tmp_path):
    """PR 5.9 CAS + PLAN 6.1: a sloppy in-place mutate must not poison the cache."""
    store = ServerConfigStore(tmp_path)
    store.add(UpstreamServer(id="a", type="stdio", command=["x"], enabled=True))

    def sloppy(server: UpstreamServer) -> UpstreamServer:
        server.enabled = False  # in-place on the copy handed to mutate
        return server

    store.update_fields("a", sloppy)
    assert store.get("a").enabled is False
