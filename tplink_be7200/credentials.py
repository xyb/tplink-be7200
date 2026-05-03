"""Persistent credential store for tplink-be7200.

Holds the bundle needed to talk to a router without re-typing the admin
password each session: ``host`` / ``username`` / ``password`` / ``stok``
/ timestamps.

**Path**: ``~/.config/tplink-be7200/<host>.json`` (override via
``XDG_CONFIG_HOME``). The file is mode 0600.

**Why config, not cache**: ``~/.cache`` is for files a system cleanup
tool may delete unilaterally (per the XDG Base Directory spec). The
stok and admin password are credentials — losing them is not the
"throw it away and regenerate" semantics that ``cache/`` implies, so
they belong under ``~/.config`` next to other CLI-managed credentials
(e.g. ``~/.config/gh/hosts.yml``).

**Why plaintext password**: the convenience of skipping the ``getpass``
prompt on every command (and silently refreshing an expired stok) is
worth the trade. Mode 0600 + a single-user macOS box is the protection
boundary; anyone with that user's shell can already MITM stdin.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Optional


def _config_dir() -> Path:
    """Return ~/.config/tplink-be7200/, honoring XDG_CONFIG_HOME."""
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    d = Path(base) / 'tplink-be7200'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _legacy_cache_dir() -> Path:
    """Return the *old* ~/.cache/tplink-be7200/ path. Used only by the
    migration logic in load(); never written to."""
    base = os.environ.get('XDG_CACHE_HOME') or str(Path.home() / '.cache')
    return Path(base) / 'tplink-be7200'


def path(host: str) -> Path:
    return _config_dir() / f'{host}.json'


def save(
    host: str,
    *,
    stok: str,
    password: Optional[str] = None,
    username: str = 'admin',
) -> Path:
    """Persist the credential bundle.

    The file is written 0600. Pass ``password=None`` to drop a stored
    password (e.g. ``--no-save-password``); the field is omitted from
    the JSON, NOT preserved silently from a previous save.
    """
    p = path(host)
    record: dict = {
        'host': host,
        'username': username,
        'stok': stok,
        'saved_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if password is not None:
        record['password'] = password
    p.write_text(json.dumps(record, indent=2))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    # Saving the new file authoritatively supersedes any legacy
    # ~/.cache copy; remove it so the next load() does not see a
    # stale file in the deprecated location.
    legacy = _legacy_cache_dir() / f'{host}.json'
    if legacy.exists():
        try:
            legacy.unlink()
        except OSError:
            pass
    return p


def load(host: str) -> Optional[dict]:
    """Return the stored record dict, or None if there is none.

    On the first call after upgrading from the old ``~/.cache`` layout,
    a legacy file is auto-migrated to the new ``~/.config`` location
    and the old file is removed. The returned record contains whatever
    fields the legacy file had (typically ``stok`` + ``created_at``).
    """
    new_p = path(host)
    if not new_p.exists():
        legacy = _legacy_cache_dir() / f'{host}.json'
        if legacy.exists():
            try:
                content = legacy.read_text()
                new_p.write_text(content)
                try:
                    os.chmod(new_p, 0o600)
                except OSError:
                    pass
                legacy.unlink()
            except OSError:
                return None
    if not new_p.exists():
        # If we got here through the "new exists, legacy also exists"
        # branch in save(), the legacy file is still on disk; clean it.
        return None
    try:
        return json.loads(new_p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def clear(host: str) -> bool:
    """Delete the stored record for one host. Returns True if a file
    was actually removed."""
    p = path(host)
    removed = False
    if p.exists():
        p.unlink()
        removed = True
    # Also clear any leftover legacy file so a stale ~/.cache copy does
    # not silently rehydrate on the next load().
    legacy = _legacy_cache_dir() / f'{host}.json'
    if legacy.exists():
        legacy.unlink()
        removed = True
    return removed


def info(host: str) -> Optional[dict]:
    """Diagnostic accessor: returns the full record (including password)
    for cmd_creds_show. Callers that print this should redact."""
    return load(host)


def config_dir() -> Path:
    """Public accessor for the storage directory; used by the CLI's
    auto-host discovery to enumerate which hosts have records."""
    return _config_dir()
