"""Local file cache for the `stok` session token.

Cache path: ~/.cache/tplink-be7200/<host>.json, mode 0600.

Format:
    {"host": "192.168.1.1", "stok": "...", "created_at": "2026-04-25T15:00:00Z"}

The router does not document the session TTL. Empirically it lasts on
the order of tens of minutes; once expired, every API call returns an
auth error (typically `-40401`). Rather than hard-coding a TTL, this
module + the CLI use a "use the cache, clear it on failure" strategy.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    d = Path(base) / "tplink-be7200"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_path(host: str) -> Path:
    return cache_dir() / f"{host}.json"


def save(host: str, stok: str) -> Path:
    """Persist the token. The file is written with mode 0600 (it is a credential)."""
    p = cache_path(host)
    data = {
        "host": host,
        "stok": stok,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    p.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def load(host: str) -> str | None:
    """Return the cached `stok`, or None if missing / unreadable."""
    p = cache_path(host)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return data.get("stok") or None
    except (OSError, json.JSONDecodeError):
        return None


def clear(host: str) -> bool:
    """Delete a host's cache file. Returns True if a file was removed."""
    p = cache_path(host)
    if p.exists():
        p.unlink()
        return True
    return False


def info(host: str) -> dict | None:
    """Return the full cached payload (useful for debugging)."""
    p = cache_path(host)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
