"""BE7200Client — thin extension of upstream TPLinkXDRClient.

Upstream `tplinkrouterc6u.client.xdr.TPLinkXDRClient` (>= 5.18.1) handles
auth + a small read-API surface (status, dhcp, ipv4, firmware) and basic
wifi enable/disable for the TL-7DR7270 1.0.18+ firmware. This subclass
adds the rest of the Web-UI API that upstream does not cover: every
read/write helper that the original `tplink_be7200.api.API` class shipped
with — PPPoE, port forwarding, LAN/DHCP server config, IP/MAC bindings,
guest network detail config, MAC ACL, signal power, scheduled WiFi /
reboot, Wake on LAN, admin lock, full client list, etc.

Every helper here goes through `self._request(...)`, so the upstream
session + cached `_stok` are shared with c6u's own methods and the two
APIs interoperate transparently.

See MIGRATION.md for the per-command coverage map.
"""

from __future__ import annotations

from typing import Callable, Optional
from urllib.parse import quote, unquote

from tplinkrouterc6u.client.xdr import TPLinkXDRClient
from tplinkrouterc6u.common.exception import ClientException


class BE7200ApiError(RuntimeError):
    """Raised when a router request returns a non-zero `error_code`."""

    def __init__(self, code: int, body: dict, request: dict):
        self.code = code
        self.body = body
        self.request = request
        super().__init__(f"API error_code={code}: {body} (req={request})")


class BE7200Client(TPLinkXDRClient):
    """Thin extension over TPLinkXDRClient with the full Web-UI surface
    we need. Constructor signature matches the parent so existing call
    sites need no changes when they switch over."""

    # Sentinel set inside _request while a re-auth attempt is in flight,
    # so a -40401 raised by authorize() itself does not feed back into
    # another retry (would loop on a wrong saved password forever).
    _refreshing: bool = False

    @classmethod
    def from_cached_stok(
        cls,
        host: str,
        stok: str,
        *,
        password: Optional[str] = None,
        on_token_refresh: Optional[Callable[[str], None]] = None,
    ) -> 'BE7200Client':
        """Build a client around a previously obtained session token.

        Skips the password / authorize handshake entirely. If
        ``password`` is also provided, ``_request()`` will silently
        re-authorize and retry the call when the router rejects a
        request with -40401 (stok expired). The optional
        ``on_token_refresh`` callback receives the new stok so the
        caller can persist it back to its credential store.
        """
        client = cls(host, password=password or '')
        client._stok = stok
        client._on_token_refresh = on_token_refresh
        return client

    def _request(self, payload: dict) -> dict:
        """Wrap upstream _request with stok-expiry auto-refresh.

        Behaviour:
          - First call goes through unchanged.
          - If the response carries error_code=-40401 *and* we have a
            saved password *and* we are not already in the middle of a
            refresh, re-authorize (one retry) and resend the original
            payload.
          - The recursion guard ``_refreshing`` ensures that a -40401
            raised during the refresh authorize itself does not retry
            again — we re-raise upstream's ClientException so the user
            sees the real cause.

        Note: upstream returns the JSON dict on success and raises
        ClientException on transport / decode failures. Auth failures
        come back as a normal dict with error_code != 0; we sniff that
        instead of trying to catch a specific exception subclass.
        """
        result = super()._request(payload)
        if (
            isinstance(result, dict)
            and result.get('error_code') == -40401
            and self.password
            and not self._refreshing
        ):
            self._refreshing = True
            try:
                self.authorize()
            finally:
                self._refreshing = False
            cb = getattr(self, '_on_token_refresh', None)
            if cb is not None:
                try:
                    cb(self._stok)
                except Exception:
                    # Persistence failure must not break the request.
                    pass
            result = super()._request(payload)
        return result

    # ========================================================================
    # Low-level request helpers (mirror the original api.API surface)
    # ========================================================================

    def call(self, body: dict, ok_codes: tuple = (0,)) -> dict:
        """Raw call. `ok_codes` are error_codes that should not raise
        (e.g. `delete` also accepts -40205 = "entry not found")."""
        resp = self._request(body)
        code = resp.get("error_code", -999)
        if code not in ok_codes:
            raise BE7200ApiError(code, resp, body)
        return resp

    def get(self, module: str, name=None, table=None) -> dict:
        body: dict = {module: {}, "method": "get"}
        if name is not None:
            body[module]["name"] = name
        if table is not None:
            body[module]["table"] = table
        return self.call(body)

    def set(self, module: str, sec: str, fields: dict) -> dict:
        return self.call({module: {sec: fields}, "method": "set"})

    def add(self, module: str, table: str, name: str, para: dict) -> dict:
        return self.call({
            module: {"table": table, "name": name, "para": para},
            "method": "add",
        })

    def delete(self, module: str, table: str, name: str) -> dict:
        return self.call(
            {module: {"table": table, "name": name}, "method": "delete"},
            ok_codes=(0, -40205),
        )

    def do(self, module: str, action: dict) -> dict:
        return self.call({module: action, "method": "do"})

    # ========================================================================
    # LAN / WAN
    # ========================================================================

    def get_lan(self) -> dict:
        return self.get("network", name="lan")["network"]["lan"]

    def set_lan_ip(self, ipaddr: str, netmask: str = "255.255.255.0") -> None:
        # The router uses ip_mode="manual" (not "static"); see the LAN GET response.
        self.set("network", "lan", {"ipaddr": ipaddr, "netmask": netmask, "ip_mode": "manual"})

    def set_lan_and_dhcp(
        self,
        lan_ip: str,
        netmask: str = "255.255.255.0",
        dhcp_pool_start: str | None = None,
        dhcp_pool_end: str | None = None,
        dhcp_lease: int | str | None = None,
    ) -> None:
        """Atomically update LAN IP and DHCP pool, so subsequent calls
        don't fail because the pool no longer matches the new LAN segment."""
        body: dict = {
            "network": {"lan": {"ipaddr": lan_ip, "netmask": netmask, "ip_mode": "manual"}},
        }
        udhcpd: dict = {}
        if dhcp_pool_start is not None:
            udhcpd["pool_start"] = dhcp_pool_start
        if dhcp_pool_end is not None:
            udhcpd["pool_end"] = dhcp_pool_end
        if dhcp_lease is not None:
            udhcpd["lease_time"] = str(dhcp_lease)
        if udhcpd:
            body["dhcpd"] = {"udhcpd": udhcpd}
        body["method"] = "set"
        self.call(body)

    def get_wan(self) -> dict:
        return self.get("protocol", name="wan")["protocol"]["wan"]

    def get_wan_status(self) -> dict:
        return self.get("network", name="wan_status")["network"]["wan_status"]

    # ========================================================================
    # DHCP server
    # ========================================================================

    def get_dhcp_server(self) -> dict:
        return self.get("dhcpd", name="udhcpd")["dhcpd"]["udhcpd"]

    def set_dhcp_server(
        self,
        enable: str | None = None,
        pool_start: str | None = None,
        pool_end: str | None = None,
        lease_time: str | int | None = None,
        pri_dns: str | None = None,
        snd_dns: str | None = None,
    ) -> None:
        fields = {}
        if enable is not None:
            fields["enable"] = str(enable)
        if pool_start is not None:
            fields["pool_start"] = pool_start
        if pool_end is not None:
            fields["pool_end"] = pool_end
        if lease_time is not None:
            fields["lease_time"] = str(lease_time)
        if pri_dns is not None:
            fields["pri_dns"] = pri_dns
        if snd_dns is not None:
            fields["snd_dns"] = snd_dns
        if not fields:
            raise ValueError("at least one field is required")
        self.set("dhcpd", "udhcpd", fields)

    # ========================================================================
    # IP / MAC static bindings
    # ========================================================================

    def list_bindings(self) -> list[dict]:
        """Return [{name: 'user_bind_1', mac, ip, hostname}, ...]."""
        resp = self.get("ip_mac_bind", table="user_bind")["ip_mac_bind"]
        out = []
        for item in resp.get("user_bind", []):
            for name, fields in item.items():
                out.append({"name": name, **fields})
        return out

    def add_binding(self, ip: str, mac: str, hostname: str = "", index: int | None = None) -> str:
        """Add one binding, returning `user_bind_<index>`. `index` defaults
        to len(bindings) + 1."""
        if index is None:
            index = len(self.list_bindings()) + 1
        name = f"user_bind_{index}"
        mac = mac.lower().replace(":", "-")
        self.add(
            "ip_mac_bind",
            table="user_bind",
            name=name,
            para={"ip": ip, "mac": mac, "hostname": hostname},
        )
        return name

    def delete_binding(self, name: str) -> None:
        """Delete one binding by name.

        Earlier versions sent ``{"ip_mac_bind":{"table":"user_bind",
        "name":"user_bind_X"},"method":"delete"}`` which the TL-7DR7270
        firmware misinterprets as "delete every row" (verified
        2026-05-03 by capturing the live web UI's payload). The web UI
        actually sends ``{"ip_mac_bind":{"name":["user_bind_X"]},
        "method":"delete"}`` — *no* ``table`` field, and ``name`` must
        be a list. This method matches that wire format.
        """
        body = {"ip_mac_bind": {"name": [name]}, "method": "delete"}
        # -40205 = "entry not found" is treated as success (idempotent
        # delete), matching the broader call() / delete() convention.
        self.call(body, ok_codes=(0, -40205))

    def delete_bindings(self, names: list[str]) -> None:
        """Delete multiple bindings in a single round-trip.

        Mirrors the web UI's "删除所选" (delete selected) payload, which
        passes all checked rows in one ``name: [...]`` list.
        """
        body = {"ip_mac_bind": {"name": list(names)}, "method": "delete"}
        self.call(body, ok_codes=(0, -40205))

    def update_binding(
        self, name: str, *, ip: str, mac: str, hostname: str = ""
    ) -> None:
        """In-place update of one binding (web UI's "编辑 → 保存" flow).

        The web UI sends ``{"ip_mac_bind":{"<name>":{"ip","mac",
        "hostname"}},"method":"set"}`` — note that the binding name is
        the *key* under ``ip_mac_bind``, not a ``name`` field. This is
        the only way to rename or fix up one binding without involving
        delete + add (which would still work, but two round-trips for
        no reason).
        """
        body = {
            "ip_mac_bind": {name: {
                "ip": ip,
                "mac": mac.lower().replace(":", "-"),
                "hostname": hostname,
            }},
            "method": "set",
        }
        self.call(body)

    def clear_bindings(self) -> int:
        """Delete every binding by listing them and sending one bulk
        ``delete`` request. Returns the number of bindings removed."""
        names = [b["name"] for b in self.list_bindings()]
        if names:
            self.delete_bindings(names)
        return len(names)

    def rebuild_bindings(self, records: list[dict]) -> int:
        """Replace the entire ip_mac_bind/user_bind table with ``records``.

        Listed names are deleted in one bulk request, then ``records``
        are added in order. Returns the number of bindings added.

        Each record is a dict with ``ip``, ``mac``, and optional
        ``hostname``. Caller is responsible for ensuring ``records``
        is the desired final state.
        """
        old_names = [b["name"] for b in self.list_bindings()]
        if old_names:
            self.delete_bindings(old_names)
        added = 0
        for r in records:
            self.add_binding(
                ip=r["ip"], mac=r["mac"],
                hostname=r.get("hostname", ""),
            )
            added += 1
        return added

    def get_arp(self) -> list[dict]:
        """Return the system ARP table (auto-learned IP/MAC pairs, not bindings)."""
        resp = self.get("ip_mac_bind", table="sys_arp")["ip_mac_bind"]
        out = []
        for item in resp.get("sys_arp", []):
            for name, fields in item.items():
                out.append({"name": name, **fields})
        return out

    # ========================================================================
    # Connected clients
    # ========================================================================

    def list_clients(self, include_offline: bool = False) -> list[dict]:
        """List devices connected to the router.

        Fields include: mac / ip / ipv6 / hostname / up_speed /
        down_speed (bytes/s) / online_time (seconds) / phy_mode /
        wifi_mode / blocked / etc. `hostname` is auto URL-decoded (the
        router returns percent-encoded UTF-8).
        """
        table = "host_info" if include_offline else "online_host"
        resp = self.get("hosts_info", table=table)["hosts_info"]
        out = []
        for item in resp.get(table, []):
            for name, fields in item.items():
                fields = dict(fields)
                if "hostname" in fields and fields["hostname"]:
                    try:
                        fields["hostname"] = unquote(fields["hostname"])
                    except Exception:
                        pass
                out.append({"_entry": name, **fields})
        return out

    # ========================================================================
    # WiFi (host SSID)
    # ========================================================================

    def get_wifi(self) -> dict:
        """Return {2g: {ssid, key, ...}, 5g: {...}}."""
        resp = self.get("wireless", name=["wlan_host_2g", "wlan_host_5g"])["wireless"]
        return {"2g": resp["wlan_host_2g"], "5g": resp["wlan_host_5g"]}

    def set_wifi_2g(self, ssid: str | None = None, key: str | None = None) -> None:
        fields = {}
        if ssid is not None:
            fields["ssid"] = ssid
        if key is not None:
            fields["key"] = key
        if fields:
            self.set("wireless", "wlan_host_2g", fields)

    def set_wifi_5g(self, ssid: str | None = None, key: str | None = None) -> None:
        fields = {}
        if ssid is not None:
            fields["ssid"] = ssid
        if key is not None:
            fields["key"] = key
        if fields:
            self.set("wireless", "wlan_host_5g", fields)

    # ========================================================================
    # Guest network
    # ========================================================================

    def get_guest(self) -> dict:
        """Return {2g: {...}, 5g: {...} or None if absent,
        time_left_2g, time_left_5g (seconds)}.

        On TL-7DR7270 only `guest_2g` exists (a single SSID covering both
        bands); the `guest_5g` endpoint returns -40101.
        """
        out: dict = {"2g": None, "5g": None, "time_left_2g": None, "time_left_5g": None}
        try:
            out["2g"] = self.get("guest_network", name="guest_2g")["guest_network"]["guest_2g"]
        except BE7200ApiError:
            pass
        try:
            out["5g"] = self.get("guest_network", name="guest_5g")["guest_network"]["guest_5g"]
        except BE7200ApiError:
            pass
        try:
            out["time_left_2g"] = int(self.get("guest_network", name="guest_left_2g")
                                      ["guest_network"]["guest_left_2g"]["time_left"])
        except (BE7200ApiError, KeyError, ValueError):
            pass
        try:
            out["time_left_5g"] = int(self.get("guest_network", name="guest_left_5g")
                                      ["guest_network"]["guest_left_5g"]["time_left"])
        except (BE7200ApiError, KeyError, ValueError):
            pass
        return out

    def set_guest(
        self,
        enable: bool | None = None,
        ssid: str | None = None,
        key: str | None = None,
        encrypt: str | None = None,
        upload: int | str | None = None,
        download: int | str | None = None,
        accright: str | None = None,
        band: str = "2g",
    ) -> None:
        """Update the guest network.

        - encrypt: "0" = open, "3" = WPA2/3 PSK (verified)
        - upload / download: rate limit in KB/s, "0" = unlimited
        - accright: "0" / "1", whether guests can talk to each other
        - band: "2g" | "5g"
        """
        sec = "guest_2g" if band == "2g" else "guest_5g"
        fields: dict = {}
        if enable is not None:
            fields["enable"] = "1" if enable else "0"
        if ssid is not None:
            fields["ssid"] = ssid
        if key is not None:
            fields["key"] = key
        if encrypt is not None:
            fields["encrypt"] = str(encrypt)
        if upload is not None:
            fields["upload"] = str(upload)
        if download is not None:
            fields["download"] = str(download)
        if accright is not None:
            fields["accright"] = str(accright)
        if not fields:
            raise ValueError("at least one field is required")
        self.set("guest_network", sec, fields)

    def enable_guest(self, band: str = "2g") -> None:
        self.set_guest(enable=True, band=band)

    def disable_guest(self, band: str = "2g") -> None:
        self.set_guest(enable=False, band=band)

    # ========================================================================
    # PPPoE / WAN
    # ========================================================================

    def get_pppoe(self) -> dict:
        return self.get("protocol", name="pppoe")["protocol"]["pppoe"]

    def set_pppoe(
        self,
        username: str,
        password: str,
        mtu: int = 1492,
        conn_mode: str = "auto",
    ) -> None:
        """Set PPPoE credentials and switch wan_type to pppoe."""
        self.set("protocol", "pppoe", {
            "username": username,
            "password": password,
            "mtu": str(mtu),
            "conn_mode": conn_mode,
        })
        self.set("protocol", "wan", {"wan_type": "pppoe"})

    def set_wan_dhcp(self) -> None:
        """Switch WAN back to DHCP (e.g. when sitting behind another router for testing)."""
        self.set("protocol", "wan", {"wan_type": "dhcp"})

    # ========================================================================
    # Port forwarding / DMZ / DDNS / UPnP
    # ========================================================================

    def list_port_forwards(self) -> list[dict]:
        resp = self.get("firewall", table="redirect")["firewall"]
        out = []
        for item in resp.get("redirect", []):
            for name, fields in item.items():
                out.append({"name": name, **fields})
        return out

    def add_port_forward(
        self,
        dest_ip: str,
        dest_port: int | str,
        src_port_start: int | str | None = None,
        src_port_end: int | str | None = None,
        proto: str = "TCP",
        index: int | None = None,
    ) -> str:
        """Port forward: WAN port range -> internal IP:port. proto: TCP / UDP / TCP_UDP."""
        if src_port_start is None:
            src_port_start = dest_port
        if src_port_end is None:
            src_port_end = src_port_start
        if index is None:
            index = len(self.list_port_forwards()) + 1
        name = f"redirect_{index}"
        para = {
            "dest_ip": dest_ip,
            "dest_port": str(dest_port),
            "src_dport_start": str(src_port_start),
            "src_dport_end": str(src_port_end),
            "proto": proto,
        }
        self.add("firewall", table="redirect", name=name, para=para)
        return name

    def delete_port_forward(self, name: str) -> None:
        self.delete("firewall", table="redirect", name=name)

    def clear_port_forwards(self, max_scan: int = 100) -> None:
        for i in range(1, max_scan + 1):
            self.delete_port_forward(f"redirect_{i}")

    def get_dmz(self) -> dict:
        return self.get("firewall", name="dmz")["firewall"]["dmz"]

    def set_dmz(self, enable: bool, dest_ip: str | None = None) -> None:
        fields = {"enable": "1" if enable else "0"}
        if dest_ip is not None:
            fields["dest_ip"] = dest_ip
        self.set("firewall", "dmz", fields)

    def get_ddns(self) -> dict:
        return self.get("ddns", name="phddns")["ddns"]["phddns"]

    def set_ddns(self, username: str, password: str, auto_login: bool = True) -> None:
        self.set("ddns", "phddns", {
            "username": username,
            "password": password,
            "auto_login": "1" if auto_login else "0",
        })

    def get_upnp(self) -> dict:
        return self.get("upnpd", name="config")["upnpd"]["config"]

    def set_upnp(self, enable: bool) -> None:
        self.set("upnpd", "config", {"enable_upnp": "1" if enable else "0"})

    def list_upnp_leases(self) -> list[dict]:
        resp = self.get("upnpd", table="upnp_lease")["upnpd"]
        out = []
        for item in resp.get("upnp_lease", []):
            for name, fields in item.items():
                out.append({"name": name, **fields})
        return out

    # ========================================================================
    # AP isolation
    # ========================================================================

    def get_ap_isolate(self) -> dict:
        wifi = self.get_wifi()
        return {
            "2g": wifi["2g"].get("isolate") == "1",
            "5g": wifi["5g"].get("isolate") == "1",
        }

    def set_ap_isolate(self, isolate: bool, band: str = "both") -> None:
        v = "1" if isolate else "0"
        if band in ("2g", "both"):
            self.set("wireless", "wlan_host_2g", {"isolate": v})
        if band in ("5g", "both"):
            self.set("wireless", "wlan_host_5g", {"isolate": v})

    # ========================================================================
    # WiFi on/off scheduler
    # ========================================================================

    def get_wifi_timer(self) -> dict:
        gen = self.get("time_switch", name="general")["time_switch"]["general"]
        rules = self.get("time_switch", table="time_switch")["time_switch"]
        rule_list = []
        for item in rules.get("time_switch", []):
            for name, fields in item.items():
                rule_list.append({"name": name, **fields})
        return {"enable": gen.get("enable") == "1", "rules": rule_list}

    def set_wifi_timer_enable(self, enable: bool) -> None:
        self.set("time_switch", "general", {"enable": "1" if enable else "0"})

    def add_wifi_timer_rule(
        self,
        start: str,
        end: str,
        days: list[str] | None = None,
        name: str | None = None,
        index: int | None = None,
    ) -> str:
        days = days or ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        days_set = {d.lower() for d in days}
        if index is None:
            index = len(self.get_wifi_timer()["rules"]) + 1
        sec_name = f"time_switch_{index}"
        para = {
            "name": name or f"rule_{index}",
            "start_time": start,
            "end_time": end,
            "enable": "1",
        }
        for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
            para[d] = "1" if d in days_set else "0"
        self.add("time_switch", table="time_switch", name=sec_name, para=para)
        return sec_name

    def delete_wifi_timer_rule(self, name: str) -> None:
        self.delete("time_switch", table="time_switch", name=name)

    # ========================================================================
    # Scheduled reboot
    # ========================================================================

    def get_reboot_timer(self) -> dict:
        st = self.get("reboot_timer", name="reboot_timer_status")["reboot_timer"]["reboot_timer_status"]
        rules = self.get("reboot_timer", table="reboot_timer_rule")["reboot_timer"]
        rule_list = []
        for item in rules.get("reboot_timer_rule", []):
            for name, fields in item.items():
                rule_list.append({"name": name, **fields})
        return {"enable": st.get("enable") == "1", "rules": rule_list}

    def set_reboot_timer_enable(self, enable: bool) -> None:
        self.set("reboot_timer", "reboot_timer_status", {"enable": "1" if enable else "0"})

    def add_reboot_timer_rule(
        self,
        reboot_time: str,
        days: list[str] | None = None,
        name: str | None = None,
        index: int | None = None,
    ) -> str:
        days = days or ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        days_set = {d.lower() for d in days}
        if index is None:
            index = len(self.get_reboot_timer()["rules"]) + 1
        sec_name = f"reboot_timer_rule_{index}"
        para = {
            "name": name or f"rule_{index}",
            "reboot_time": reboot_time,
            "enable": "1",
        }
        for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
            para[d] = "1" if d in days_set else "0"
        self.add("reboot_timer", table="reboot_timer_rule", name=sec_name, para=para)
        return sec_name

    def delete_reboot_timer_rule(self, name: str) -> None:
        self.delete("reboot_timer", table="reboot_timer_rule", name=name)

    # ========================================================================
    # Wake on LAN
    # ========================================================================

    def list_wol_devices(self) -> list[dict]:
        resp = self.get("wake_on_lan", table="device_list")["wake_on_lan"]
        out = []
        for item in resp.get("device_list", []):
            for name, fields in item.items():
                out.append({"name": name, **fields})
        return out

    def wake(self, mac: str) -> dict:
        return self.do("wake_on_lan", {"wake_device": {"mac": mac.lower().replace(":", "-")}})

    # ========================================================================
    # Admin lock
    # ========================================================================

    def get_admin_lock(self) -> dict:
        cfg = self.get("firewall", name="lan_manage")["firewall"]["lan_manage"]
        macs = [cfg.get(f"mac{i}") for i in range(1, 5)]
        macs = [m for m in macs if m and m != "00-00-00-00-00-00"]
        return {"enable_all": cfg.get("enable_all") == "1", "allowed_macs": macs}

    def set_admin_lock(self, enable_all: bool, allowed_macs: list[str] | None = None) -> None:
        macs = (allowed_macs or [])[:4]
        macs = [m.lower().replace(":", "-") for m in macs]
        while len(macs) < 4:
            macs.append("00-00-00-00-00-00")
        fields = {"enable_all": "1" if enable_all else "0"}
        for i, m in enumerate(macs, 1):
            fields[f"mac{i}"] = m
        self.set("firewall", "lan_manage", fields)

    # ========================================================================
    # Signal strength (wireless_power)
    # ========================================================================

    POWER_LEVELS = {"boost": "0", "high": "0", "normal": "1", "middle": "1", "saving": "2", "low": "2"}

    def get_signal_power(self) -> dict:
        wifi = self.get_wifi()
        try:
            avail = self.get("wireless_power", name="power_info")["wireless_power"]["power_info"]["power_list"]
        except (BE7200ApiError, KeyError):
            avail = ["low", "middle", "high"]
        return {"2g": wifi["2g"].get("power", "0"), "5g": wifi["5g"].get("power", "0"), "available": avail}

    def set_signal_power(self, level: str, band: str = "both") -> None:
        v = self.POWER_LEVELS.get(level.lower())
        if v is None:
            raise ValueError(f"unknown level {level!r}; use one of: {sorted(self.POWER_LEVELS)}")
        body: dict = {}
        if band in ("2g", "both"):
            body["wlan_host_2g"] = {"power": v}
        if band in ("5g", "both"):
            body["wlan_host_5g"] = {"power": v}
        self.call({"wireless": body, "method": "set"})

    # ========================================================================
    # Wireless access control (MAC ACL)
    # ========================================================================

    def get_mac_acl(self) -> dict:
        """Return {enable, white_list}."""
        cfg = self.get("wlan_access", name="config")["wlan_access"]["config"]
        try:
            wl_raw = self.do("wlan_access", {"get_white_list": {}})["wlan_access"].get("white_list", [])
        except (BE7200ApiError, KeyError):
            wl_raw = []

        wl = []
        for item in wl_raw or []:
            if not isinstance(item, dict):
                continue
            for sec_name, fields in item.items():
                if not isinstance(fields, dict):
                    continue
                row = {"section": sec_name}
                for k, v in fields.items():
                    if k == "name" and isinstance(v, str) and "%" in v:
                        row["hostname"] = unquote(v)
                    elif k == "name":
                        row["hostname"] = v
                    else:
                        row[k] = v
                wl.append(row)

        return {"enable": cfg.get("enable", "0"), "white_list": wl}

    def set_mac_acl_mode(self, mode: str) -> None:
        m = {"off": "0", "whitelist": "1"}.get(mode.lower())
        if m is None:
            raise ValueError("mode must be 'off' or 'whitelist'")
        self.set("wlan_access", "config", {"enable": m})

    def add_mac_acl(self, mac: str, hostname: str = "", index: int | None = None) -> str:
        current = self.get_mac_acl()["white_list"]
        mac_norm = mac.lower().replace(":", "-")
        for r in current:
            if r.get("mac", "").lower() == mac_norm:
                return r.get("section", "")
        if index is None:
            taken = {int(r.get("section", "white_list_0").rsplit("_", 1)[-1])
                     for r in current if r.get("section", "").startswith("white_list_")}
            i = 1
            while i in taken:
                i += 1
            index = i
        sec = f"white_list_{index}"
        para = {"mac": mac_norm, "name": quote(hostname or "")}
        self.add("wlan_access", table="white_list", name=sec, para=para)
        return sec

    def delete_mac_acl(self, mac: str | None = None, section: str | None = None) -> None:
        if section is None:
            if mac is None:
                raise ValueError("provide mac or section")
            mac_norm = mac.lower().replace(":", "-")
            for r in self.get_mac_acl()["white_list"]:
                if r.get("mac", "").lower() == mac_norm:
                    section = r["section"]
                    break
            if section is None:
                return
        self.delete("wlan_access", table="white_list", name=section)

    def clear_mac_acl(self) -> None:
        for r in self.get_mac_acl()["white_list"]:
            self.delete("wlan_access", table="white_list", name=r["section"])

    # ========================================================================
    # System
    # ========================================================================

    def get_device_info(self) -> dict:
        return self.get("system", name="device_info")["system"]["device_info"]

    def get_sys_mode(self) -> dict:
        return self.get("system", name="sys_mode")["system"]["sys_mode"]

    # ========================================================================
    # Full export
    # ========================================================================

    def export_all(self) -> dict:
        """Dump every readable module into a single dict, useful for
        backups and config diffs."""
        out = {}
        sources = [
            ("lan", self.get_lan),
            ("wan", self.get_wan),
            ("wan_status", self.get_wan_status),
            ("dhcp_server", self.get_dhcp_server),
            ("pppoe", self.get_pppoe),
            ("wifi", self.get_wifi),
            ("bindings", self.list_bindings),
            ("port_forwards", self.list_port_forwards),
            ("dmz", self.get_dmz),
            ("ddns", self.get_ddns),
            ("upnp", self.get_upnp),
            ("device_info", self.get_device_info),
            ("sys_mode", self.get_sys_mode),
        ]
        for key, fn in sources:
            try:
                out[key] = fn()
            except BE7200ApiError as e:
                out[key] = {"error": str(e)}
        return out
