"""The single owner of host-level loopback trust.

Both boundaries that refuse a non-loopback host -- the MCP door and the billed-
provider serve bind -- delegate to `is_loopback_host`, so its contract is
pinned once here.
"""

from __future__ import annotations

import pytest

from atelier2.host.address import is_loopback_host


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "[::1]"])
def test_a_literal_loopback_address_is_trusted(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize(
    "host",
    ["localhost", "cockpit.example", "0.0.0.0", "::", "192.168.1.10", "not-an-ip", ""],
)
def test_a_name_or_non_loopback_or_malformed_host_is_refused(host: str) -> None:
    assert not is_loopback_host(host)
