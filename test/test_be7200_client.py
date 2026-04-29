"""Tests for the BE7200Client wrapper around tplinkrouterc6u.TPLinkXDRClient.

The migration plan (MIGRATION.md) calls for replacing our self-contained
auth/api code with a thin subclass of TPLinkXDRClient that adds the few
write APIs upstream does not yet provide.
"""

import unittest

from tplinkrouterc6u.client.xdr import TPLinkXDRClient

from tplink_be7200.client import BE7200Client


class TestBE7200Client(unittest.TestCase):
    def test_be7200_client_subclasses_xdr_client(self):
        """BE7200Client must extend TPLinkXDRClient so we get its full API
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
