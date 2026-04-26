#!/usr/bin/env python3
"""Bulk-migrate static DHCP bindings from an ASUS Asuswrt-Merlin nvram dump
to a TL-7DR7270 router.

Inputs:
  - <nvram_dump>            output of `nvram show` on the ASUS, including
                            the `dhcp_staticlist=` line
  - --leases asus_leases    optional dnsmasq.leases file, used to recover
                            hostnames by MAC
  - --hosts asus_hosts      optional hosts.dnsmasq file, used to recover
                            hostnames by IP

Usage:
  export TPLINK_STOK=XXX
  ./migrate_from_asus_nvram.py nvram.txt --dry-run
  ./migrate_from_asus_nvram.py nvram.txt --leases asus_leases.txt \\
      --hosts asus_hosts.txt --cleanup
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Run as a script without requiring `pip install`.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from tplink_be7200 import API, ApiError  # noqa: E402


def parse_dhcp_staticlist(nvram_text: str) -> list[dict]:
    m = re.search(r"^dhcp_staticlist=(.*)$", nvram_text, re.M)
    if not m:
        sys.exit("dhcp_staticlist= not found in nvram dump")
    raw = m.group(1)
    out = []
    # ASUS format: <MAC1>IP1><MAC2>IP2>...
    for mac_upper, ip in re.findall(r"<([^>]+)>([^>]+)>", raw):
        out.append({"mac": mac_upper.lower().replace(":", "-"), "ip": ip})
    return out


def load_hostname_maps(leases_path: str | None, hosts_path: str | None) -> tuple[dict, dict]:
    """Return (mac->name, ip->name) lookup tables."""
    by_mac, by_ip = {}, {}
    if leases_path and os.path.exists(leases_path):
        for line in Path(leases_path).read_text().splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            mac = parts[1].lower().replace(":", "-")
            ip = parts[2]
            name = parts[3]
            if name and name != "*":
                # Trim long suffixes like `lumi-acpartner-v2_miio79167037`.
                name = name.split("_")[0]
                by_mac[mac] = name
                by_ip[ip] = name
    if hosts_path and os.path.exists(hosts_path):
        for line in Path(hosts_path).read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                by_ip[parts[0]] = parts[1]
    return by_mac, by_ip


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("nvram", help="ASUS `nvram show` dump file")
    p.add_argument("--leases", help="dnsmasq.leases (hostname source 1)")
    p.add_argument("--hosts", help="hosts.dnsmasq (hostname source 2, by IP)")
    p.add_argument("--token", help="or env TPLINK_STOK")
    p.add_argument("--host", default="192.168.1.1")
    p.add_argument("--cleanup", action="store_true", help="clear existing bindings before import")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    nvram_text = Path(args.nvram).read_text()
    bindings = parse_dhcp_staticlist(nvram_text)
    by_mac, by_ip = load_hostname_maps(args.leases, args.hosts)

    print(f"ASUS has {len(bindings)} static bindings, will push to TP-Link:")
    for i, b in enumerate(bindings, 1):
        b["hostname"] = by_mac.get(b["mac"]) or by_ip.get(b["ip"], "")
        print(f"  {i:2d}. {b['mac']}  {b['ip']:<16}  {b['hostname']}")

    if args.dry_run:
        print("\n--dry-run, no write")
        return

    token = args.token or os.environ.get("TPLINK_STOK", "")
    if not token:
        sys.exit("no token: pass --token or export TPLINK_STOK=XXX")
    api = API(token=token, host=args.host)

    cur = api.list_bindings()
    print(f"\nTP-Link currently has {len(cur)} bindings")

    if args.cleanup and cur:
        print("clearing existing first:")
        api.clear_bindings()
        print(f"  cleared, {len(api.list_bindings())} remain")

    print("\nadding:")
    for i, b in enumerate(bindings, 1):
        try:
            name = api.add_binding(ip=b["ip"], mac=b["mac"], hostname=b["hostname"], index=i)
            print(f"  {i:2d}. {name}  {b['mac']} -> {b['ip']:<16} {b['hostname']}  OK")
        except ApiError as e:
            print(f"  {i:2d}. FAIL: {b['mac']} -> {b['ip']}  {e}")

    print(f"\ndone, TP-Link now has {len(api.list_bindings())} bindings")


if __name__ == "__main__":
    main()
