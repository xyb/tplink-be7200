"""BE7200Client — thin extension of upstream TPLinkXDRClient.

Upstream `tplinkrouterc6u.client.xdr.TPLinkXDRClient` (>= 5.18.1) handles
auth + read APIs (status, dhcp, ipv4, firmware) and basic wifi enable/disable
for the TL-7DR7270 1.0.18+ firmware. This subclass is the place to add the
extended write APIs that upstream does not cover (PPPoE, port forwarding,
LAN/DHCP server config, IP/MAC bindings beyond simple reservations, guest
network detail config, etc.). See MIGRATION.md for the full coverage map.
"""

from __future__ import annotations

from tplinkrouterc6u.client.xdr import TPLinkXDRClient


class BE7200Client(TPLinkXDRClient):
    """Thin extension over TPLinkXDRClient. Constructor signature matches the
    parent so existing call sites need no changes when they switch over."""

    @classmethod
    def from_cached_stok(cls, host: str, stok: str) -> 'BE7200Client':
        """Build a client around a previously obtained session token.

        Skips the password / authorize handshake entirely — useful when the
        caller already has a valid stok cached on disk from a prior login.
        All read/write methods that go through `_request()` (status, dhcp,
        ipv4 reservations, set_wifi, logout, ...) work immediately because
        they only need `self._stok` + `self._session`.

        If the cached token is stale, the router will reject calls with an
        auth error; the caller is responsible for clearing the cache and
        falling back to a real password authorize.
        """
        client = cls(host, password='')
        client._stok = stok
        return client
