"""Tests for the BE7200Client wrapper around tplinkrouterc6u.TPLinkXDRClient.

The migration plan (MIGRATION.md) calls for replacing our self-contained
auth/api code with a thin subclass of TPLinkXDRClient that adds the few
write APIs upstream does not yet provide, plus a `from_cached_stok`
factory so we keep the existing "log in once, reuse the token" CLI UX.
"""

import unittest

from tplinkrouterc6u.client.xdr import TPLinkXDRClient

from tplink_be7200.client import BE7200Client


class _MockSession:
    """Minimal stand-in for requests.Session — captures the URL/body/timeout
    each .post() call sees and returns whatever payload the test wired in."""

    def __init__(self, payload):
        self.payload = payload
        self.posts = []

    def post(self, url, json, timeout, verify):
        self.posts.append({'url': url, 'json': json,
                           'timeout': timeout, 'verify': verify})

        class _Resp:
            def __init__(self, p):
                self._p = p

            def json(self):
                return self._p
        return _Resp(self.payload)


class TestBE7200Client(unittest.TestCase):
    def test_be7200_client_subclasses_xdr_client(self):
        """BE7200Client extends TPLinkXDRClient so we get its full API
        (authorize, get_status, set_wifi, get_ipv4_*, etc.) for free."""
        self.assertTrue(issubclass(BE7200Client, TPLinkXDRClient))

    def test_be7200_client_constructor_matches_xdr(self):
        """Same positional signature so cli.py can swap in BE7200Client
        without touching call sites."""
        client = BE7200Client('192.168.1.1', 'mypassword')
        self.assertEqual(client.host, 'http://192.168.1.1')
        self.assertEqual(client.password, 'mypassword')
        self.assertEqual(client.username, 'admin')
        self.assertFalse(client._stok)  # c6u inits as '' (falsy), not None

    def test_from_cached_stok_skips_authorize(self):
        """from_cached_stok injects a previously obtained token so cli
        commands can talk to the router without re-running the password
        authorize handshake. password is not needed (and not used) in
        this code path."""
        client = BE7200Client.from_cached_stok('192.168.1.1', 'CACHED_TOK')
        self.assertEqual(client._stok, 'CACHED_TOK')
        self.assertEqual(client.host, 'http://192.168.1.1')

    def test_from_cached_stok_request_uses_injected_stok(self):
        """The whole point: an instance built from a cached token must be
        able to send authenticated requests immediately, with no authorize
        round-trip. Verifies the URL c6u builds includes our token."""
        client = BE7200Client.from_cached_stok('192.168.1.1', 'CACHED_TOK')
        client._session = _MockSession({'error_code': 0, 'data': 'ok'})

        result = client._request({'method': 'get', 'foo': None})

        self.assertEqual(result, {'error_code': 0, 'data': 'ok'})
        self.assertEqual(len(client._session.posts), 1)
        self.assertEqual(client._session.posts[0]['url'],
                         'http://192.168.1.1/stok=CACHED_TOK/ds')
