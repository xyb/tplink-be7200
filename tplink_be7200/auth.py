"""Login flow that obtains the `stok` session token.

Protocol observed by sniffing the Web UI on firmware 1.0.18:

1. Fetch encryption parameters:
   POST http://<host>/
   {"method":"do","user_management":{"get_encrypt_info":null}}
   ->
   {"nonce": "...", "key": "<RSA-pubkey-PEM>", "encrypt_type": ["3"], ...}

2. Log in:
   POST http://<host>/
   {"method":"do","login":{"password":"<MD5(pwd+:+nonce).hex()>","encrypt_type":"3"}}
   ->
   {"error_code": 0, "stok": "..."}

Legacy firmware fallback (when `encrypt_type` does not include "3"):
   Use the historic XOR-based `orgAuthPwd` encoding without the
   `encrypt_type` field.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request

from .api import API, ApiError

# Two fixed salts that TP-Link has reused for years (from class.js orgAuthPwd).
_ORG_KEY1 = "RDpbLfCPsJZ7fiv"
_ORG_CHARSET = (
    "yLwVl0zKqws7LgKPRQ84Mdt708T1qQ3Ha7xv3H7NyU84p21BriUWBU43odz3iP4rBL3cD02KZci"
    "XTysVXiV8ngg6vL48rPJyAUw0HurW20xqxv9aYb4M9wK1Ae0wlro510qXeU07kV57fQMc8L6aL"
    "gMLwygtc0F10a0Dg70TOoouyFhdysuRMO51yY5ZlOZZLEal1h0t9YQW0Ko7oBwmCAHoic4HYbU"
    "yVeU3sfQ1xtXcPcf1aT303wAQhv66qzW"
)


def _security_encode(a: str, b: str, c: str) -> str:
    """class.js `securityEncode`: XOR two strings, look up in a charset.

    Used only for the legacy-firmware fallback.
    """
    out = []
    g, h, k = len(a), len(b), len(c)
    f = max(g, h)
    for p in range(f):
        m = ord(a[p]) if p < g else 187
        n = ord(b[p]) if p < h else 187
        out.append(c[(m ^ n) % k])
    return "".join(out)


def _org_auth_pwd(password: str) -> str:
    """Legacy firmware password encoding."""
    return _security_encode(_ORG_KEY1, password, _ORG_CHARSET)


def _post_root(host: str, body: dict, timeout: float) -> dict:
    """The login endpoints POST to `/`, not `/stok=X/ds`."""
    req = urllib.request.Request(
        f"http://{host}/",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.URLError as e:
        raise ApiError(-1, {"network_error": str(e)}, body) from e

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # When the router has not finished initial setup (no WAN / admin
        # password configured yet), the root URL serves a setup-wizard HTML
        # page that is usually GBK-encoded, so json.loads explodes. Surface
        # a readable error instead.
        head = raw[:200]
        try:
            head_str = raw.decode("gbk", errors="replace")[:200]
        except Exception:
            head_str = repr(head)
        raise ApiError(
            -2,
            {
                "decode_error": str(e),
                "hint": (
                    "router returned non-JSON content; common causes: "
                    "(1) router is still in factory state, finish the setup "
                    "wizard in a browser first; "
                    "(2) router is rebooting / service unhealthy; "
                    "(3) wrong URL (host typo?)"
                ),
                "body_preview": head_str,
            },
            body,
        ) from e


def get_encrypt_info(host: str = "192.168.1.1", timeout: float = 8.0) -> dict:
    """Step 1: fetch the nonce, RSA pubkey, and supported encryption types."""
    body = {"method": "do", "user_management": {"get_encrypt_info": None}}
    resp = _post_root(host, body, timeout)
    code = resp.get("error_code", -999)
    if code != 0:
        raise ApiError(code, resp, body)
    return resp


def encrypt_password(password: str, nonce: str) -> str:
    """New-firmware encryption: MD5(password + ":" + nonce).hex(), 32 chars."""
    return hashlib.md5(f"{password}:{nonce}".encode()).hexdigest()


def login(password: str, host: str = "192.168.1.1", timeout: float = 8.0) -> API:
    """Log in and return an `API` instance bound to the resulting token.

    Example:
        api = login("my-password")
        print(api.get_lan())
    """
    info = get_encrypt_info(host=host, timeout=timeout)
    nonce = info["nonce"]
    enc_types = info.get("encrypt_type", [])

    if "3" in enc_types:
        encrypted = encrypt_password(password, nonce)
        body = {"method": "do", "login": {"password": encrypted, "encrypt_type": "3"}}
    else:
        # Legacy path; not exercised on this device but implemented per class.js.
        encrypted = _org_auth_pwd(password)
        body = {"method": "do", "login": {"password": encrypted}}

    resp = _post_root(host, body, timeout)
    code = resp.get("error_code", -999)
    if code != 0:
        # -40401 = wrong password; data.time carries remaining attempts
        # (max_time=20; the account locks after consecutive failures).
        raise ApiError(code, resp, body)

    token = resp["stok"]
    return API(token=token, host=host, timeout=timeout)
