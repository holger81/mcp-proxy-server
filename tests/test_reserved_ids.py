"""PR 4.5: `mcp-tools-admin` (and the built-in admin domain id) are reserved.

Registering a user upstream with that id collided with the proxy's own
virtual admin server (`_ADMIN_SERVER_ID` routing in proxy_mcp). The models
now refuse the ids with an explanatory error.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_proxy.domain_store import DomainRecord
from mcp_proxy.models import UpstreamServer


def _server(**kw) -> UpstreamServer:
    base = {"id": "fine", "type": "stdio", "command": ["true"]}
    base.update(kw)
    return UpstreamServer(**base)


def test_reserved_server_id_rejected() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        _server(id="mcp-tools-admin")


def test_similar_server_ids_still_allowed() -> None:
    for sid in ("mcp-tools-admin-x", "x-mcp-tools-admin", "mcp-tools-admin2"):
        assert _server(id=sid).id == sid


def test_reserved_domain_rejected_on_server() -> None:
    with pytest.raises(ValidationError, match="mcp-tools-administration"):
        _server(domain="mcp-tools-administration")


def test_domain_record_reserved_id_rejected() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        DomainRecord(id="mcp-tools-administration", label="dup")


def test_domain_record_normal_id_allowed() -> None:
    assert DomainRecord(id="mcp-tools-admin-x", label="ok").id == "mcp-tools-admin-x"
