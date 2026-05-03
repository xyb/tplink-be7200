"""tplink-be7200 CLI entry point.

Token resolution order (first match wins):
  1. `--token XXX` command-line flag
  2. `TPLINK_STOK` environment variable
  3. Local cache at `~/.cache/tplink-be7200/<host>.json`
  4. Auto-login using `TPLINK_PASSWORD`

Most common flow:
  tplink-be7200 login              # interactive password prompt (no echo), writes cache
  tplink-be7200 bindings list      # subsequent commands reuse the cached token

Password sources are intentionally limited to interactive `getpass` and the
`TPLINK_PASSWORD` env var. The original `-p` and `--password-stdin` options
were removed because both leak the password into shell history (and `-p`
also exposes it via `ps`).

Subcommands at a glance:
  login              log in with a password, cache the token, print it
  cache show|clear   inspect / delete the cached token
  export             dump full configuration as JSON
  get <module>       raw GET
  raw <json-body>    arbitrary request body (advanced)
  bindings list|add|delete|clear|import-csv
  wifi show|set
  pppoe show|set
  lan show|set-ip
  dhcp show|set
  wan show
  device-info
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import os
import sys

from typing import Optional

from . import BE7200Client, BE7200ApiError, __version__, credentials
from requests.exceptions import (
    ConnectionError as _ReqConnectionError,
    ConnectTimeout as _ReqConnectTimeout,
)
from tplinkrouterc6u.common.exception import ClientException as _UpstreamAuthError

# stale-token signal from the router (auth check failed; cached stok rejected)
_AUTH_ERROR_CODE = -40401


def _client(args) -> BE7200Client:
    """Resolve credentials in order: --token > TPLINK_STOK > cache > auto-login.

    Returns a BE7200Client whose `_stok` is already populated. Cached
    tokens are validated lazily — the first request that fails with the
    router's auth error code (-40401) clears the cache and re-authorizes
    using `TPLINK_PASSWORD` if available.
    """
    host = args.host
    no_cache = getattr(args, "no_cache", False)
    verbose = getattr(args, "verbose", False)

    token = args.token or os.environ.get("TPLINK_STOK", "")
    src = "arg/env"
    saved_password: Optional[str] = None
    if not token and not no_cache:
        record = credentials.load(host) or {}
        token = record.get("stok") or ""
        saved_password = record.get("password") or None
        if token:
            src = "credentials"

    if token:
        if verbose:
            print(f"[token from {src}]", file=sys.stderr)
        # When we also have a saved password, hand it to the client so
        # _request() can auto-reauthorize on stok expiry without prompt.
        on_refresh = (
            (lambda new_stok: credentials.save(
                host, stok=new_stok, password=saved_password,
            ))
            if (saved_password and not no_cache)
            else None
        )
        return BE7200Client.from_cached_stok(
            host, token,
            password=saved_password,
            on_token_refresh=on_refresh,
        )

    password = os.environ.get("TPLINK_PASSWORD", "")
    if password:
        if verbose:
            print("[token: auto-login via TPLINK_PASSWORD]", file=sys.stderr)
        client = BE7200Client(host, password)
        client.authorize()
        if not no_cache:
            credentials.save(host, stok=client._stok, password=password)
        # Wire the refresh hook so subsequent stok expiries are handled
        # without re-running the env-driven login flow.
        client._on_token_refresh = lambda new_stok: credentials.save(
            host, stok=new_stok, password=password,
        )
        return client

    sys.exit(
        "no token. provide one of:\n"
        "  1. tplink-be7200 login              # interactive password, writes cache\n"
        "  2. --token XXX\n"
        "  3. export TPLINK_STOK=XXX\n"
        "  4. export TPLINK_PASSWORD=XXX     # auto-login"
    )


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _to_jsonable(obj):
    """Convert c6u dataclass results to plain dicts so JSON output stays JSON."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    return obj


# ============================================================================
# Subcommand implementations
# ============================================================================


def cmd_login(args):
    """Log in and print the token; also writes the local cache by default.

    Password sources (in priority order):
      1. The `TPLINK_PASSWORD` env var (intended for scripts; note the
         process environment is readable by other processes on the host).
      2. Interactive `getpass` prompt (default; no echo, no shell history).
    """
    pwd = os.environ.get("TPLINK_PASSWORD") or getpass.getpass("admin password: ")
    if not pwd:
        sys.exit("empty password")
    client = BE7200Client(args.host, pwd)
    print(f"[info] connecting to http://{args.host}/", file=sys.stderr)
    try:
        client.authorize()
    except _ReqConnectTimeout as e:
        print(
            f"ERROR: router unreachable — connect timeout to {args.host}:80.\n"
            f"  - Common cause: wrong --host. Verify the router IP from your\n"
            f"    machine's default gateway (e.g. `route get default` on macOS\n"
            f"    or `ip route` on Linux).\n"
            f"  - Alternative: device on a different network or router offline.\n"
            f"  - Override host: --host <router-ip> or export TPLINK_HOST=<router-ip>\n"
            f"  - Underlying error: {e}",
            file=sys.stderr,
        )
        sys.exit(2)
    except _ReqConnectionError as e:
        print(
            f"ERROR: router unreachable — connection failed to {args.host}:80.\n"
            f"  - Check network: are you on the LAN? Is the router online?\n"
            f"  - Verify the host: --host <router-ip> or export TPLINK_HOST=<router-ip>\n"
            f"  - Underlying error: {e}",
            file=sys.stderr,
        )
        sys.exit(2)
    except (_UpstreamAuthError, BE7200ApiError) as e:
        msg = str(e)
        if "-40401" in msg:
            print(
                "ERROR: router rejected login (error_code=-40401, password-attempts limit).\n"
                "  - Check the password is correct (do NOT retry blindly: each wrong attempt\n"
                "    burns one of the remaining tries before permanent lockout).\n"
                "  - Web UI login does NOT reset this counter; wait for the lockout window\n"
                "    to expire (typically a few minutes after no further attempts).\n"
                "  - Full router message:\n    " + msg,
                file=sys.stderr,
            )
            sys.exit(2)
        if "HTTP 502" in msg or "HTTP 503" in msg or "HTTP 504" in msg:
            print(
                "ERROR: router login endpoint is rate-limited (5xx HTML response).\n"
                "  - Wait 5-10 minutes for the router to clear the throttle, then retry.\n"
                "  - Full router message:\n    " + msg,
                file=sys.stderr,
            )
            sys.exit(2)
        raise
    token = client._stok
    if not args.no_cache:
        # By default we persist the password too, so future commands can
        # auto-reauthorize on stok expiry without prompting. The user
        # can opt out with --no-save-password (token only, prompt every
        # time the stok dies).
        save_pwd = pwd if not getattr(args, 'no_save_password', False) else None
        p = credentials.save(args.host, stok=token, password=save_pwd)
        if save_pwd:
            print(f"# saved (token+password) to: {p}", file=sys.stderr)
        else:
            print(f"# saved (token only, no password) to: {p}", file=sys.stderr)
    # Identity echo: which router did we actually authenticate against?
    # Goes to stderr so scripted callers piping the token from stdout are
    # unaffected. Non-fatal — a transient API blip must not break login.
    try:
        lan = client.get_lan() or {}
        info = client.get_device_info() or {}
        identity = (
            f"[info] router IP={lan.get('ipaddr', '?')} "
            f"MAC={lan.get('macaddr', '?')} "
            f"model={info.get('device_model', '?')}"
        )
        print(identity, file=sys.stderr)
    except Exception as e:
        print(f"[info] router identity lookup skipped: {e}", file=sys.stderr)
    if args.export:
        print(f"export TPLINK_STOK={token}")
    elif args.json:
        _print({"stok": token, "host": args.host})
    else:
        print(token)


def cmd_cache_show(args):
    """Show the saved credential record for one host. Password is
    redacted by default — pass ``--reveal-password`` to print it."""
    rec = credentials.info(args.host)
    if not rec:
        print(f"no saved credentials for {args.host}")
        sys.exit(1)
    if not getattr(args, 'reveal_password', False) and 'password' in rec:
        rec = dict(rec)
        rec['password'] = '***redacted (use --reveal-password to show)***'
    _print(rec)


def cmd_cache_clear(args):
    if credentials.clear(args.host):
        print(f"cleared credentials for {args.host}")
    else:
        print(f"no saved credentials for {args.host}")


def cmd_export(args):
    client = _client(args)
    data = client.export_all()
    if args.output:
        with open(args.output, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"saved: {args.output}")
    else:
        _print(data)


def cmd_get(args):
    client = _client(args)
    kwargs = {}
    if args.name:
        kwargs["name"] = args.name
    if args.table:
        kwargs["table"] = args.table
    _print(client.get(args.module, **kwargs))


def cmd_raw(args):
    client = _client(args)
    body = json.loads(args.body)
    _print(client.call(body))


# --- bindings ---


def cmd_bindings_list(args):
    client = _client(args)
    rows = client.list_bindings()
    if args.json:
        _print(rows)
        return
    print(f"{'name':<14} {'mac':<18} {'ip':<16} hostname")
    for r in rows:
        print(f"{r['name']:<14} {r.get('mac',''):<18} {r.get('ip',''):<16} {r.get('hostname','')}")


def cmd_bindings_add(args):
    client = _client(args)
    name = client.add_binding(ip=args.ip, mac=args.mac, hostname=args.hostname)
    print(f"added: {name}  {args.mac} -> {args.ip}  {args.hostname}")


def cmd_bindings_delete(args):
    """Delete one binding by name (safe — list-form payload).

    Note: earlier firmware-version code paths assumed delete truncated
    the table; that was a payload format error in this client. The
    web UI's actual delete uses ``name: [list]`` with no ``table``
    field, which deletes only the named entries.
    """
    client = _client(args)
    client.delete_binding(args.name)
    print(f"deleted: {args.name}")


def cmd_bindings_clear(args):
    if not args.yes:
        sys.exit("pass --yes to confirm clear")
    client = _client(args)
    n = client.clear_bindings()
    print(f"cleared {n} bindings")


def cmd_bindings_rename(args):
    """Rename or remove a single binding.

    Rename uses the web UI's in-place ``set`` payload — one round-trip,
    no delete needed. Remove uses the safe list-form delete.
    """
    client = _client(args)
    current = client.list_bindings()
    target_ip = args.ip
    matched = [b for b in current if b.get('ip') == target_ip]
    if not matched:
        sys.exit(f"no binding for ip={target_ip}")
    if len(matched) > 1:
        sys.exit(f"ambiguous: {len(matched)} bindings for ip={target_ip}")
    b = matched[0]
    if args.remove:
        client.delete_binding(b['name'])
        print(f"removed: {b['name']}  {b['ip']}")
    else:
        client.update_binding(
            b['name'], ip=b['ip'], mac=b['mac'], hostname=args.hostname,
        )
        print(f"renamed: {b['name']}  {b['ip']}  hostname={args.hostname}")


def cmd_bindings_import_csv(args):
    """CSV columns: mac, ip, hostname (hostname optional)."""
    import csv

    client = _client(args)
    if args.cleanup:
        if not args.yes:
            sys.exit("--cleanup requires --yes")
        client.clear_bindings()
        print("cleared existing")
    with open(args.path) as f:
        reader = csv.DictReader(f) if args.has_header else csv.reader(f)
        rows = list(reader)
    print(f"read {len(rows)} rows, start add:")
    for i, row in enumerate(rows, 1):
        if isinstance(row, dict):
            mac, ip, hostname = row["mac"], row["ip"], row.get("hostname", "")
        else:
            mac, ip = row[0], row[1]
            hostname = row[2] if len(row) > 2 else ""
        try:
            name = client.add_binding(ip=ip, mac=mac, hostname=hostname, index=i)
            print(f"  {i:3d}. {name}  {mac} -> {ip}  {hostname}  OK")
        except BE7200ApiError as e:
            print(f"  {i:3d}. FAIL: {mac} -> {ip}  {e}")


# --- wifi ---


def cmd_wifi_show(args):
    _print(_client(args).get_wifi())


def cmd_wifi_set(args):
    client = _client(args)
    if args.band in ("2g", "both"):
        client.set_wifi_2g(ssid=args.ssid, key=args.psk)
        print(f"2.4G set: ssid={args.ssid} psk={'*' * len(args.psk) if args.psk else None}")
    if args.band in ("5g", "both"):
        client.set_wifi_5g(ssid=args.ssid + "_5G" if args.ssid and args.band == "both" else args.ssid, key=args.psk)
        print(f"5G   set: ssid={args.ssid}{('_5G' if args.band=='both' else '')} psk={'*' * len(args.psk) if args.psk else None}")


# --- pppoe ---


def cmd_pppoe_show(args):
    _print(_client(args).get_pppoe())


def cmd_pppoe_set(args):
    client = _client(args)
    client.set_pppoe(username=args.username, password=args.password, mtu=args.mtu)
    print(f"PPPoE set: username={args.username} mtu={args.mtu} (wan_type switched to pppoe)")


# --- lan / dhcp / wan ---


def cmd_lan_show(args):
    _print(_client(args).get_lan())


def cmd_lan_set_ip(args):
    client = _client(args)
    client.set_lan_ip(args.ipaddr, args.netmask)
    print(f"LAN IP -> {args.ipaddr}/{args.netmask} (router IP changed, use new address next time)")


def cmd_dhcp_show(args):
    _print(_client(args).get_dhcp_server())


def cmd_dhcp_set(args):
    client = _client(args)
    client.set_dhcp_server(
        pool_start=args.pool_start,
        pool_end=args.pool_end,
        lease_time=args.lease,
        pri_dns=args.pri_dns,
        snd_dns=args.snd_dns,
    )
    print("DHCP server updated")


def cmd_wan_show(args):
    client = _client(args)
    _print({"wan": client.get_wan(), "status": client.get_wan_status()})


def cmd_device_info(args):
    _print(_client(args).get_device_info())


def cmd_guest_show(args):
    client = _client(args)
    g = client.get_guest()
    if args.json:
        _print(g)
        return
    for band in ("2g", "5g"):
        cfg = g.get(band)
        if cfg is None:
            print(f"[{band.upper()}] not supported / not configured")
            continue
        on = "ON" if cfg.get("enable") == "1" else "OFF"
        ssid = cfg.get("ssid", "")
        enc = cfg.get("encrypt", "0")
        enc_str = "WPA2/3" if enc != "0" else "Open"
        key_set = "set" if cfg.get("key") else "none"
        up = cfg.get("upload", "0")
        down = cfg.get("download", "0")
        acc = "intra" if cfg.get("accright") == "1" else "iso"
        print(
            f"[{band.upper()}] {on:<3}  SSID={ssid:<24} ENC={enc_str:<7} KEY={key_set:<4} "
            f"UP={up}K DOWN={down}K ACC={acc}"
        )
        tl = g.get(f"time_left_{band}")
        if tl is not None:
            print(f"       timer={tl}s (0=no limit)")


def cmd_guest_enable(args):
    client = _client(args)
    client.enable_guest(band=args.band)
    print(f"[{args.band.upper()}] guest network enabled")


def cmd_guest_disable(args):
    client = _client(args)
    client.disable_guest(band=args.band)
    print(f"[{args.band.upper()}] guest network disabled")


def cmd_guest_set(args):
    client = _client(args)
    client.set_guest(
        ssid=args.ssid,
        key=args.psk,
        encrypt="3" if args.psk else None,
        upload=args.upload,
        download=args.download,
        accright="1" if args.allow_intra else ("0" if args.isolate else None),
        band=args.band,
    )
    print(f"[{args.band.upper()}] guest network updated")


# --- port-forward ---

def cmd_pf_list(args):
    client = _client(args)
    rows = client.list_port_forwards()
    if args.json:
        _print(rows); return
    print(f"{'NAME':<14} {'PROTO':<7} {'EXT':<13} {'→':<2} INTERNAL")
    for r in rows:
        ext = f"{r.get('src_dport_start','?')}-{r.get('src_dport_end','?')}"
        print(f"{r.get('name',''):<14} {r.get('proto','?'):<7} {ext:<13} →  {r.get('dest_ip','?')}:{r.get('dest_port','?')}")
    print(f"\n{len(rows)} rule(s)")


def cmd_pf_add(args):
    client = _client(args)
    name = client.add_port_forward(
        dest_ip=args.dest_ip, dest_port=args.dest_port,
        src_port_start=args.src_start, src_port_end=args.src_end,
        proto=args.proto,
    )
    print(f"added {name}: {args.proto} {args.src_start or args.dest_port}-{args.src_end or args.dest_port} -> {args.dest_ip}:{args.dest_port}")


def cmd_pf_delete(args):
    _client(args).delete_port_forward(args.name); print(f"deleted {args.name}")


def cmd_pf_clear(args):
    if not args.yes:
        sys.exit("--yes required")
    _client(args).clear_port_forwards()
    print("cleared")


# --- dmz ---

def cmd_dmz_show(args):
    _print(_client(args).get_dmz())


def cmd_dmz_set(args):
    _client(args).set_dmz(enable=True, dest_ip=args.ip)
    print(f"DMZ enabled -> {args.ip}")


def cmd_dmz_off(args):
    _client(args).set_dmz(enable=False)
    print("DMZ disabled")


# --- ddns ---

def cmd_ddns_show(args):
    _print(_client(args).get_ddns())


def cmd_ddns_set(args):
    _client(args).set_ddns(args.username, args.password)
    print(f"DDNS set: {args.username}")


# --- upnp ---

def cmd_upnp_show(args):
    client = _client(args)
    cfg = client.get_upnp()
    print(f"UPnP: {'ON' if cfg.get('enable_upnp') == '1' else 'OFF'}")
    if args.leases:
        for l in client.list_upnp_leases():
            print(f"  {l}")


def cmd_upnp_enable(args):
    _client(args).set_upnp(True); print("UPnP ON")


def cmd_upnp_disable(args):
    _client(args).set_upnp(False); print("UPnP OFF")


# --- ap-isolate ---

def cmd_apiso_show(args):
    iso = _client(args).get_ap_isolate()
    print(f"AP isolation: 2G={'ON' if iso['2g'] else 'OFF'}  5G={'ON' if iso['5g'] else 'OFF'}")


def cmd_apiso_set(args):
    _client(args).set_ap_isolate(isolate=args.on, band=args.band)
    print(f"[{args.band.upper()}] isolation -> {'ON' if args.on else 'OFF'}")


# --- wifi-timer / reboot-timer ---

DAYS_FULL = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

def _parse_days(s: str | None) -> list[str]:
    if not s or s == "all":
        return DAYS_FULL
    return [d.strip().lower() for d in s.split(",") if d.strip()]


def cmd_wifi_timer_show(args):
    t = _client(args).get_wifi_timer()
    print(f"WiFi timer: {'ON' if t['enable'] else 'OFF'}")
    for r in t["rules"]:
        days = ",".join(d for d in DAYS_FULL if r.get(d) == "1") or "none"
        print(f"  {r['name']:<22} {r.get('start_time','?')}-{r.get('end_time','?')}  days={days}  enabled={r.get('enable')}")


def cmd_wifi_timer_enable(args):
    _client(args).set_wifi_timer_enable(True); print("WiFi timer ON")


def cmd_wifi_timer_disable(args):
    _client(args).set_wifi_timer_enable(False); print("WiFi timer OFF")


def cmd_wifi_timer_add(args):
    name = _client(args).add_wifi_timer_rule(
        start=args.start, end=args.end,
        days=_parse_days(args.days), name=args.name,
    )
    print(f"added {name}: {args.start}-{args.end} on {args.days or 'all'}")


def cmd_wifi_timer_delete(args):
    _client(args).delete_wifi_timer_rule(args.name); print(f"deleted {args.name}")


def cmd_reboot_timer_show(args):
    t = _client(args).get_reboot_timer()
    print(f"Reboot timer: {'ON' if t['enable'] else 'OFF'}")
    for r in t["rules"]:
        days = ",".join(d for d in DAYS_FULL if r.get(d) == "1") or "none"
        print(f"  {r['name']:<22} at {r.get('reboot_time','?')}  days={days}  enabled={r.get('enable')}")


def cmd_reboot_timer_enable(args):
    _client(args).set_reboot_timer_enable(True); print("Reboot timer ON")


def cmd_reboot_timer_disable(args):
    _client(args).set_reboot_timer_enable(False); print("Reboot timer OFF")


def cmd_reboot_timer_add(args):
    name = _client(args).add_reboot_timer_rule(
        reboot_time=args.time, days=_parse_days(args.days), name=args.name,
    )
    print(f"added {name}: at {args.time} on {args.days or 'all'}")


def cmd_reboot_timer_delete(args):
    _client(args).delete_reboot_timer_rule(args.name); print(f"deleted {args.name}")


# --- wol ---

def cmd_wol_list(args):
    rows = _client(args).list_wol_devices()
    if args.json:
        _print(rows); return
    print(f"{'NAME':<22} {'MAC':<19} {'IP':<16} {'STATE'}")
    for r in rows:
        st = "online" if r.get("online") == "1" else ("waking" if r.get("waking") == "1" else "offline")
        print(f"{r.get('name',''):<22} {r.get('mac',''):<19} {r.get('ip',''):<16} {st}")


def cmd_wol_wake(args):
    r = _client(args).wake(args.mac)
    print(f"wake sent: {args.mac}  resp={r}")


# --- signal / mac-acl / admin-lock ---

def cmd_signal_show(args):
    s = _client(args).get_signal_power()
    name = {"0": "boost", "1": "normal", "2": "saving"}
    print(f"signal power: 2G={name.get(s['2g'], s['2g'])}  5G={name.get(s['5g'], s['5g'])}  (available: {s['available']})")


def cmd_signal_set(args):
    _client(args).set_signal_power(level=args.level, band=args.band)
    print(f"[{args.band.upper()}] signal -> {args.level}")


def cmd_macacl_show(args):
    a = _client(args).get_mac_acl()
    mode = {"0": "off", "1": "whitelist"}.get(a["enable"], a["enable"])
    print(f"MAC ACL mode: {mode}")
    print(f"\nwhitelist ({len(a['white_list'])}):")
    if not a["white_list"]:
        print("  (empty)")
    else:
        print(f"  {'SECTION':<16} {'MAC':<19} HOSTNAME")
        for r in a["white_list"]:
            print(f"  {r.get('section',''):<16} {r.get('mac',''):<19} {r.get('hostname','')}")


def cmd_macacl_mode(args):
    if args.mode == "whitelist" and not args.yes:
        sys.exit("whitelist mode locks out everyone not on the list. Add --yes to confirm.")
    _client(args).set_mac_acl_mode(args.mode)
    print(f"MAC ACL mode -> {args.mode}")


def cmd_macacl_add(args):
    _client(args).add_mac_acl(mac=args.mac, hostname=args.hostname)
    print(f"added {args.mac}  {args.hostname}")


def cmd_macacl_delete(args):
    _client(args).delete_mac_acl(args.mac)
    print(f"deleted {args.mac}")


def cmd_macacl_clear(args):
    if not args.yes:
        sys.exit("--yes required (will lock out everyone if mode=whitelist)")
    _client(args).clear_mac_acl()
    print("whitelist cleared")


def cmd_admin_show(args):
    a = _client(args).get_admin_lock()
    if a["enable_all"]:
        print("admin login: open to all clients")
    else:
        print("admin login: only allowed MACs:")
        for m in a["allowed_macs"]:
            print(f"  {m}")


def cmd_admin_lock(args):
    macs = [m.strip() for m in args.macs.split(",") if m.strip()]
    if not macs:
        sys.exit("at least one MAC required")
    if len(macs) > 4:
        sys.exit("max 4 MACs supported by router")
    _client(args).set_admin_lock(enable_all=False, allowed_macs=macs)
    print(f"admin login restricted to {len(macs)} MAC(s): {macs}")


def cmd_admin_open(args):
    _client(args).set_admin_lock(enable_all=True)
    print("admin login opened to all")


def cmd_clients(args):
    client = _client(args)
    rows = client.list_clients(include_offline=args.all)
    if args.json:
        _print(rows)
        return

    # Build IP -> binding-hostname map so we can fill in blanks below.
    # Soft-fail: a bindings API blip should never break the read-only
    # clients table, so swallow exceptions and keep going.
    binding_hostname = {}
    try:
        for b in client.list_bindings() or ():
            ip = (b.get('ip') or '').strip()
            name = (b.get('hostname') or '').strip()
            if ip and name:
                binding_hostname[ip] = name
    except Exception:
        pass

    def fmt_speed(b):
        b = int(b or 0)
        if b > 1024 * 1024:
            return f"{b/1024/1024:.1f}M"
        if b > 1024:
            return f"{b/1024:.0f}K"
        return f"{b}B" if b else "-"

    def fmt_time(s):
        s = int(s or 0)
        if not s:
            return "-"
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s//60}m"
        if s < 86400:
            return f"{s//3600}h{(s%3600)//60}m"
        return f"{s//86400}d{(s%86400)//3600}h"

    # `type`:      0 = wired, 1 = wireless
    # `wifi_mode`: 0 = 2.4 GHz, 1 = 5 GHz (only meaningful when type == 1)
    # `phy_mode`:  WiFi standard code (verified to match WiFi N labels)
    phy_map = {
        "0": "-",  # wired or unknown
        "1": "11b", "2": "11g", "3": "11n",
        "4": "WiFi4", "5": "WiFi5", "6": "WiFi6", "7": "WiFi6E", "8": "WiFi7",
    }

    def link_label(row):
        if row.get("type") == "0":
            return "wired"
        return "5G" if row.get("wifi_mode") == "1" else "2.4G"

    print(f"{'IP':<16} {'MAC':<19} {'LINK':<6} {'STD':<7} {'UPTIME':<8} {'UP':<7} {'DOWN':<7} HOSTNAME")
    rows.sort(key=lambda r: tuple(int(x) for x in (r.get("ip") or "0.0.0.0").split(".") if x.isdigit()) or (0,))
    for r in rows:
        wired = r.get("type", "") == "0"
        std = "-" if wired else phy_map.get(r.get("phy_mode", ""), r.get("phy_mode", "-"))
        hostname = r.get("hostname", "") or ""
        if not hostname:
            hostname = binding_hostname.get(r.get("ip", ""), "")
        print(
            f"{r.get('ip','-'):<16} {r.get('mac','-'):<19} "
            f"{link_label(r):<6} "
            f"{std:<7} "
            f"{fmt_time(r.get('online_time')):<8} "
            f"{fmt_speed(r.get('up_speed')):<7} {fmt_speed(r.get('down_speed')):<7} "
            f"{hostname}"
        )
    print(f"\n{len(rows)} client(s)" + (" (incl. offline)" if args.all else " online"))


# ============================================================================
# argparse
# ============================================================================


def build_parser():
    p = argparse.ArgumentParser(prog="tplink-be7200", description="TL-7DR7270 Web API CLI")
    p.add_argument("--version", action="version", version=__version__)
    # Default host: env > first host in cache dir (warns on stderr if multiple) > 192.168.1.1
    default_host = os.environ.get("TPLINK_HOST")
    auto_pick_warning = None
    if not default_host:
        try:
            cached = sorted(p2.stem for p2 in credentials.config_dir().glob("*.json"))
            if cached:
                default_host = cached[0]
                if len(cached) > 1:
                    auto_pick_warning = (
                        f"[note] {len(cached)} hosts in cache ({', '.join(cached)}); "
                        f"auto-picked {default_host}. "
                        f"Override with --host X or export TPLINK_HOST=X."
                    )
        except Exception:
            pass
    if not default_host:
        default_host = "192.168.1.1"
    p.add_argument("--host", default=default_host,
                   help=f"router IP (default {default_host}; or env TPLINK_HOST)")
    p._tplink_auto_warning = auto_pick_warning
    p.add_argument("--token", help="override token (highest priority)")
    p.add_argument("--no-cache", action="store_true", help="don't read/write local token cache")
    p.add_argument("-v", "--verbose", action="store_true", help="print token source")
    sub = p.add_subparsers(dest="cmd", required=True)

    # login (interactive only — password never via cmdline / stdin pipe to avoid leak)
    sp = sub.add_parser("login", help="login interactively (getpass), save credentials + print token")
    sp.add_argument("--export", action="store_true", help="print 'export TPLINK_STOK=...' for eval")
    sp.add_argument("--json", action="store_true", help="JSON output {stok, host}")
    sp.add_argument(
        "--no-save-password", action="store_true",
        help=("only save the token, not the password. token-only mode "
              "still works but stok expiry will require re-running login."),
    )
    sp.set_defaults(func=cmd_login)

    # cache (kept name for muscle-memory; backed by credentials store)
    sp = sub.add_parser("cache", help="show / clear saved credentials")
    sub_c = sp.add_subparsers(dest="action", required=True)
    sp_show = sub_c.add_parser("show")
    sp_show.add_argument(
        "--reveal-password", action="store_true",
        help="print the stored password instead of redacting it",
    )
    sp_show.set_defaults(func=cmd_cache_show)
    sub_c.add_parser("clear").set_defaults(func=cmd_cache_clear)

    # export
    sp = sub.add_parser("export", help="export all config as JSON")
    sp.add_argument("-o", "--output", help="output file (stdout by default)")
    sp.set_defaults(func=cmd_export)

    # get
    sp = sub.add_parser("get", help="raw GET")
    sp.add_argument("module")
    sp.add_argument("--name")
    sp.add_argument("--table")
    sp.set_defaults(func=cmd_get)

    # raw
    sp = sub.add_parser("raw", help="arbitrary JSON body")
    sp.add_argument("body", help='e.g. \'{"network":{"name":"lan"},"method":"get"}\'')
    sp.set_defaults(func=cmd_raw)

    # bindings
    sp = sub.add_parser("bindings", help="DHCP static IP/MAC binding")
    sub_b = sp.add_subparsers(dest="action", required=True)

    spa = sub_b.add_parser("list")
    spa.add_argument("--json", action="store_true")
    spa.set_defaults(func=cmd_bindings_list)

    spa = sub_b.add_parser("add")
    spa.add_argument("--ip", required=True)
    spa.add_argument("--mac", required=True)
    spa.add_argument("--hostname", default="")
    spa.set_defaults(func=cmd_bindings_add)

    spa = sub_b.add_parser("delete", help="delete one binding by name (safe list-form)")
    spa.add_argument("name", help="e.g. user_bind_3")
    spa.set_defaults(func=cmd_bindings_delete)

    spa = sub_b.add_parser("clear", help="delete all static bindings")
    spa.add_argument("--yes", action="store_true", help="confirm")
    spa.set_defaults(func=cmd_bindings_clear)

    spa = sub_b.add_parser("rename",
        help="rename or remove a single binding via safe full-table rebuild")
    spa.add_argument("--ip", required=True, help="bind whose IP to operate on")
    grp = spa.add_mutually_exclusive_group(required=True)
    grp.add_argument("--hostname", help="new hostname (rename)")
    grp.add_argument("--remove", action="store_true", help="remove this binding")
    spa.set_defaults(func=cmd_bindings_rename)

    spa = sub_b.add_parser("import-csv", help="bulk import from csv")
    spa.add_argument("path", help="csv with columns mac,ip,hostname")
    spa.add_argument("--has-header", action="store_true")
    spa.add_argument("--cleanup", action="store_true", help="clear existing before import")
    spa.add_argument("--yes", action="store_true")
    spa.set_defaults(func=cmd_bindings_import_csv)

    # wifi
    sp = sub.add_parser("wifi")
    sub_w = sp.add_subparsers(dest="action", required=True)
    sub_w.add_parser("show").set_defaults(func=cmd_wifi_show)
    spa = sub_w.add_parser("set")
    spa.add_argument("--band", choices=("2g", "5g", "both"), default="both")
    spa.add_argument("--ssid")
    spa.add_argument("--psk")
    spa.set_defaults(func=cmd_wifi_set)

    # pppoe
    sp = sub.add_parser("pppoe")
    sub_p = sp.add_subparsers(dest="action", required=True)
    sub_p.add_parser("show").set_defaults(func=cmd_pppoe_show)
    spa = sub_p.add_parser("set")
    spa.add_argument("--username", required=True)
    spa.add_argument("--password", required=True)
    spa.add_argument("--mtu", type=int, default=1492)
    spa.set_defaults(func=cmd_pppoe_set)

    # lan
    sp = sub.add_parser("lan")
    sub_l = sp.add_subparsers(dest="action", required=True)
    sub_l.add_parser("show").set_defaults(func=cmd_lan_show)
    spa = sub_l.add_parser("set-ip")
    spa.add_argument("ipaddr")
    spa.add_argument("--netmask", default="255.255.255.0")
    spa.set_defaults(func=cmd_lan_set_ip)

    # dhcp
    sp = sub.add_parser("dhcp")
    sub_d = sp.add_subparsers(dest="action", required=True)
    sub_d.add_parser("show").set_defaults(func=cmd_dhcp_show)
    spa = sub_d.add_parser("set")
    spa.add_argument("--pool-start")
    spa.add_argument("--pool-end")
    spa.add_argument("--lease", help="lease time in seconds")
    spa.add_argument("--pri-dns")
    spa.add_argument("--snd-dns")
    spa.set_defaults(func=cmd_dhcp_set)

    # wan
    sp = sub.add_parser("wan")
    sub_w2 = sp.add_subparsers(dest="action", required=True)
    sub_w2.add_parser("show").set_defaults(func=cmd_wan_show)

    # device-info
    sub.add_parser("device-info").set_defaults(func=cmd_device_info)

    # clients
    sp = sub.add_parser("clients", help="list connected devices")
    sp.add_argument("-a", "--all", action="store_true", help="include offline history")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_clients)

    # guest
    sp = sub.add_parser("guest", help="guest network")
    sub_g = sp.add_subparsers(dest="action", required=True)
    spa = sub_g.add_parser("show")
    spa.add_argument("--json", action="store_true")
    spa.set_defaults(func=cmd_guest_show)

    spa = sub_g.add_parser("enable")
    spa.add_argument("--band", choices=("2g", "5g"), default="2g")
    spa.set_defaults(func=cmd_guest_enable)

    spa = sub_g.add_parser("disable")
    spa.add_argument("--band", choices=("2g", "5g"), default="2g")
    spa.set_defaults(func=cmd_guest_disable)

    spa = sub_g.add_parser("set", help="set SSID/PSK/rate-limit")
    spa.add_argument("--band", choices=("2g", "5g"), default="2g")
    spa.add_argument("--ssid")
    spa.add_argument("--psk", help="setting psk auto-enables WPA2/3")
    spa.add_argument("--upload", type=int, help="upload limit KB/s (0=unlimited)")
    spa.add_argument("--download", type=int, help="download limit KB/s (0=unlimited)")
    grp = spa.add_mutually_exclusive_group()
    grp.add_argument("--isolate", action="store_true", help="isolate guests from each other")
    grp.add_argument("--allow-intra", action="store_true", help="allow guests to talk to each other")
    spa.set_defaults(func=cmd_guest_set)

    # port-forward
    sp = sub.add_parser("port-forward", help="virtual server / port forwarding")
    sub_pf = sp.add_subparsers(dest="action", required=True)
    spa = sub_pf.add_parser("list"); spa.add_argument("--json", action="store_true"); spa.set_defaults(func=cmd_pf_list)
    spa = sub_pf.add_parser("add")
    spa.add_argument("--dest-ip", required=True)
    spa.add_argument("--dest-port", required=True)
    spa.add_argument("--src-start", help="external start port (defaults to dest-port)")
    spa.add_argument("--src-end", help="external end port")
    spa.add_argument("--proto", default="TCP", choices=("TCP", "UDP", "TCP_UDP"))
    spa.set_defaults(func=cmd_pf_add)
    spa = sub_pf.add_parser("delete"); spa.add_argument("name"); spa.set_defaults(func=cmd_pf_delete)
    spa = sub_pf.add_parser("clear"); spa.add_argument("--yes", action="store_true"); spa.set_defaults(func=cmd_pf_clear)

    # dmz
    sp = sub.add_parser("dmz", help="DMZ host")
    sub_d = sp.add_subparsers(dest="action", required=True)
    sub_d.add_parser("show").set_defaults(func=cmd_dmz_show)
    spa = sub_d.add_parser("set"); spa.add_argument("ip"); spa.set_defaults(func=cmd_dmz_set)
    sub_d.add_parser("off").set_defaults(func=cmd_dmz_off)

    # ddns
    sp = sub.add_parser("ddns", help="third-party DDNS (peanut hull / etc)")
    sub_dd = sp.add_subparsers(dest="action", required=True)
    sub_dd.add_parser("show").set_defaults(func=cmd_ddns_show)
    spa = sub_dd.add_parser("set"); spa.add_argument("--username", required=True); spa.add_argument("--password", required=True); spa.set_defaults(func=cmd_ddns_set)

    # upnp
    sp = sub.add_parser("upnp")
    sub_u = sp.add_subparsers(dest="action", required=True)
    spa = sub_u.add_parser("show"); spa.add_argument("--leases", action="store_true"); spa.set_defaults(func=cmd_upnp_show)
    sub_u.add_parser("enable").set_defaults(func=cmd_upnp_enable)
    sub_u.add_parser("disable").set_defaults(func=cmd_upnp_disable)

    # ap-isolate
    sp = sub.add_parser("ap-isolate", help="AP isolation toggle")
    sub_a = sp.add_subparsers(dest="action", required=True)
    sub_a.add_parser("show").set_defaults(func=cmd_apiso_show)
    spa = sub_a.add_parser("on"); spa.add_argument("--band", choices=("2g","5g","both"), default="both"); spa.set_defaults(func=lambda a: (setattr(a, 'on', True), cmd_apiso_set(a))[1])
    spa = sub_a.add_parser("off"); spa.add_argument("--band", choices=("2g","5g","both"), default="both"); spa.set_defaults(func=lambda a: (setattr(a, 'on', False), cmd_apiso_set(a))[1])

    # wifi-timer
    sp = sub.add_parser("wifi-timer", help="schedule WiFi on/off")
    sub_wt = sp.add_subparsers(dest="action", required=True)
    sub_wt.add_parser("show").set_defaults(func=cmd_wifi_timer_show)
    sub_wt.add_parser("enable").set_defaults(func=cmd_wifi_timer_enable)
    sub_wt.add_parser("disable").set_defaults(func=cmd_wifi_timer_disable)
    spa = sub_wt.add_parser("add")
    spa.add_argument("--start", required=True, help="HH:MM")
    spa.add_argument("--end", required=True, help="HH:MM")
    spa.add_argument("--days", help="comma list mon/tue/...; default all")
    spa.add_argument("--name")
    spa.set_defaults(func=cmd_wifi_timer_add)
    spa = sub_wt.add_parser("delete"); spa.add_argument("name"); spa.set_defaults(func=cmd_wifi_timer_delete)

    # reboot-timer
    sp = sub.add_parser("reboot-timer", help="schedule auto-reboot")
    sub_rt = sp.add_subparsers(dest="action", required=True)
    sub_rt.add_parser("show").set_defaults(func=cmd_reboot_timer_show)
    sub_rt.add_parser("enable").set_defaults(func=cmd_reboot_timer_enable)
    sub_rt.add_parser("disable").set_defaults(func=cmd_reboot_timer_disable)
    spa = sub_rt.add_parser("add")
    spa.add_argument("--time", required=True, help="HH:MM")
    spa.add_argument("--days", help="comma list, default all")
    spa.add_argument("--name")
    spa.set_defaults(func=cmd_reboot_timer_add)
    spa = sub_rt.add_parser("delete"); spa.add_argument("name"); spa.set_defaults(func=cmd_reboot_timer_delete)

    # wol
    sp = sub.add_parser("wol", help="Wake on LAN")
    sub_w = sp.add_subparsers(dest="action", required=True)
    spa = sub_w.add_parser("list"); spa.add_argument("--json", action="store_true"); spa.set_defaults(func=cmd_wol_list)
    spa = sub_w.add_parser("wake"); spa.add_argument("mac"); spa.set_defaults(func=cmd_wol_wake)

    # signal
    sp = sub.add_parser("signal", help="WiFi TX power level")
    sub_s = sp.add_subparsers(dest="action", required=True)
    sub_s.add_parser("show").set_defaults(func=cmd_signal_show)
    spa = sub_s.add_parser("set")
    spa.add_argument("level", choices=("boost", "high", "normal", "middle", "saving", "low"))
    spa.add_argument("--band", choices=("2g", "5g", "both"), default="both")
    spa.set_defaults(func=cmd_signal_set)

    # mac-acl
    sp = sub.add_parser("mac-acl", help="wireless MAC whitelist (only listed MACs can connect)")
    sub_m = sp.add_subparsers(dest="action", required=True)
    sub_m.add_parser("show").set_defaults(func=cmd_macacl_show)
    spa = sub_m.add_parser("mode", help="set ACL mode off/whitelist")
    spa.add_argument("mode", choices=("off", "whitelist"))
    spa.add_argument("--yes", action="store_true", help="required for whitelist (lockout risk)")
    spa.set_defaults(func=cmd_macacl_mode)
    spa = sub_m.add_parser("add")
    spa.add_argument("mac")
    spa.add_argument("--hostname", default="")
    spa.set_defaults(func=cmd_macacl_add)
    spa = sub_m.add_parser("delete")
    spa.add_argument("mac")
    spa.set_defaults(func=cmd_macacl_delete)
    spa = sub_m.add_parser("clear")
    spa.add_argument("--yes", action="store_true")
    spa.set_defaults(func=cmd_macacl_clear)

    # admin-lock
    sp = sub.add_parser("admin-lock", help="restrict admin UI to specific MACs")
    sub_al = sp.add_subparsers(dest="action", required=True)
    sub_al.add_parser("show").set_defaults(func=cmd_admin_show)
    spa = sub_al.add_parser("set"); spa.add_argument("macs", help="comma-separated MACs (max 4)"); spa.set_defaults(func=cmd_admin_lock)
    sub_al.add_parser("open").set_defaults(func=cmd_admin_open)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    # If multiple hosts are cached, warn which one was auto-picked. Only
    # warn when the user did not explicitly set --host or TPLINK_HOST.
    warn = getattr(parser, "_tplink_auto_warning", None)
    if warn and not os.environ.get("TPLINK_HOST"):
        if args.host == parser.get_default("host"):
            print(warn, file=sys.stderr)
    try:
        args.func(args)
    except BE7200ApiError as e:
        # If the cached token is stale, try to clear it and re-login if a
        # password is available; otherwise surface the error.
        if e.code == _AUTH_ERROR_CODE and not getattr(args, "no_cache", False):
            credentials.clear(args.host)
            if os.environ.get("TPLINK_PASSWORD"):
                print("[cached token stale; cleared cache, retry the command]", file=sys.stderr)
            else:
                print(
                    "[cached token stale; cleared cache. set TPLINK_PASSWORD or run "
                    "`tplink-be7200 login` and retry]", file=sys.stderr,
                )
        print(f"API error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
