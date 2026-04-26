<sub><b>🌐 English</b> · <a href="README.zh.md">中文</a></sub>

# tplink-be7200

A Python SDK and CLI for the TP-Link **BE7200 (TL-7DR7270)** Web UI API.

Verified against firmware **1.0.18**. The Web UI itself talks plain JSON
over HTTP with no transport encryption, so curl works directly. This SDK
just wraps the request bodies and the login handshake to make scripting
pleasant.

> Not an official tool. The protocol may change across firmware
> versions; run `tplink-be7200 export` first to see the current shape of
> things.

## Quick start

```sh
# 1. Log in once; the token is cached locally.
./bin/tplink-be7200 login              # prompts for the password (no echo)

# 2. Subsequent commands reuse the cached token.
./bin/tplink-be7200 device-info
./bin/tplink-be7200 bindings list
./bin/tplink-be7200 export -o backup.json

# 3. Or install onto your PATH.
pip install -e .
tplink-be7200 wifi show
```

### Token resolution order

The CLI looks for a token in this order:

1. `--token XXX` command-line flag
2. `TPLINK_STOK` environment variable
3. Local cache at `~/.cache/tplink-be7200/<host>.json` (written by
   `login`; skip with `--no-cache`)
4. Auto-login using the `TPLINK_PASSWORD` environment variable

Add `-v` to see which one was used.

### Grabbing a token from the browser

The Web UI is a single-page app, so the browser address bar stays at
`http://192.168.1.1` and never reveals the token. To recover it from
DevTools:

1. Open the browser's DevTools (F12) and switch to the **Network** tab.
2. Log in normally.
3. After login, look for any request whose URL contains `stok=`, e.g.
   `http://192.168.1.1/stok=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX/pc/Content.htm`
   or `http://192.168.1.1/stok=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX/ds`.
   Copy the part between `stok=` and the next `/`.

```sh
export TPLINK_STOK=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
# Or write it directly into the cache:
echo '{"host":"192.168.1.1","stok":"XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"}' \
  > ~/.cache/tplink-be7200/192.168.1.1.json
```

### Python SDK

```python
from tplink_be7200 import API, login

# Option 1: bring your own token.
api = API(token="XXX")

# Option 2: log in with a password.
api = login("my-password")

print(api.get_lan())
api.add_binding(ip="192.168.1.42", mac="aa-bb-cc-dd-ee-ff", hostname="my-server")
api.set_pppoe(username="<isp-account>", password="<isp-password>")
```

### Token TTL

The router does not document the session TTL. Empirically it lasts
tens of minutes; once expired, every API call returns an authentication
error. The strategy is *use the cache, clear it on failure* — the SDK
deliberately does not hard-code a TTL on the client side. After
expiry, the next command will report "missing token" and you simply
run `login` again. (Auto-retry could be added; it isn't, because most
real use is one-shot scripting.)

## Protocol summary

```
POST http://<host>/stok=<TOKEN>/ds
Content-Type: application/json
```

Five methods:

| method | Purpose | Request body |
|---|---|---|
| `get` | Read | `{"<module>":{"name":"<sec>"}}` or `{"<module>":{"table":"<sec>"}}` |
| `set` | Update single section | `{"<module>":{"<sec>":{<fields>}}}` |
| `add` | Append a row to a table | `{"<module>":{"table":"<sec>","name":"<sec>_<idx>","para":{<fields>}}}` |
| `delete` | Remove a row | `{"<module>":{"table":"<sec>","name":"<sec>_<idx>"}}` |
| `do` | Trigger an action | `{"<module>":{"<action>":<payload>}}` |

Common error codes:

- `0` success
- `-40101` unknown module / field name
- `-40205` entry not found (also returned by a successful `delete`, normal)
- `-40210` malformed request body (typically `add` missing `table` / `name` / `para`)

## CLI subcommands

```
tplink-be7200 login [--export | --json] [--no-cache]
tplink-be7200 cache show | clear

tplink-be7200 export [-o file]                       export full config
tplink-be7200 get <module> [--name X | --table X]
tplink-be7200 raw '<json-body>'                      arbitrary body
tplink-be7200 device-info
tplink-be7200 lan show | set-ip <IP>
tplink-be7200 wan show
tplink-be7200 dhcp show | set [--pool-start --pool-end --lease ...]
tplink-be7200 wifi show | set [--band 2g/5g/both --ssid X --psk Y]
tplink-be7200 pppoe show | set --username X --password Y [--mtu 1492]
tplink-be7200 bindings list [--json]
tplink-be7200 bindings add --ip X --mac Y [--hostname Z]
tplink-be7200 bindings delete <name>
tplink-be7200 bindings clear --yes
tplink-be7200 bindings import-csv <path> [--cleanup --yes --has-header]
tplink-be7200 clients [-a] [--json]
tplink-be7200 guest show | enable | disable | set
tplink-be7200 port-forward list | add | delete | clear
tplink-be7200 dmz show | set <ip> | off
tplink-be7200 ddns show | set --username X --password Y
tplink-be7200 upnp show | enable | disable
tplink-be7200 ap-isolate show | on | off
tplink-be7200 wifi-timer show | enable | disable | add | delete
tplink-be7200 reboot-timer show | enable | disable | add | delete
tplink-be7200 wol list | wake <mac>
tplink-be7200 signal show | set <level>
tplink-be7200 mac-acl show | mode <off|whitelist> | add | delete | clear
tplink-be7200 admin-lock show | set <macs> | open
```

Global options: `--host X`, `--token X`, `--no-cache`, `-v`.

## Login protocol (verified on firmware 1.0.18)

The two requests captured during a browser login:

```sh
# 1. Fetch encryption parameters.
curl -s -X POST http://192.168.1.1/ \
  -H "Content-Type: application/json" \
  -d '{"method":"do","user_management":{"get_encrypt_info":null}}'
# -> {"nonce":"...","key":"<RSA-PEM>","encrypt_type":["3"], ...}

# 2. Log in (password = MD5(plain + ":" + nonce).hex()).
HASH=$(echo -n "<password>:<nonce>" | md5sum | cut -d' ' -f1)
curl -s -X POST http://192.168.1.1/ \
  -H "Content-Type: application/json" \
  -d "{\"method\":\"do\",\"login\":{\"password\":\"$HASH\",\"encrypt_type\":\"3\"}}"
# -> {"error_code":0,"stok":"..."}
```

Older firmware (where `encrypt_type` does not contain `"3"`) takes a
fallback path using TP-Link's historic XOR `orgAuthPwd` encoding with
no `encrypt_type` field. The SDK implements that fallback, but on this
device the MD5 path is the one in use.

## Verified modules

| Module | Purpose | SDK method |
|---|---|---|
| `network.lan` | LAN IP / netmask | `get_lan()` / `set_lan_ip()` |
| `protocol.wan` | WAN type / MAC | `get_wan()` / `set_wan_dhcp()` |
| `network.wan_status` | WAN runtime status | `get_wan_status()` |
| `protocol.pppoe` | PPPoE dial-in | `get_pppoe()` / `set_pppoe()` |
| `dhcpd.udhcpd` | DHCP server | `get_dhcp_server()` / `set_dhcp_server()` |
| `ip_mac_bind.user_bind` | Static bindings (table) | `list_bindings()` / `add_binding()` / `delete_binding()` / `clear_bindings()` |
| `ip_mac_bind.sys_arp` | System ARP table (read-only) | `get_arp()` |
| `wireless.wlan_host_2g/5g` | Main WiFi SSID / PSK | `get_wifi()` / `set_wifi_2g()` / `set_wifi_5g()` |
| `guest_network.guest_2g` | Guest WiFi | `get_guest()` / `set_guest()` / `enable_guest()` / `disable_guest()` |
| `firewall.redirect` | Port forwarding (table) | `list_port_forwards()` / `add_port_forward()` / `delete_port_forward()` |
| `firewall.dmz` | DMZ | `get_dmz()` / `set_dmz()` |
| `firewall.lan_manage` | Restrict admin login by MAC | `get_admin_lock()` / `set_admin_lock()` |
| `ddns.phddns` | Third-party DDNS | `get_ddns()` / `set_ddns()` |
| `upnpd.config` | UPnP toggle | `get_upnp()` / `set_upnp()` / `list_upnp_leases()` |
| `time_switch` | WiFi on/off scheduler | `get_wifi_timer()` / `add_wifi_timer_rule()` |
| `reboot_timer` | Scheduled reboot | `get_reboot_timer()` / `add_reboot_timer_rule()` |
| `wake_on_lan` | Wake on LAN | `list_wol_devices()` / `wake()` |
| `wireless_power.power_info` | TX power level | `get_signal_power()` / `set_signal_power()` |
| `wlan_access` | Wireless MAC ACL (whitelist) | `get_mac_acl()` / `set_mac_acl_mode()` / `add_mac_acl()` / `delete_mac_acl()` |
| `hosts_info.online_host` | Connected clients | `list_clients()` |
| `system.device_info` | Device model / serial | `get_device_info()` |
| `system.sys_mode` | Router operating mode | `get_sys_mode()` |

## Known gotchas

1. **Tokens live in the URL path, not cookies.** Hitting a non-existent
   `.htm` will log you out.
2. **`add` requires three fields**: `table`, `name`, and `para`. Missing
   any of them yields `-40210`.
3. **`name` is `<table>_<index>`**, with the index auto-incrementing.
   The Web UI manages it itself.
4. **`delete` returning `-40205` is not a failure.** Re-run a `get` to
   see the real state. The SDK already treats `-40205` as success.
5. **JS `optName` keys are not the on-the-wire field names.** For
   example the IP key in JS is `ipAddr`, but the API field is `ip`.
   The SDK normalises this for you.
6. **The wireless MAC ACL has a `block_list` table, but the Web UI
   exposes only the whitelist.** The SDK matches the UI to avoid
   "invisible" rules. If you switch to whitelist mode with an empty
   list, every wireless client is kicked off — confirm before toggling.

## Helper scripts

- `scripts/migrate_from_asus_nvram.py` — read an ASUS Asuswrt-Merlin
  `nvram show` dump and push every `dhcp_staticlist` entry to a
  TL-7DR7270 as a static binding, optionally backfilling hostnames from
  `dnsmasq.leases` / `hosts.dnsmasq`.

## License

MIT.
