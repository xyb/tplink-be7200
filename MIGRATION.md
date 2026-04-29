# Migration to tplinkrouterc6u

Migration plan for switching `tplink-be7200` from a self-contained
implementation to a thin wrapper / extension of the upstream
[`tplinkrouterc6u`](https://github.com/AlexandrErohin/TP-Link-Archer-C6U)
library.

## Why

Upstream `tplinkrouterc6u >= 5.18.1` now ships TL-7DR7270 1.0.18+ MD5 nonce
login (PR [#155](https://github.com/AlexandrErohin/TP-Link-Archer-C6U/pull/155)
merged 2026-04-28). Re-implementing auth + status/dhcp/wifi read in our repo
is duplicated effort. We keep this repo focused on what upstream does *not*
cover: extended write APIs (PPPoE, LAN/DHCP server, port forwarding,
IP/MAC bindings beyond reservations, guest network detail config) and the
ASUS NVRAM migration helper.

## Coverage map

| Our CLI command(s)                    | Source after migration                              |
| ------------------------------------- | --------------------------------------------------- |
| `login`                               | c6u `TPLinkXDRClient.authorize()`                   |
| `device-info`                         | c6u `get_firmware()` + `get_status()`               |
| `wifi show`                           | c6u `get_status()` (wifi fields)                    |
| `wifi enable/disable host/guest 2g/5g`| c6u `set_wifi(Connection, enable)`                  |
| `wifi set ssid/password/...`          | **extend** (c6u only toggles enable)                |
| `dhcp show` (lease list)              | c6u `get_ipv4_dhcp_leases()`                        |
| `dhcp set` (server config)            | **extend**                                          |
| `lan show`                            | c6u `get_ipv4_status()`                             |
| `lan set-ip`                          | **extend**                                          |
| `wan show`                            | c6u `get_ipv4_status()` (wan fields)                |
| `bindings list` (IP reservations)     | c6u `get_ipv4_reservations()`                       |
| `bindings add/delete/clear/import-csv`| **extend**                                          |
| `pppoe show/set`                      | **extend**                                          |
| `guest show/enable/disable`           | c6u `set_wifi()` for enable; show via `get_status()`|
| `guest set` (SSID/password)           | **extend**                                          |
| `pf list/add/delete` (port forwarding)| **extend**                                          |
| `cache show/clear` (schema cache)     | local utility, unchanged                            |
| `export / get / raw`                  | local utility, talks to c6u session                 |
| `migrate-from-asus-nvram`             | local utility, unchanged                            |

## Plan

1. Add `tplinkrouterc6u >= 5.18.1` to `pyproject.toml` deps. ✓
2. Replace `tplink_be7200/auth.py` — c6u handles login.
3. New `tplink_be7200/client.py`: a `BE7200Client(TPLinkXDRClient)` subclass
   that adds the extended write methods listed above. Uses c6u's `_request`
   internally.
4. Rewrite `tplink_be7200/api.py` to be a thin facade over `BE7200Client`
   (or delete it and have `cli.py` import the subclass directly).
5. Rewrite `cli.py` command-by-command, in roughly this order:
   - `login`, `device-info`, `wifi show`, `dhcp show`, `lan show`,
     `wan show`, `bindings list` (read-only, c6u-direct, lowest risk)
   - `wifi enable/disable`, `guest enable/disable` (c6u write, low risk)
   - `wifi set` detail, `bindings add/delete`, `pppoe`, `lan set-ip`,
     `dhcp set`, `pf` (extended write, our own implementation)
6. Test against live TL-7DR7270 1.0.18+ at each phase before merging the
   branch back to `main`.
