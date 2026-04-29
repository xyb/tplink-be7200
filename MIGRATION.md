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
2. Delete `tplink_be7200/auth.py` — c6u handles login. ✓
3. New `tplink_be7200/client.py`: `BE7200Client(TPLinkXDRClient)` subclass
   adding the extended write surface. ✓
4. Delete `tplink_be7200/api.py`; `cli.py` imports `BE7200Client` directly. ✓
5. Rewrite `cli.py` command-by-command, every command now goes through
   `BE7200Client` either via c6u's own methods or our extension methods. ✓
6. Test against live TL-7DR7270 1.0.18+ before merging the branch back to
   `main`. **Pending — needs live device.** Mock tests (60 total) pass.

## Post-migration status (2026-04-29)

- `tplink_be7200.API` / `tplink_be7200.login` are gone. New entry point:
  `from tplink_be7200 import BE7200Client, BE7200ApiError, cache`.
- `BE7200Client.from_cached_stok(host, stok)` lets cli.py keep the
  "log in once, reuse the cached stok" UX without re-running authorize on
  every command.
- Stale-cache fallback is implemented in `cli._client(args)`: on auth
  error the cache is cleared and a password authorize is retried (using
  `TPLINK_PASSWORD` env var or interactive `getpass`).
- 60 mock unit tests in `test/` cover the client surface; live verification
  on TL-7DR7270 still TODO before merging to `main`.
