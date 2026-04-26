"""TL-7DR7270 Web API wrapper.

Request format: POST http://<host>/stok=<token>/ds, body is plain JSON.
Five methods: get / set / add / delete / do.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class ApiError(RuntimeError):
    def __init__(self, code: int, body: dict, request: dict):
        self.code = code
        self.body = body
        self.request = request
        super().__init__(f"API error_code={code}: {body} (req={request})")


class API:
    def __init__(self, token: str, host: str = "192.168.1.1", timeout: float = 8.0):
        if not token:
            raise ValueError(
                "token is required; copy stok=XXX from the browser address bar after login"
            )
        self.token = token
        self.host = host
        self.timeout = timeout
        self.url = f"http://{host}/stok={token}/ds"

    # ========================================================================
    # Low-level
    # ========================================================================

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.URLError as e:
            raise ApiError(-1, {"network_error": str(e)}, body) from e
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            try:
                preview = raw.decode("gbk", errors="replace")[:200]
            except Exception:
                preview = repr(raw[:200])
            raise ApiError(
                -2,
                {
                    "decode_error": str(e),
                    "hint": "router returned non-JSON (HTML setup page? token expired? host wrong?)",
                    "body_preview": preview,
                },
                body,
            ) from e

    def call(self, body: dict, ok_codes: tuple = (0,)) -> dict:
        """Raw call. `ok_codes` are error_codes that should not raise
        (e.g. `delete` also accepts -40205 = "entry not found")."""
        resp = self._post(body)
        code = resp.get("error_code", -999)
        if code not in ok_codes:
            raise ApiError(code, resp, body)
        return resp

    # ========================================================================
    # The five methods
    # ========================================================================

    def get(self, module: str, name: str | list | None = None, table: str | list | None = None) -> dict:
        body: dict = {module: {}, "method": "get"}
        if name is not None:
            body[module]["name"] = name
        if table is not None:
            body[module]["table"] = table
        return self.call(body)

    def set(self, module: str, sec: str, fields: dict) -> dict:
        """Update fields of a single-instance section, e.g. set
        `dhcpd.udhcpd.lease_time`."""
        return self.call({module: {sec: fields}, "method": "set"})

    def add(self, module: str, table: str, name: str, para: dict) -> dict:
        """Add a row to a table. `name` must be of the form `<table>_<index>`."""
        return self.call({
            module: {"table": table, "name": name, "para": para},
            "method": "add",
        })

    def delete(self, module: str, table: str, name: str) -> dict:
        """Delete the row with the given `name`. -40205 ("entry not found")
        is treated as success."""
        return self.call(
            {module: {"table": table, "name": name}, "method": "delete"},
            ok_codes=(0, -40205),
        )

    def do(self, module: str, action: dict) -> dict:
        """Trigger an action (neither read nor write)."""
        return self.call({module: action, "method": "do"})

    # ========================================================================
    # High-level helpers, grouped by module
    # ========================================================================

    # --- LAN / WAN ---

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

    # --- DHCP server ---

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

    # --- IP / MAC static bindings ---

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
        self.delete("ip_mac_bind", table="user_bind", name=name)

    def clear_bindings(self, max_scan: int = 200) -> int:
        """Delete every static binding; returns the highest scanned index."""
        for i in range(1, max_scan + 1):
            self.delete_binding(f"user_bind_{i}")
        return max_scan

    def get_arp(self) -> list[dict]:
        """Return the system ARP table (auto-learned IP/MAC pairs, not bindings)."""
        resp = self.get("ip_mac_bind", table="sys_arp")["ip_mac_bind"]
        out = []
        for item in resp.get("sys_arp", []):
            for name, fields in item.items():
                out.append({"name": name, **fields})
        return out

    # --- Connected clients ---

    def list_clients(self, include_offline: bool = False) -> list[dict]:
        """List devices connected to the router.

        Fields include: mac / ip / ipv6 / hostname / up_speed /
        down_speed (bytes/s) / online_time (seconds) / phy_mode /
        wifi_mode / blocked / etc. `hostname` is auto URL-decoded (the
        router returns percent-encoded UTF-8).
        """
        from urllib.parse import unquote

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

    # --- WiFi ---

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

    # --- Guest network ---

    def get_guest(self) -> dict:
        """Return {2g: {...}, 5g: {...} or None if absent,
        time_left_2g, time_left_5g (seconds)}.

        On TL-7DR7270 only `guest_2g` exists (a single SSID covering both
        bands); the `guest_5g` endpoint returns -40101.
        """
        out: dict = {"2g": None, "5g": None, "time_left_2g": None, "time_left_5g": None}
        try:
            out["2g"] = self.get("guest_network", name="guest_2g")["guest_network"]["guest_2g"]
        except ApiError:
            pass
        try:
            out["5g"] = self.get("guest_network", name="guest_5g")["guest_network"]["guest_5g"]
        except ApiError:
            pass
        try:
            out["time_left_2g"] = int(self.get("guest_network", name="guest_left_2g")
                                      ["guest_network"]["guest_left_2g"]["time_left"])
        except (ApiError, KeyError, ValueError):
            pass
        try:
            out["time_left_5g"] = int(self.get("guest_network", name="guest_left_5g")
                                      ["guest_network"]["guest_left_5g"]["time_left"])
        except (ApiError, KeyError, ValueError):
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

    # --- PPPoE / WAN ---

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

    # --- Port forwarding / DMZ / DDNS ---

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

    # --- AP isolation (the `isolate` field on wlan_host_*) ---

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

    # --- WiFi on/off scheduler ---

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
        """Add a WiFi schedule rule.

        - start / end: HH:MM format
        - days: e.g. ['mon', 'tue', ...]; defaults to every day
        - name: rule name; auto-generated when omitted
        """
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

    # --- Scheduled reboot ---

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
        """Add a scheduled-reboot rule. `reboot_time` is HH:MM; `days`
        is the same as for the WiFi timer."""
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

    # --- Wake on LAN ---

    def list_wol_devices(self) -> list[dict]:
        resp = self.get("wake_on_lan", table="device_list")["wake_on_lan"]
        out = []
        for item in resp.get("device_list", []):
            for name, fields in item.items():
                out.append({"name": name, **fields})
        return out

    def wake(self, mac: str) -> dict:
        """Send a magic packet to the given MAC."""
        return self.do("wake_on_lan", {"wake_device": {"mac": mac.lower().replace(":", "-")}})

    # --- Admin lock (restrict admin UI login to specific MACs) ---

    def get_admin_lock(self) -> dict:
        cfg = self.get("firewall", name="lan_manage")["firewall"]["lan_manage"]
        macs = [cfg.get(f"mac{i}") for i in range(1, 5)]
        macs = [m for m in macs if m and m != "00-00-00-00-00-00"]
        return {"enable_all": cfg.get("enable_all") == "1", "allowed_macs": macs}

    # --- Signal strength (wireless_power) ---

    POWER_LEVELS = {"boost": "0", "high": "0", "normal": "1", "middle": "1", "saving": "2", "low": "2"}

    def get_signal_power(self) -> dict:
        """Return {2g, 5g, available}: the current power level on each band
        and the available levels (low / middle / high)."""
        wifi = self.get_wifi()
        try:
            avail = self.get("wireless_power", name="power_info")["wireless_power"]["power_info"]["power_list"]
        except (ApiError, KeyError):
            avail = ["low", "middle", "high"]
        return {"2g": wifi["2g"].get("power", "0"), "5g": wifi["5g"].get("power", "0"), "available": avail}

    def set_signal_power(self, level: str, band: str = "both") -> None:
        """Set TX power level. `level`: boost/high | normal/middle | saving/low."""
        v = self.POWER_LEVELS.get(level.lower())
        if v is None:
            raise ValueError(f"unknown level {level!r}; use one of: {sorted(self.POWER_LEVELS)}")
        body: dict = {}
        if band in ("2g", "both"):
            body["wlan_host_2g"] = {"power": v}
        if band in ("5g", "both"):
            body["wlan_host_5g"] = {"power": v}
        self.call({"wireless": body, "method": "set"})

    # --- Wireless access control (wlan_access, MAC ACL) ---

    def get_mac_acl(self) -> dict:
        """Return {enable, white_list}, each entry has
        {section, mac, hostname, mac_type, mld_mac}.

        `enable`: "0" = off, "1" = whitelist mode (only listed MACs may
        connect to the main network). `hostname` is auto URL-decoded
        (the router stores it as percent-encoded UTF-8).

        Note: the firmware exposes a `block_list` table under `wlan_access`,
        but the Web UI provides no UI for blocklists. This SDK also only
        wraps the whitelist to avoid confusion (a blocklist entry would be
        applied silently and never visible in the UI).
        """
        from urllib.parse import unquote

        cfg = self.get("wlan_access", name="config")["wlan_access"]["config"]
        try:
            wl_raw = self.do("wlan_access", {"get_white_list": {}})["wlan_access"].get("white_list", [])
        except (ApiError, KeyError):
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
        """`mode`: 'off' / 'whitelist'.

        WARNING: switching to `whitelist` with an empty list will kick
        every wireless client offline. Use with care.
        """
        m = {"off": "0", "whitelist": "1"}.get(mode.lower())
        if m is None:
            raise ValueError("mode must be 'off' or 'whitelist'")
        self.set("wlan_access", "config", {"enable": m})

    def add_mac_acl(self, mac: str, hostname: str = "", index: int | None = None) -> str:
        """Add one entry to the whitelist (standard `add` request)."""
        from urllib.parse import quote

        current = self.get_mac_acl()["white_list"]
        mac_norm = mac.lower().replace(":", "-")
        for r in current:
            if r.get("mac", "").lower() == mac_norm:
                return r.get("section", "")
        # The router stores `name` percent-encoded internally.
        if index is None:
            # Find the next free section index.
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
        """Delete one entry, identified by either `mac` or `section` name."""
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

    # --- Admin lock (set) ---

    def set_admin_lock(self, enable_all: bool, allowed_macs: list[str] | None = None) -> None:
        """`enable_all=True`: any MAC may log in to the admin UI (default).
        `enable_all=False`: only `allowed_macs` (up to 4) may log in."""
        macs = (allowed_macs or [])[:4]
        macs = [m.lower().replace(":", "-") for m in macs]
        while len(macs) < 4:
            macs.append("00-00-00-00-00-00")
        fields = {"enable_all": "1" if enable_all else "0"}
        for i, m in enumerate(macs, 1):
            fields[f"mac{i}"] = m
        self.set("firewall", "lan_manage", fields)

    # --- System ---

    def get_device_info(self) -> dict:
        return self.get("system", name="device_info")["system"]["device_info"]

    def get_sys_mode(self) -> dict:
        return self.get("system", name="sys_mode")["system"]["sys_mode"]

    # --- Full export ---

    def export_all(self) -> dict:
        """Dump every readable module into a single dict, useful for
        backups and config diffs."""
        out = {}
        try:
            out["lan"] = self.get_lan()
        except ApiError as e:
            out["lan"] = {"error": str(e)}
        try:
            out["wan"] = self.get_wan()
        except ApiError as e:
            out["wan"] = {"error": str(e)}
        try:
            out["wan_status"] = self.get_wan_status()
        except ApiError as e:
            out["wan_status"] = {"error": str(e)}
        try:
            out["dhcp_server"] = self.get_dhcp_server()
        except ApiError as e:
            out["dhcp_server"] = {"error": str(e)}
        try:
            out["pppoe"] = self.get_pppoe()
        except ApiError as e:
            out["pppoe"] = {"error": str(e)}
        try:
            out["wifi"] = self.get_wifi()
        except ApiError as e:
            out["wifi"] = {"error": str(e)}
        try:
            out["bindings"] = self.list_bindings()
        except ApiError as e:
            out["bindings"] = {"error": str(e)}
        try:
            out["port_forwards"] = self.list_port_forwards()
        except ApiError as e:
            out["port_forwards"] = {"error": str(e)}
        try:
            out["dmz"] = self.get_dmz()
        except ApiError as e:
            out["dmz"] = {"error": str(e)}
        try:
            out["ddns"] = self.get_ddns()
        except ApiError as e:
            out["ddns"] = {"error": str(e)}
        try:
            out["upnp"] = self.get_upnp()
        except ApiError as e:
            out["upnp"] = {"error": str(e)}
        try:
            out["device_info"] = self.get_device_info()
        except ApiError as e:
            out["device_info"] = {"error": str(e)}
        try:
            out["sys_mode"] = self.get_sys_mode()
        except ApiError as e:
            out["sys_mode"] = {"error": str(e)}
        return out
