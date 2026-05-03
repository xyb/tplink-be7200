"""Tests for the credentials module (renamed from cache.py).

The persistent store holds the bundle a user needs to talk to a router
without re-typing their password every session: host/username/password
plus the most recent stok and timestamps.

Three contracts pinned here:

  1. Path lives under XDG_CONFIG_HOME (~/.config by default), NOT
     ~/.cache. The token is a credential — system cleanup tools should
     never have permission to delete it implicitly.

  2. The file is mode 0600. Stored plaintext password reduces the
     friction of token expiry but only at the cost of trusting the
     local user account; permissions enforce that.

  3. A legacy file at ~/.cache/tplink-be7200/<host>.json (old layout)
     is auto-migrated the first time load() runs against the new path.
     This keeps old shells working after the upgrade.
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tplink_be7200 import credentials


class _TmpHome:
    """Context manager pinning HOME and XDG_* to a sandbox dir, so
    tests do not touch the real ~/.config or ~/.cache."""

    def __init__(self):
        self._tmp = None
        self._patches = []

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        env = {
            'HOME': str(home),
            # Explicitly clear XDG_* so tests exercise the default
            # ($HOME/.config and $HOME/.cache).
            'XDG_CONFIG_HOME': '',
            'XDG_CACHE_HOME': '',
        }
        # Pop empty-string ones so os.environ.get('XDG_*') returns None,
        # matching a truly unset env var.
        env_unset = {k: '' for k in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME')}
        p = patch.dict(os.environ, {'HOME': str(home)}, clear=False)
        p.start()
        self._patches.append(p)
        # And remove XDG vars if set
        for k in env_unset:
            if k in os.environ:
                p2 = patch.dict(os.environ, {}, clear=False)
                p2.start()
                # Easier: directly delete
                del os.environ[k]
        self.home = home
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class CredentialsPath(unittest.TestCase):

    def test_default_path_under_config_not_cache(self):
        """The store must live in ~/.config/tplink-be7200/<host>.json,
        not the legacy ~/.cache. Tokens are credentials; system cleanup
        utilities should not be able to wipe them."""
        with _TmpHome() as tmp:
            p = credentials.path('192.168.1.1')
            self.assertEqual(
                p, tmp.home / '.config' / 'tplink-be7200' / '192.168.1.1.json',
            )
            self.assertNotIn('.cache', str(p))

    def test_xdg_config_home_overrides_default(self):
        """If XDG_CONFIG_HOME is set, paths root there. This is the
        spec'd override for users who rearrange their home dir."""
        with _TmpHome() as tmp:
            custom = tmp.home / 'custom-config'
            with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(custom)}):
                p = credentials.path('192.168.1.1')
                self.assertEqual(
                    p, custom / 'tplink-be7200' / '192.168.1.1.json',
                )


class CredentialsSaveLoad(unittest.TestCase):

    def test_save_writes_record_with_password_and_stok(self):
        """save() persists the full bundle so future _client() calls can
        skip the password prompt. Plaintext is acceptable per project
        choice; the gate is filesystem permissions."""
        with _TmpHome():
            credentials.save(
                '192.168.1.1',
                stok='TOKEN-A',
                password='HUNTER2',
                username='admin',
            )
            data = json.loads(credentials.path('192.168.1.1').read_text())
            self.assertEqual(data['host'], '192.168.1.1')
            self.assertEqual(data['stok'], 'TOKEN-A')
            self.assertEqual(data['password'], 'HUNTER2')
            self.assertEqual(data['username'], 'admin')

    def test_save_sets_mode_0600(self):
        """Plaintext password mandates strict file mode. World/group
        readable would be a bug."""
        with _TmpHome():
            credentials.save('192.168.1.1', stok='T', password='P')
            mode = credentials.path('192.168.1.1').stat().st_mode
            self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_save_without_password_omits_password_field(self):
        """When the caller deliberately does not pass a password (e.g.
        --no-save-password), the field must NOT appear — silently
        keeping a stale value would be a security regression."""
        with _TmpHome():
            credentials.save('192.168.1.1', stok='T', password='OLDPW')
            credentials.save('192.168.1.1', stok='T2', password=None)
            data = json.loads(credentials.path('192.168.1.1').read_text())
            self.assertNotIn('password', data)
            self.assertEqual(data['stok'], 'T2')

    def test_load_returns_full_record_dict(self):
        """load() returns the dict so callers can pull whichever field
        they need (stok for cached-call mode, password for refresh)."""
        with _TmpHome():
            credentials.save('192.168.1.1', stok='TOK', password='PW')
            rec = credentials.load('192.168.1.1')
            self.assertIsInstance(rec, dict)
            self.assertEqual(rec['stok'], 'TOK')
            self.assertEqual(rec['password'], 'PW')

    def test_load_missing_returns_none(self):
        with _TmpHome():
            self.assertIsNone(credentials.load('192.168.1.99'))

    def test_clear_removes_file(self):
        with _TmpHome():
            credentials.save('192.168.1.1', stok='T')
            self.assertTrue(credentials.path('192.168.1.1').exists())
            self.assertTrue(credentials.clear('192.168.1.1'))
            self.assertFalse(credentials.path('192.168.1.1').exists())
            # Second clear is a no-op, returns False.
            self.assertFalse(credentials.clear('192.168.1.1'))


class CredentialsLegacyMigration(unittest.TestCase):

    def test_legacy_cache_file_migrated_on_load(self):
        """A user upgrading from the old layout must keep working: when
        ~/.config/.../X.json is missing but ~/.cache/.../X.json exists,
        load() copies it over and removes the old file. This way the
        cached token survives the upgrade and the next API call doesn't
        force a fresh password prompt."""
        with _TmpHome() as tmp:
            legacy_dir = tmp.home / '.cache' / 'tplink-be7200'
            legacy_dir.mkdir(parents=True)
            legacy = legacy_dir / '192.168.1.1.json'
            legacy.write_text(json.dumps({
                'host': '192.168.1.1',
                'stok': 'OLD-TOKEN',
                'created_at': '2026-01-01T00:00:00Z',
            }))

            rec = credentials.load('192.168.1.1')

            self.assertIsNotNone(rec)
            self.assertEqual(rec['stok'], 'OLD-TOKEN')
            # New file exists; legacy file is gone.
            self.assertTrue(credentials.path('192.168.1.1').exists())
            self.assertFalse(legacy.exists())

    def test_legacy_does_not_overwrite_existing_new_file(self):
        """If both old and new files exist, the NEW one wins (it is the
        most recent one the program wrote). The legacy file is removed
        anyway to clean up the stale location."""
        with _TmpHome() as tmp:
            legacy_dir = tmp.home / '.cache' / 'tplink-be7200'
            legacy_dir.mkdir(parents=True)
            legacy = legacy_dir / '192.168.1.1.json'
            legacy.write_text(json.dumps({'stok': 'OLD'}))

            credentials.save('192.168.1.1', stok='NEW')
            rec = credentials.load('192.168.1.1')

            self.assertEqual(rec['stok'], 'NEW')
            self.assertFalse(legacy.exists())  # cleaned up either way


if __name__ == '__main__':
    unittest.main()
