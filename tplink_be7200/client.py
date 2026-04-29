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
    pass
