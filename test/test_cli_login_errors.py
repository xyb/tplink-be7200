"""Tests for cli.cmd_login error translation.

When the upstream library raises ClientException with the new structured
messages, ``cmd_login`` must translate the two highest-friction cases
into actionable user-facing stderr output and exit non-zero, so that:

  - The user does not blindly retry on a -40401 (which would burn one of
    the few remaining tries before the router permanently locks).

  - The user understands a 5xx is the router's rate limit, not a fatal
    problem with their setup.

Anything else should pass through unchanged so we don't accidentally
swallow real bugs.
"""

import io
import sys
import unittest
from unittest.mock import patch

from requests.exceptions import ConnectTimeout, ConnectionError as ReqConnectionError
from tplinkrouterc6u.common.exception import ClientException

from tplink_be7200 import cli


class _Args:
    """Minimal argparse.Namespace stand-in matching cmd_login's reads."""
    host = '192.168.1.1'
    no_cache = True
    export = False
    json = False


def _run_login_with_authorize_error(error):
    """Run cli.cmd_login() with TPLINK_PASSWORD set, having authorize()
    raise the given exception. Capture stderr and exit code."""
    stderr = io.StringIO()
    with patch.dict('os.environ', {'TPLINK_PASSWORD': 'pw'}, clear=False), \
         patch('tplink_be7200.cli.BE7200Client') as MockClient, \
         patch.object(sys, 'stderr', stderr):
        instance = MockClient.return_value
        instance.authorize.side_effect = error
        try:
            cli.cmd_login(_Args())
        except SystemExit as e:
            return stderr.getvalue(), e.code
    return stderr.getvalue(), None


class CmdLoginErrorTranslation(unittest.TestCase):

    def test_minus_40401_is_translated_to_friendly_warning(self):
        """A -40401 ClientException becomes a multi-line stderr message
        that names the password-attempts limit and tells the user
        explicitly to verify the password before retrying."""
        err = ClientException(
            'TplinkRouter - BE7200Client - Cannot authorize! '
            'error_code=-40401 password attempts 19/20: ...'
        )

        stderr, code = _run_login_with_authorize_error(err)

        self.assertEqual(code, 2)
        self.assertIn('-40401', stderr)
        self.assertIn('password', stderr.lower())
        self.assertIn('Check the password', stderr)
        self.assertIn('Web UI', stderr)

    def test_502_is_translated_to_rate_limit_advice(self):
        """A 5xx ClientException becomes a stderr message naming the
        rate-limit and suggesting a wait. Exit code is 2 (consistent
        with -40401), distinguishing user-actionable failure from
        crashes."""
        err = ClientException(
            'TplinkRouter - BE7200Client - Cannot authorize! '
            'HTTP 502 Bad Gateway (non-JSON body: ...). '
            'Likely cause: router rate-limited ...'
        )

        stderr, code = _run_login_with_authorize_error(err)

        self.assertEqual(code, 2)
        self.assertIn('rate-limited', stderr)
        self.assertIn('Wait', stderr)

    def test_503_and_504_use_same_path_as_502(self):
        """The translation key is HTTP 5xx, not literally 502; cover the
        nearby codes so future router behavior changes do not silently
        regress to a raw stack trace."""
        for status in (503, 504):
            with self.subTest(status=status):
                err = ClientException(
                    f'TplinkRouter - BE7200Client - Cannot authorize! '
                    f'HTTP {status} ... '
                )
                stderr, code = _run_login_with_authorize_error(err)
                self.assertEqual(code, 2)
                self.assertIn('rate-limited', stderr)

    def test_connect_timeout_names_host_and_suggests_check(self):
        """ConnectTimeout from requests means the router IP is wrong, the
        machine is on a different network, or the router is genuinely
        offline. The message must name the host so the user can spot a
        wrong --host (192.168.1.1 vs 192.168.1.1 is a common mix-up)."""
        err = ConnectTimeout("HTTPConnectionPool(host='192.168.1.1', port=80): timeout")

        stderr, code = _run_login_with_authorize_error(err)

        self.assertEqual(code, 2)
        self.assertIn('192.168.1.1', stderr)
        # Should hint at the most common cause (wrong host) without
        # over-specifying — 'host', '--host', or 'TPLINK_HOST' all qualify.
        self.assertTrue(
            any(s in stderr for s in ('--host', 'TPLINK_HOST', 'wrong host')),
            f"expected host-config hint in stderr, got: {stderr!r}",
        )

    def test_generic_connection_error_is_translated_too(self):
        """Beyond timeout, requests can raise ConnectionError for refused
        connections, DNS failures, etc. All should funnel through the
        same 'router unreachable' translation, not a raw stack trace."""
        err = ReqConnectionError("Failed to establish a new connection: refused")

        stderr, code = _run_login_with_authorize_error(err)

        self.assertEqual(code, 2)
        self.assertIn('unreachable', stderr.lower())

    def test_unrelated_clientexception_is_re_raised(self):
        """A novel error message we have not learned to translate must
        propagate unchanged — silently swallowing it would mask real
        bugs (e.g. wrong host, broken upstream)."""
        err = ClientException('TplinkRouter - BE7200Client - some new failure')

        stderr = io.StringIO()
        with patch.dict('os.environ', {'TPLINK_PASSWORD': 'pw'}, clear=False), \
             patch('tplink_be7200.cli.BE7200Client') as MockClient, \
             patch.object(sys, 'stderr', stderr):
            MockClient.return_value.authorize.side_effect = err
            with self.assertRaises(ClientException):
                cli.cmd_login(_Args())


class CmdLoginIdentityDisplay(unittest.TestCase):
    """After a successful login, the user should be able to see at a glance
    *which router* they actually authenticated against — IP and MAC address.

    A wrong --host catches today: the user types ``tplink-be7200 login`` and
    the CLI silently authenticates against an unintended box (e.g. an old
    TP-Link sitting on 192.168.1.1 leftover from factory defaults). Echoing
    the IP+MAC the moment the login succeeds makes that mistake visible
    before any destructive write commands run.
    """

    def _run_login_success(self, *, lan_payload):
        """Run cmd_login() against a mocked client whose authorize()
        succeeds and whose get_lan()/get_device_info() return preset
        data. Capture stderr/stdout."""
        stderr = io.StringIO()
        stdout = io.StringIO()
        with patch.dict('os.environ', {'TPLINK_PASSWORD': 'pw'}, clear=False), \
             patch('tplink_be7200.cli.BE7200Client') as MockClient, \
             patch('tplink_be7200.cli.credentials') as MockCache, \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'stdout', stdout):
            instance = MockClient.return_value
            instance.authorize.return_value = None
            instance._stok = 'TOK'
            instance.get_lan.return_value = lan_payload
            instance.get_device_info.return_value = {'device_model': 'TL-7DR7270'}
            MockCache.save.return_value = '/tmp/fake.json'
            cli.cmd_login(_Args())
        return stdout.getvalue(), stderr.getvalue()

    def test_prints_connecting_target_before_authorize(self):
        """Before the login round-trip runs, the user should see *what
        host the CLI is about to talk to*, so a typo or wrong default
        is caught before the password is sent over the wire."""
        stdout, stderr = self._run_login_success(
            lan_payload={'ipaddr': '192.168.1.1',
                         'macaddr': '60-A3-E3-8A-FF-DB'},
        )
        self.assertIn('192.168.1.1', stderr)
        self.assertIn('connecting', stderr.lower())

    def test_prints_router_ip_mac_model_after_authorize(self):
        """Post-login confirmation line: IP, MAC, and model. This is the
        receipt that says 'you really did authenticate against the box
        you meant to'. Goes to stderr so it does not pollute scripted
        callers piping the token from stdout."""
        stdout, stderr = self._run_login_success(
            lan_payload={'ipaddr': '192.168.1.1',
                         'macaddr': '60-A3-E3-8A-FF-DB'},
        )
        self.assertIn('192.168.1.1', stderr)
        self.assertIn('60-A3-E3-8A-FF-DB', stderr)
        self.assertIn('TL-7DR7270', stderr)
        # token still goes to stdout untouched (existing contract)
        self.assertIn('TOK', stdout)

    def test_identity_lookup_failure_does_not_break_login(self):
        """If the post-login get_lan()/get_device_info() call fails for
        any reason, login itself must still succeed — the token has
        already been issued and cached, and the identity echo is purely
        informational. Otherwise we would regress the happy path."""
        stderr = io.StringIO()
        stdout = io.StringIO()
        with patch.dict('os.environ', {'TPLINK_PASSWORD': 'pw'}, clear=False), \
             patch('tplink_be7200.cli.BE7200Client') as MockClient, \
             patch('tplink_be7200.cli.credentials') as MockCache, \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'stdout', stdout):
            instance = MockClient.return_value
            instance.authorize.return_value = None
            instance._stok = 'TOK'
            instance.get_lan.side_effect = RuntimeError("boom")
            MockCache.save.return_value = '/tmp/fake.json'
            cli.cmd_login(_Args())  # must NOT raise
        self.assertIn('TOK', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
