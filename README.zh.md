<sub>🌐 <a href="README.md">English</a> · <b>中文</b></sub>

# tplink-be7200

TP-Link **BE7200（TL-7DR7270）** Web UI API 的 Python SDK + CLI。

固件实测版本 **1.0.18**。Web UI 自身用的是明文 JSON over HTTP，没有传输加密，curl 直通。这个 SDK 只是把请求体格式和登录握手封装一下，方便日常脚本化。

> 不是官方工具。固件升级后协议可能变，用前先 `tplink-be7200 export` 看一眼字段。

## 快速上手

```sh
# 1. 一次登录，自动写本地缓存
./bin/tplink-be7200 login              # 交互输密码（不回显）

# 2. 之后所有命令直接跑，CLI 自动从缓存取 token
./bin/tplink-be7200 device-info
./bin/tplink-be7200 bindings list
./bin/tplink-be7200 export -o backup.json

# 3. 也可以装到 PATH
pip install -e .
tplink-be7200 wifi show
```

### Token 解析顺序

CLI 按下面顺序找 token，命中即用：

1. `--token XXX` 命令行参数
2. `TPLINK_STOK` 环境变量
3. 本地缓存 `~/.cache/tplink-be7200/<host>.json`（`login` 自动写入，`--no-cache` 跳过）
4. 自动登录：`TPLINK_PASSWORD` 环境变量

加 `-v` 可以看走的哪条。

### 从浏览器拿 token

Web UI 是单页应用，浏览器地址栏一直停在 `http://192.168.1.1`，看不到 token。要从 DevTools 里拿：

1. 打开浏览器 DevTools（F12），切到 **Network** 标签。
2. 正常登录。
3. 登录后在 Network 列表里找任何 URL 含 `stok=` 的请求，例如：
   `http://192.168.1.1/stok=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX/pc/Content.htm`
   或 `http://192.168.1.1/stok=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX/ds`。
   把 `stok=` 和后面第一个 `/` 之间的部分复制出来。

```sh
export TPLINK_STOK=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
# 或者直接写进缓存文件
echo '{"host":"192.168.1.1","stok":"XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"}' \
  > ~/.cache/tplink-be7200/192.168.1.1.json
```

### Python SDK

```python
from tplink_be7200 import API, login

# 方式 1：自带 token
api = API(token="XXX")

# 方式 2：用密码登录
api = login("my-password")

print(api.get_lan())
api.add_binding(ip="192.168.1.42", mac="aa-bb-cc-dd-ee-ff", hostname="my-server")
api.set_pppoe(username="<isp-account>", password="<isp-password>")
```

### Token TTL

路由器没公开 session 超时时间。实测看上去几十分钟，session 失效后任何 API 调用都会返回认证错。SDK 的策略是**先用缓存，失败再清缓存重登**，不在客户端硬编码 TTL。Token 失效后下一条命令会报"缺 token"，重新 `login` 即可。（自动重试可以加，目前没加，因为多数场景就是一次性脚本。）

## 协议总结

```
POST http://<host>/stok=<TOKEN>/ds
Content-Type: application/json
```

五种 method：

| method | 用途 | 请求体 |
|---|---|---|
| `get` | 读 | `{"<module>":{"name":"<sec>"}}` 或 `{"<module>":{"table":"<sec>"}}` |
| `set` | 改单实例 section | `{"<module>":{"<sec>":{<fields>}}}` |
| `add` | 给 table 加一行 | `{"<module>":{"table":"<sec>","name":"<sec>_<idx>","para":{<fields>}}}` |
| `delete` | 删一行 | `{"<module>":{"table":"<sec>","name":"<sec>_<idx>"}}` |
| `do` | 触发动作 | `{"<module>":{"<action>":<payload>}}` |

常见错误码：

- `0` 成功
- `-40101` 模块或字段名错
- `-40205` 条目不存在（`delete` 成功后通常也返这个，正常）
- `-40210` 请求体结构错（`add` 漏 `table` / `name` / `para` 时常见）

## CLI 子命令

```
tplink-be7200 login [--export | --json] [--no-cache]
tplink-be7200 cache show | clear

tplink-be7200 export [-o file]                       导出全部配置
tplink-be7200 get <module> [--name X | --table X]
tplink-be7200 raw '<json-body>'                      任意请求体
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

全局选项：`--host X`、`--token X`、`--no-cache`、`-v`。

## 登录协议（实测固件 1.0.18）

抓浏览器登录请求拿到的两步：

```sh
# 1. 拿加密参数
curl -s -X POST http://192.168.1.1/ \
  -H "Content-Type: application/json" \
  -d '{"method":"do","user_management":{"get_encrypt_info":null}}'
# -> {"nonce":"...","key":"<RSA-PEM>","encrypt_type":["3"], ...}

# 2. 登录（密码 = MD5(plain + ":" + nonce).hex()）
HASH=$(echo -n "<password>:<nonce>" | md5sum | cut -d' ' -f1)
curl -s -X POST http://192.168.1.1/ \
  -H "Content-Type: application/json" \
  -d "{\"method\":\"do\",\"login\":{\"password\":\"$HASH\",\"encrypt_type\":\"3\"}}"
# -> {"error_code":0,"stok":"..."}
```

老固件（`encrypt_type` 不含 `"3"`）走 fallback：用 TP-Link 沿用多年的 XOR `orgAuthPwd` 编码、不带 `encrypt_type` 字段。SDK 也实现了，但本设备走 MD5 路径。

## 已验证模块

| 模块 | 用途 | SDK 方法 |
|---|---|---|
| `network.lan` | LAN IP / 掩码 | `get_lan()` / `set_lan_ip()` |
| `protocol.wan` | WAN 类型 / MAC | `get_wan()` / `set_wan_dhcp()` |
| `network.wan_status` | WAN 实时状态 | `get_wan_status()` |
| `protocol.pppoe` | PPPoE 拨号 | `get_pppoe()` / `set_pppoe()` |
| `dhcpd.udhcpd` | DHCP 服务器 | `get_dhcp_server()` / `set_dhcp_server()` |
| `ip_mac_bind.user_bind` | 静态绑定（table） | `list_bindings()` / `add_binding()` / `delete_binding()` / `clear_bindings()` |
| `ip_mac_bind.sys_arp` | 系统 ARP 表（只读） | `get_arp()` |
| `wireless.wlan_host_2g/5g` | 主 WiFi SSID / PSK | `get_wifi()` / `set_wifi_2g()` / `set_wifi_5g()` |
| `guest_network.guest_2g` | 访客 WiFi | `get_guest()` / `set_guest()` / `enable_guest()` / `disable_guest()` |
| `firewall.redirect` | 端口转发（table） | `list_port_forwards()` / `add_port_forward()` / `delete_port_forward()` |
| `firewall.dmz` | DMZ | `get_dmz()` / `set_dmz()` |
| `firewall.lan_manage` | 限定 MAC 才能登后台 | `get_admin_lock()` / `set_admin_lock()` |
| `ddns.phddns` | 第三方 DDNS | `get_ddns()` / `set_ddns()` |
| `upnpd.config` | UPnP 开关 | `get_upnp()` / `set_upnp()` / `list_upnp_leases()` |
| `time_switch` | WiFi 定时开关 | `get_wifi_timer()` / `add_wifi_timer_rule()` |
| `reboot_timer` | 定时重启 | `get_reboot_timer()` / `add_reboot_timer_rule()` |
| `wake_on_lan` | Wake on LAN | `list_wol_devices()` / `wake()` |
| `wireless_power.power_info` | 发射功率档位 | `get_signal_power()` / `set_signal_power()` |
| `wlan_access` | 无线 MAC 白名单 | `get_mac_acl()` / `set_mac_acl_mode()` / `add_mac_acl()` / `delete_mac_acl()` |
| `hosts_info.online_host` | 在线设备列表 | `list_clients()` |
| `system.device_info` | 设备型号 / 序列号 | `get_device_info()` |
| `system.sys_mode` | 路由器工作模式 | `get_sys_mode()` |

## 已知坑

1. **Token 在 URL path 里，不是 cookie**。访问不存在的 `.htm` 会被踢登出。
2. **`add` 必须有 `table` / `name` / `para` 三层**，少任何一层 `-40210`。
3. **`name` 形如 `<table>_<index>`**，index 自增，Web UI 自己维护。
4. **`delete` 返回 `-40205` 不算失败**，重跑一次 `get` 看真实状态。SDK 已自动把 `-40205` 当作成功。
5. **JS 里的 `optName` key 不是真实字段名**。比如 IP 在 JS 里叫 `ipAddr`，但 API 字段是 `ip`。SDK 已经帮你封装好。
6. **无线 MAC ACL 后台有 `block_list` table，但 Web UI 只暴露白名单**。SDK 与 UI 保持一致，避免出现"看不见的规则"。切到 whitelist 模式且白名单为空会把所有无线客户端踢下线，操作前请确认。

## 辅助脚本

- `scripts/migrate_from_asus_nvram.py`：读 ASUS Asuswrt-Merlin 的 `nvram show` dump，把里面所有 `dhcp_staticlist` 条目作为静态绑定批量推到 TL-7DR7270，可选用 `dnsmasq.leases` / `hosts.dnsmasq` 回填 hostname。

## License

MIT。
