"""Tests for BE7200Client auto-renew on stok expiry.

The router's session token (stok) expires after roughly tens of minutes.
Without auto-renew, every cached call eventually fails with -40401 and
the user has to re-run ``tplink-be7200 login`` and re-type the password.

This test pins the contract: when a request returns -40401 *and* the
client was constructed with a saved password, the client transparently
re-authorizes once and retries the call. It must NOT loop on persistent
-40401 (e.g. wrong password — re-auth would fail with the same code),
and it must notify a registered callback so the CLI can persist the
fresh stok back to the credentials store.
"""

import unittest
from unittest.mock import MagicMock

from tplinkrouterc6u.common.exception import ClientException

from tplink_be7200.client import BE7200Client


class _StubResp:
    """Minimal requests.Response stand-in."""
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _make_session(post_responses):
    """Return a fake requests.Session whose .post() returns each entry
    of ``post_responses`` in order. Each entry is the dict the router
    would normally return, wrapped in a stub Response."""
    session = MagicMock()
    queue = list(post_responses)

    def fake_post(url, json=None, timeout=None, verify=None):
        if not queue:
            raise AssertionError(f"unexpected extra POST: {json!r}")
        return _StubResp(queue.pop(0))

    session.post.side_effect = fake_post
    return session


class AutoRefreshOnStokExpiry(unittest.TestCase):

    def test_first_call_succeeds_no_refresh_attempted(self):
        """When the first _request returns success, no re-authorize
        happens — the auto-refresh path must be a pure error-handler,
        not a wrapper that double-calls the API on every request."""
        client = BE7200Client.from_cached_stok(
            '192.168.1.1', 'OLD_STOK', password='PW',
            on_token_refresh=MagicMock(),
        )
        client._session = _make_session([
            {'error_code': 0, 'data': {'ok': True}},
        ])
        result = client._request({'method': 'get'})
        self.assertEqual(result, {'error_code': 0, 'data': {'ok': True}})
        # Only one POST happened — the original call.
        self.assertEqual(client._session.post.call_count, 1)

    def test_minus_40401_with_password_triggers_one_reauth_and_retry(self):
        """When _request gets -40401 and the client knows the password,
        it should silently re-authorize and retry the original call.
        From the caller's point of view, the call simply succeeds."""
        client = BE7200Client.from_cached_stok(
            '192.168.1.1', 'OLD_STOK', password='PW',
            on_token_refresh=MagicMock(),
        )
        client._session = _make_session([
            # 1) original call: stok expired
            {'error_code': -40401, 'data': {'code': -40401}},
            # 2) re-authorize: encrypt_info probe
            {'error_code': 0, 'nonce': 'N', 'encrypt_type': ['3']},
            # 3) actual login
            {'error_code': 0, 'stok': 'NEW_STOK'},
            # 4) retry of original call: now succeeds
            {'error_code': 0, 'data': {'ok': True}},
        ])
        result = client._request({'method': 'get'})
        self.assertEqual(result, {'error_code': 0, 'data': {'ok': True}})
        self.assertEqual(client._stok, 'NEW_STOK')

    def test_refresh_callback_invoked_with_new_stok(self):
        """After a successful re-authorize, the registered callback is
        called with the fresh stok so the CLI layer can persist it."""
        cb = MagicMock()
        client = BE7200Client.from_cached_stok(
            '192.168.1.1', 'OLD_STOK', password='PW',
            on_token_refresh=cb,
        )
        client._session = _make_session([
            {'error_code': -40401, 'data': {}},
            {'error_code': 0, 'nonce': 'N', 'encrypt_type': ['3']},
            {'error_code': 0, 'stok': 'NEW_STOK'},
            {'error_code': 0, 'data': {'ok': True}},
        ])
        client._request({'method': 'get'})
        cb.assert_called_once_with('NEW_STOK')

    def test_no_password_means_no_refresh(self):
        """If from_cached_stok was called without a password, auto-refresh
        is impossible — the client must surface the original error
        instead of trying to re-authorize and crashing on missing pwd."""
        client = BE7200Client.from_cached_stok('192.168.1.1', 'OLD_STOK')
        # No password passed → password attr is empty.
        client._session = _make_session([
            {'error_code': -40401, 'data': {}},
        ])
        # Without password, the request layer should pass the error
        # back as a regular error_code response (no swallowing/retry).
        result = client._request({'method': 'get'})
        self.assertEqual(result['error_code'], -40401)

    def test_persistent_40401_does_not_loop(self):
        """If re-authorize itself fails with -40401 (e.g. saved password
        is now wrong), the client must NOT keep retrying. It re-raises
        the authorize-time ClientException so the user sees the real
        cause instead of an infinite loop."""
        client = BE7200Client.from_cached_stok(
            '192.168.1.1', 'OLD_STOK', password='WRONG_PW',
            on_token_refresh=MagicMock(),
        )
        # 1) original call: -40401
        # 2) re-authorize probe: ok
        # 3) re-authorize login: -40401 again (saved password wrong)
        # No 4th response — if we loop, the queue runs dry.
        client._session = _make_session([
            {'error_code': -40401, 'data': {}},
            {'error_code': 0, 'nonce': 'N', 'encrypt_type': ['3']},
            {'error_code': -40401,
             'data': {'code': -40401, 'max_time': 20, 'time': 5}},
        ])
        with self.assertRaises(ClientException):
            client._request({'method': 'get'})


if __name__ == '__main__':
    unittest.main()
